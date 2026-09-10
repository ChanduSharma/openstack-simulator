"""Status dashboard (port 10000): a single static page polling one JSON stats endpoint."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import PORTS, iso, now_utc, settings
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


PAGE = """<!doctype html>
<html lang="en" class="h-full">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenStack-Simulator &middot; node status</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="h-full bg-slate-950 text-slate-200 font-sans antialiased">
<div class="max-w-7xl mx-auto p-4 sm:p-6 space-y-6">

  <header class="flex flex-wrap items-baseline justify-between gap-3 border-b border-slate-800 pb-4">
    <div>
      <h1 class="text-2xl font-semibold text-white">OpenStack-Simulator</h1>
      <p class="text-sm text-slate-400" id="host-line">bare-metal emulation &middot; loading&hellip;</p>
    </div>
    <div class="text-right text-xs text-slate-500">
      <div>updated <span id="updated">--</span></div>
      <div>polling every 5s</div>
    </div>
  </header>

  <section class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4" id="meters"></section>

  <section class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-7 gap-3" id="counts"></section>

  <section id="scenario-wrap" class="hidden">
    <h2 class="text-sm uppercase tracking-wider text-amber-400 mb-2">Active failure injections</h2>
    <div class="rounded-lg border border-amber-700/60 bg-amber-950/30 divide-y divide-amber-900/40"
         id="scenarios"></div>
  </section>

  <section class="grid grid-cols-1 xl:grid-cols-2 gap-6">
    <div>
      <h2 class="text-sm uppercase tracking-wider text-slate-400 mb-2">Instances</h2>
      <div class="overflow-x-auto rounded-lg border border-slate-800">
        <table class="min-w-full text-sm">
          <thead class="bg-slate-900 text-slate-400 text-xs uppercase">
            <tr><th class="text-left px-3 py-2">Name</th><th class="text-left px-3 py-2">Status</th>
            <th class="text-left px-3 py-2">Flavor</th><th class="text-right px-3 py-2">vCPU</th>
            <th class="text-right px-3 py-2">RAM</th><th class="text-left px-3 py-2">IP</th></tr>
          </thead>
          <tbody id="servers" class="divide-y divide-slate-800"></tbody>
        </table>
      </div>
    </div>

    <div class="space-y-6">
      <div>
        <h2 class="text-sm uppercase tracking-wider text-slate-400 mb-2">Volumes</h2>
        <div class="overflow-x-auto rounded-lg border border-slate-800">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-900 text-slate-400 text-xs uppercase">
              <tr><th class="text-left px-3 py-2">Name</th><th class="text-left px-3 py-2">Status</th>
              <th class="text-right px-3 py-2">GB</th><th class="text-left px-3 py-2">Type</th></tr>
            </thead>
            <tbody id="volumes" class="divide-y divide-slate-800"></tbody>
          </table>
        </div>
      </div>
      <div>
        <h2 class="text-sm uppercase tracking-wider text-slate-400 mb-2">Load balancers</h2>
        <div class="overflow-x-auto rounded-lg border border-slate-800">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-900 text-slate-400 text-xs uppercase">
              <tr><th class="text-left px-3 py-2">Name</th><th class="text-left px-3 py-2">Provisioning</th>
              <th class="text-left px-3 py-2">Operating</th><th class="text-left px-3 py-2">VIP</th></tr>
            </thead>
            <tbody id="lbs" class="divide-y divide-slate-800"></tbody>
          </table>
        </div>
      </div>
    </div>
  </section>

  <section>
    <h2 class="text-sm uppercase tracking-wider text-slate-400 mb-2">Rating (on-the-fly)</h2>
    <div class="rounded-lg border border-slate-800 divide-y divide-slate-800" id="billing"></div>
  </section>
</div>

<script>
const fmt = new Intl.NumberFormat('en', {maximumFractionDigits: 2});

function meter(label, used, total, pct, unit) {
  const tone = pct > 90 ? 'bg-rose-500' : pct > 70 ? 'bg-amber-500' : 'bg-emerald-500';
  return `<div class="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
    <div class="flex justify-between items-baseline">
      <span class="text-sm text-slate-300">${label}</span>
      <span class="text-xs text-slate-500">${pct}%</span>
    </div>
    <div class="mt-2 h-2 w-full rounded bg-slate-800 overflow-hidden">
      <div class="h-full ${tone} transition-all duration-500" style="width:${Math.min(pct,100)}%"></div>
    </div>
    <div class="mt-2 text-xs text-slate-400">${fmt.format(used)} / ${fmt.format(total)} ${unit}</div>
  </div>`;
}

function badge(text) {
  const map = {
    ACTIVE: 'bg-emerald-900/60 text-emerald-300', ONLINE: 'bg-emerald-900/60 text-emerald-300',
    available: 'bg-emerald-900/60 text-emerald-300',
    BUILD: 'bg-sky-900/60 text-sky-300', creating: 'bg-sky-900/60 text-sky-300',
    PENDING_CREATE: 'bg-sky-900/60 text-sky-300',
    SHUTOFF: 'bg-slate-800 text-slate-300', OFFLINE: 'bg-slate-800 text-slate-300',
    'in-use': 'bg-indigo-900/60 text-indigo-300',
    SHELVED_OFFLOADED: 'bg-violet-900/60 text-violet-300',
    ERROR: 'bg-rose-900/60 text-rose-300'
  };
  const tone = map[text] || 'bg-slate-800 text-slate-300';
  return `<span class="px-2 py-0.5 rounded text-xs ${tone}">${text}</span>`;
}

