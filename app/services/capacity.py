"""Bare-metal depletion engine.

Every allocation (instance, volume, security-group rule) is booked against the single
simulated node. Placement, Nova's hypervisor API and the dashboard all read from here so
the numbers can never drift apart.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.compute import (
    STATES_HOLDING_COMPUTE,
    STATES_HOLDING_DISK,
    Hypervisor,
    Server,
)
from app.models.network import SecurityGroupRule
from app.models.storage import VOLUME_STATES_HOLDING_DISK, Volume

# Placement resource classes tracked on the node.
RC_VCPU = "VCPU"
RC_MEMORY_MB = "MEMORY_MB"
RC_DISK_GB = "DISK_GB"


class CapacityError(Exception):
    """Raised when the node cannot satisfy a request -- the depletion envelope is full."""

    def __init__(self, resource: str, requested: float, used: float, limit: float) -> None:
        self.resource = resource
        self.requested = requested
        self.used = used
        self.limit = limit
        super().__init__(
            f"Insufficient {resource} on host: requested {requested:g}, "
            f"used {used:g} of {limit:g} (free {max(limit - used, 0):g})"
        )


@dataclass(slots=True)
class HostUsage:
    """A point-in-time snapshot of the node's depletion envelope."""

    host: str
    vcpus_total: int
    vcpus_allocatable: float
    vcpus_used: int
    ram_total_mb: int
    ram_allocatable_mb: float
    ram_used_mb: int
    disk_total_gb: int
    disk_allocatable_gb: float
    disk_used_gb: int
    disk_used_instances_gb: int
    disk_used_volumes_gb: int
    conntrack_max: int
    conntrack_used: int
    running_vms: int
    total_instances: int

    @property
    def vcpus_free(self) -> float:
        return max(self.vcpus_allocatable - self.vcpus_used, 0)

    @property
    def ram_free_mb(self) -> float:
        return max(self.ram_allocatable_mb - self.ram_used_mb, 0)

    @property
    def disk_free_gb(self) -> float:
        return max(self.disk_allocatable_gb - self.disk_used_gb, 0)

    @property
    def conntrack_free(self) -> int:
        return max(self.conntrack_max - self.conntrack_used, 0)

    def pct(self, used: float, total: float) -> float:
        return round(100.0 * used / total, 2) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            {
                "vcpus_free": self.vcpus_free,
                "ram_free_mb": self.ram_free_mb,
                "disk_free_gb": self.disk_free_gb,
                "conntrack_free": self.conntrack_free,
                "vcpus_pct": self.pct(self.vcpus_used, self.vcpus_allocatable),
                "ram_pct": self.pct(self.ram_used_mb, self.ram_allocatable_mb),
                "disk_pct": self.pct(self.disk_used_gb, self.disk_allocatable_gb),
                "conntrack_pct": self.pct(self.conntrack_used, self.conntrack_max),
            }
        )
        return data


async def get_host(session: AsyncSession) -> Hypervisor:
    """The single simulated node. Falls back to config defaults if the DB is unseeded."""
    host = (
        await session.execute(select(Hypervisor).order_by(Hypervisor.hostname).limit(1))
    ).scalar_one_or_none()
    if host is not None:
        return host
    return Hypervisor(
        id="00000000-0000-0000-0000-000000000001",
        hostname=settings.host_name,
        sockets=settings.host_sockets,
        cores=settings.host_cores,
        threads=settings.host_threads,
        vcpus=settings.total_vcpus,
        memory_mb=settings.host_ram_mb,
        local_gb=settings.host_disk_gb,
        cpu_allocation_ratio=settings.cpu_allocation_ratio,
        ram_allocation_ratio=settings.ram_allocation_ratio,
        disk_allocation_ratio=settings.disk_allocation_ratio,
        reserved_memory_mb=settings.host_reserved_ram_mb,
        reserved_disk_gb=settings.host_reserved_disk_gb,
        conntrack_max=settings.host_conntrack_max,
    )


