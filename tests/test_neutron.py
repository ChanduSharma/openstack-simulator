"""Neutron Networking v2.0 API tests, including IPAM and conntrack accounting."""
from __future__ import annotations

import pytest

from app.core.database import SessionLocal
from app.models.compute import Hypervisor
from sqlalchemy import select

pytestmark = pytest.mark.anyio


async def _network(api, name="testnet", **extra) -> dict:
    response = await api["neutron"].post("/v2.0/networks",
                                         json={"network": {"name": name, **extra}})
    assert response.status_code == 201, response.text
    return response.json()["network"]


async def _subnet(api, network_id, cidr="192.168.1.0/24", **extra) -> dict:
    response = await api["neutron"].post("/v2.0/subnets", json={
        "subnet": {"network_id": network_id, "cidr": cidr, **extra}})
    assert response.status_code == 201, response.text
    return response.json()["subnet"]


async def test_version_and_extensions(raw_clients, api) -> None:
    versions = (await raw_clients["neutron"].get("/")).json()["versions"]
    assert versions[0]["id"] == "v2.0"
    aliases = {e["alias"] for e in (await api["neutron"].get("/v2.0/extensions")).json()["extensions"]}
    assert {"security-group", "external-net", "port-security"} <= aliases


async def test_seeded_networks(api, cloud) -> None:
    networks = {n["name"]: n for n in (await api["neutron"].get("/v2.0/networks")).json()["networks"]}
    assert set(networks) == {"private", "public"}
    assert networks["public"]["router:external"] is True
    assert networks["private"]["shared"] is True
    assert networks["private"]["project_id"] == cloud.project_id
    assert len(networks["private"]["subnets"]) == 1


async def test_network_crud(api) -> None:
    network = await _network(api, name="crud-net")
    assert network["status"] == "ACTIVE"
    assert network["provider:network_type"] == "vxlan"
    assert network["mtu"] == 1450

    fetched = (await api["neutron"].get(f"/v2.0/networks/{network['id']}")).json()["network"]
    assert fetched["name"] == "crud-net"

    updated = await api["neutron"].put(f"/v2.0/networks/{network['id']}",
                                       json={"network": {"name": "renamed", "mtu": 1400}})
    assert updated.json()["network"]["name"] == "renamed"
    assert updated.json()["network"]["revision_number"] == 2

    assert (await api["neutron"].delete(f"/v2.0/networks/{network['id']}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/networks/{network['id']}")).status_code == 404


async def test_network_filters(api) -> None:
    await _network(api, name="filtered")
    assert len((await api["neutron"].get("/v2.0/networks?name=filtered")).json()["networks"]) == 1
    external = (await api["neutron"].get("/v2.0/networks?router:external=true")).json()["networks"]
    assert [n["name"] for n in external] == ["public"]


async def test_network_in_use_cannot_be_deleted(api) -> None:
    network = await _network(api, name="busy")
    await _subnet(api, network["id"])
    await api["neutron"].post("/v2.0/ports", json={
        "port": {"network_id": network["id"], "device_id": "vm-1"}})
    response = await api["neutron"].delete(f"/v2.0/networks/{network['id']}")
    assert response.status_code == 409
    assert response.json()["NeutronError"]["type"] == "NetworkInUse"


async def test_subnet_cidr_math(api) -> None:
    network = await _network(api)
    subnet = await _subnet(api, network["id"], cidr="10.20.30.0/24")
    assert subnet["gateway_ip"] == "10.20.30.1"
    assert subnet["allocation_pools"] == [{"start": "10.20.30.2", "end": "10.20.30.254"}]
    assert subnet["ip_version"] == 4
    assert subnet["enable_dhcp"] is True


async def test_subnet_honours_an_explicit_pool_and_gateway(api) -> None:
    network = await _network(api)
    subnet = await _subnet(api, network["id"], cidr="10.9.0.0/24", gateway_ip="10.9.0.254",
                           allocation_pools=[{"start": "10.9.0.10", "end": "10.9.0.20"}])
    assert subnet["gateway_ip"] == "10.9.0.254"
    assert subnet["allocation_pools"] == [{"start": "10.9.0.10", "end": "10.9.0.20"}]


