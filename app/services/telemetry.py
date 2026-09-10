"""Synthetic telemetry: instance uptime, Nova diagnostics and cloud-init console logs.

Nothing here reads a real hypervisor -- figures are derived deterministically from the
server UUID so repeated polls of the same instance stay self-consistent.
"""
from __future__ import annotations

import hashlib
import random
from datetime import timedelta
from typing import Any

from app.core.config import now_utc
from app.models.compute import Flavor, Server

POWER_STATE_NOSTATE = 0
POWER_STATE_RUNNING = 1
POWER_STATE_PAUSED = 3
POWER_STATE_SHUTDOWN = 4
POWER_STATE_SUSPENDED = 7

# status -> (vm_state, power_state)
STATE_MAP: dict[str, tuple[str, int]] = {
    "BUILD": ("building", POWER_STATE_NOSTATE),
    "ACTIVE": ("active", POWER_STATE_RUNNING),
    "SHUTOFF": ("stopped", POWER_STATE_SHUTDOWN),
    "PAUSED": ("paused", POWER_STATE_PAUSED),
    "SUSPENDED": ("suspended", POWER_STATE_SUSPENDED),
    "REBOOT": ("active", POWER_STATE_RUNNING),
    "HARD_REBOOT": ("active", POWER_STATE_RUNNING),
    "SHELVED": ("shelved", POWER_STATE_SHUTDOWN),
    "SHELVED_OFFLOADED": ("shelved_offloaded", POWER_STATE_SHUTDOWN),
    "ERROR": ("error", POWER_STATE_NOSTATE),
    "DELETED": ("deleted", POWER_STATE_NOSTATE),
    "RESCUE": ("rescued", POWER_STATE_RUNNING),
    "VERIFY_RESIZE": ("resized", POWER_STATE_RUNNING),
}


