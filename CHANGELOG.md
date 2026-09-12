# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Two numbers move independently:

- the **project version** (`0.1.0`), reported by `main.py --version`;
- the **database schema version** (`v1`), stamped into the SQLite file. It is bumped only
  when an existing database cannot be reused as-is, and every bump lands with either a
  migration or an explicit note that a `seed.py --reset` is required.

## [Unreleased]

### Added

- `main.py --database` / `seed.py --database` (`-D`), and the matching
  `OPENSTACK_SIMULATOR_DATABASE` variable, to run against a chosen database file — one
  per environment, so a `dev.db` and a `prod.db` keep entirely separate clouds. Accepts a
  SQLite path (a bare name gains `.db`, a missing parent directory is created),
  `:memory:` for a throwaway cloud that is seeded automatically at startup, or a full
  SQLAlchemy url. The default database and behaviour are unchanged.
- The database in use is reported in the startup banner, in `--status` (read back from
  the pid file, which now records it) and on the dashboard — the ports are identical in
  every environment, so nothing else distinguishes them.
- Startup warns when the chosen database has no identity seeded yet, naming the
  `seed.py --database …` command, instead of leaving every request to fail with `401`.

## [0.1.0] — 2026-09-11

Database schema **v1**.

### Added

- Eleven simulated services on their native OpenStack ports from one process: Keystone,
  Nova, Cinder, Glance, Neutron, Placement, Octavia, Swift, CloudKitty, a failure
  injection control plane and a live status dashboard.
- Bare-metal depletion model: a seeded node whose vCPU, RAM, disk and conntrack capacity
  is really consumed, with per-state booking (`SHUTOFF` holds its cores and RAM,
  `SHELVED_OFFLOADED` releases them and keeps its disk).
- Failure injection: `500_error`, `503_error`, `rate_limit`, `latency`, `timeout` and
  `quota_exhausted`, narrowable by path, method and probability, expiring on their own.
- On-the-fly CloudKitty rating computed from SQL aggregates, with no collector.
- `main.py --detach` to run in the background, returning only once every port answers,
  plus `--status`, `--stop` and a pre-flight check that names a port held by another
  process before anything is spawned.
- `main.py --version` and `seed.py --version`; the version is also carried on every
  response as `X-OpenStack-Simulator-Version` and shown in each service's OpenAPI docs.
- Database schema versioning: the schema version is stamped into the SQLite file, a
  mismatch is reported at startup naming the build that wrote it, and registered
  migrations are applied in sequence.

### Fixed

- Error bodies now match what each upstream service actually puts on the wire. 401s are
  Keystone-shaped for every service fronted by `keystonemiddleware` rather than rendered
  in each service's own dialect, with the challenge as
  `Keystone uri="http://127.0.0.1:5000"`; Swift answers with swob HTML and its own
  `Swift realm="AUTH_{project}"`.
- `GET /v2.1/flavors/{id}` returns `404 itemNotFound` for an unknown flavor, matching
  Nova. A bad `flavorRef` on boot or resize is still `400`.
- The startup banner no longer raises `KeyError: 'dashboard'` when `--service` narrows the
  run to a set that excludes the dashboard.

[Unreleased]: https://github.com/ChanduSharma/openstack-simulator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ChanduSharma/openstack-simulator/releases/tag/v0.1.0
