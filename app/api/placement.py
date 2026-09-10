"""Placement API (port 8778): resource providers, inventories, usages, allocations.

Figures are derived from the same capacity service Nova books against, so Placement can
never disagree with the hypervisor view.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import API_VERSIONS, service_url
from app.core.database import get_session
from app.core.middleware import AuthContext, fault, require
from app.models.compute import STATES_HOLDING_COMPUTE, STATES_HOLDING_DISK, Server
from app.services.capacity import (
    RC_DISK_GB,
    RC_MEMORY_MB,
    RC_VCPU,
    consumer_allocations,
    get_host,
    get_usage,
    placement_inventory,
    placement_usages,
)

SERVICE = "placement"
router = APIRouter()
auth_dep = require(SERVICE)

_, MIN_VERSION, MAX_VERSION = API_VERSIONS["placement"]

TRAITS: list[str] = [
    "HW_CPU_X86_SSE42",
    "HW_CPU_X86_AVX2",
    "COMPUTE_NET_VIF_MODEL_VIRTIO",
    "COMPUTE_VOLUME_ATTACH",
    "COMPUTE_IMAGE_TYPE_QCOW2",
    "COMPUTE_IMAGE_TYPE_RAW",
    "COMPUTE_TRUSTED_CERTS",
]


def _provider_links(uuid: str) -> list[dict[str, str]]:
    return [
        {"rel": "self", "href": f"/resource_providers/{uuid}"},
        {"rel": "inventories", "href": f"/resource_providers/{uuid}/inventories"},
        {"rel": "usages", "href": f"/resource_providers/{uuid}/usages"},
        {"rel": "aggregates", "href": f"/resource_providers/{uuid}/aggregates"},
        {"rel": "traits", "href": f"/resource_providers/{uuid}/traits"},
        {"rel": "allocations", "href": f"/resource_providers/{uuid}/allocations"},
    ]


async def _generation(session: AsyncSession) -> int:
    """Bump the generation with every allocation change, as real Placement does."""
    count = (
        await session.execute(select(Server.id).where(Server.deleted.is_(False)))
    ).scalars().all()
    return len(count) + 1


async def _provider(session: AsyncSession) -> dict[str, Any]:
    host = await get_host(session)
    return {
        "uuid": host.id,
        "name": host.hostname,
        "generation": await _generation(session),
        "parent_provider_uuid": None,
        "root_provider_uuid": host.id,
        "links": _provider_links(host.id),
    }


async def _require_provider(session: AsyncSession, uuid: str) -> Any:
    host = await get_host(session)
    if uuid not in (host.id, host.hostname):
        raise fault(
            SERVICE,
            404,
            f"No resource provider with uuid {uuid} found",
            code="placement.resource_provider.not_found",
        )
    return host


# --------------------------------------------------------------------------------------
# Version discovery
# --------------------------------------------------------------------------------------


@router.get("/", include_in_schema=False)
async def versions() -> dict[str, Any]:
    return {
        "versions": [
            {
                "id": "v1.0",
                "status": "CURRENT",
                "max_version": MAX_VERSION,
                "min_version": MIN_VERSION,
                "links": [{"rel": "self", "href": service_url(SERVICE, "/")}],
            }
        ]
    }


# --------------------------------------------------------------------------------------
# Resource providers
# --------------------------------------------------------------------------------------


@router.get("/resource_providers")
async def list_resource_providers(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    provider = await _provider(session)
    name = request.query_params.get("name")
    if name and name != provider["name"]:
        return {"resource_providers": []}
    return {"resource_providers": [provider]}


@router.get("/resource_providers/{uuid}")
async def get_resource_provider(
    uuid: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_provider(session, uuid)
    return await _provider(session)


@router.get("/resource_providers/{uuid}/inventories")
async def get_inventories(
    uuid: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_provider(session, uuid)
    return {
        "inventories": await placement_inventory(session),
        "resource_provider_generation": await _generation(session),
    }


@router.get("/resource_providers/{uuid}/inventories/{resource_class}")
async def get_inventory(
    uuid: str,
    resource_class: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_provider(session, uuid)
    inventories = await placement_inventory(session)
    if resource_class not in inventories:
        raise fault(
            SERVICE,
            404,
            f"No inventory of class {resource_class} for {uuid}",
            code="placement.inventory.not_found",
        )
    body = dict(inventories[resource_class])
    body["resource_provider_generation"] = await _generation(session)
    return body


@router.get("/resource_providers/{uuid}/usages")
async def get_provider_usages(
    uuid: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_provider(session, uuid)
    return {
        "resource_provider_generation": await _generation(session),
        "usages": await placement_usages(session),
    }


@router.get("/resource_providers/{uuid}/aggregates")
async def get_aggregates(
    uuid: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_provider(session, uuid)
    return {"aggregates": [], "resource_provider_generation": await _generation(session)}


@router.get("/resource_providers/{uuid}/traits")
async def get_traits(
    uuid: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_provider(session, uuid)
    return {"traits": TRAITS, "resource_provider_generation": await _generation(session)}


@router.get("/resource_providers/{uuid}/allocations")
async def get_provider_allocations(
    uuid: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_provider(session, uuid)
    servers = (
        await session.execute(
            select(Server).where(
                Server.deleted.is_(False),
                Server.status.in_(set(STATES_HOLDING_COMPUTE) | set(STATES_HOLDING_DISK)),
            )
        )
    ).scalars().all()
    allocations: dict[str, Any] = {}
    for server in servers:
        resources = await consumer_allocations(session, server.id)
        if resources:
            allocations[server.id] = {
                "resources": resources,
                "consumer_generation": 1,
                "project_id": server.project_id,
                "user_id": server.user_id,
            }
    return {
        "allocations": allocations,
        "resource_provider_generation": await _generation(session),
    }


# --------------------------------------------------------------------------------------
# Allocations / usages / candidates
# --------------------------------------------------------------------------------------


@router.get("/allocations/{consumer_uuid}")
async def get_allocations(
    consumer_uuid: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    host = await get_host(session)
    resources = await consumer_allocations(session, consumer_uuid)
    server = await session.get(Server, consumer_uuid)
    body: dict[str, Any] = {"allocations": {}, "consumer_generation": 1}
    if resources:
        body["allocations"] = {
            host.id: {"resources": resources, "generation": await _generation(session)}
        }
    if server is not None:
        body["project_id"] = server.project_id
        body["user_id"] = server.user_id
    return body


@router.put("/allocations/{consumer_uuid}", status_code=204)
async def put_allocations(
    consumer_uuid: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Accepted for client compatibility; bookings are owned by the Nova simulator."""
    server = await session.get(Server, consumer_uuid)
    if server is None:
        raise fault(
            SERVICE,
            409,
            f"Unable to allocate for unknown consumer {consumer_uuid}",
            code="placement.concurrent_update",
        )
    return Response(status_code=204)