async def get_usage(session: AsyncSession, host: Hypervisor | None = None) -> HostUsage:
    """Aggregate every booked resource with four cheap SQL sums."""
    host = host or await get_host(session)

    compute_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(Server.allocated_vcpus), 0),
                func.coalesce(
                    func.sum(Server.allocated_ram_mb + Server.overhead_ram_mb), 0
                ),
                func.count(Server.id),
            ).where(
                Server.deleted.is_(False), Server.status.in_(STATES_HOLDING_COMPUTE)
            )
        )
    ).one()

    instance_disk = (
        await session.execute(
            select(func.coalesce(func.sum(Server.allocated_disk_gb), 0)).where(
                Server.deleted.is_(False), Server.status.in_(STATES_HOLDING_DISK)
            )
        )
    ).scalar_one()

    volume_disk = (
        await session.execute(
            select(func.coalesce(func.sum(Volume.size), 0)).where(
                Volume.deleted.is_(False),
                Volume.status.in_(VOLUME_STATES_HOLDING_DISK),
            )
        )
    ).scalar_one()

    conntrack_used = (
        await session.execute(select(func.count(SecurityGroupRule.id)))
    ).scalar_one()

    running = (
        await session.execute(
            select(func.count(Server.id)).where(
                Server.deleted.is_(False), Server.status.in_(("ACTIVE", "BUILD"))
            )
        )
    ).scalar_one()

    total_instances = (
        await session.execute(
            select(func.count(Server.id)).where(Server.deleted.is_(False))
        )
    ).scalar_one()

    allocatable_ram = (host.memory_mb - host.reserved_memory_mb) * host.ram_allocation_ratio
    allocatable_disk = (host.local_gb - host.reserved_disk_gb) * host.disk_allocation_ratio

    return HostUsage(
        host=host.hostname,
        vcpus_total=host.vcpus,
        vcpus_allocatable=host.vcpus * host.cpu_allocation_ratio,
        vcpus_used=int(compute_row[0]),
        ram_total_mb=host.memory_mb,
        ram_allocatable_mb=allocatable_ram,
        ram_used_mb=int(compute_row[1]),
        disk_total_gb=host.local_gb,
        disk_allocatable_gb=allocatable_disk,
        disk_used_gb=int(instance_disk) + int(volume_disk),
        disk_used_instances_gb=int(instance_disk),
        disk_used_volumes_gb=int(volume_disk),
        conntrack_max=host.conntrack_max,
        conntrack_used=int(conntrack_used),
        running_vms=int(running),
        total_instances=int(total_instances),
    )


async def check_instance_capacity(
    session: AsyncSession, vcpus: int, ram_mb: int, disk_gb: int
) -> HostUsage:
    """Raise CapacityError unless the node can host one more instance of this shape."""
    usage = await get_usage(session)
    total_ram = ram_mb + settings.qemu_overhead_mb

    if usage.vcpus_used + vcpus > usage.vcpus_allocatable:
        raise CapacityError("cores", vcpus, usage.vcpus_used, usage.vcpus_allocatable)
    if usage.ram_used_mb + total_ram > usage.ram_allocatable_mb:
        raise CapacityError("ram", total_ram, usage.ram_used_mb, usage.ram_allocatable_mb)
    if disk_gb and usage.disk_used_gb + disk_gb > usage.disk_allocatable_gb:
        raise CapacityError(
            "disk_gb", disk_gb, usage.disk_used_gb, usage.disk_allocatable_gb
        )
    return usage


async def check_volume_capacity(session: AsyncSession, size_gb: int) -> HostUsage:
    """Volumes are carved out of the same physical storage pool as instance disks."""
    usage = await get_usage(session)
    if usage.disk_used_gb + size_gb > usage.disk_allocatable_gb:
        raise CapacityError(
            "gigabytes", size_gb, usage.disk_used_gb, usage.disk_allocatable_gb
        )
    return usage


async def check_conntrack_capacity(session: AsyncSession, entries: int = 1) -> HostUsage:
    """Each security-group rule consumes one conntrack slot on the node."""
    usage = await get_usage(session)
    if usage.conntrack_used + entries > usage.conntrack_max:
        raise CapacityError(
            "conntrack_entries", entries, usage.conntrack_used, usage.conntrack_max
        )
    return usage


async def placement_inventory(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Inventory records for the node's resource provider, in Placement's schema."""
    host = await get_host(session)
    return {
        RC_VCPU: {
            "allocation_ratio": host.cpu_allocation_ratio,
            "max_unit": host.vcpus,
            "min_unit": 1,
            "reserved": 0,
            "step_size": 1,
            "total": host.vcpus,
        },
        RC_MEMORY_MB: {
            "allocation_ratio": host.ram_allocation_ratio,
            "max_unit": host.memory_mb,
            "min_unit": 1,
            "reserved": host.reserved_memory_mb,
            "step_size": 1,
            "total": host.memory_mb,
        },
        RC_DISK_GB: {
            "allocation_ratio": host.disk_allocation_ratio,
            "max_unit": host.local_gb,
            "min_unit": 1,
            "reserved": host.reserved_disk_gb,
            "step_size": 1,
            "total": host.local_gb,
        },
    }


async def placement_usages(session: AsyncSession) -> dict[str, int]:
    """Live VCPU / MEMORY_MB / DISK_GB consumption, mirroring Nova's bookings."""
    usage = await get_usage(session)
    return {
        RC_VCPU: usage.vcpus_used,
        RC_MEMORY_MB: usage.ram_used_mb,
        RC_DISK_GB: usage.disk_used_instances_gb,
    }


async def consumer_allocations(session: AsyncSession, consumer_uuid: str) -> dict[str, int]:
    """Per-instance allocation record, keyed by the Nova server UUID (the consumer)."""
    server = await session.get(Server, consumer_uuid)
    if server is None or server.deleted:
        return {}
    resources: dict[str, int] = {}
    if server.status in STATES_HOLDING_COMPUTE:
        resources[RC_VCPU] = server.allocated_vcpus
        resources[RC_MEMORY_MB] = server.allocated_ram_mb + server.overhead_ram_mb
    if server.status in STATES_HOLDING_DISK:
        resources[RC_DISK_GB] = server.allocated_disk_gb
    return resources
