"""Schema versioning: what happens to a database file that is not this build's.

These build their own file-backed engines rather than using the shared in-memory one --
the whole point is what survives across restarts, which an in-memory database cannot show.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - registers every mapper onto Base.metadata
from app.core.database import Base
from app.models.identity import Project
from app.core.schema import (
    METADATA_TABLE,
    Migration,
    SchemaVersionError,
    ensure_schema,
    schema_report,
)

pytestmark = pytest.mark.anyio


async def _apply(path: Path):
    """Run ensure_schema against a file, returning what it did."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.begin() as conn:
            return await ensure_schema(conn)
    finally:
        await engine.dispose()


def _stamp(path: Path, version: int, app_version: str = "9.9.9") -> None:
    """Forge a database written by some other build."""
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {version}")
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {METADATA_TABLE} "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        f"INSERT OR REPLACE INTO {METADATA_TABLE} VALUES ('app_version', ?)",
        (app_version,),
    )
    connection.commit()
    connection.close()


def _user_version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()


# -- the happy paths -------------------------------------------------------------------


async def test_a_new_file_is_created_and_stamped(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    state = await _apply(database)
    assert state.action == "created"
    assert state.to_version == 1
    assert _user_version(database) == 1
    assert "servers" in _tables(database)
    assert METADATA_TABLE in _tables(database)


async def test_reopening_the_same_file_is_a_no_op(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    await _apply(database)
    state = await _apply(database)
    assert state.action == "current"
    assert state.from_version == state.to_version == 1


async def test_an_unversioned_file_is_adopted_not_destroyed(tmp_path: Path) -> None:
    """A database written before versioning existed keeps its rows."""
    database = tmp_path / "legacy.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Through the ORM, so the row carries whatever defaults the model declares.
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(Project(id="p1", name="keepme"))
        await session.commit()
    await engine.dispose()
    assert _user_version(database) == 0  # unstamped, as a pre-versioning file would be

    state = await _apply(database)

    assert state.action == "adopted"
    assert _user_version(database) == 1
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT name FROM projects").fetchone()[0] == "keepme"
    connection.close()


async def test_a_new_table_needs_no_version_bump(tmp_path: Path) -> None:
    """create_all adds a missing table on its own -- only columns need a migration."""
    database = tmp_path / "fresh.db"
    await _apply(database)
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE servers")
    connection.commit()
    connection.close()

    state = await _apply(database)

    assert state.action == "current"
    assert "servers" in _tables(database)


# -- the refusals ----------------------------------------------------------------------


async def test_a_newer_database_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "newer.db"
    await _apply(database)
    _stamp(database, 99, app_version="9.9.9")

    with pytest.raises(SchemaVersionError) as raised:
        await _apply(database)

    message = str(raised.value)
    assert "schema v99" in message
    assert "9.9.9" in message, "the message must name the build that wrote the file"
    assert "seed.py --reset" in message, "and the remedy"


async def test_an_older_database_with_no_migration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "older.db"
    await _apply(database)
    # This build now expects v3, and knows no way to get there from v1.
    monkeypatch.setattr("app.core.schema.SCHEMA_VERSION", 3)

    with pytest.raises(SchemaVersionError) as raised:
        await _apply(database)

    assert "No migration from schema v1 to v2" in str(raised.value)
    assert "seed.py --reset" in str(raised.value)


# -- migrating -------------------------------------------------------------------------


async def test_a_registered_migration_is_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "migrate.db"
    await _apply(database)
    monkeypatch.setattr("app.core.schema.SCHEMA_VERSION", 2)
    monkeypatch.setitem(
        __import__("app.core.schema", fromlist=["MIGRATIONS"]).MIGRATIONS,
        1,
        Migration(
            "servers gained a description column",
            ("ALTER TABLE servers ADD COLUMN description VARCHAR",),
        ),
    )

    state = await _apply(database)

    assert state.action == "migrated"
    assert state.from_version == 1 and state.to_version == 2
    assert _user_version(database) == 2
    connection = sqlite3.connect(database)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(servers)")}
    connection.close()
    assert "description" in columns


async def test_migrations_run_in_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "chain.db"
    await _apply(database)
    schema = __import__("app.core.schema", fromlist=["MIGRATIONS"])
    monkeypatch.setattr("app.core.schema.SCHEMA_VERSION", 3)
    monkeypatch.setitem(
        schema.MIGRATIONS, 1, Migration("one", ("ALTER TABLE servers ADD COLUMN one VARCHAR",))
    )
    monkeypatch.setitem(
        schema.MIGRATIONS, 2, Migration("two", ("ALTER TABLE servers ADD COLUMN two VARCHAR",))
    )

    state = await _apply(database)

    assert state.to_version == 3
    connection = sqlite3.connect(database)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(servers)")}
    connection.close()
    assert {"one", "two"} <= columns


# -- reporting -------------------------------------------------------------------------


async def test_report_names_the_writer(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    await _apply(database)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as conn:
        report = await schema_report(conn)
    await engine.dispose()

    assert report["schema_version"] == 1
    assert report["expected_schema_version"] == 1
    assert report["recorded_app_version"] == report["app_version"]
