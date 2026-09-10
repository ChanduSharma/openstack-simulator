"""Octavia Load Balancer v2 API tests."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio

LBAAS = "/v2/lbaas"


async def _subnet_id(api) -> str:
    networks = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"]
    return networks[0]["subnets"][0]


async def _lb(api, name="lb1") -> dict:
    subnet = await _subnet_id(api)
    response = await api["octavia"].post(f"{LBAAS}/loadbalancers", json={
        "loadbalancer": {"name": name, "vip_subnet_id": subnet}})
    assert response.status_code == 201, response.text
    return response.json()["loadbalancer"]


async def _listener(api, lb_id, port=80) -> dict:
    response = await api["octavia"].post(f"{LBAAS}/listeners", json={
        "listener": {"loadbalancer_id": lb_id, "protocol": "HTTP", "protocol_port": port}})
    assert response.status_code == 201, response.text
    return response.json()["listener"]


async def _pool(api, listener_id) -> dict:
    response = await api["octavia"].post(f"{LBAAS}/pools", json={
        "pool": {"listener_id": listener_id, "protocol": "HTTP",
                 "lb_algorithm": "ROUND_ROBIN"}})
    assert response.status_code == 201, response.text
    return response.json()["pool"]


async def test_version_document(raw_clients) -> None:
    versions = (await raw_clients["octavia"].get("/")).json()["versions"]
    assert {v["id"] for v in versions} == {"v2.0", "v2.27"}


async def test_loadbalancer_creation_allocates_a_vip(api, cloud) -> None:
    lb = await _lb(api)
    # Creation always answers PENDING_CREATE; the flip happens on the next read.
    assert lb["provisioning_status"] == "PENDING_CREATE"
    assert lb["operating_status"] == "OFFLINE"
    settled = (await api["octavia"].get(f"{LBAAS}/loadbalancers/{lb['id']}")).json()["loadbalancer"]
    assert settled["provisioning_status"] == "ACTIVE"
    assert settled["operating_status"] == "ONLINE"
    assert lb["vip_address"].startswith("10.0.0.")
    assert lb["vip_port_id"]
    assert lb["provider"] == "amphora"
    assert lb["project_id"] == cloud.project_id

    ports = (await api["neutron"].get(f"/v2.0/ports/{lb['vip_port_id']}")).json()["port"]
    assert ports["device_owner"] == "Octavia"


async def test_loadbalancer_without_a_subnet(api) -> None:
    response = await api["octavia"].post(f"{LBAAS}/loadbalancers",
                                         json={"loadbalancer": {"name": "bare"}})
    assert response.status_code == 201
    assert response.json()["loadbalancer"]["vip_address"] == ""


async def test_loadbalancer_with_an_unknown_subnet_is_a_404(api) -> None:
    response = await api["octavia"].post(f"{LBAAS}/loadbalancers",
                                         json={"loadbalancer": {"vip_subnet_id": "ghost"}})
    assert response.status_code == 404
    assert response.json()["faultcode"] == "Client"


async def test_both_lbaas_path_generations_work(api) -> None:
    lb = await _lb(api)
    old = await api["octavia"].get(f"/v2.0/lbaas/loadbalancers/{lb['id']}")
    new = await api["octavia"].get(f"/v2/lbaas/loadbalancers/{lb['id']}")
    assert old.status_code == new.status_code == 200
    assert old.json() == new.json()


async def test_loadbalancer_crud(api) -> None:
    lb = await _lb(api, name="crud")
    listed = (await api["octavia"].get(f"{LBAAS}/loadbalancers")).json()["loadbalancers"]
    assert len(listed) == 1
    named = (await api["octavia"].get(f"{LBAAS}/loadbalancers?name=crud")).json()
    assert len(named["loadbalancers"]) == 1

    updated = await api["octavia"].put(f"{LBAAS}/loadbalancers/{lb['id']}",
                                       json={"loadbalancer": {"name": "renamed"}})
    assert updated.json()["loadbalancer"]["name"] == "renamed"

    assert (await api["octavia"].delete(f"{LBAAS}/loadbalancers/{lb['id']}")).status_code == 204
    assert (await api["octavia"].get(f"{LBAAS}/loadbalancers/{lb['id']}")).status_code == 404


async def test_delete_with_children_requires_cascade(api) -> None:
    lb = await _lb(api)
    await _listener(api, lb["id"])
    refused = await api["octavia"].delete(f"{LBAAS}/loadbalancers/{lb['id']}")
    assert refused.status_code == 409
    cascaded = await api["octavia"].delete(f"{LBAAS}/loadbalancers/{lb['id']}?cascade=true")
    assert cascaded.status_code == 204
    assert (await api["octavia"].get(f"{LBAAS}/listeners")).json()["listeners"] == []


async def test_listener_crud(api) -> None:
    lb = await _lb(api)
    listener = await _listener(api, lb["id"], port=8080)
    assert listener["protocol_port"] == 8080
    assert listener["loadbalancers"] == [{"id": lb["id"]}]
    assert listener["timeout_client_data"] == 50000

    assert (await api["octavia"].get(f"{LBAAS}/listeners/{listener['id']}")).status_code == 200
    assert len((await api["octavia"].get(f"{LBAAS}/listeners")).json()["listeners"]) == 1

    updated = await api["octavia"].put(f"{LBAAS}/listeners/{listener['id']}",
                                       json={"listener": {"name": "front",
                                                          "connection_limit": 200}})
    assert updated.json()["listener"]["connection_limit"] == 200

    assert (await api["octavia"].delete(f"{LBAAS}/listeners/{listener['id']}")).status_code == 204
    assert (await api["octavia"].get(f"{LBAAS}/listeners/{listener['id']}")).status_code == 404


async def test_listener_needs_a_real_loadbalancer(api) -> None:
    response = await api["octavia"].post(f"{LBAAS}/listeners", json={
        "listener": {"loadbalancer_id": "ghost", "protocol": "HTTP", "protocol_port": 80}})
    assert response.status_code == 404


async def test_pool_inherits_the_loadbalancer_from_its_listener(api) -> None:
    lb = await _lb(api)
    listener = await _listener(api, lb["id"])
    pool = await _pool(api, listener["id"])
    assert pool["loadbalancers"] == [{"id": lb["id"]}]
    assert pool["lb_algorithm"] == "ROUND_ROBIN"

    refreshed = (await api["octavia"].get(f"{LBAAS}/listeners/{listener['id']}")).json()["listener"]
    assert refreshed["default_pool_id"] == pool["id"]


async def test_pool_requires_a_parent(api) -> None:
    response = await api["octavia"].post(f"{LBAAS}/pools",
                                         json={"pool": {"protocol": "HTTP"}})
    assert response.status_code == 400


async def test_pool_crud(api) -> None:
    lb = await _lb(api)
    pool = (await api["octavia"].post(f"{LBAAS}/pools", json={
        "pool": {"loadbalancer_id": lb["id"], "protocol": "TCP",
                 "lb_algorithm": "LEAST_CONNECTIONS"}})).json()["pool"]
    assert pool["protocol"] == "TCP"
    assert (await api["octavia"].get(f"{LBAAS}/pools/{pool['id']}")).status_code == 200
    assert len((await api["octavia"].get(f"{LBAAS}/pools")).json()["pools"]) == 1
    updated = await api["octavia"].put(f"{LBAAS}/pools/{pool['id']}",
                                       json={"pool": {"lb_algorithm": "SOURCE_IP"}})
    assert updated.json()["pool"]["lb_algorithm"] == "SOURCE_IP"
    assert (await api["octavia"].delete(f"{LBAAS}/pools/{pool['id']}")).status_code == 204


async def test_member_crud(api) -> None:
    lb = await _lb(api)
    listener = await _listener(api, lb["id"])
    pool = await _pool(api, listener["id"])

    created = await api["octavia"].post(f"{LBAAS}/pools/{pool['id']}/members", json={
        "member": {"address": "10.0.0.51", "protocol_port": 80, "weight": 3}})
    assert created.status_code == 201
    member = created.json()["member"]
    assert member["address"] == "10.0.0.51" and member["weight"] == 3

    listed = (await api["octavia"].get(f"{LBAAS}/pools/{pool['id']}/members")).json()["members"]
    assert len(listed) == 1
    assert (await api["octavia"].get(
        f"{LBAAS}/pools/{pool['id']}/members/{member['id']}")).status_code == 200

    updated = await api["octavia"].put(f"{LBAAS}/pools/{pool['id']}/members/{member['id']}",
                                       json={"member": {"weight": 10}})
    assert updated.json()["member"]["weight"] == 10

    assert (await api["octavia"].delete(
        f"{LBAAS}/pools/{pool['id']}/members/{member['id']}")).status_code == 204
    assert (await api["octavia"].get(
        f"{LBAAS}/pools/{pool['id']}/members/{member['id']}")).status_code == 404


async def test_member_on_an_unknown_pool_is_a_404(api) -> None:
    response = await api["octavia"].post(f"{LBAAS}/pools/ghost/members", json={
        "member": {"address": "10.0.0.1", "protocol_port": 80}})
    assert response.status_code == 404


async def test_health_monitor_crud(api) -> None:
    lb = await _lb(api)
    listener = await _listener(api, lb["id"])
    pool = await _pool(api, listener["id"])

    created = await api["octavia"].post(f"{LBAAS}/healthmonitors", json={
        "healthmonitor": {"pool_id": pool["id"], "type": "HTTP", "delay": 10,
                          "timeout": 5, "max_retries": 4, "url_path": "/healthz"}})
    assert created.status_code == 201
    monitor = created.json()["healthmonitor"]
    assert monitor["url_path"] == "/healthz" and monitor["delay"] == 10

    refreshed = (await api["octavia"].get(f"{LBAAS}/pools/{pool['id']}")).json()["pool"]
    assert refreshed["healthmonitor_id"] == monitor["id"]

    assert len((await api["octavia"].get(f"{LBAAS}/healthmonitors")).json()["healthmonitors"]) == 1
    assert (await api["octavia"].delete(
        f"{LBAAS}/healthmonitors/{monitor['id']}")).status_code == 204
    cleared = (await api["octavia"].get(f"{LBAAS}/pools/{pool['id']}")).json()["pool"]
    assert cleared["healthmonitor_id"] is None


async def test_status_tree(api) -> None:
    lb = await _lb(api, name="tree")
    listener = await _listener(api, lb["id"])
    pool = await _pool(api, listener["id"])
    await api["octavia"].post(f"{LBAAS}/pools/{pool['id']}/members",
                              json={"member": {"address": "10.0.0.60", "protocol_port": 8080}})
    await api["octavia"].post(f"{LBAAS}/healthmonitors",
                              json={"healthmonitor": {"pool_id": pool["id"], "type": "HTTP"}})

    statuses = (await api["octavia"].get(
        f"{LBAAS}/loadbalancers/{lb['id']}/status")).json()["statuses"]["loadbalancer"]
    assert statuses["name"] == "tree"
    assert statuses["operating_status"] == "ONLINE"
    tree_listener = statuses["listeners"][0]
    tree_pool = tree_listener["pools"][0]
    assert tree_pool["members"][0]["address"] == "10.0.0.60"
    assert tree_pool["health_monitor"]["type"] == "HTTP"


async def test_providers_and_flavors(api) -> None:
    providers = (await api["octavia"].get(f"{LBAAS}/providers")).json()["providers"]
    assert {p["name"] for p in providers} == {"amphora", "octavia"}
    assert (await api["octavia"].get(f"{LBAAS}/flavors")).json()["flavors"] == []
