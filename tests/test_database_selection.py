"""Choosing which database a run uses: ``--database`` / ``OPENSTACK_SIMULATOR_DATABASE``.

The interesting part is not the argument parsing but the swap: the API modules bind
``SessionLocal`` at import time, so a test that only checked ``settings.database_url``
would pass while every request still wrote to the database chosen before the flag was
read. These go through the session factory and look at the files on disk.

Every test here is async, including the ones that only call a pure function: the
autouse ``fresh_db`` fixture in conftest is async, and pytest will not run it for a
synchronous test.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

import main
from app.core.config import (
    database_label,
    database_path,
    resolve_database_url,
    settings,
)
from app.core.database import (
    SessionLocal,
    configure_database,
    dispose_db,
    init_db,
    use_database,
)
from app.models.identity import Project

pytestmark = pytest.mark.anyio


@pytest.fixture
async def restore_database():
    """Put the shared in-memory database back, whatever the test switched to.

    Every other test runs against it, and the autouse ``fresh_db`` fixture rebuilds the
    schema per test -- so handing back a brand-new in-memory engine is enough.
    """
    original = settings.database_url
    yield
    await dispose_db()
    configure_database(original)
    await init_db()


# --------------------------------------------------------------------------------------
# Resolving what the user typed
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dev.db", "sqlite+aiosqlite:///dev.db"),
        ("./dev.db", "sqlite+aiosqlite:///dev.db"),
        ("clouds/prod.db", "sqlite+aiosqlite:///clouds/prod.db"),
        ("/srv/prod.db", "sqlite+aiosqlite:////srv/prod.db"),
        (":memory:", "sqlite+aiosqlite:///:memory:"),
        ("memory", "sqlite+aiosqlite:///:memory:"),
        # A url is already a url, whatever backend it names.
        ("postgresql+asyncpg://u:p@db/cloud", "postgresql+asyncpg://u:p@db/cloud"),
        ("sqlite+aiosqlite:///given.db", "sqlite+aiosqlite:///given.db"),
    ],
)
async def test_resolve_database_url(value: str, expected: str) -> None:
    assert resolve_database_url(value) == expected


async def test_bare_name_gains_the_db_suffix() -> None:
    """--database prod and --database prod.db must not be two different files."""
    assert resolve_database_url("prod") == resolve_database_url("prod.db")


async def test_home_relative_paths_are_expanded() -> None:
    url = resolve_database_url("~/clouds/prod.db")
    assert "~" not in url
    assert url.endswith("/clouds/prod.db")


async def test_empty_database_is_rejected() -> None:
    with pytest.raises(ValueError):
        resolve_database_url("   ")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite+aiosqlite:///dev.db", Path("dev.db")),
        ("sqlite+aiosqlite:////srv/prod.db", Path("/srv/prod.db")),
        ("sqlite+aiosqlite:///:memory:", None),
        ("postgresql+asyncpg://u:p@db/cloud", None),
    ],
)
async def test_database_path(url: str, expected: Path | None) -> None:
    assert database_path(url) == expected


async def test_label_does_not_echo_credentials() -> None:
    """The label lands in banners and log files, so a password must not ride along."""
    label = database_label("postgresql+asyncpg://admin:hunter2@db/cloud")
    assert "hunter2" not in label and "admin" not in label


async def test_label_calls_out_an_ephemeral_database() -> None:
    assert "nothing is persisted" in database_label("sqlite+aiosqlite:///:memory:")


# --------------------------------------------------------------------------------------
# Switching
# --------------------------------------------------------------------------------------


async def test_sessions_follow_the_switch(tmp_path: Path, restore_database) -> None:
    """The proxied SessionLocal writes to the database chosen at startup, not at import."""
    dev, prod = tmp_path / "dev.db", tmp_path / "prod.db"

    for database, project in ((dev, "dev-only"), (prod, "prod-only")):
        await dispose_db()
        use_database(str(database))
        await init_db()
        async with SessionLocal() as session:
            session.add(Project(id=project, name=project, domain_id="default"))
            await session.commit()

    # Each file holds its own project and nothing of the other's.
    for database, project in ((dev, "dev-only"), (prod, "prod-only")):
        connection = sqlite3.connect(database)
        try:
            names = [row[0] for row in connection.execute("SELECT name FROM projects")]
        finally:
            connection.close()
        assert names == [project]


async def test_reopening_a_database_sees_what_was_written(
    tmp_path: Path, restore_database
) -> None:
    """Two runs against one file are one environment -- that is the whole point."""
    database = tmp_path / "staging.db"

    await dispose_db()
    use_database(str(database))
    await init_db()
    async with SessionLocal() as session:
        session.add(Project(id="kept", name="kept", domain_id="default"))
        await session.commit()

    # Drop the engine entirely, as stopping and restarting the simulator would.
    await dispose_db()
    configure_database("sqlite+aiosqlite:///:memory:")
    await init_db()
    await dispose_db()
    use_database(str(database))
    await init_db()

    async with SessionLocal() as session:
        found = (await session.execute(select(Project.name))).scalars().all()
    assert found == ["kept"]


async def test_use_database_creates_the_parent_directory(tmp_path: Path) -> None:
    """Otherwise the driver fails with 'unable to open database file' and no reason."""
    target = tmp_path / "does" / "not" / "exist" / "prod.db"
    original = settings.database_url
    try:
        use_database(str(target))
        assert target.parent.is_dir()
    finally:
        configure_database(original)


async def test_configure_database_is_a_no_op_for_the_current_url() -> None:
    """Startup calls it unconditionally; re-pointing at the same file must not churn."""
    current = settings.database_url
    assert configure_database(current) == current
    assert settings.database_url == current


# --------------------------------------------------------------------------------------
# What the CLI reports
# --------------------------------------------------------------------------------------


async def test_pid_file_records_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--status has no other way to tell which environment is up: the ports are shared."""
    monkeypatch.setattr(main, "PID_FILE", tmp_path / "sim.pid")
    monkeypatch.setattr(main, "_process_alive", lambda pid: True)

    main._write_pid()
    pid, database = main._read_pidfile()
    assert pid is not None
    assert database == settings.database_url


async def test_pid_file_from_an_older_build_still_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file written before the database line existed must not break --stop."""
    pid_file = tmp_path / "sim.pid"
    pid_file.write_text("4242\n")
    monkeypatch.setattr(main, "PID_FILE", pid_file)
    monkeypatch.setattr(main, "_process_alive", lambda pid: True)

    assert main._read_pidfile() == (4242, "")


@pytest.mark.parametrize("value", ["prod", "clouds/dev.db", "~/staging.db", ":memory:"])
async def test_resolving_a_resolved_url_is_idempotent(value: str) -> None:
    """--detach hands the child the resolved url, so a second pass must not move it.

    Otherwise the parent would report one database and the child would open another.
    """
    once = resolve_database_url(value)
    assert resolve_database_url(once) == once
