# OpenStack-Simulator — a lightweight OpenStack API simulator for local development and CI

Run a full OpenStack control plane on a laptop. Eleven services — Keystone, Nova, Cinder,
Glance, Neutron, Placement, Octavia, Swift, CloudKitty, a failure-injection API and a live
dashboard — answer on their **native OpenStack ports** from a single ~90 MB Python process.
No hypervisor, no virtual machines, no DevStack, no hardware.

It is a **DevStack alternative** for the cases DevStack is too heavy for: testing
`python-openstackclient`, the OpenStack SDK and the Terraform OpenStack provider in CI, on
an old laptop, or inside a container.

What makes it more than a set of stub endpoints is that it models **bare-metal resource
depletion and control-plane state**. Nothing is virtualised, but booting a `m1.medium`
deducts 2 vCPU, 4096 + 256 MB of RAM and 40 GB of disk from a simulated 256 GB node — and
the node fills up, refuses the next boot, and reports the shortfall exactly as Nova would.

![The status dashboard on port 10000: live capacity meters for vCPU, RAM, disk and
conntrack, instances across BUILD / ACTIVE / SHUTOFF / SHELVED_OFFLOADED, attached
volumes, load balancers and on-the-fly rating](docs/dashboard.png)

## Quick start

```bash
uv venv .venv && uv pip install -r requirements.txt   # or: python -m venv .venv; pip install -r requirements.txt
.venv/bin/python seed.py --reset                      # node, identity, catalog, flavors, images, networks
.venv/bin/python main.py                              # all 11 services, one process (~90 MB RSS)

source openrc.sh                                      # or: cp clouds.yaml ~/.config/openstack/
openstack server create --flavor m1.small --image cirros --network private vm1
open http://127.0.0.1:10000/                          # live capacity dashboard
```

Ctrl-C stops all eleven services. If you started it in the background:

```bash
.venv/bin/python main.py --status                     # running? on which ports?
.venv/bin/python main.py --stop                       # SIGTERM, then wait for a clean exit
```

## Starting and stopping

`main.py` writes its pid to `openstack-simulator.pid` on start and removes it on exit
(override the location with `OPENSTACK_SIMULATOR_PID_FILE`). A stale file left by a
`kill -9` is detected and cleaned up rather than trusted, and the pid is checked against
`/proc` before any signal is sent, so a recycled pid can never be signalled by mistake.
Starting a second instance is refused with a clear message instead of eleven
`Address already in use` errors.

## Services

| Service | Port | Base path | Notes |
| --- | --- | --- | --- |
| Keystone | 5000 | `/v3` | UUID tokens via `X-Subject-Token`, full 10-service catalog |
| Nova | 8774 | `/v2.1` | servers, flavors, keypairs, hypervisors, diagnostics, console |
| Cinder | 8776 | `/v3/{project_id}` | volumes, types, snapshots, attachments (`/v3/...` also works) |
| Glance | 9292 | `/v2` | image catalog; uploads are hashed and discarded |
| Neutron | 9696 | `/v2.0` | networks, subnets, ports, security groups, floating IPs |
| Placement | 8778 | `/` | resource providers, inventories, usages, allocations |
| Octavia | 9876 | `/v2/lbaas` | load balancers, listeners, pools, members, monitors |
| Swift | 8080 | `/v1/AUTH_{project}` | containers + object metadata; bodies discarded |
| CloudKitty | 8889 | `/v1` | rating computed per request from SQL aggregates |
| Scenarios | 8999 | `/v1/scenarios` | failure injection control plane |
| Dashboard | 10000 | `/` | live capacity bars, instances, volumes, LBs, billing |

## Using with the OpenStack CLI

Every command below is verified against this simulator with `python-openstackclient`
10.3.0. The catalog it returns points at the loopback ports, so no endpoint overrides are
needed.

```bash
pip install python-openstackclient python-octaviaclient
source openrc.sh

openstack token issue                       # 32-char UUID token
openstack catalog list                      # all 10 services
openstack flavor list
openstack image list

openstack server create --flavor m1.small --image cirros --network private web-01
openstack server list                       # BUILD, for a random 10-60s
openstack console log show web-01           # synthetic cloud-init boot log

# Instances and volumes are not actionable until they leave their transition window,
# exactly as on a real cloud -- acting too early returns 409, so wait for it:
until openstack server show web-01 -f value -c status | grep -qx ACTIVE; do sleep 5; done

openstack server stop web-01                # SHUTOFF still holds its cores and RAM

openstack volume create --size 25 data-vol
until openstack volume show data-vol -f value -c status | grep -qx available; do sleep 5; done
openstack server add volume web-01 data-vol
openstack floating ip create public
openstack security group create web-sg

openstack container create backups
openstack object create backups ./big.iso   # streamed, hashed, discarded

openstack hypervisor stats show             # watch the node deplete
openstack loadbalancer list
```

## API documentation

Each service serves its own interactive docs, because each one is a separate FastAPI app
on its own port:

