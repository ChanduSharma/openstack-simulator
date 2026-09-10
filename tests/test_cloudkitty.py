"""CloudKitty Rating v1 API tests."""
from __future__ import annotations

import pytest

from app.core.config import now_utc, settings
from app.core.database import SessionLocal
from app.models.compute import Server
from sqlalchemy import update
from datetime import timedelta

pytestmark = pytest.mark.anyio


async def _boot(api, flavor="m1.medium") -> str:
    flavors = {f["name"]: f["id"]
               for f in (await api["nova"].get("/v2.1/flavors/detail")).json()["flavors"]}
    images = {i["name"]: i["id"]
              for i in (await api["glance"].get("/v2/images")).json()["images"]}
    response = await api["nova"].post("/v2.1/servers", json={"server": {
        "name": "billed", "flavorRef": flavors[flavor], "imageRef": images["cirros"],
        "networks": "none"}})
    return response.json()["server"]["id"]


async def _age(server_id: str, hours: float) -> None:
    """Backdate the accrual clock so a measurable amount of time has 'passed'."""
    stamp = now_utc() - timedelta(hours=hours)
    async with SessionLocal() as session:
        await session.execute(
            update(Server).where(Server.id == server_id)
            .values(accounted_at=stamp, created_at=stamp, launched_at=stamp)
        )
        await session.commit()


async def test_index_and_version(raw_clients) -> None:
    assert (await raw_clients["cloudkitty"].get("/")).json()["versions"][0]["id"] == "v1"
    assert (await raw_clients["cloudkitty"].get("/v1")).json()["version"] == "1.0"


async def test_summary_of_an_empty_cloud(api) -> None:
    body = (await api["cloudkitty"].get("/v1/report/summary")).json()
    assert body["summary"] == []


async def test_instance_cost_accrues_from_stored_seconds(api, cloud) -> None:
    server_id = await _boot(api)
    await _age(server_id, hours=10)

    body = (await api["cloudkitty"].get("/v1/report/summary?groupby=res_type")).json()
    line = next(r for r in body["summary"] if r["res_type"] == "instance")
    assert line["qty"] == pytest.approx(10.0, rel=0.02)
    expected = 10 * (2 * settings.rate_vcpu_hour + 4 * settings.rate_ram_gb_hour)
    assert line["rate"] == pytest.approx(expected, rel=0.02)


async def test_stopped_instances_move_to_the_idle_line(api) -> None:
    server_id = await _boot(api)
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"os-stop": None})
    await _age(server_id, hours=8)

    kinds = {r["res_type"]: r
             for r in (await api["cloudkitty"].get(
                 "/v1/report/summary?groupby=res_type")).json()["summary"]}
    assert "instance.idle" in kinds
    idle = kinds["instance.idle"]
    full = 8 * (2 * settings.rate_vcpu_hour + 4 * settings.rate_ram_gb_hour)
    assert idle["rate"] == pytest.approx(full * settings.rate_idle_multiplier, rel=0.02)


async def test_volumes_and_floating_ips_appear_on_the_bill(api, cloud) -> None:
    await api["cinder"].post("/v3/volumes", json={"volume": {"size": 100}})
    public = (await api["neutron"].get("/v2.0/networks?name=public")).json()["networks"][0]
    await api["neutron"].post("/v2.0/floatingips",
                              json={"floatingip": {"floating_network_id": public["id"]}})

    kinds = {r["res_type"] for r in (await api["cloudkitty"].get(
        "/v1/report/summary?groupby=res_type")).json()["summary"]}
    assert {"volume.size", "network.floating.ip"} <= kinds


async def test_summary_can_group_by_tenant(api, cloud) -> None:
    server_id = await _boot(api)
    await _age(server_id, hours=1)
    body = (await api["cloudkitty"].get("/v1/report/summary?groupby=tenant_id")).json()
    assert [r["tenant_id"] for r in body["summary"]] == [cloud.project_id]
    assert body["summary"][0]["res_type"] == "ALL"


async def test_summary_is_scoped_to_the_callers_project_by_default(api) -> None:
    server_id = await _boot(api)
    await _age(server_id, hours=1)
    body = (await api["cloudkitty"].get("/v1/report/summary?tenant_id=someone-else")).json()
    assert body["summary"] == []


async def test_total_endpoint(api, cloud) -> None:
    server_id = await _boot(api)
    await _age(server_id, hours=5)
    body = (await api["cloudkitty"].get("/v1/report/total")).json()
    assert body["tenant_id"] == cloud.project_id
    assert body["total"] > 0
    assert body["rate"] == body["total"]


async def test_tenants_endpoint(api, cloud) -> None:
    await _boot(api)
    assert (await api["cloudkitty"].get("/v1/report/tenants")).json() == [cloud.project_id]


async def test_begin_and_end_are_echoed(api) -> None:
    server_id = await _boot(api)
    await _age(server_id, hours=1)
    body = (await api["cloudkitty"].get(
        "/v1/report/summary?begin=2026-01-01T00:00:00&groupby=res_type")).json()
    assert body["summary"][0]["begin"] == "2026-01-01T00:00:00Z"
    assert body["summary"][0]["end"] is not None


async def test_invalid_timestamp_is_rejected(api) -> None:
    response = await api["cloudkitty"].get("/v1/report/summary?begin=yesterday")
    assert response.status_code == 400


async def test_rating_modules(api) -> None:
    modules = (await api["cloudkitty"].get("/v1/rating/modules")).json()["modules"]
    assert {m["module_id"] for m in modules} == {"hashmap", "noop"}
    assert (await api["cloudkitty"].get("/v1/rating/modules/hashmap")).json()["enabled"] is True
    assert (await api["cloudkitty"].get("/v1/rating/modules/ghost")).status_code == 404


async def test_quote_prices_a_hypothetical_instance(api) -> None:
    quoted = await api["cloudkitty"].post("/v1/rating/quote", json={
        "resources": [{"service": "compute", "volume": 2,
                       "desc": {"vcpus": 2, "memory": 4096}}]})
    assert quoted.status_code == 200
    expected = 2 * (2 * settings.rate_vcpu_hour + 4 * settings.rate_ram_gb_hour)
    assert quoted.json() == pytest.approx(expected)


async def test_config_and_service_info(api) -> None:
    config = (await api["cloudkitty"].get("/v1/info/config")).json()
    assert config["rates"]["vcpu_hour"] == settings.rate_vcpu_hour
    services = (await api["cloudkitty"].get("/v1/info/service")).json()["services"]
    assert {s["service_id"] for s in services} >= {"instance", "volume.size"}


async def test_dataframes(api) -> None:
    server_id = await _boot(api)
    await _age(server_id, hours=2)
    body = (await api["cloudkitty"].get("/v1/storage/dataframes")).json()
    assert body["total"] >= 1
    assert body["dataframes"][0]["resources"][0]["service"] == "instance"
