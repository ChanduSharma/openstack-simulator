"""Swift Object Store v1 API tests -- metadata is kept, payloads are not."""
from __future__ import annotations

import hashlib

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
def account(cloud) -> str:
    return f"/v1/AUTH_{cloud.project_id}"


async def test_info_is_unauthenticated(raw_clients) -> None:
    body = (await raw_clients["swift"].get("/info")).json()
    assert body["swift"]["max_file_size"] > 0
    assert body["swift"]["policies"][0]["name"] == "Policy-0"


async def test_empty_account(api, account) -> None:
    response = await api["swift"].get(account)
    assert response.status_code == 204
    assert response.headers["x-account-container-count"] == "0"
    assert response.headers["x-account-bytes-used"] == "0"


async def test_container_create_is_idempotent(api, account) -> None:
    first = await api["swift"].put(f"{account}/docs")
    assert first.status_code == 201
    again = await api["swift"].put(f"{account}/docs")
    assert again.status_code == 202, "an existing container reports 202, not 201"


async def test_account_listing_reports_containers(api, account) -> None:
    await api["swift"].put(f"{account}/alpha")
    await api["swift"].put(f"{account}/beta")

    plain = await api["swift"].get(account)
    assert plain.text.split() == ["alpha", "beta"]
    assert plain.headers["x-account-container-count"] == "2"

    listing = (await api["swift"].get(f"{account}?format=json")).json()
    assert [c["name"] for c in listing] == ["alpha", "beta"]
    assert all(c["count"] == 0 for c in listing)


async def test_account_head_and_metadata(api, account) -> None:
    posted = await api["swift"].post(account, headers={"X-Account-Meta-Team": "infra"})
    assert posted.status_code == 204
    head = await api["swift"].head(account)
    assert head.status_code == 204
    assert head.headers["x-account-meta-team"] == "infra"