@router.delete("/allocations/{consumer_uuid}", status_code=204)
async def delete_allocations(
    consumer_uuid: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    return Response(status_code=204)


@router.get("/usages")
async def get_usages(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    project_id = request.query_params.get("project_id")
    stmt = select(Server).where(
        Server.deleted.is_(False), Server.status.in_(STATES_HOLDING_COMPUTE)
    )
    if project_id:
        stmt = stmt.where(Server.project_id == project_id)
    servers = (await session.execute(stmt)).scalars().all()
    return {
        "usages": {
            RC_VCPU: sum(s.allocated_vcpus for s in servers),
            RC_MEMORY_MB: sum(s.allocated_ram_mb + s.overhead_ram_mb for s in servers),
            RC_DISK_GB: sum(
                s.allocated_disk_gb for s in servers if s.status in STATES_HOLDING_DISK
            ),
        }
    }


@router.get("/traits")
async def list_traits(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {"traits": TRAITS}


@router.get("/resource_classes")
async def list_resource_classes(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "resource_classes": [
            {"name": name, "links": [{"rel": "self", "href": f"/resource_classes/{name}"}]}
            for name in (RC_VCPU, RC_MEMORY_MB, RC_DISK_GB, "PCPU", "IPV4_ADDRESS")
        ]
    }


@router.get("/allocation_candidates")
async def allocation_candidates(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """One provider, so candidates are either "the node" or nothing."""
    host = await get_host(session)
    usage = await get_usage(session, host)
    inventories = await placement_inventory(session)
    usages = await placement_usages(session)

    resources = request.query_params.get("resources", "")
    requested: dict[str, int] = {}
    for item in filter(None, resources.split(",")):
        name, _, amount = item.partition(":")
        if amount.isdigit():
            requested[name] = int(amount)

    fits = (
        usage.vcpus_used + requested.get(RC_VCPU, 0) <= usage.vcpus_allocatable
        and usage.ram_used_mb + requested.get(RC_MEMORY_MB, 0) <= usage.ram_allocatable_mb
        and usage.disk_used_gb + requested.get(RC_DISK_GB, 0) <= usage.disk_allocatable_gb
    )
    if not fits:
        return {"allocation_requests": [], "provider_summaries": {}}
    return {
        "allocation_requests": [
            {"allocations": {host.id: {"resources": requested or {RC_VCPU: 1}}}}
        ],
        "provider_summaries": {
            host.id: {
                "resources": {
                    name: {
                        "capacity": int(
                            (spec["total"] - spec["reserved"]) * spec["allocation_ratio"]
                        ),
                        "used": usages.get(name, 0),
                    }
                    for name, spec in inventories.items()
                },
                "traits": TRAITS,
            }
        },
    }
