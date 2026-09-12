"""SQLAlchemy 2.0 asyncio engine, session factory and declarative base.

The engine is built at import time from :data:`settings.database_url`, but which
database a run uses is a startup decision -- ``main.py --database dev.db`` picks one
long after this module has been imported. :func:`configure_database` therefore swaps
the engine in place, and everything reaches the session factory through a proxy so the
``from app.core.database import SessionLocal`` bindings scattered across the API
modules follow the swap instead of holding a session factory for the old file.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.core.config import database_path, resolve_database_url, settings


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base shared by every simulated service."""

    type_annotation_map: dict[Any, Any] = {}


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """WAL + relaxed sync keeps IOPS low on spinning/older laptop disks."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _make_engine(url: str) -> AsyncEngine:
    kwargs: dict[str, Any] = {"echo": settings.sql_echo, "future": True}
    if ":memory:" in url:
        # One shared connection, or each session would get its own empty database.
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    new_engine = create_async_engine(url, **kwargs)
    event.listen(new_engine.sync_engine, "connect", _sqlite_pragmas)
    return new_engine


_engine: AsyncEngine = _make_engine(settings.database_url)
_sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _engine, expire_on_commit=False, autoflush=False
)


def get_engine() -> AsyncEngine:
    """The engine currently in use. Call it per use -- it is replaced, not mutated."""
    return _engine


class _SessionFactory:
    """Callable stand-in for the live ``async_sessionmaker``.

    ``from app.core.database import SessionLocal`` copies a reference at import time,
    so rebinding the module global on :func:`configure_database` would leave every
    importer talking to the database chosen before the flag was read. Delegating each
    call keeps them all pointed at the engine actually in use.
    """

    __slots__ = ()

    def __call__(self, **kwargs: Any) -> AsyncSession:
        return _sessionmaker(**kwargs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SessionLocal {settings.database_url}>"


SessionLocal = _SessionFactory()


def configure_database(url: str) -> str:
    """Point the simulator at ``url``, replacing any engine already built.

    Meant for startup, before anything has opened a connection: the engine built at
    import is then discarded unused, with nothing to close. The old engine is
    deliberately *not* disposed here -- disposing an aiosqlite engine has to be awaited,
    and this runs before the event loop exists. A caller swapping databases on a live
    engine should ``await dispose_db()`` first.

    Returns the url in effect.
    """
    global _engine, _sessionmaker

    if url == settings.database_url:
        return url

    settings.database_url = url
    _engine = _make_engine(url)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False)
    return url


def use_database(value: str) -> str:
    """Resolve a friendly ``--database`` value, make it openable, and switch to it.

    Creating the parent directory is the difference between ``--database
    ~/clouds/prod.db`` working on a fresh machine and failing deep inside the driver
    with "unable to open database file", which says nothing about a missing folder.
    """
    url = resolve_database_url(value)
    path = database_path(url)
    if path is not None and str(path.parent) != ".":
        path.parent.mkdir(parents=True, exist_ok=True)
    return configure_database(url)


async def init_db(drop: bool = False) -> Any:
    """Bring the schema up to date. Imports the model package for its side effects.

    Returns the :class:`~app.core.schema.SchemaState` describing what happened, so the
    caller can report a migration rather than performing one silently.
    """
    import app.models as _models  # noqa: F401 - registers every mapper

    # Imported here rather than at module scope: schema.py needs Base from this module.
    from app.core.schema import METADATA_TABLE, ensure_schema

    assert _models  # imported purely for the mapper registrations above

    async with _engine.begin() as conn:
        if drop:
            await conn.run_sync(Base.metadata.drop_all)
            # Not a mapped table, so drop_all does not know about it.
            await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {METADATA_TABLE}")
            await conn.exec_driver_sql("PRAGMA user_version = 0")
        return await ensure_schema(conn)


async def dispose_db() -> None:
    await _engine.dispose()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped async session."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
