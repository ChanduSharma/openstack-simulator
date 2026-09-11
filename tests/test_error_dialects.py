"""Error bodies are compared against what a real deployment puts on the wire.

The shapes here are not the simulator's own convention -- each one is the format the
corresponding upstream project emits, so a test written against a real cloud sees the
same body here.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


# -- 401: keystonemiddleware answers, not the service --------------------------------

# Every service except Swift runs behind keystonemiddleware in a real deployment, so an
# unauthenticated request is rejected before it reaches the service's own WSGI app and
# the body is Keystone's -- never the service's own dialect.
KEYSTONE_FRONTED = [
    ("nova", "/v2.1/servers"),
    ("cinder", "/v3/volumes"),
    ("neutron", "/v2.0/networks"),
    ("glance", "/v2/images"),
    ("placement", "/resource_providers"),
    ("octavia", "/v2/lbaas/loadbalancers"),
    ("keystone", "/v3/projects"),
]


@pytest.mark.parametrize(("service", "path"), KEYSTONE_FRONTED)
async def test_unauthenticated_requests_get_the_keystone_body(
    raw_clients, cloud, service, path
) -> None:
    response = await raw_clients[service].get(path)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {
            "code": 401,
            "title": "Unauthorized",
            "message": "The request you have made requires authentication.",
        }
    }
    # keystonemiddleware points the client at Keystone itself, with a full URL.
    assert response.headers["WWW-Authenticate"] == 'Keystone uri="http://127.0.0.1:5000"'


async def test_swift_challenges_with_its_own_realm(
    raw_clients, cloud
) -> None:
    response = await raw_clients["swift"].get(f"/v1/AUTH_{cloud.project_id}/box")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == f'Swift realm="AUTH_{cloud.project_id}"'
    assert response.text == (
        "<html><h1>Unauthorized</h1><p>This server could not verify that you are "
        "authorized to access the document you requested.</p></html>"
    )


# -- Swift speaks swob HTML, not JSON -------------------------------------------------


async def test_swift_errors_are_swob_html(api, cloud) -> None:
    response = await api["swift"].get(f"/v1/AUTH_{cloud.project_id}/missing")
    assert response.status_code == 404
    assert response.headers["content-type"] == "text/html; charset=UTF-8"
    assert response.text == "<html><h1>Not Found</h1><p>The resource could not be found.</p></html>"
    # swob drops the caller's message; ours is kept on a header of our own instead.
    assert response.headers["X-OpenStack-Simulator-Detail"] == "Container not found."


async def test_swift_conflict_uses_the_canned_reason(
    api, cloud
) -> None:
    account = f"/v1/AUTH_{cloud.project_id}"
    await api["swift"].put(f"{account}/keep")
    await api["swift"].put(f"{account}/keep/obj", content=b"data")
    response = await api["swift"].delete(f"{account}/keep")
    assert response.status_code == 409
    assert response.text == (
        "<html><h1>Conflict</h1><p>There was a conflict when trying to complete "
        "your request.</p></html>"
    )


# -- Nova splits a bad flavorRef from a missing flavor resource ------------------------


async def test_missing_flavor_resource_is_404(api) -> None:
    for path in ("/v2.1/flavors/nope", "/v2.1/flavors/nope/os-extra_specs"):
        response = await api["nova"].get(path)
        assert response.status_code == 404, path
        assert response.json() == {
            "itemNotFound": {"message": "Flavor nope could not be found.", "code": 404}
        }
    assert (await api["nova"].delete("/v2.1/flavors/nope")).status_code == 404


async def test_bad_flavorref_on_boot_is_400(api, cloud) -> None:
    response = await api["nova"].post(
        "/v2.1/servers", json={"server": {"name": "vm", "flavorRef": "nope", "imageRef": "x"}}
    )
    assert response.status_code == 400
    assert response.json() == {
        "badRequest": {"message": "Flavor nope could not be found.", "code": 400}
    }
