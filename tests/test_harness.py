"""Sanity checks on the fixtures themselves."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models.compute import Flavor, Hypervisor

pytestmark = pytest.mark.anyio


async def test_database_is_isolated_per_test() -> None:
    async with SessionLocal() as session:
        count = (await session.execute(select(func.count(Hypervisor.id)))).scalar_one()
    assert count == 0, "each test must start from an empty schema"


async def test_cloud_fixture_seeds_the_node(cloud) -> None:
    async with SessionLocal() as session:
        host = (await session.execute(select(Hypervisor))).scalar_one()
        flavors = (await session.execute(select(Flavor))).scalars().all()
    assert host.hostname == cloud.host_name == "node-01"
    assert host.threads == 64 and host.memory_mb == 262144 and host.conntrack_max == 65536
    assert {f.name for f in flavors} == {"m1.tiny", "m1.small", "m1.medium"}


async def test_every_service_answers_in_process(api) -> None:
    assert set(api) == {
        "keystone", "nova", "cinder", "glance", "neutron", "placement",
        "octavia", "swift", "cloudkitty", "scenarios", "dashboard",
    }
    assert (await api["nova"].get("/v2.1/flavors")).status_code == 200
    assert (await api["dashboard"].get("/healthz")).status_code == 200


async def test_token_is_a_32_char_uuid(token) -> None:
    assert len(token) == 32 and token.isalnum()
