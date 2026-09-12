"""Status dashboard (port 10000): a single static page polling one JSON stats endpoint."""
from __future__ import annotations

from typing import Any

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import PORTS, database_label, iso, now_utc, settings
from app.core.database import get_session
from app.models.compute import Flavor, Server
from app.models.failure import FailureInjection
from app.models.loadbalancer import LoadBalancer
from app.models.network import FloatingIP, Network, Port, SecurityGroup
from app.models.objectstore import Container, ObjectMetadata
from app.models.storage import Image, Volume
from app.services import rating
from app.services.capacity import get_usage

router = APIRouter()

# The markup and client script live as real files so editors can lint and highlight
# them; there is nothing to template, since the page renders itself from /api/stats.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
NO_CACHE = {"Cache-Control": "no-store"}


@router.get("/api/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Everything the page draws, in one round trip."""
    usage = await get_usage(session)

    servers = (
        await session.execute(
            select(Server)
            .where(Server.deleted.is_(False))
            .order_by(Server.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    flavors = {
        f.id: f
        for f in (
            await session.execute(
                select(Flavor).where(Flavor.id.in_({s.flavor_id for s in servers} or {""}))
            )
        ).scalars()
    }
    ports = (
        await session.execute(
            select(Port).where(Port.device_id.in_({s.id for s in servers} or {""}))
        )
    ).scalars().all()
    ips: dict[str, str] = {}
    for port in ports:
        if port.ip_address:
            ips.setdefault(port.device_id, port.ip_address)

    volumes = (
        await session.execute(
            select(Volume)
            .where(Volume.deleted.is_(False))
            .order_by(Volume.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    balancers = (
        await session.execute(
            select(LoadBalancer)
            .where(LoadBalancer.deleted.is_(False))
            .order_by(LoadBalancer.created_at.desc())
            .limit(25)
        )
    ).scalars().all()
    networks = (await session.execute(select(Network))).scalars().all()
    images = (
        await session.execute(select(Image).where(Image.deleted.is_(False)))
    ).scalars().all()
    groups = (await session.execute(select(SecurityGroup))).scalars().all()
    floating = (
        await session.execute(
            select(FloatingIP).where(FloatingIP.released.is_(False))
        )
    ).scalars().all()
    containers = (await session.execute(select(Container))).scalars().all()
    objects = (await session.execute(select(ObjectMetadata))).scalars().all()
    scenarios = (
        await session.execute(
            select(FailureInjection).where(
                FailureInjection.active.is_(True),
                FailureInjection.expires_at > now_utc(),
            )
        )
    ).scalars().all()

    return {
        "generated_at": iso(now_utc()),
        "host": usage.as_dict(),
        "config": {
            # Which environment this is: every database binds the same ports, so the
            # page would otherwise look identical whichever one is loaded.
            "database": database_label(),
            "cpu_allocation_ratio": settings.cpu_allocation_ratio,
            "ram_allocation_ratio": settings.ram_allocation_ratio,
            "qemu_overhead_mb": settings.qemu_overhead_mb,
            "transition_window": [
                settings.transition_min_seconds,
                settings.transition_max_seconds,
            ],
            "ports": PORTS,
        },
        "servers": [
            {
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "task_state": s.task_state,
                "flavor": flavors[s.flavor_id].name if s.flavor_id in flavors else "?",
                "vcpus": s.allocated_vcpus,
                "ram_mb": s.allocated_ram_mb + s.overhead_ram_mb,
                "disk_gb": s.allocated_disk_gb,
                "ip": ips.get(s.id),
                "created": iso(s.created_at),
                "pending_until": iso(s.transition_until),
            }
            for s in servers
        ],
        "volumes": [
            {
                "id": v.id,
                "name": v.name,
                "status": v.status,
                "size": v.size,
                "type": v.volume_type,
                "bootable": v.bootable,
                "pending_until": iso(v.transition_until),
            }
            for v in volumes
        ],
        "loadbalancers": [
            {
                "id": lb.id,
                "name": lb.name,
                "vip": lb.vip_address,
                "provisioning_status": lb.provisioning_status,
                "operating_status": lb.operating_status,
                "pending_until": iso(lb.transition_until),
            }
            for lb in balancers
        ],
        "scenarios": [
            {
                "id": r.id,
                "service": r.service,
                "action": r.action,
                "hits": r.hits,
                "remaining_seconds": round(
                    max((r.expires_at - now_utc()).total_seconds(), 0), 1
                ),
            }
            for r in scenarios
        ],
        "counts": {
            "networks": len(networks),
            "images": len(images),
            "security_groups": len(groups),
            "floating_ips": len(floating),
            "containers": len(containers),
            "objects": len(objects),
            "object_bytes": sum(o.bytes for o in objects),
        },
        "billing": {
            "total": await rating.total_cost(session),
            "lines": await rating.summary(session, groupby=["res_type"]),
        },
    }


@router.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    """The page itself is static -- every figure on it comes from /api/stats."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html",
                        headers=NO_CACHE)


@router.get("/dashboard.js", include_in_schema=False)
async def dashboard_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.js",
                        media_type="application/javascript", headers=NO_CACHE)


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "time": iso(now_utc())}
