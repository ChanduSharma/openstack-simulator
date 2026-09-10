"""Unit tests for the on-the-fly rating engine."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.config import gen_id, now_utc, settings
from app.core.database import SessionLocal
from app.models.compute import Flavor, Server
from app.models.loadbalancer import LoadBalancer
from app.models.network import FloatingIP
from app.models.objectstore import Container, ObjectMetadata
from app.models.storage import Volume
from app.services import rating

pytestmark = pytest.mark.anyio


async def _flavor() -> None:
    """Servers carry an FK to flavors, so these unit tests need one to exist."""
    async with SessionLocal() as session:
        if await session.get(Flavor, "3") is None:
            session.add(Flavor(id="3", name="m1.medium", vcpus=2, ram=4096, disk=40))
            await session.commit()


async def _server(status: str, *, age_hours: float = 0.0, vcpus: int = 2,
                  ram: int = 4096, project_id: str = "p1") -> str:
    await _flavor()
    accounted = now_utc() - timedelta(hours=age_hours)
    server = Server(
        id=gen_id(), name="billed", project_id=project_id, user_id="u1", flavor_id="3",
        host="node-01", status=status, allocated_vcpus=vcpus, allocated_ram_mb=ram,
        allocated_disk_gb=40, overhead_ram_mb=256,
        created_at=accounted, accounted_at=accounted,
    )
    async with SessionLocal() as session:
        session.add(server)
        await session.commit()
    return server.id


async def _aged(model, **kwargs) -> str:
    created = now_utc() - timedelta(hours=kwargs.pop("age_hours", 1.0))
    row = model(id=gen_id(), created_at=created, **kwargs)
    async with SessionLocal() as session:
        session.add(row)
        await session.commit()
    return row.id


async def test_hourly_cost_is_vcpu_plus_ram() -> None:
    cost = rating.instance_hourly_cost(vcpus=2, ram_mb=4096)
    expected = 2 * settings.rate_vcpu_hour + 4.0 * settings.rate_ram_gb_hour
    assert cost == pytest.approx(expected)


async def test_accrue_advances_active_seconds() -> None:
    server_id = await _server("ACTIVE", age_hours=2)
    async with SessionLocal() as session:
        await rating.accrue(session)
        server = await session.get(Server, server_id)
    assert server.active_seconds == pytest.approx(7200, rel=0.01)
    assert server.idle_seconds == 0


async def test_accrue_advances_idle_seconds_for_stopped_instances() -> None:
    server_id = await _server("SHUTOFF", age_hours=3)
    async with SessionLocal() as session:
        await rating.accrue(session)
        server = await session.get(Server, server_id)
    assert server.idle_seconds == pytest.approx(10800, rel=0.01)
    assert server.active_seconds == 0


async def test_accrue_is_idempotent_within_the_same_instant() -> None:
    server_id = await _server("ACTIVE", age_hours=1)
    async with SessionLocal() as session:
        await rating.accrue(session)
        first = (await session.get(Server, server_id)).active_seconds
        await rating.accrue(session)
        second = (await session.get(Server, server_id)).active_seconds
    assert second == pytest.approx(first, abs=1.0), "no double counting on re-read"


async def test_deleted_instances_stop_accruing() -> None:
    server_id = await _server("ACTIVE", age_hours=1)
    async with SessionLocal() as session:
        server = await session.get(Server, server_id)
        server.deleted = True
        await session.commit()
        await rating.accrue(session)
        assert (await session.get(Server, server_id)).active_seconds == 0


async def test_instance_line_uses_accumulated_seconds_over_3600() -> None:
    await _server("ACTIVE", age_hours=10, vcpus=2, ram=4096)
    async with SessionLocal() as session:
        rows = await rating.collect(session, "p1")
    line = next(r for r in rows if r["res_type"] == rating.RES_INSTANCE)
    assert line["qty"] == pytest.approx(10.0, rel=0.01)
    assert line["rate"] == pytest.approx(10.0 * rating.instance_hourly_cost(2, 4096), rel=0.01)


async def test_idle_instances_bill_at_the_reduced_multiplier() -> None:
    await _server("SHUTOFF", age_hours=10, vcpus=2, ram=4096)
    async with SessionLocal() as session:
        rows = await rating.collect(session, "p1")
    line = next(r for r in rows if r["res_type"] == rating.RES_INSTANCE_IDLE)
    full = 10.0 * rating.instance_hourly_cost(2, 4096)
    assert line["rate"] == pytest.approx(full * settings.rate_idle_multiplier, rel=0.01)
    assert line["rate"] < full


async def test_volume_is_billed_per_gigabyte_hour() -> None:
    await _aged(Volume, name="v", project_id="p1", user_id="u1", size=100,
                status="available", age_hours=2)
    async with SessionLocal() as session:
        rows = await rating.collect(session, "p1")
    line = next(r for r in rows if r["res_type"] == rating.RES_VOLUME)
    assert line["qty"] == pytest.approx(200.0, rel=0.02)   # 100 GB x 2 h
    assert line["rate"] == pytest.approx(200.0 * settings.rate_volume_gb_hour, rel=0.02)


async def test_floating_ip_and_loadbalancer_are_billed_per_hour() -> None:
    await _aged(FloatingIP, project_id="p1", floating_network_id="n1",
                floating_ip_address="172.24.4.9", age_hours=4)
    await _aged(LoadBalancer, project_id="p1", name="lb", age_hours=4)
    async with SessionLocal() as session:
        rows = await rating.collect(session, "p1")
    fip = next(r for r in rows if r["res_type"] == rating.RES_FLOATING_IP)
    lb = next(r for r in rows if r["res_type"] == rating.RES_LOADBALANCER)
    assert fip["rate"] == pytest.approx(4 * settings.rate_floating_ip_hour, rel=0.02)
    assert lb["rate"] == pytest.approx(4 * settings.rate_loadbalancer_hour, rel=0.02)


async def test_released_floating_ip_stops_billing() -> None:
    fip_id = await _aged(FloatingIP, project_id="p1", floating_network_id="n1",
                         floating_ip_address="172.24.4.10", age_hours=4)
    async with SessionLocal() as session:
        (await session.get(FloatingIP, fip_id)).released = True
        await session.commit()
        rows = await rating.collect(session, "p1")
    assert not [r for r in rows if r["res_type"] == rating.RES_FLOATING_IP]


async def test_objects_are_billed_per_gib_hour() -> None:
    async with SessionLocal() as session:
        container = Container(id=gen_id(), name="c", project_id="p1")
        session.add(container)
        await session.flush()
        session.add(ObjectMetadata(
            id=gen_id(), container_id=container.id, name="o", project_id="p1",
            bytes=2 * 1024 ** 3, created_at=now_utc() - timedelta(hours=5),
        ))
        await session.commit()
        rows = await rating.collect(session, "p1")
    line = next(r for r in rows if r["res_type"] == rating.RES_OBJECT)
    assert line["qty"] == pytest.approx(10.0, rel=0.05)      # 2 GiB x 5 h


async def test_summary_groups_by_resource_type() -> None:
    await _server("ACTIVE", age_hours=1)
    await _aged(Volume, name="v", project_id="p1", user_id="u1", size=10,
                status="available", age_hours=1)
    async with SessionLocal() as session:
        rows = await rating.summary(session, project_id="p1", groupby=["res_type"])
    kinds = {r["res_type"] for r in rows}
    assert kinds == {rating.RES_INSTANCE, rating.RES_VOLUME}
    assert all(r["tenant_id"] == "ALL" for r in rows)


async def test_summary_groups_by_tenant() -> None:
    await _server("ACTIVE", age_hours=1, project_id="p1")
    await _server("ACTIVE", age_hours=1, project_id="p2")
    async with SessionLocal() as session:
        rows = await rating.summary(session, groupby=["tenant_id"])
    assert {r["tenant_id"] for r in rows} == {"p1", "p2"}
    assert all(r["res_type"] == "ALL" for r in rows)


async def test_summary_without_groupby_collapses_to_one_bucket() -> None:
    await _server("ACTIVE", age_hours=1, project_id="p1")
    await _server("SHUTOFF", age_hours=1, project_id="p1")
    async with SessionLocal() as session:
        rows = await rating.summary(session, groupby=[])
    assert len(rows) == 1
    assert rows[0]["res_type"] == "ALL" and rows[0]["tenant_id"] == "ALL"


async def test_project_filter_excludes_other_tenants() -> None:
    await _server("ACTIVE", age_hours=1, project_id="p1")
    await _server("ACTIVE", age_hours=1, project_id="p2")
    async with SessionLocal() as session:
        rows = await rating.summary(session, project_id="p1", groupby=["tenant_id"])
    assert [r["tenant_id"] for r in rows] == ["p1"]


async def test_total_cost_equals_the_sum_of_the_lines() -> None:
    await _server("ACTIVE", age_hours=3)
    await _aged(Volume, name="v", project_id="p1", user_id="u1", size=50,
                status="available", age_hours=3)
    async with SessionLocal() as session:
        rows = await rating.collect(session, "p1")
        total = await rating.total_cost(session, "p1")
    assert total == pytest.approx(sum(r["rate"] for r in rows), rel=0.02)
    assert total > 0


async def test_empty_cloud_costs_nothing() -> None:
    async with SessionLocal() as session:
        assert await rating.total_cost(session) == 0
        assert await rating.summary(session, groupby=["res_type"]) == []
