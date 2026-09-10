"""Status dashboard page and stats endpoint."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def test_page_renders(raw_clients) -> None:
    response = await raw_clients["dashboard"].get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "OpenStack-Simulator" in body
    assert "tailwindcss" in body, "styled via the Tailwind CDN"
    assert "/api/stats" in body, "the page polls the stats endpoint"


async def test_healthz(raw_clients) -> None:
    body = (await raw_clients["dashboard"].get("/healthz")).json()
    assert body["status"] == "ok" and body["time"]


async def test_stats_needs_no_token(raw_clients, cloud) -> None:
    """The dashboard is a local inspection tool, so its data endpoint is open."""
    assert (await raw_clients["dashboard"].get("/api/stats")).status_code == 200


async def test_stats_of_an_idle_cloud(api) -> None:
    body = (await api["dashboard"].get("/api/stats")).json()
    host = body["host"]
    assert host["host"] == "node-01"
    assert host["vcpus_total"] == 64 and host["vcpus_allocatable"] == 192.0
    assert host["ram_total_mb"] == 262144
    assert host["disk_total_gb"] == 4096
    assert host["conntrack_max"] == 65536
    assert host["vcpus_used"] == 0 and host["vcpus_pct"] == 0.0
    assert body["config"]["cpu_allocation_ratio"] == 3.0
    assert body["config"]["qemu_overhead_mb"] == 256
    assert set(body["config"]["ports"]) >= {"nova", "keystone", "dashboard"}
    assert body["counts"]["networks"] == 2
    assert body["counts"]["images"] == 2
    assert body["servers"] == [] and body["volumes"] == [] and body["loadbalancers"] == []


async def test_stats_track_live_resources(api) -> None:
    flavors = {f["name"]: f["id"]
               for f in (await api["nova"].get("/v2.1/flavors/detail")).json()["flavors"]}
    images = {i["name"]: i["id"]
              for i in (await api["glance"].get("/v2/images")).json()["images"]}
    await api["nova"].post("/v2.1/servers", json={"server": {
        "name": "dash-vm", "flavorRef": flavors["m1.medium"], "imageRef": images["cirros"]}})
    await api["cinder"].post("/v3/volumes", json={"volume": {"size": 50, "name": "dash-vol"}})
    networks = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"]
    await api["octavia"].post("/v2/lbaas/loadbalancers", json={
        "loadbalancer": {"name": "dash-lb", "vip_subnet_id": networks[0]["subnets"][0]}})

    body = (await api["dashboard"].get("/api/stats")).json()
    assert body["host"]["vcpus_used"] == 2
    assert body["host"]["ram_used_mb"] == 4352
    assert body["host"]["disk_used_gb"] == 90, "40 GB root disk + 50 GB volume"
    assert body["host"]["ram_pct"] > 0

    server = body["servers"][0]
    assert server["name"] == "dash-vm" and server["flavor"] == "m1.medium"
    assert server["vcpus"] == 2 and server["ram_mb"] == 4352
    assert server["ip"].startswith("10.0.0.")

    assert body["volumes"][0]["name"] == "dash-vol" and body["volumes"][0]["size"] == 50
    assert body["loadbalancers"][0]["name"] == "dash-lb"


async def test_stats_include_billing(api) -> None:
    flavors = {f["name"]: f["id"]
               for f in (await api["nova"].get("/v2.1/flavors/detail")).json()["flavors"]}
    images = {i["name"]: i["id"]
              for i in (await api["glance"].get("/v2/images")).json()["images"]}
    await api["nova"].post("/v2.1/servers", json={"server": {
        "name": "billed", "flavorRef": flavors["m1.tiny"], "imageRef": images["cirros"],
        "networks": "none"}})
    body = (await api["dashboard"].get("/api/stats")).json()
    assert "total" in body["billing"] and isinstance(body["billing"]["lines"], list)
    assert body["billing"]["total"] >= 0


async def test_object_counts_are_reported(api, cloud) -> None:
    account = f"/v1/AUTH_{cloud.project_id}"
    await api["swift"].put(f"{account}/c")
    await api["swift"].put(f"{account}/c/o", content=b"1234567890")
    counts = (await api["dashboard"].get("/api/stats")).json()["counts"]
    assert counts["containers"] == 1
    assert counts["objects"] == 1
    assert counts["object_bytes"] == 10
