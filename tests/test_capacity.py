"""Unit tests for the bare-metal depletion engine."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import gen_id, settings
from app.core.database import SessionLocal
from app.models.compute import Server
from app.models.network import SecurityGroup, SecurityGroupRule
from app.models.storage import Volume
from app.services.capacity import (
    CapacityError,
    check_conntrack_capacity,
    check_instance_capacity,
    check_volume_capacity,
    consumer_allocations,
    get_host,
    get_usage,
    placement_inventory,
    placement_usages,
)

pytestmark = pytest.mark.anyio


async def _add_server(status: str = "ACTIVE", vcpus: int = 2, ram: int = 4096,
                      disk: int = 40, project_id: str = "p1") -> str:
    server = Server(
        id=gen_id(), name=f"srv-{status.lower()}", project_id=project_id, user_id="u1",
        flavor_id="3", host="node-01", status=status,
        allocated_vcpus=vcpus, allocated_ram_mb=ram, allocated_disk_gb=disk,
        overhead_ram_mb=settings.qemu_overhead_mb,
    )
    async with SessionLocal() as session:
        session.add(server)
        await session.commit()
    return server.id


async def _add_volume(size: int = 100, status: str = "available") -> str:
    volume = Volume(id=gen_id(), name="v", project_id="p1", user_id="u1",
                    size=size, status=status)
    async with SessionLocal() as session:
        session.add(volume)
        await session.commit()
    return volume.id


async def test_unseeded_host_falls_back_to_config_defaults() -> None:
    async with SessionLocal() as session:
        host = await get_host(session)
    assert host.hostname == settings.host_name
    assert host.memory_mb == settings.host_ram_mb
    assert host.conntrack_max == settings.host_conntrack_max


async def test_empty_node_reports_zero_usage(cloud) -> None:
    async with SessionLocal() as session:
        usage = await get_usage(session)
    assert (usage.vcpus_used, usage.ram_used_mb, usage.disk_used_gb) == (0, 0, 0)
    assert usage.vcpus_allocatable == 64 * 3.0        # 3x overcommit
    assert usage.ram_allocatable_mb == 262144 - 512   # strict 1.0x, minus reserved
    assert usage.disk_allocatable_gb == 4096
    assert usage.conntrack_used == 4                  # the seeded default group's rules


async def test_instance_books_vcpu_ram_overhead_and_disk(cloud) -> None:
    await _add_server()
    async with SessionLocal() as session:
        usage = await get_usage(session)
    assert usage.vcpus_used == 2
    assert usage.ram_used_mb == 4096 + 256, "flavor RAM plus per-VM QEMU overhead"
    assert usage.disk_used_instances_gb == 40
    assert usage.running_vms == 1


@pytest.mark.parametrize(
    "status, holds_compute, holds_disk",
    [
        ("ACTIVE", True, True),
        ("BUILD", True, True),
        ("SHUTOFF", True, True),
        ("PAUSED", True, True),
        ("SUSPENDED", True, True),
        ("ERROR", True, True),
        ("SHELVED", True, True),
        ("SHELVED_OFFLOADED", False, True),
    ],
)
async def test_state_decides_what_stays_booked(cloud, status, holds_compute, holds_disk) -> None:
    await _add_server(status=status)
    async with SessionLocal() as session:
        usage = await get_usage(session)
    assert (usage.vcpus_used == 2) is holds_compute
    assert (usage.ram_used_mb == 4352) is holds_compute
    assert (usage.disk_used_instances_gb == 40) is holds_disk


async def test_deleted_instance_releases_everything(cloud) -> None:
    server_id = await _add_server()
    async with SessionLocal() as session:
        server = await session.get(Server, server_id)
        server.deleted = True
        server.status = "DELETED"
        await session.commit()
        usage = await get_usage(session)
    assert (usage.vcpus_used, usage.ram_used_mb, usage.disk_used_gb) == (0, 0, 0)


async def test_volumes_share_the_instance_disk_pool(cloud) -> None:
    await _add_server()
    await _add_volume(size=100)
    async with SessionLocal() as session:
        usage = await get_usage(session)
        placement = await placement_usages(session)
    assert usage.disk_used_gb == 140, "40 GB root disk + 100 GB volume"
    assert usage.disk_used_volumes_gb == 100
    # Placement's DISK_GB tracks instance disks only, as real Placement does.
    assert placement["DISK_GB"] == 40


async def test_deleted_volume_releases_its_gigabytes(cloud) -> None:
    volume_id = await _add_volume(size=250)
    async with SessionLocal() as session:
        volume = await session.get(Volume, volume_id)
        volume.deleted = True
        volume.status = "deleted"
        await session.commit()
        usage = await get_usage(session)
    assert usage.disk_used_volumes_gb == 0


async def test_cpu_overcommit_is_enforced_at_three_times(cloud) -> None:
    # 192 allocatable vCPU; book 190 with a negligible RAM footprint.
    await _add_server(vcpus=190, ram=1024, disk=0)
    async with SessionLocal() as session:
        await check_instance_capacity(session, 2, 512, 0)          # exactly fits
        with pytest.raises(CapacityError) as excinfo:
            await check_instance_capacity(session, 3, 512, 0)
    assert excinfo.value.resource == "cores"
    assert excinfo.value.limit == 192.0


async def test_ram_is_not_overcommitted(cloud) -> None:
    await _add_server(vcpus=1, ram=256000, disk=0)   # 256000 + 256 booked
    async with SessionLocal() as session:
        with pytest.raises(CapacityError) as excinfo:
            await check_instance_capacity(session, 1, 6000, 0)
    assert excinfo.value.resource == "ram"
    assert excinfo.value.requested == 6000 + 256


async def test_disk_exhaustion_is_reported_separately(cloud) -> None:
    await _add_volume(size=4000)
    async with SessionLocal() as session:
        with pytest.raises(CapacityError) as excinfo:
            await check_instance_capacity(session, 1, 512, 200)
    assert excinfo.value.resource == "disk_gb"
    with pytest.raises(CapacityError) as excinfo:
        async with SessionLocal() as session:
            await check_volume_capacity(session, 200)
    assert excinfo.value.resource == "gigabytes"


async def test_capacity_error_message_names_the_shortfall(cloud) -> None:
    await _add_server(vcpus=1, ram=260000, disk=0)
    async with SessionLocal() as session:
        with pytest.raises(CapacityError) as excinfo:
            await check_instance_capacity(session, 1, 4096, 0)
    message = str(excinfo.value)
    assert "Insufficient ram" in message and "free" in message


async def test_each_security_group_rule_burns_one_conntrack_slot(cloud) -> None:
    async with SessionLocal() as session:
        group = (await session.execute(select(SecurityGroup))).scalars().first()
        before = (await get_usage(session)).conntrack_used
        session.add(
            SecurityGroupRule(id=gen_id(), security_group_id=group.id,
                              project_id=cloud.project_id)
        )
        await session.commit()
        after = (await get_usage(session)).conntrack_used
    assert after == before + 1


async def test_conntrack_capacity_check_respects_the_cap(cloud) -> None:
    async with SessionLocal() as session:
        host = await get_host(session)
        host.conntrack_max = 5          # 4 already used by the seeded default group
        await session.commit()
        await check_conntrack_capacity(session, 1)
        with pytest.raises(CapacityError) as excinfo:
            await check_conntrack_capacity(session, 2)
    assert excinfo.value.resource == "conntrack_entries"


async def test_placement_inventory_mirrors_the_node(cloud) -> None:
    async with SessionLocal() as session:
        inventory = await placement_inventory(session)
    assert set(inventory) == {"VCPU", "MEMORY_MB", "DISK_GB"}
    assert inventory["VCPU"]["total"] == 64
    assert inventory["VCPU"]["allocation_ratio"] == 3.0
    assert inventory["MEMORY_MB"]["allocation_ratio"] == 1.0
    assert inventory["MEMORY_MB"]["reserved"] == 512
    assert inventory["DISK_GB"]["total"] == 4096


async def test_placement_usage_tracks_nova_bookings(cloud) -> None:
    await _add_server()
    await _add_server(status="SHELVED_OFFLOADED")
    async with SessionLocal() as session:
        usages = await placement_usages(session)
    assert usages["VCPU"] == 2                 # the offloaded one released its vCPU
    assert usages["MEMORY_MB"] == 4352
    assert usages["DISK_GB"] == 80             # but both still hold their root disk


async def test_consumer_allocations_follow_the_instance_state(cloud) -> None:
    active = await _add_server(status="ACTIVE")
    offloaded = await _add_server(status="SHELVED_OFFLOADED")
    async with SessionLocal() as session:
        assert await consumer_allocations(session, active) == {
            "VCPU": 2, "MEMORY_MB": 4352, "DISK_GB": 40
        }
        assert await consumer_allocations(session, offloaded) == {"DISK_GB": 40}
        assert await consumer_allocations(session, "does-not-exist") == {}


async def test_usage_percentages_and_free_pools(cloud) -> None:
    await _add_server(vcpus=96, ram=1024, disk=0)
    async with SessionLocal() as session:
        usage = await get_usage(session)
    assert usage.vcpus_free == 96.0
    assert usage.pct(usage.vcpus_used, usage.vcpus_allocatable) == 50.0
    payload = usage.as_dict()
    assert payload["vcpus_pct"] == 50.0
    assert payload["conntrack_free"] == 65536 - payload["conntrack_used"]
