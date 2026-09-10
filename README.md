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

## Layout

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
