"""SQLAlchemy 2.0 asyncio engine, session factory and declarative base."""
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

from app.core.config import settings


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base shared by every simulated service."""

    type_annotation_map: dict[Any, Any] = {}


def _make_engine() -> AsyncEngine:
    kwargs: dict[str, Any] = {"echo": settings.sql_echo, "future": True}
    if ":memory:" in settings.database_url:
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_async_engine(settings.database_url, **kwargs)


engine: AsyncEngine = _make_engine()
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, autoflush=False
)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """WAL + relaxed sync keeps IOPS low on spinning/older laptop disks."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async def init_db(drop: bool = False) -> None:
    """Create every table. Imports the model package for its side effects."""
    import app.models as _models  # noqa: F401 - registers every mapper

    assert _models  # imported purely for the mapper registrations above

    async with engine.begin() as conn:
        if drop:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def dispose_db() -> None:
    await engine.dispose()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped async session."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