def _rng(server_id: str, salt: str = "") -> random.Random:
    """Stable per-server RNG so diagnostics don't jitter wildly between polls."""
    digest = hashlib.sha256(f"{server_id}:{salt}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def uptime_seconds(server: Server) -> int:
    started = server.launched_at or server.created_at
    if server.status in ("SHUTOFF", "SHELVED", "SHELVED_OFFLOADED", "SUSPENDED"):
        return 0
    return max(int((now_utc() - started).total_seconds()), 0)


def diagnostics(server: Server, flavor: Flavor, ip_address: str | None = None) -> dict[str, Any]:
    """Nova 2.48-style diagnostics document."""
    rng = _rng(server.id, "diag")
    up = uptime_seconds(server)
    running = server.status == "ACTIVE"
    cpu_details = [
        {
            "id": index,
            "time": int(up * 1e9 * rng.uniform(0.05, 0.45)),
            "utilisation": round(rng.uniform(1.0, 38.0), 2) if running else 0.0,
        }
        for index in range(max(server.allocated_vcpus or flavor.vcpus, 1))
    ]
    used_ram = int((server.allocated_ram_mb or flavor.ram) * rng.uniform(0.25, 0.85))
    return {
        "config_drive": server.config_drive,
        "state": "running" if running else STATE_MAP.get(server.status, ("stopped", 4))[0],
        "driver": "libvirt",
        "hypervisor": "kvm",
        "hypervisor_os": "linux",
        "uptime": up,
        "num_cpus": len(cpu_details),
        "num_nics": 1,
        "num_disks": 1,
        "memory_details": {
            "maximum": (server.allocated_ram_mb or flavor.ram) * 1024,
            "used": used_ram * 1024 if running else 0,
        },
        "cpu_details": cpu_details,
        "nic_details": [
            {
                "mac_address": _mac_for(server.id),
                "rx_octets": int(up * rng.uniform(120, 9000)) if running else 0,
                "rx_errors": 0,
                "rx_drop": 0,
                "rx_packets": int(up * rng.uniform(1, 40)) if running else 0,
                "rx_rate": None,
                "tx_octets": int(up * rng.uniform(120, 9000)) if running else 0,
                "tx_errors": 0,
                "tx_drop": 0,
                "tx_packets": int(up * rng.uniform(1, 40)) if running else 0,
                "tx_rate": None,
            }
        ],
        "disk_details": [
            {
                "read_bytes": int(rng.uniform(1e6, 8e8)),
                "read_requests": int(rng.uniform(100, 90000)),
                "write_bytes": int(rng.uniform(1e6, 4e8)) if running else 0,
                "write_requests": int(rng.uniform(100, 50000)) if running else 0,
                "errors_count": 0,
            }
        ],
    }


def _mac_for(server_id: str) -> str:
    digest = hashlib.md5(server_id.encode()).hexdigest()
    return "fa:16:3e:%s:%s:%s" % (digest[0:2], digest[2:4], digest[4:6])


def console_output(
    server: Server,
    flavor: Flavor,
    image_name: str = "cirros",
    ip_address: str | None = None,
    length: int | None = None,
) -> str:
    """Generate a plausible kernel + cloud-init boot transcript for os-getConsoleOutput."""
    rng = _rng(server.id, "console")
    ip = ip_address or "10.0.0.15"
    mac = _mac_for(server.id)
    ram_kb = (server.allocated_ram_mb or flavor.ram) * 1024
    boot = server.launched_at or server.created_at
    hostname = server.name.replace("_", "-").lower()
    clock = 0.0

    lines: list[str] = []

    def kernel(text: str, step: float = 0.08) -> None:
        nonlocal clock
        clock += rng.uniform(0.001, step)
        lines.append(f"[{clock:10.6f}] {text}")

    def cloud_init(unit: str, text: str) -> None:
        nonlocal clock
        clock += rng.uniform(0.01, 0.4)
        stamp = (boot + timedelta(seconds=clock)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        lines.append(f"ci-info: {stamp} | {unit} | {text}")

    lines.append("SeaBIOS (version 1.16.2-debian-1.16.2-1)")
    lines.append("Booting from Hard Disk...")
    lines.append("")
    kernel("Linux version 6.8.0-45-generic (buildd@openstack-simulator) #45-Ubuntu SMP")
    kernel("Command line: root=LABEL=cloudimg-rootfs ro console=tty1 console=ttyS0")
    kernel(f"Memory: {ram_kb}K/{ram_kb}K available")
    kernel(f"smpboot: Allowing {server.allocated_vcpus or flavor.vcpus} CPUs")
    kernel("ACPI: Core revision 20230628")
    kernel("virtio_blk virtio1: [vda] %d 512-byte logical blocks"
           % ((server.allocated_disk_gb or flavor.disk) * 2097152))
    kernel(f"virtio_net virtio0 eth0: renamed from eth0 ({mac})")
    kernel("EXT4-fs (vda1): mounted filesystem with ordered data mode")
    kernel("systemd[1]: Detected virtualization kvm.")
    kernel("systemd[1]: Hostname set to <%s>." % hostname)
    lines.append("")
    lines.append("Starting cloud-init...")
    cloud_init("cloud-init", f"Cloud-init v. 24.1.3 running 'init-local' on {hostname}")
    cloud_init("DataSourceOpenStack", "Attempting to read from http://169.254.169.254/openstack")
    cloud_init("DataSourceOpenStack", f"Reading metadata for instance-id {server.id}")
    cloud_init("net", f"eth0 | True | {ip} | 255.255.255.0 | global | {mac}")
    cloud_init("net", "lo | True | 127.0.0.1 | 255.0.0.0 | host | .")
    cloud_init("route", f"0.0.0.0 | 0.0.0.0 | {ip.rsplit('.', 1)[0]}.1 | eth0 | UG")
    cloud_init("cloud-init", f"config-drive: {'enabled' if server.config_drive else 'disabled'}")
    if server.key_name:
        cloud_init("cc_ssh", f"Imported public key for keypair '{server.key_name}'")
        lines.append("ci-info: ++++++++Authorized keys from /home/cirros/.ssh/authorized_keys+++++++")
        lines.append("ci-info: | Keytype | Fingerprint (sha256) | Options | Comment |")
        lines.append(f"ci-info: | ssh-rsa | SHA256:{hashlib.sha256(server.id.encode()).hexdigest()[:43]} | - | {server.key_name} |")
    if server.user_data:
        cloud_init("cc_scripts_user", "Running user-data script (%d bytes)" % len(server.user_data))
    lines.append("")
    lines.append("Generating public/private rsa key pair.")
    lines.append(f"ec2: SHA256:{hashlib.sha256((server.id + 'rsa').encode()).hexdigest()[:43]} root@{hostname} (RSA)")
    lines.append(f"ec2: SHA256:{hashlib.sha256((server.id + 'ed').encode()).hexdigest()[:43]} root@{hostname} (ED25519)")
    lines.append("")
    lines.append(f"cloud-init[{rng.randint(400, 999)}]: Cloud-init v. 24.1.3 finished at "
                 f"{(boot + timedelta(seconds=clock + 1.4)).strftime('%a, %d %b %Y %H:%M:%S +0000')}. "
                 f"Up {clock + 1.4:.2f} seconds")
    lines.append("")
    lines.append(f"{image_name} login: ")

    if length and length > 0:
        lines = lines[-length:]
    return "\n".join(lines) + "\n"