| | | |
|---|---|---|
| Keystone `:5000/docs` | Nova `:8774/docs` | Cinder `:8776/docs` |
| Glance `:9292/docs` | Neutron `:9696/docs` | Placement `:8778/docs` |
| Octavia `:9876/docs` | Swift `:8080/docs` | CloudKitty `:8889/docs` |
| Scenarios `:8999/docs` | Dashboard `:10000/docs` | |

`/redoc` and `/openapi.json` are served alongside. The schema is generated lazily on first
request, so it costs nothing at startup.

```bash
curl -s localhost:8774/openapi.json | jq -r '.paths | keys[]'    # list Nova's paths
```

Note that most request bodies show as a free-form object rather than a typed schema. That
is deliberate: handlers accept the raw body and validate inside, because real OpenStack
payloads carry a long tail of vendor extensions that a strict signature would reject. For
endpoint *semantics*, [the official API reference](https://docs.openstack.org/api-ref/) is
authoritative — this simulator follows those wire formats.

## Error formats

Errors are returned in the dialect the real service speaks, not a house style — so a test
that asserts on an error body against a real cloud sees the same body here.

| Service | Body |
|---|---|
| Nova, Cinder | `{"itemNotFound": {"message": ..., "code": 404}}` |
| Neutron | `{"NeutronError": {"type": "NetworkNotFound", "message": ..., "detail": ""}}` |
| Keystone | `{"error": {"code": ..., "title": ..., "message": ...}}` |
| Placement | `{"errors": [{"status", "title", "detail", "code", "request_id"}]}` |
| Octavia | `{"faultcode": "Client", "faultstring": ..., "debuginfo": null}` |
| Glance | `{"message": ..., "code": ..., "title": ...}` |
| Swift | `text/html` — `<html><h1>Not Found</h1><p>The resource could not be found.</p></html>` |

Two cases are not what the addressed service would produce on its own, because in a real
deployment it never gets the chance:

- **Every 401 is Keystone-shaped.** `keystonemiddleware` sits in front of Nova, Cinder,
  Neutron, Glance, Placement and Octavia and rejects an unauthenticated request before it
  reaches the service, so the body is Keystone's and the challenge is
  `WWW-Authenticate: Keystone uri="http://127.0.0.1:5000"`.
- **Swift authenticates itself**, so it answers 401 with its own
  `WWW-Authenticate: Swift realm="AUTH_{project}"` and a swob HTML body.

Swift's HTML is canned per status code and has no room for a message, exactly as upstream.
The simulator's own explanation is kept on an `X-OpenStack-Simulator-Detail` header.

## The four operating principles

**Stateless polling delays.** No worker threads, no background jobs. Creating a resource
stores a `transition_until` timestamp 10–60 s in the future. While `now() < transition_until`
reads return `BUILD` / `creating` / `PENDING_CREATE`; the first read after it returns
`ACTIVE` / `available` / `ACTIVE`+`ONLINE` and persists the flip.

**Zero-storage payloads.** `PUT /v2/images/{id}/file` and `PUT /v1/AUTH_x/{c}/{o}` stream
the body slice by slice, update an MD5 (so the ETag a client verifies is real), and drop
every byte. 13 MB of uploads leaves the SQLite file at ~620 KB.

**On-the-fly billing.** CloudKitty has no collector. Instance seconds are accrued lazily
onto the row at read time; volumes, floating IPs, load balancers and objects are priced
with `julianday()` age arithmetic inside SQLite. `(accumulated_seconds / 3600) * unit_cost`.

**Microversion tolerance.** Modern versions (`compute 2.79`, `placement 1.36`,
`volume 3.70`, `load-balancer 2.27`) are echoed on every response regardless of what the
client negotiated.

## Depletion model

Seeded node `node-01`: 2 sockets / 32 cores / 64 threads, 262144 MB RAM, 4096 GB disk,
65536 conntrack entries.

- **vCPU** — overcommitted 3.0x (192 allocatable).
- **RAM** — strictly 1.0x, plus **256 MB QEMU overhead per VM**. This is normally the
  binding constraint: 56 × `m1.medium` fills the node, then boots return `403 Quota exceeded`.
- **Disk** — instance root disks *and* Cinder volumes come out of the same 4 TB pool.
- **Conntrack** — one entry per security-group rule; exhaustion returns a `409 NeutronError`.

State affects the booking, exactly as on real hardware:

| State | vCPU | RAM | Disk |
| --- | --- | --- | --- |
| `ACTIVE` / `BUILD` | held | held | held |
| `SHUTOFF` | **held** | **held** | held |
| `SHELVED_OFFLOADED` | released | released | **held** |
| deleted | released | released | released |

Nova, Placement, `/v2.1/limits` and the dashboard all read the same aggregation, so they
cannot disagree.

## Failure injection

```bash
curl -X POST http://127.0.0.1:8999/v1/scenarios \
  -H 'Content-Type: application/json' \
  -d '{"service": "nova", "action": "500_error", "duration_seconds": 30}'
```

![The dashboard highlighting two active failure injections, with hit counts and the
time left on each](docs/dashboard-failure-injection.png)

Active rules surface on the dashboard with a live hit count, so you can see exactly how
many client calls each one intercepted.

Actions: `500_error`, `503_error`, `rate_limit` (429 + `Retry-After`), `latency`,
`timeout` (504), `quota_exhausted` (403). Narrow a rule with `path_contains`, `method`
and `probability`. Rules take effect within a second and expire on their own.
`GET /v1/scenarios/actions` documents them; `DELETE /v1/scenarios` clears everything.

## Configuration

Every knob is an `OPENSTACK_SIMULATOR_*` environment variable — see `app/core/config.py`. Useful ones:

```bash
OPENSTACK_SIMULATOR_CPU_ALLOCATION_RATIO=16.0   # more aggressive overcommit
OPENSTACK_SIMULATOR_TRANSITION_MIN=1            # fast transitions for CI
OPENSTACK_SIMULATOR_TRANSITION_MAX=3
OPENSTACK_SIMULATOR_HOST_RAM_MB=8192            # emulate a smaller node
OPENSTACK_SIMULATOR_REQUIRE_AUTH=0              # skip tokens for curl-driven demos
```

`python main.py --service nova --service keystone` runs a subset.

## Tests

```bash
uv pip install -r requirements-dev.txt
.venv/bin/python -m pytest                       # whole suite
.venv/bin/python -m pytest tests/test_nova.py -v # one service
.venv/bin/python -m pytest -k transitions        # one theme
```

> **Coverage numbers are unreliable here.** `pytest-cov` is installed, but on this
> Python 3.12 / coverage 7.16 combination it fails to attribute lines executed inside the
> async endpoint bodies — it reports the import-time lines only, so a fully exercised
> module reads as ~50%. This reproduces without pytest at all (a plain `coverage run`
> that issues one request and gets a correct 200 back still records no body lines), and
> the tracer is demonstrably active during the request. Treat the percentage as noise.

The suite runs entirely **in-process**: httpx drives each ASGI app directly, so no ports
are bound and no server needs to be started. Every test gets a freshly built in-memory
SQLite schema.

Two details make it fast and deterministic:

- The 10-60 s transition windows collapse to zero by default. Tests that need to observe
  a pending state take the `slow_transitions` fixture, then use `expire` to rewind the
  stored deadline into the past — so the state machine is exercised in milliseconds
  rather than by sleeping. `test_transitions.py` separately asserts that the real
  randomised delay stays inside [10, 60] and uses the full window.
- `tests/conftest.py` seeds the node, identity, catalog, flavors, images and networks
  through `seed.py` itself, so the fixtures and the shipped seeder cannot drift apart.

## Project structure

```
app/api/        one module per service, each exporting a `router`
app/static/     dashboard markup and client script (plain files, no template engine:
                the page has no server-side variables -- it renders itself from /api/stats)
app/core/       config (specs, ratios, rates), async engine, middleware + app factory
app/models/     typed SQLAlchemy 2.0 models
app/services/   capacity (depletion), telemetry (diagnostics/console), rating (billing)
main.py         runs every service on one asyncio loop
seed.py         idempotent seeder (`--reset` to start over)
tests/          pytest suite (unit + per-service API tests), in-process via httpx
```

## Limitations

Worth knowing before you trust it for something:

- **The Terraform OpenStack provider is a design target, not a verified one.**
  `python-openstackclient` 10.3.0 and the OpenStack SDK are tested end to end; Terraform
  has not been exercised yet.
- **Project isolation yes, RBAC no.** Resources are owned by the project their token was
  scoped to. Nova, Cinder and Neutron scope reads to the caller's project — another
  tenant's resource returns `404`, as Neutron does — while shared and external networks
  stay visible to everyone, and an admin-roled token sees all projects. What is *not*
  modelled is per-role authorisation: inside its own project, a `reader` token can do
  everything a `member` or `admin` token can. Use it to test multi-tenancy; do not use it
  to test policy files.
- **Glance and Octavia do not scope reads yet.** They stamp the owning project on create,
  but their listings return every project's images and load balancers. Nova, Cinder,
  Neutron, Swift and CloudKitty do scope correctly.
- **Uploaded bytes are gone.** Glance and Swift hash the payload for a correct ETag and
  then discard it. `GET` on an image returns `204`; `GET` on an object returns the real
  metadata with an empty body. Anything that reads its data back will fail.
- **Physics is not simulated.** No NUMA, ballooning, page sharing, fragmentation, CPU
  contention, IO throughput or network bandwidth. Diagnostics figures are plausible
  numbers derived from the instance UUID, not measurements. What *is* modelled faithfully
  is the control plane's accounting — which is what actually breaks integrations.
- **One of everything.** A single node, region (`RegionOne`), and domain (`Default`).
  There is no scheduler to test, because there is nowhere else to place an instance.
- **Generated keypairs are decorative.** Importing a public key works properly; asking
  Nova to generate one returns synthetic material. There is no VM to log in to either way.
- **Not for exposure.** Plain HTTP, tokens that are opaque UUIDs rather than Fernet, and
  a seeded password of `secret`. Bind it to loopback and keep it there.
- **Services not simulated:** Heat, Barbican, Magnum, Manila, Ironic, Designate, Ceilometer.

## License

MIT — see [LICENSE](LICENSE).