async def test_object_upload_is_discarded_but_hashed(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    payload = b"a" * (512 * 1024)
    response = await api["swift"].put(f"{account}/data/blob.bin", content=payload,
                                      headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 201
    assert response.headers["etag"] == hashlib.md5(payload).hexdigest()
    assert response.headers["x-openstack-simulator-discarded-bytes"] == str(len(payload))


async def test_object_head_reports_the_original_size(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    payload = b"x" * 4096
    await api["swift"].put(f"{account}/data/f.bin", content=payload,
                           headers={"Content-Type": "text/plain",
                                    "X-Object-Meta-Owner": "alice"})
    head = await api["swift"].head(f"{account}/data/f.bin")
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(payload))
    assert head.headers["content-type"] == "text/plain"
    assert head.headers["x-object-meta-owner"] == "alice"


async def test_object_get_returns_metadata_without_a_body(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    payload = b"y" * 2048
    await api["swift"].put(f"{account}/data/f.bin", content=payload)
    response = await api["swift"].get(f"{account}/data/f.bin")
    assert response.status_code == 200
    assert response.content == b"", "the bytes were never stored"
    assert response.headers["x-object-sim-original-length"] == str(len(payload))
    assert response.headers["x-openstack-simulator-zero-storage"] == "true"


async def test_etag_mismatch_is_rejected(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    response = await api["swift"].put(f"{account}/data/f.bin", content=b"hello",
                                      headers={"ETag": "0" * 32})
    assert response.status_code == 422


async def test_matching_etag_is_accepted(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    payload = b"hello"
    response = await api["swift"].put(
        f"{account}/data/f.bin", content=payload,
        headers={"ETag": hashlib.md5(payload).hexdigest()})
    assert response.status_code == 201


async def test_overwriting_an_object_updates_its_metadata(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    await api["swift"].put(f"{account}/data/f.bin", content=b"short")
    await api["swift"].put(f"{account}/data/f.bin", content=b"a much longer payload")
    head = await api["swift"].head(f"{account}/data/f.bin")
    assert head.headers["content-length"] == "21"
    listing = (await api["swift"].get(f"{account}/data?format=json")).json()
    assert len(listing) == 1, "an overwrite replaces rather than duplicates"


async def test_container_listing_and_stats(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    await api["swift"].put(f"{account}/data/a.txt", content=b"1234")
    await api["swift"].put(f"{account}/data/b.txt", content=b"5678")

    plain = await api["swift"].get(f"{account}/data")
    assert plain.text.split() == ["a.txt", "b.txt"]

    head = await api["swift"].head(f"{account}/data")
    assert head.headers["x-container-object-count"] == "2"
    assert head.headers["x-container-bytes-used"] == "8"

    listing = (await api["swift"].get(f"{account}/data?format=json")).json()
    assert {entry["name"] for entry in listing} == {"a.txt", "b.txt"}
    assert listing[0]["hash"] == hashlib.md5(b"1234").hexdigest()


async def test_container_prefix_filter(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    for name in ("logs/one", "logs/two", "other"):
        await api["swift"].put(f"{account}/data/{name}", content=b"x")
    listing = (await api["swift"].get(f"{account}/data?prefix=logs/&format=json")).json()
    assert {entry["name"] for entry in listing} == {"logs/one", "logs/two"}


async def test_nested_object_names_are_preserved(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    await api["swift"].put(f"{account}/data/a/deep/path/file.txt", content=b"x")
    head = await api["swift"].head(f"{account}/data/a/deep/path/file.txt")
    assert head.status_code == 200


async def test_container_metadata(api, account) -> None:
    await api["swift"].put(f"{account}/data", headers={"X-Container-Meta-Purpose": "logs"})
    head = await api["swift"].head(f"{account}/data")
    assert head.headers["x-container-meta-purpose"] == "logs"
    await api["swift"].post(f"{account}/data", headers={"X-Container-Meta-Purpose": "audit"})
    head = await api["swift"].head(f"{account}/data")
    assert head.headers["x-container-meta-purpose"] == "audit"


async def test_object_post_updates_metadata(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    await api["swift"].put(f"{account}/data/f.bin", content=b"x")
    posted = await api["swift"].post(f"{account}/data/f.bin",
                                     headers={"X-Object-Meta-Stage": "final"})
    assert posted.status_code == 202
    head = await api["swift"].head(f"{account}/data/f.bin")
    assert head.headers["x-object-meta-stage"] == "final"


async def test_object_delete(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    await api["swift"].put(f"{account}/data/f.bin", content=b"x")
    assert (await api["swift"].delete(f"{account}/data/f.bin")).status_code == 204
    assert (await api["swift"].head(f"{account}/data/f.bin")).status_code == 404
    head = await api["swift"].head(f"{account}/data")
    assert head.headers["x-container-object-count"] == "0"


async def test_container_delete_requires_it_to_be_empty(api, account) -> None:
    await api["swift"].put(f"{account}/data")
    await api["swift"].put(f"{account}/data/f.bin", content=b"x")
    refused = await api["swift"].delete(f"{account}/data")
    assert refused.status_code == 409
    await api["swift"].delete(f"{account}/data/f.bin")
    assert (await api["swift"].delete(f"{account}/data")).status_code == 204


async def test_missing_resources_are_404s(api, account) -> None:
    assert (await api["swift"].get(f"{account}/ghost")).status_code == 404
    assert (await api["swift"].delete(f"{account}/ghost")).status_code == 404
    await api["swift"].put(f"{account}/data")
    assert (await api["swift"].get(f"{account}/data/ghost.bin")).status_code == 404
    assert (await api["swift"].post(f"{account}/data/ghost.bin")).status_code == 404


async def test_uploading_into_a_missing_container_is_a_404(api, account) -> None:
    assert (await api["swift"].put(f"{account}/ghost/f.bin", content=b"x")).status_code == 404


async def test_admin_may_reach_another_account(api) -> None:
    """The admin role is a Swift reseller admin and crosses account boundaries."""
    assert (await api["swift"].get("/v1/AUTH_someone-else")).status_code == 204


async def test_a_plain_user_cannot_reach_another_account(raw_clients, api, cloud) -> None:
    keystone = api["keystone"]
    other = (await keystone.post("/v3/projects",
                                 json={"project": {"name": "tenant-b"}})).json()["project"]
    await keystone.post("/v3/users", json={"user": {
        "name": "carol", "password": "pw", "default_project_id": other["id"]}})
    issued = await raw_clients["keystone"].post("/v3/auth/tokens", json={"auth": {
        "identity": {"methods": ["password"], "password": {"user": {
            "name": "carol", "domain": {"name": "Default"}, "password": "pw"}}},
        "scope": {"project": {"id": other["id"]}}}})
    token = issued.headers["X-Subject-Token"]
    assert "admin" not in [r["name"] for r in issued.json()["token"]["roles"]]

    response = await raw_clients["swift"].get(f"/v1/AUTH_{cloud.project_id}",
                                              headers={"X-Auth-Token": token})
    assert response.status_code == 403
