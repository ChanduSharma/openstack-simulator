"""Placement API tests -- must always agree with Nova."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def _boot(api, flavor="m1.medium") -> str:
    flavors = {f["name"]: f["id"]
               for f in (await api["nova"].get("/v2.1/flavors/detail")).json()["flavors"]}
    images = {i["name"]: i["id"]
              for i in (await api["glance"].get("/v2/images")).json()["images"]}
    response = await api["nova"].post("/v2.1/servers", json={"server": {
        "name": "rp-vm", "flavorRef": flavors[flavor], "imageRef": images["cirros"],
        "networks": "none"}})
    return response.json()["server"]["id"]


async def test_version_document(raw_clients) -> None:
    body = (await raw_clients["placement"].get("/")).json()["versions"][0]
    assert body["max_version"] == "1.36" and body["min_version"] == "1.0"


async def test_microversion_header(api) -> None:
    headers = (await api["placement"].get("/resource_providers")).headers
    assert headers["openstack-api-version"] == "placement 1.36"


async def test_the_node_is_the_only_resource_provider(api, cloud) -> None:
    body = (await api["placement"].get("/resource_providers")).json()
    assert len(body["resource_providers"]) == 1
    provider = body["resource_providers"][0]
    assert provider["uuid"] == cloud.host_id
    assert provider["name"] == "node-01"
    assert provider["root_provider_uuid"] == provider["uuid"]
    assert provider["parent_provider_uuid"] is None
    assert {link["rel"] for link in provider["links"]} >= {"inventories", "usages", "allocations"}


async def test_provider_name_filter(api) -> None:
    assert len((await api["placement"].get("/resource_providers?name=node-01")).json()
               ["resource_providers"]) == 1
    assert (await api["placement"].get("/resource_providers?name=other")).json()[
        "resource_providers"] == []


async def test_provider_lookup_and_404(api, cloud) -> None:
    assert (await api["placement"].get(f"/resource_providers/{cloud.host_id}")).status_code == 200
    missing = await api["placement"].get("/resource_providers/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    # Placement has its own error envelope.
    assert missing.json()["errors"][0]["status"] == 404
    assert missing.json()["errors"][0]["title"] == "Not Found"


async def test_inventories_expose_the_three_resource_classes(api, cloud) -> None:
    body = (await api["placement"].get(f"/resource_providers/{cloud.host_id}/inventories")).json()
    assert set(body["inventories"]) == {"VCPU", "MEMORY_MB", "DISK_GB"}
    assert body["inventories"]["VCPU"]["total"] == 64
    assert body["inventories"]["VCPU"]["allocation_ratio"] == 3.0
    assert body["inventories"]["MEMORY_MB"]["total"] == 262144
    assert body["inventories"]["MEMORY_MB"]["reserved"] == 512
    assert body["inventories"]["DISK_GB"]["total"] == 4096
    assert "resource_provider_generation" in body


async def test_single_inventory_lookup(api, cloud) -> None:
    body = (await api["placement"].get(
        f"/resource_providers/{cloud.host_id}/inventories/VCPU")).json()
    assert body["total"] == 64 and "resource_provider_generation" in body
    missing = await api["placement"].get(
        f"/resource_providers/{cloud.host_id}/inventories/PGPU")
    assert missing.status_code == 404


async def test_usages_track_nova(api, cloud) -> None:
    await _boot(api)
    placement = (await api["placement"].get(
        f"/resource_providers/{cloud.host_id}/usages")).json()["usages"]
    nova = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert placement["VCPU"] == nova["vcpus_used"] == 2
    assert placement["MEMORY_MB"] == nova["memory_mb_used"] == 4352
    assert placement["DISK_GB"] == 40


async def test_allocations_for_a_consumer(api, cloud) -> None:
    server_id = await _boot(api)
    body = (await api["placement"].get(f"/allocations/{server_id}")).json()
    assert body["project_id"] == cloud.project_id
    resources = body["allocations"][cloud.host_id]["resources"]
    assert resources == {"VCPU": 2, "MEMORY_MB": 4352, "DISK_GB": 40}


async def test_allocations_follow_shelve_offload(api, cloud) -> None:
    server_id = await _boot(api)
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"shelveOffload": None})
    body = (await api["placement"].get(f"/allocations/{server_id}")).json()
    assert body["allocations"][cloud.host_id]["resources"] == {"DISK_GB": 40}


async def test_allocations_vanish_when_the_instance_is_deleted(api) -> None:
    server_id = await _boot(api)
    await api["nova"].delete(f"/v2.1/servers/{server_id}")
    body = (await api["placement"].get(f"/allocations/{server_id}")).json()
    assert body["allocations"] == {}


async def test_unknown_consumer_has_no_allocations(api) -> None:
    body = (await api["placement"].get("/allocations/does-not-exist")).json()
    assert body["allocations"] == {}


async def test_provider_allocations_list_every_consumer(api, cloud) -> None:
    first = await _boot(api)
    second = await _boot(api, flavor="m1.tiny")
    body = (await api["placement"].get(
        f"/resource_providers/{cloud.host_id}/allocations")).json()
    assert set(body["allocations"]) == {first, second}
    assert body["allocations"][second]["resources"]["VCPU"] == 1


async def test_put_allocations_is_accepted_for_known_consumers(api) -> None:
    server_id = await _boot(api)
    payload = {"allocations": {}, "consumer_generation": 1, "project_id": "p", "user_id": "u"}
    assert (await api["placement"].put(f"/allocations/{server_id}",
                                       json=payload)).status_code == 204
    unknown = await api["placement"].put("/allocations/nope", json=payload)
    assert unknown.status_code == 409
    assert (await api["placement"].delete(f"/allocations/{server_id}")).status_code == 204


async def test_project_scoped_usages(api, cloud) -> None:
    await _boot(api)
    body = (await api["placement"].get(f"/usages?project_id={cloud.project_id}")).json()
    assert body["usages"]["VCPU"] == 2
    empty = (await api["placement"].get("/usages?project_id=other")).json()
    assert empty["usages"]["VCPU"] == 0


async def test_traits_aggregates_and_resource_classes(api, cloud) -> None:
    traits = (await api["placement"].get("/traits")).json()["traits"]
    assert "HW_CPU_X86_AVX2" in traits
    provider_traits = (await api["placement"].get(
        f"/resource_providers/{cloud.host_id}/traits")).json()
    assert provider_traits["traits"] == traits
    assert (await api["placement"].get(
        f"/resource_providers/{cloud.host_id}/aggregates")).json()["aggregates"] == []
    classes = (await api["placement"].get("/resource_classes")).json()["resource_classes"]
    assert {c["name"] for c in classes} >= {"VCPU", "MEMORY_MB", "DISK_GB"}


async def test_allocation_candidates_when_the_request_fits(api, cloud) -> None:
    body = (await api["placement"].get(
        "/allocation_candidates?resources=VCPU:2,MEMORY_MB:4096,DISK_GB:40")).json()
    assert len(body["allocation_requests"]) == 1
    assert body["allocation_requests"][0]["allocations"][cloud.host_id]["resources"]["VCPU"] == 2
    summary = body["provider_summaries"][cloud.host_id]["resources"]
    assert summary["VCPU"]["capacity"] == 192
    assert summary["MEMORY_MB"]["capacity"] == 261632


async def test_allocation_candidates_when_the_request_is_too_large(api) -> None:
    body = (await api["placement"].get(
        "/allocation_candidates?resources=MEMORY_MB:9999999")).json()
    assert body["allocation_requests"] == []
    assert body["provider_summaries"] == {}