function counter(label, value) {
  return `<div class="rounded-lg border border-slate-800 bg-slate-900/50 px-3 py-2">
    <div class="text-lg font-semibold text-white">${value}</div>
    <div class="text-xs text-slate-400">${label}</div></div>`;
}

async function refresh() {
  let data;
  try { data = await (await fetch('/api/stats')).json(); }
  catch (e) { document.getElementById('updated').textContent = 'unreachable'; return; }

  const h = data.host;
  document.getElementById('host-line').textContent =
    `${h.host} · ${h.vcpus_total} threads · ${fmt.format(h.ram_total_mb/1024)} GB RAM · ` +
    `${fmt.format(h.disk_total_gb/1024)} TB disk · overcommit ${data.config.cpu_allocation_ratio}x`;
  document.getElementById('updated').textContent = data.generated_at;

  document.getElementById('meters').innerHTML =
    meter('vCPU', h.vcpus_used, h.vcpus_allocatable, h.vcpus_pct, 'vCPU (overcommitted)') +
    meter('RAM', h.ram_used_mb/1024, h.ram_allocatable_mb/1024, h.ram_pct, 'GB (+256MB/VM)') +
    meter('Disk', h.disk_used_gb, h.disk_allocatable_gb, h.disk_pct, 'GB (instances + volumes)') +
    meter('Conntrack', h.conntrack_used, h.conntrack_max, h.conntrack_pct, 'entries');

  const c = data.counts;
  document.getElementById('counts').innerHTML =
    counter('instances', h.total_instances) + counter('running', h.running_vms) +
    counter('networks', c.networks) + counter('images', c.images) +
    counter('sec groups', c.security_groups) + counter('floating IPs', c.floating_ips) +
    counter('objects', c.objects);

  const wrap = document.getElementById('scenario-wrap');
  wrap.classList.toggle('hidden', data.scenarios.length === 0);
  document.getElementById('scenarios').innerHTML = data.scenarios.map(s =>
    `<div class="px-3 py-2 text-sm flex justify-between">
       <span class="text-amber-200">${s.service} → ${s.action}</span>
       <span class="text-amber-400/70 text-xs">${s.hits} hits · ${s.remaining_seconds}s left</span>
     </div>`).join('');

  document.getElementById('servers').innerHTML = data.servers.map(s =>
    `<tr class="hover:bg-slate-900/60">
      <td class="px-3 py-2 text-slate-200">${s.name}</td>
      <td class="px-3 py-2">${badge(s.status)}${s.task_state ? `<span class="ml-1 text-xs text-slate-500">${s.task_state}</span>` : ''}</td>
      <td class="px-3 py-2 text-slate-400">${s.flavor}</td>
      <td class="px-3 py-2 text-right text-slate-400">${s.vcpus}</td>
      <td class="px-3 py-2 text-right text-slate-400">${fmt.format(s.ram_mb)}M</td>
      <td class="px-3 py-2 text-slate-400 font-mono text-xs">${s.ip || '—'}</td>
    </tr>`).join('') ||
    '<tr><td colspan="6" class="px-3 py-6 text-center text-slate-600">no instances</td></tr>';

  document.getElementById('volumes').innerHTML = data.volumes.map(v =>
    `<tr class="hover:bg-slate-900/60">
      <td class="px-3 py-2 text-slate-200">${v.name || v.id.slice(0,8)}</td>
      <td class="px-3 py-2">${badge(v.status)}</td>
      <td class="px-3 py-2 text-right text-slate-400">${v.size}</td>
      <td class="px-3 py-2 text-slate-400">${v.type}</td>
    </tr>`).join('') ||
    '<tr><td colspan="4" class="px-3 py-6 text-center text-slate-600">no volumes</td></tr>';

  document.getElementById('lbs').innerHTML = data.loadbalancers.map(l =>
    `<tr class="hover:bg-slate-900/60">
      <td class="px-3 py-2 text-slate-200">${l.name || l.id.slice(0,8)}</td>
      <td class="px-3 py-2">${badge(l.provisioning_status)}</td>
      <td class="px-3 py-2">${badge(l.operating_status)}</td>
      <td class="px-3 py-2 text-slate-400 font-mono text-xs">${l.vip || '—'}</td>
    </tr>`).join('') ||
    '<tr><td colspan="4" class="px-3 py-6 text-center text-slate-600">no load balancers</td></tr>';

  document.getElementById('billing').innerHTML =
    data.billing.lines.map(l =>
      `<div class="px-3 py-2 flex justify-between text-sm">
        <span class="text-slate-300">${l.res_type}</span>
        <span class="text-slate-400">${fmt.format(l.qty)} units · <span class="text-emerald-300">$${l.rate.toFixed(4)}</span></span>
      </div>`).join('') +
    `<div class="px-3 py-2 flex justify-between text-sm bg-slate-900/60">
       <span class="text-white font-medium">total</span>
       <span class="text-emerald-300 font-medium">$${data.billing.total.toFixed(4)}</span></div>`;
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(PAGE)


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "time": iso(now_utc())}
