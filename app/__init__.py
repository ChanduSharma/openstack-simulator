"""OpenStack-Simulator.

The one place the version is written down. Everything that reports a version -- the CLI,
the per-service OpenAPI metadata, the response header, the dashboard -- reads it from
here, so a release is a single-line change.
"""
from __future__ import annotations

__version__ = "0.1.0"

# Bumped only when the SQLite schema changes in a way that an existing database file
# cannot simply be reused for. Adding a new *model* does not need a bump -- create_all
# picks up a missing table on its own. Adding or changing a *column* on an existing
# model does, because create_all never emits ALTER. See app/core/schema.py.
SCHEMA_VERSION = 1

__all__ = ["SCHEMA_VERSION", "__version__"]
