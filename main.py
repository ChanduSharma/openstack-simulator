"""OpenStack-Sim entry point.

Every simulated service gets its own native OpenStack port, but they all share one
process and one asyncio event loop -- so the whole cloud costs a single Python
interpreter (~80 MB RSS) instead of eleven.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

import uvicorn
from starlette.types import ASGIApp

from app.api import (
    cinder,
    cloudkitty,
    dashboard,
    glance,
    keystone,
    neutron,
    nova,
    octavia,
    placement,
    scenarios,
    swift,
)
from app.core.config import PORTS, settings
from app.core.database import dispose_db, init_db
from app.core.middleware import create_service_app


def build_apps() -> dict[str, ASGIApp]:
    """One ASGI app per service, keyed by the service name used everywhere else."""
    apps: dict[str, ASGIApp] = {}

    keystone_app = create_service_app("keystone", "Keystone Identity v3 (simulated)")
    keystone_app.include_router(keystone.router)
    apps["keystone"] = keystone_app

    nova_app = create_service_app("nova", "Nova Compute v2.1 (simulated)")
    nova_app.include_router(nova.router)
    apps["nova"] = nova_app

    cinder_app = create_service_app("cinder", "Cinder Block Storage v3 (simulated)")
    cinder_app.include_router(cinder.router)
    # Accept the tenant-scoped catalog URL (/v3/{project_id}/...) as well as /v3/...
    apps["cinder"] = cinder.ProjectPathMiddleware(cinder_app)

    glance_app = create_service_app("glance", "Glance Image v2 (simulated)")
    glance_app.include_router(glance.router)
    apps["glance"] = glance_app

    neutron_app = create_service_app("neutron", "Neutron Networking v2.0 (simulated)")
    neutron_app.include_router(neutron.router)
    apps["neutron"] = neutron_app

    placement_app = create_service_app("placement", "Placement API (simulated)")
    placement_app.include_router(placement.router)
    apps["placement"] = placement_app

    octavia_app = create_service_app("octavia", "Octavia Load Balancer v2 (simulated)")
    octavia_app.include_router(octavia.versions_router)
    # Both generations of the LBaaS path prefix point at the same handlers.
    octavia_app.include_router(octavia.router, prefix="/v2/lbaas")
    octavia_app.include_router(octavia.router, prefix="/v2.0/lbaas")
    apps["octavia"] = octavia_app

    swift_app = create_service_app("swift", "Swift Object Store v1 (simulated)")
    swift_app.include_router(swift.router)
    apps["swift"] = swift_app

    cloudkitty_app = create_service_app("cloudkitty", "CloudKitty Rating v1 (simulated)")
    cloudkitty_app.include_router(cloudkitty.router)
    apps["cloudkitty"] = cloudkitty_app

    scenarios_app = create_service_app("scenarios", "Failure injection control plane")
    scenarios_app.include_router(scenarios.router)
    apps["scenarios"] = scenarios_app

    dashboard_app = create_service_app("dashboard", "OpenStack-Sim status dashboard")
    dashboard_app.include_router(dashboard.router)
    apps["dashboard"] = dashboard_app

    return apps


class _Server(uvicorn.Server):
    """Signal handling is owned by the harness, not by each individual server."""

    def install_signal_handlers(self) -> None:  # pragma: no cover - trivial override
        pass


def _banner(host: str) -> str:
    lines = ["", "  OpenStack-Sim is up", ""]
    width = max(len(name) for name in PORTS)
    for name, port in PORTS.items():
        lines.append(f"    {name.ljust(width)}  http://{host}:{port}")
    lines += [
        "",
        f"    dashboard   http://{settings.advertise_host}:{PORTS['dashboard']}/",
        "    credentials  source openrc.sh   (or: openstack --os-cloud openstack-sim ...)",
        "",
    ]
    return "\n".join(lines)


async def serve(log_level: str = "info", access_log: bool = False) -> None:
    await init_db()
    apps = build_apps()

    servers = [
        _Server(
            uvicorn.Config(
                app,
                host=settings.bind_host,
                port=PORTS[name],
                log_level=log_level,
                access_log=access_log,
                # One loop, one process: keep per-server overhead minimal.
                timeout_keep_alive=15,
                server_header=False,
                date_header=True,
            )
        )
        for name, app in apps.items()
        if name in PORTS  # --service may have narrowed the set
    ]

    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown)

    print(_banner(settings.advertise_host), flush=True)
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        await dispose_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the OpenStack-Sim service suite.")
    parser.add_argument(
        "--log-level",
        default="warning",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level (default: warning, which keeps CPU use down)",
    )
    parser.add_argument(
        "--access-log", action="store_true", help="log every request (noisy)"
    )
    parser.add_argument(
        "--service",
        action="append",
        choices=sorted(PORTS),
        help="run only the named service(s) instead of all of them",
    )
    args = parser.parse_args(argv)

    if args.service:
        for name in list(PORTS):
            if name not in args.service:
                PORTS.pop(name)

    try:
        asyncio.run(serve(log_level=args.log_level, access_log=args.access_log))
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