async def test_subnet_validation(api) -> None:
    network = await _network(api)
    assert (await api["neutron"].post("/v2.0/subnets", json={
        "subnet": {"network_id": network["id"]}})).status_code == 400
    assert (await api["neutron"].post("/v2.0/subnets", json={
        "subnet": {"network_id": network["id"], "cidr": "not-a-cidr"}})).status_code == 400
    assert (await api["neutron"].post("/v2.0/subnets", json={
        "subnet": {"network_id": "ghost", "cidr": "10.0.1.0/24"}})).status_code == 404


async def test_subnet_crud(api) -> None:
    network = await _network(api)
    subnet = await _subnet(api, network["id"], name="sub", dns_nameservers=["9.9.9.9"])
    assert subnet["dns_nameservers"] == ["9.9.9.9"]
    listed = (await api["neutron"].get(f"/v2.0/subnets?network_id={network['id']}")).json()
    assert len(listed["subnets"]) == 1
    updated = await api["neutron"].put(f"/v2.0/subnets/{subnet['id']}",
                                       json={"subnet": {"name": "renamed"}})
    assert updated.json()["subnet"]["name"] == "renamed"
    assert (await api["neutron"].delete(f"/v2.0/subnets/{subnet['id']}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/subnets/{subnet['id']}")).status_code == 404


async def test_ports_get_sequential_addresses_and_a_mac(api) -> None:
    network = await _network(api)
    await _subnet(api, network["id"], cidr="10.50.0.0/24")
    addresses = []
    for index in range(3):
        port = (await api["neutron"].post("/v2.0/ports", json={
            "port": {"network_id": network["id"], "name": f"p{index}"}})).json()["port"]
        addresses.append(port["fixed_ips"][0]["ip_address"])
        assert port["mac_address"].startswith("fa:16:3e:")
    assert addresses == ["10.50.0.2", "10.50.0.3", "10.50.0.4"]


async def test_port_with_an_explicit_address(api) -> None:
    network = await _network(api)
    await _subnet(api, network["id"], cidr="10.60.0.0/24")
    port = (await api["neutron"].post("/v2.0/ports", json={
        "port": {"network_id": network["id"],
                 "fixed_ips": [{"ip_address": "10.60.0.99"}]}})).json()["port"]
    assert port["fixed_ips"][0]["ip_address"] == "10.60.0.99"


async def test_port_exhaustion_is_reported(api) -> None:
    network = await _network(api)
    # A /30 holds .1 and .2; .1 is the gateway, so exactly one address is allocatable.
    subnet = await _subnet(api, network["id"], cidr="10.70.0.0/30")
    assert subnet["allocation_pools"] == [{"start": "10.70.0.2", "end": "10.70.0.2"}]
    first = await api["neutron"].post("/v2.0/ports", json={"port": {"network_id": network["id"]}})
    assert first.status_code == 201
    assert first.json()["port"]["fixed_ips"][0]["ip_address"] == "10.70.0.2"
    second = await api["neutron"].post("/v2.0/ports", json={"port": {"network_id": network["id"]}})
    assert second.status_code == 409
    assert second.json()["NeutronError"]["type"] == "IpAddressGenerationFailure"


async def test_port_crud_and_filters(api) -> None:
    network = await _network(api)
    await _subnet(api, network["id"], cidr="10.80.0.0/24")
    port = (await api["neutron"].post("/v2.0/ports", json={
        "port": {"network_id": network["id"], "name": "p1", "device_id": "vm-9"}})).json()["port"]
    assert port["status"] == "ACTIVE", "a bound port comes up ACTIVE"

    by_device = (await api["neutron"].get("/v2.0/ports?device_id=vm-9")).json()["ports"]
    assert len(by_device) == 1
    by_mac = (await api["neutron"].get(f"/v2.0/ports?mac_address={port['mac_address']}")).json()
    assert len(by_mac["ports"]) == 1

    updated = await api["neutron"].put(f"/v2.0/ports/{port['id']}",
                                       json={"port": {"name": "renamed", "device_id": ""}})
    assert updated.json()["port"]["name"] == "renamed"
    assert (await api["neutron"].delete(f"/v2.0/ports/{port['id']}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/ports/{port['id']}")).status_code == 404


