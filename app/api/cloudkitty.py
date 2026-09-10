"""CloudKitty Rating v1 (port 8889): usage-cost evaluation computed on request."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import iso, now_utc, settings
from app.core.database import get_session
from app.core.middleware import AuthContext, fault, require
from app.models.compute import Server
from app.services import rating

SERVICE = "cloudkitty"
router = APIRouter()
auth_dep = require(SERVICE)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise fault(SERVICE, 400, f"Invalid timestamp: {value}")


@router.get("/")
async def index() -> dict[str, Any]:
    return {
        "versions": [
            {
                "id": "v1",
                "status": "CURRENT",
                "links": [{"rel": "self", "href": "/v1"}],
            }
        ]
    }


@router.get("/v1")
async def version() -> dict[str, Any]:
    return {
        "version": "1.0",
        "resources": ["report", "rating", "storage", "info"],
    }


@router.get("/v1/report/summary")
async def report_summary(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """(accumulated_seconds / 3600) * unit_cost, aggregated live from SQLite."""
    params = request.query_params
    tenant_id = params.get("tenant_id") or params.get("project_id")
    if params.get("all_tenants", "false").lower() not in ("true", "1"):
        tenant_id = tenant_id or auth.project_id
    groupby = [g for g in params.get("groupby", "").split(",") if g] or [
        "res_type",
        "tenant_id",
    ]
    rows = await rating.summary(
        session,
        project_id=tenant_id,
        groupby=groupby,
        begin=_parse_time(params.get("begin")),
        end=_parse_time(params.get("end")),
    )
    return {"summary": rows}


@router.get("/v1/report/total")
async def report_total(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_id = request.query_params.get("tenant_id") or auth.project_id
    total = await rating.total_cost(session, tenant_id)
    return {
        "begin": None,
        "end": iso(now_utc()),
        "tenant_id": tenant_id,
        "rate": total,
        "total": total,
    }


@router.get("/v1/report/tenants")
async def report_tenants(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> list[str]:
    rows = (
        await session.execute(select(Server.project_id).distinct())
    ).scalars().all()
    return [row for row in rows if row]


@router.get("/v1/rating/modules")
async def rating_modules(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "modules": [
            {
                "module_id": "hashmap",
                "description": "Simulated hashmap rating module",
                "enabled": True,
                "hot_config": False,
                "priority": 1,
            },
            {
                "module_id": "noop",
                "description": "No-op rating module",
                "enabled": False,
                "hot_config": False,
                "priority": 0,
            },
        ]
    }


@router.get("/v1/rating/modules/{module_id}")
async def rating_module(module_id: str, auth: AuthContext = auth_dep) -> dict[str, Any]:
    if module_id not in ("hashmap", "noop"):
        raise fault(SERVICE, 404, f"Module {module_id} not found.")
    return {
        "module_id": module_id,
        "enabled": module_id == "hashmap",
        "hot_config": False,
        "priority": 1 if module_id == "hashmap" else 0,
    }


@router.post("/v1/rating/quote")
async def rating_quote(
    body: dict[str, Any] | None = None, auth: AuthContext = auth_dep
) -> float:
    """Price a hypothetical instance without creating anything."""
    resources = (body or {}).get("resources", [])
    total = 0.0
    for resource in resources:
        desc = resource.get("desc", {})
        total += rating.instance_hourly_cost(
            int(desc.get("vcpus", 1)), int(desc.get("memory", 512))
        ) * float(resource.get("volume", 1))
    return round(total, 6)


@router.get("/v1/info/config")
async def info_config(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "collect": {"period": 3600, "wait_periods": 0, "scope_key": "project_id"},
        "rates": {
            "vcpu_hour": settings.rate_vcpu_hour,
            "ram_gb_hour": settings.rate_ram_gb_hour,
            "idle_multiplier": settings.rate_idle_multiplier,
            "volume_gb_hour": settings.rate_volume_gb_hour,
            "floating_ip_hour": settings.rate_floating_ip_hour,
            "loadbalancer_hour": settings.rate_loadbalancer_hour,
            "object_gb_hour": settings.rate_object_gb_hour,
        },
    }


@router.get("/v1/info/service")
async def info_service(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "services": [
            {"service_id": res, "unit": unit}
            for res, unit in (
                (rating.RES_INSTANCE, "hour"),
                (rating.RES_INSTANCE_IDLE, "hour"),
                (rating.RES_VOLUME, "GiB-hour"),
                (rating.RES_FLOATING_IP, "hour"),
                (rating.RES_LOADBALANCER, "hour"),
                (rating.RES_OBJECT, "GiB-hour"),
            )
        ]
    }


@router.get("/v1/storage/dataframes")
async def dataframes(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """One synthetic dataframe covering the current period."""
    rows = await rating.collect(session, auth.project_id)
    total_servers = (
        await session.execute(
            select(func.count(Server.id)).where(Server.deleted.is_(False))
        )
    ).scalar_one()
    return {
        "total": len(rows),
        "dataframes": [
            {
                "begin": None,
                "end": iso(now_utc()),
                "tenant_id": row["tenant_id"],
                "resources": [
                    {
                        "service": row["res_type"],
                        "volume": row["qty"],
                        "rating": row["rate"],
                        "desc": {"instances": total_servers},
                    }
                ],
            }
            for row in rows
        ],
    }
