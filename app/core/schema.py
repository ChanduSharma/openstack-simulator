"""SQLite schema versioning.

``Base.metadata.create_all`` creates the tables that do not exist yet and leaves every
table that does completely alone -- it never emits ``ALTER``. So adding a new *model* is
picked up automatically on an existing database file, while adding a *column* to an
existing model is silently ignored, and only surfaces much later as ``no such column``
from a live request.

Stamping the schema version into the file turns that silent drift into a startup error
that names the remedy, and gives migrations somewhere to hang.

Two things are recorded, for two different jobs:

* ``PRAGMA user_version`` is the gate. It is an integer that lives in the file header, so
  it can be read from a database with no tables at all and cannot be dropped by accident.
* ``simulator_metadata`` is the report. A pragma cannot hold the app version string, and
  "written by 0.1.0" is what makes a mismatch actionable for whoever hits it.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from app import SCHEMA_VERSION, __version__
from app.core.config import now_utc
from app.core.database import Base

METADATA_TABLE = "simulator_metadata"


@dataclass(frozen=True, slots=True)
class Migration:
    """One step from schema version N to N+1."""

    description: str
    statements: tuple[str, ...]


# Keyed by the version each step upgrades *from*, so 1 -> 2 lives under key 1. A gap is
# deliberate rather than an oversight: with no path from the stamped version to the
# current one, startup fails and says to reset, instead of limping along against a schema
# that does not match the models.
#
# Example of what an entry looks like:
#     1: Migration(
#         "servers gained a description column",
#         ("ALTER TABLE servers ADD COLUMN description VARCHAR",),
#     ),
MIGRATIONS: dict[int, Migration] = {}


class SchemaVersionError(RuntimeError):
    """The database file cannot be used by this build of the simulator."""


@dataclass(frozen=True, slots=True)
class SchemaState:
    """What ``ensure_schema`` did, so the caller can report it."""

    action: str  # created | current | adopted | migrated
    from_version: int
    to_version: int
    written_by: str

    def summary(self) -> str:
        if self.action == "created":
            return f"schema v{self.to_version} created"
        if self.action == "migrated":
            return f"schema migrated v{self.from_version} -> v{self.to_version}"
        if self.action == "adopted":
            return f"unversioned database adopted as schema v{self.to_version}"
        return f"schema v{self.to_version}"


async def _table_names(conn: AsyncConnection) -> set[str]:
    result = await conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    return {row[0] for row in result}


async def _read_user_version(conn: AsyncConnection) -> int:
    row = (await conn.exec_driver_sql("PRAGMA user_version")).first()
    return int(row[0]) if row else 0


async def _write_user_version(conn: AsyncConnection, version: int) -> None:
    # A pragma value cannot be a bound parameter, hence the interpolation. The int()
    # keeps that from ever being anything but a number.
    await conn.exec_driver_sql(f"PRAGMA user_version = {int(version)}")


async def _ensure_metadata_table(conn: AsyncConnection) -> None:
    await conn.exec_driver_sql(
        f"CREATE TABLE IF NOT EXISTS {METADATA_TABLE} "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


async def _read_metadata(conn: AsyncConnection) -> dict[str, str]:
    if METADATA_TABLE not in await _table_names(conn):
        return {}
    result = await conn.exec_driver_sql(f"SELECT key, value FROM {METADATA_TABLE}")
    return {row[0]: row[1] for row in result}


async def _write_metadata(conn: AsyncConnection, **values: str) -> None:
    await _ensure_metadata_table(conn)
    for key, value in values.items():
        await conn.exec_driver_sql(
            f"INSERT INTO {METADATA_TABLE} (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


async def _stamp(conn: AsyncConnection, version: int, created: bool) -> None:
    await _write_user_version(conn, version)
    stamp = {
        "schema_version": str(version),
        "app_version": __version__,
        "updated_at": now_utc().isoformat(),
    }
    if created:
        stamp["created_at"] = stamp["updated_at"]
    await _write_metadata(conn, **stamp)


async def _migrate(conn: AsyncConnection, start: int, written_by: str) -> None:
    version = start
    while version < SCHEMA_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise SchemaVersionError(
                f"No migration from schema v{version} to v{version + 1}.\n"
                f"This database was written by OpenStack-Simulator {written_by}; "
                f"this is {__version__} (schema v{SCHEMA_VERSION}).\n"
                f"The simulator holds no data worth preserving -- rebuild it with:\n"
                f"    python seed.py --reset"
            )
        for statement in step.statements:
            await conn.exec_driver_sql(statement)
        version += 1


async def ensure_schema(conn: AsyncConnection) -> SchemaState:
    """Bring the connected database up to the schema this build expects.

    Returns what it did. Raises :class:`SchemaVersionError` when the file cannot be
    reconciled, rather than leaving a half-matching schema in place.
    """
    if conn.dialect.name != "sqlite":
        # Versioning here is built on SQLite's file-header pragma; anything else just
        # gets the tables and no claims about migration.
        await conn.run_sync(Base.metadata.create_all)
        return SchemaState("current", SCHEMA_VERSION, SCHEMA_VERSION, __version__)

    tables = await _table_names(conn)
    stamped = await _read_user_version(conn)
    metadata = await _read_metadata(conn)
    written_by = metadata.get("app_version", "an unknown version")

    # Nothing here yet: create everything and stamp it.
    if not tables:
        await conn.run_sync(Base.metadata.create_all)
        await _stamp(conn, SCHEMA_VERSION, created=True)
        return SchemaState("created", 0, SCHEMA_VERSION, __version__)

    # Tables but no stamp: a file from before versioning existed. Its schema is v1 by
    # definition -- that is the version versioning was introduced at -- so adopt it
    # rather than making the user throw away a database that is already correct.
    action = "current"
    if stamped == 0:
        stamped = 1
        action = "adopted"

    if stamped > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"This database is at schema v{stamped}, but this build only understands "
            f"v{SCHEMA_VERSION}.\n"
            f"It was written by OpenStack-Simulator {written_by}; this is {__version__}.\n"
            f"Upgrade the simulator, or start over with:\n"
            f"    python seed.py --reset"
        )

    if stamped < SCHEMA_VERSION:
        await _migrate(conn, stamped, written_by)
        action = "migrated"

    # Picks up any table added since the file was written; existing tables are untouched.
    await conn.run_sync(Base.metadata.create_all)
    await _stamp(conn, SCHEMA_VERSION, created=False)
    return SchemaState(action, stamped, SCHEMA_VERSION, written_by)


async def schema_report(conn: AsyncConnection) -> dict[str, str | int]:
    """What the file says about itself, without changing anything."""
    return {
        "schema_version": await _read_user_version(conn),
        "expected_schema_version": SCHEMA_VERSION,
        "app_version": __version__,
        **{f"recorded_{k}": v for k, v in (await _read_metadata(conn)).items()},
    }