async def test_unbound_port_is_down(api) -> None:
    network = await _network(api)
    await _subnet(api, network["id"], cidr="10.85.0.0/24")
    port = (await api["neutron"].post("/v2.0/ports",
                                      json={"port": {"network_id": network["id"]}})).json()["port"]
    assert port["status"] == "DOWN"


async def test_security_group_creation_costs_two_conntrack_slots(api) -> None:
    before = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    created = await api["neutron"].post("/v2.0/security-groups",
                                        json={"security_group": {"name": "web"}})
    assert created.status_code == 201
    rules = created.json()["security_group"]["security_group_rules"]
    assert len(rules) == 2
    assert {r["direction"] for r in rules} == {"egress"}
    assert {r["ethertype"] for r in rules} == {"IPv4", "IPv6"}

    after = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    assert after - before == 2


async def test_each_rule_costs_one_more_slot(api) -> None:
    group = (await api["neutron"].post("/v2.0/security-groups",
                                       json={"security_group": {"name": "web"}})).json()["security_group"]
    before = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    created = await api["neutron"].post("/v2.0/security-group-rules", json={
        "security_group_rule": {"security_group_id": group["id"], "direction": "ingress",
                                "protocol": "tcp", "port_range_min": 22, "port_range_max": 22,
                                "remote_ip_prefix": "0.0.0.0/0"}})
    assert created.status_code == 201
    rule = created.json()["security_group_rule"]
    assert rule["port_range_min"] == 22 and rule["protocol"] == "tcp"
    after = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    assert after - before == 1


async def test_deleting_a_group_returns_its_slots(api) -> None:
    before = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    group = (await api["neutron"].post("/v2.0/security-groups",
                                       json={"security_group": {"name": "temp"}})).json()["security_group"]
    await api["neutron"].post("/v2.0/security-group-rules", json={
        "security_group_rule": {"security_group_id": group["id"]}})
    assert (await api["neutron"].delete(f"/v2.0/security-groups/{group['id']}")).status_code == 204
    after = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    assert after == before, "rules cascade with the group"


async def test_conntrack_exhaustion_blocks_new_groups(api) -> None:
    async with SessionLocal() as session:
        host = (await session.execute(select(Hypervisor))).scalar_one()
        host.conntrack_max = 5          # the seeded default group already uses 4
        await session.commit()
    response = await api["neutron"].post("/v2.0/security-groups",
                                         json={"security_group": {"name": "overflow"}})
    assert response.status_code == 409
    assert response.json()["NeutronError"]["type"] == "SecurityGroupLimitExceeded"


async def test_conntrack_exhaustion_blocks_new_rules(api) -> None:
    group = (await api["neutron"].get("/v2.0/security-groups")).json()["security_groups"][0]
    async with SessionLocal() as session:
        host = (await session.execute(select(Hypervisor))).scalar_one()
        host.conntrack_max = 4
        await session.commit()
    response = await api["neutron"].post("/v2.0/security-group-rules", json={
        "security_group_rule": {"security_group_id": group["id"]}})
    assert response.status_code == 409


async def test_security_group_crud(api) -> None:
    group = (await api["neutron"].post("/v2.0/security-groups",
                                       json={"security_group": {"name": "app"}})).json()["security_group"]
    assert (await api["neutron"].get(f"/v2.0/security-groups/{group['id']}")).status_code == 200
    named = (await api["neutron"].get("/v2.0/security-groups?name=app")).json()["security_groups"]
    assert len(named) == 1
    updated = await api["neutron"].put(f"/v2.0/security-groups/{group['id']}",
                                       json={"security_group": {"description": "the app"}})
    assert updated.json()["security_group"]["description"] == "the app"
    assert (await api["neutron"].get("/v2.0/security-groups/ghost")).status_code == 404


async def test_security_group_rule_lookup_and_delete(api) -> None:
    group = (await api["neutron"].get("/v2.0/security-groups")).json()["security_groups"][0]
    rules = (await api["neutron"].get(
        f"/v2.0/security-group-rules?security_group_id={group['id']}")).json()["security_group_rules"]
    assert len(rules) == 4, "the seeded default group has 2 egress + 2 ingress rules"
    rule_id = rules[0]["id"]
    assert (await api["neutron"].get(f"/v2.0/security-group-rules/{rule_id}")).status_code == 200
    assert (await api["neutron"].delete(f"/v2.0/security-group-rules/{rule_id}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/security-group-rules/{rule_id}")).status_code == 404


