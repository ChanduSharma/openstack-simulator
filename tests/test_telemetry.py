"""Unit tests for the synthetic telemetry generators."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.config import gen_id, now_utc
from app.models.compute import Flavor, Server
from app.services import telemetry

pytestmark = pytest.mark.anyio


def _server(**kwargs) -> Server:
    defaults = dict(
        id="11111111-2222-3333-4444-555555555555", name="web_01", project_id="p1",
        user_id="u1", flavor_id="3", host="node-01", status="ACTIVE",
        allocated_vcpus=2, allocated_ram_mb=4096, allocated_disk_gb=40,
        overhead_ram_mb=256, created_at=now_utc() - timedelta(hours=1),
        launched_at=now_utc() - timedelta(hours=1), config_drive=False,
        key_name=None, user_data=None, metadata_={}, tags=[], security_group_names=[],
    )
    defaults.update(kwargs)
    return Server(**defaults)


FLAVOR = Flavor(id="3", name="m1.medium", vcpus=2, ram=4096, disk=40)


async def test_state_map_covers_every_status_nova_can_set() -> None:
    for status in ("BUILD", "ACTIVE", "SHUTOFF", "PAUSED", "SUSPENDED", "REBOOT",
                   "HARD_REBOOT", "SHELVED", "SHELVED_OFFLOADED", "ERROR", "DELETED",
                   "RESCUE", "VERIFY_RESIZE"):
        assert status in telemetry.STATE_MAP
    assert telemetry.STATE_MAP["ACTIVE"] == ("active", telemetry.POWER_STATE_RUNNING)
    assert telemetry.STATE_MAP["SHUTOFF"] == ("stopped", telemetry.POWER_STATE_SHUTDOWN)


async def test_uptime_counts_from_launch() -> None:
    assert telemetry.uptime_seconds(_server()) == pytest.approx(3600, abs=5)


@pytest.mark.parametrize("status", ["SHUTOFF", "SHELVED", "SHELVED_OFFLOADED", "SUSPENDED"])
async def test_uptime_is_zero_when_not_running(status) -> None:
    assert telemetry.uptime_seconds(_server(status=status)) == 0


async def test_diagnostics_are_stable_across_repeated_polls() -> None:
    server = _server()
    first = telemetry.diagnostics(server, FLAVOR)
    second = telemetry.diagnostics(server, FLAVOR)
    assert first["cpu_details"][0]["utilisation"] == second["cpu_details"][0]["utilisation"]
    assert first["nic_details"][0]["mac_address"] == second["nic_details"][0]["mac_address"]


async def test_diagnostics_differ_between_instances() -> None:
    a = telemetry.diagnostics(_server(), FLAVOR)
    b = telemetry.diagnostics(_server(id=gen_id()), FLAVOR)
    assert a["nic_details"][0]["mac_address"] != b["nic_details"][0]["mac_address"]


async def test_diagnostics_shape_matches_microversion_248() -> None:
    body = telemetry.diagnostics(_server(), FLAVOR)
    assert body["driver"] == "libvirt" and body["hypervisor"] == "kvm"
    assert body["state"] == "running"
    assert len(body["cpu_details"]) == 2, "one entry per allocated vCPU"
    assert body["memory_details"]["maximum"] == 4096 * 1024
    assert set(body["nic_details"][0]) >= {"mac_address", "rx_octets", "tx_octets"}
    assert set(body["disk_details"][0]) >= {"read_bytes", "write_bytes", "errors_count"}


async def test_stopped_instance_reports_no_activity() -> None:
    body = telemetry.diagnostics(_server(status="SHUTOFF"), FLAVOR)
    assert body["state"] == "stopped"
    assert body["uptime"] == 0
    assert body["memory_details"]["used"] == 0
    assert body["nic_details"][0]["rx_octets"] == 0
    assert all(cpu["utilisation"] == 0.0 for cpu in body["cpu_details"])


async def test_console_log_looks_like_a_real_boot() -> None:
    log = telemetry.console_output(_server(), FLAVOR, image_name="cirros",
                                   ip_address="10.0.0.7")
    assert "SeaBIOS" in log
    assert "Linux version" in log
    assert "cloud-init" in log
    assert "DataSourceOpenStack" in log
    assert "10.0.0.7" in log
    assert "web-01" in log, "hostname is normalised from the instance name"
    assert log.rstrip().endswith("cirros login:")


async def test_console_log_mentions_the_keypair_when_one_is_set() -> None:
    plain = telemetry.console_output(_server(), FLAVOR)
    keyed = telemetry.console_output(_server(key_name="mykey"), FLAVOR)
    assert "Authorized keys" not in plain
    assert "Imported public key for keypair 'mykey'" in keyed


async def test_console_log_reports_user_data_and_config_drive() -> None:
    log = telemetry.console_output(
        _server(user_data="#!/bin/sh\necho hi", config_drive=True), FLAVOR
    )
    assert "config-drive: enabled" in log
    assert "Running user-data script" in log


async def test_console_log_length_returns_the_tail() -> None:
    full = telemetry.console_output(_server(), FLAVOR)
    tail = telemetry.console_output(_server(), FLAVOR, length=5)
    assert len(tail.strip().splitlines()) <= 5
    assert full.rstrip().endswith(tail.rstrip())


async def test_console_log_is_deterministic_per_instance() -> None:
    assert telemetry.console_output(_server(), FLAVOR) == telemetry.console_output(
        _server(), FLAVOR
    )