async def test_rule_for_an_unknown_group_is_a_404(api) -> None:
    response = await api["neutron"].post("/v2.0/security-group-rules", json={
        "security_group_rule": {"security_group_id": "ghost"}})
    assert response.status_code == 404


async def test_floating_ip_allocation_and_association(api) -> None:
    public = (await api["neutron"].get("/v2.0/networks?name=public")).json()["networks"][0]
    private = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"][0]
    port = (await api["neutron"].post("/v2.0/ports",
                                      json={"port": {"network_id": private["id"]}})).json()["port"]

    created = await api["neutron"].post("/v2.0/floatingips",
                                        json={"floatingip": {"floating_network_id": public["id"]}})
    assert created.status_code == 201
    fip = created.json()["floatingip"]
    assert fip["floating_ip_address"].startswith("172.24.4.")
    assert fip["status"] == "DOWN" and fip["port_id"] is None

    associated = await api["neutron"].put(f"/v2.0/floatingips/{fip['id']}",
                                          json={"floatingip": {"port_id": port["id"]}})
    body = associated.json()["floatingip"]
    assert body["status"] == "ACTIVE"
    assert body["fixed_ip_address"] == port["fixed_ips"][0]["ip_address"]

    disassociated = await api["neutron"].put(f"/v2.0/floatingips/{fip['id']}",
                                             json={"floatingip": {"port_id": None}})
    assert disassociated.json()["floatingip"]["status"] == "DOWN"
    assert disassociated.json()["floatingip"]["fixed_ip_address"] is None


async def test_floating_ip_can_be_created_already_associated(api) -> None:
    public = (await api["neutron"].get("/v2.0/networks?name=public")).json()["networks"][0]
    private = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"][0]
    port = (await api["neutron"].post("/v2.0/ports",
                                      json={"port": {"network_id": private["id"]}})).json()["port"]
    fip = (await api["neutron"].post("/v2.0/floatingips", json={
        "floatingip": {"floating_network_id": public["id"], "port_id": port["id"]}})).json()["floatingip"]
    assert fip["status"] == "ACTIVE"


async def test_floating_ips_are_unique_and_listable(api) -> None:
    public = (await api["neutron"].get("/v2.0/networks?name=public")).json()["networks"][0]
    addresses = set()
    for _ in range(3):
        fip = (await api["neutron"].post("/v2.0/floatingips", json={
            "floatingip": {"floating_network_id": public["id"]}})).json()["floatingip"]
        addresses.add(fip["floating_ip_address"])
    assert len(addresses) == 3
    listed = (await api["neutron"].get("/v2.0/floatingips")).json()["floatingips"]
    assert len(listed) == 3


async def test_floating_ip_release_hides_it(api) -> None:
    public = (await api["neutron"].get("/v2.0/networks?name=public")).json()["networks"][0]
    fip = (await api["neutron"].post("/v2.0/floatingips", json={
        "floatingip": {"floating_network_id": public["id"]}})).json()["floatingip"]
    assert (await api["neutron"].delete(f"/v2.0/floatingips/{fip['id']}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/floatingips/{fip['id']}")).status_code == 404
    assert (await api["neutron"].get("/v2.0/floatingips")).json()["floatingips"] == []


async def test_floating_ip_on_a_non_external_network_still_needs_a_subnet(api) -> None:
    empty = await _network(api, name="no-subnets")
    response = await api["neutron"].post("/v2.0/floatingips",
                                         json={"floatingip": {"floating_network_id": empty["id"]}})
    assert response.status_code == 400
    response = await api["neutron"].post("/v2.0/floatingips",
                                         json={"floatingip": {"floating_network_id": "ghost"}})
    assert response.status_code == 404


async def test_quotas_and_availability_zones(api, cloud) -> None:
    quota = (await api["neutron"].get(f"/v2.0/quotas/{cloud.project_id}")).json()["quota"]
    assert quota["security_group_rule"] == 65536
    zones = (await api["neutron"].get("/v2.0/availability_zones")).json()["availability_zones"]
    assert {z["resource"] for z in zones} == {"network", "router"}


async def test_missing_body_object_is_rejected(api) -> None:
    assert (await api["neutron"].post("/v2.0/networks", json={"nope": {}})).status_code == 400
