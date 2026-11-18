# SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT

"""Metric collectors for PS-side telemetry.

Each collector reads from Linux /proc or /sys and returns a typed dict.
All functions accept a ``procfs_root`` or ``sysfs_root`` parameter for
testability — unit tests point these to temporary directories with
synthetic data.

Target: Zynq-7020 running buildroot-debian.
Dependencies: Python 3 stdlib only (no pip packages).
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Type alias for metric dicts
MetricDict = Dict[str, Any]


# ---------------------------------------------------------------------------
# CPU Load — /proc/stat
# ---------------------------------------------------------------------------

def read_cpu_times(procfs_root: str = "/proc") -> Dict[str, int]:
    """Parse the first (aggregate) line of /proc/stat.

    Returns dict with keys: user, nice, system, idle, iowait, irq,
    softirq, steal.
    """
    stat_path = Path(procfs_root) / "stat"
    with open(stat_path) as f:
        for line in f:
            if line.startswith("cpu "):
                parts = line.split()
                # cpu user nice system idle iowait irq softirq steal ...
                keys = [
                    "user", "nice", "system", "idle",
                    "iowait", "irq", "softirq", "steal",
                ]
                return {k: int(v) for k, v in zip(keys, parts[1:])}
    raise ValueError(f"No 'cpu ' line found in {stat_path}")


def collect_cpu_load(
    procfs_root: str = "/proc",
    interval_s: float = 0.1,
) -> MetricDict:
    """Measure CPU load via two delta reads of /proc/stat.

    Args:
        procfs_root: Path to /proc (override for testing).
        interval_s: Seconds between two samples (default 100ms).

    Returns:
        ``{"user": float, "system": float, "idle": float, "load_pct": float}``
        All values are percentages (0–100).
    """
    t1 = read_cpu_times(procfs_root)
    time.sleep(interval_s)
    t2 = read_cpu_times(procfs_root)
    return collect_cpu_load_from_snapshots(t1, t2)


def collect_cpu_load_from_snapshots(
    snap1: Dict[str, int],
    snap2: Dict[str, int],
) -> MetricDict:
    """Compute CPU load from two pre-captured /proc/stat snapshots.

    This is the testable core — ``collect_cpu_load`` is the public API
    that handles the sleeping and reading.
    """
    deltas = {k: snap2[k] - snap1[k] for k in snap1}
    total = sum(deltas.values())

    if total == 0:
        return {"user": 0.0, "system": 0.0, "idle": 100.0, "load_pct": 0.0}

    user_pct = (deltas["user"] + deltas["nice"]) / total * 100
    system_pct = (
        deltas["system"] + deltas["irq"] + deltas["softirq"]
    ) / total * 100
    idle_pct = (deltas["idle"] + deltas["iowait"]) / total * 100
    load_pct = 100.0 - idle_pct

    return {
        "user": round(user_pct, 1),
        "system": round(system_pct, 1),
        "idle": round(idle_pct, 1),
        "load_pct": round(load_pct, 1),
    }


# ---------------------------------------------------------------------------
# Memory — /proc/meminfo
# ---------------------------------------------------------------------------

def collect_memory(procfs_root: str = "/proc") -> MetricDict:
    """Parse /proc/meminfo for key memory stats.

    Returns:
        ``{"total_kb": int, "available_kb": int, "used_pct": float,
           "buffers_kb": int, "cached_kb": int, "free_kb": int}``
    """
    meminfo_path = Path(procfs_root) / "meminfo"
    fields: Dict[str, int] = {}

    wanted = {
        "MemTotal", "MemAvailable", "MemFree",
        "Buffers", "Cached", "SwapTotal", "SwapFree",
    }

    with open(meminfo_path) as f:
        for line in f:
            parts = line.split()
            key = parts[0].rstrip(":")
            if key in wanted:
                fields[key] = int(parts[1])  # always in kB

    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable", fields.get("MemFree", 0))
    used_pct = ((total - available) / total * 100) if total > 0 else 0.0

    return {
        "total_kb": total,
        "available_kb": available,
        "free_kb": fields.get("MemFree", 0),
        "used_pct": round(used_pct, 1),
        "buffers_kb": fields.get("Buffers", 0),
        "cached_kb": fields.get("Cached", 0),
        "swap_total_kb": fields.get("SwapTotal", 0),
        "swap_free_kb": fields.get("SwapFree", 0),
    }


# ---------------------------------------------------------------------------
# XADC Temperature — IIO sysfs
# ---------------------------------------------------------------------------

def collect_xadc_temperature(
    sysfs_root: str = "/sys",
    device: str = "iio:device0",
) -> MetricDict:
    """Read Zynq XADC temperature via Linux IIO sysfs.

    The Xilinx XADC driver exposes:
        - ``in_temp0_raw``   — ADC raw reading
        - ``in_temp0_scale`` — mV-per-LSB scale factor
        - ``in_temp0_offset`` — offset (in raw units)

    Temperature (°C) = (raw + offset) × scale / 1000

    Returns:
        ``{"temp_celsius": float, "raw": int}``
        or ``{"temp_celsius": null, "raw": null, "error": "..."}``
    """
    iio_base = Path(sysfs_root) / "bus" / "iio" / "devices" / device

    try:
        raw = int(_read_sysfs_value(iio_base / "in_temp0_raw"))
        scale = float(_read_sysfs_value(iio_base / "in_temp0_scale"))
        offset = float(_read_sysfs_value(iio_base / "in_temp0_offset"))
    except (FileNotFoundError, ValueError, OSError) as exc:
        logger.debug("XADC read failed: %s", exc)
        return {"temp_celsius": None, "raw": None, "error": str(exc)}

    # Xilinx XADC: temp_celsius = (raw + offset) * scale / 1000
    temp_c = (raw + offset) * scale / 1000.0
    return {"temp_celsius": round(temp_c, 2), "raw": raw}


def _read_sysfs_value(path: Path) -> str:
    """Read a single-line sysfs file and return its stripped content."""
    with open(path) as f:
        return f.read().strip()


# ---------------------------------------------------------------------------
# IRQ Rate — /proc/interrupts
# ---------------------------------------------------------------------------

def parse_interrupts(procfs_root: str = "/proc") -> Dict[str, int]:
    """Parse /proc/interrupts and return {irq_name: total_count}.

    Each line has format:
        <IRQ#>:  <CPU0>  <CPU1> ...  <type>  <name>

    We sum across all CPUs for each IRQ line.
    """
    interrupts_path = Path(procfs_root) / "interrupts"
    result: Dict[str, int] = {}

    with open(interrupts_path) as f:
        # Skip header line
        header = f.readline()
        num_cpus = len(header.split())

        for line in f:
            parts = line.split()
            if len(parts) < num_cpus + 2:
                continue

            irq_id = parts[0].rstrip(":")
            # Sum counts across all CPUs
            try:
                total = sum(int(parts[i + 1]) for i in range(num_cpus))
            except (ValueError, IndexError):
                continue

            # Name is the last token(s)
            name = parts[-1] if len(parts) > num_cpus + 1 else irq_id
            result[f"{irq_id}:{name}"] = total

    return result


def collect_irq_rate(
    procfs_root: str = "/proc",
    interval_s: float = 1.0,
    top_n: int = 5,
) -> MetricDict:
    """Measure IRQ rate by taking two snapshots of /proc/interrupts.

    Args:
        procfs_root: Path override for testing.
        interval_s: Seconds between samples.
        top_n: Number of top IRQs to include.

    Returns:
        ``{"total_irqs_per_sec": float,
           "top_irqs": [{"irq": str, "name": str, "rate": float}, ...]}``
    """
    snap1 = parse_interrupts(procfs_root)
    time.sleep(interval_s)
    snap2 = parse_interrupts(procfs_root)
    return compute_irq_rate_from_snapshots(snap1, snap2, interval_s, top_n)


def compute_irq_rate_from_snapshots(
    snap1: Dict[str, int],
    snap2: Dict[str, int],
    interval_s: float,
    top_n: int = 5,
) -> MetricDict:
    """Compute IRQ rate from two pre-captured snapshots (testable core)."""
    rates: List[Dict[str, Any]] = []
    total_delta = 0

    for key in snap2:
        delta = snap2[key] - snap1.get(key, 0)
        if delta > 0:
            total_delta += delta
            irq_id, _, name = key.partition(":")
            rates.append({
                "irq": irq_id,
                "name": name,
                "rate": round(delta / interval_s, 1),
            })

    # Sort by rate descending, keep top N
    rates.sort(key=lambda x: x["rate"], reverse=True)

    return {
        "total_irqs_per_sec": round(total_delta / interval_s, 1),
        "top_irqs": rates[:top_n],
    }


# ---------------------------------------------------------------------------
# Kernel Errors — dmesg
# ---------------------------------------------------------------------------

def collect_kernel_errors(
    max_lines: int = 20,
    _dmesg_cmd: Optional[List[str]] = None,
) -> MetricDict:
    """Collect recent kernel errors from dmesg.

    Uses ``dmesg --level=err,warn -T`` for human-readable timestamps.
    Falls back gracefully if dmesg is unavailable or permission is denied.

    Args:
        max_lines: Maximum number of recent error lines to return.
        _dmesg_cmd: Override dmesg command (for testing/mocking).

    Returns:
        ``{"error_count": int,
           "recent_errors": [{"timestamp": str, "message": str}, ...]}``
    """
    cmd = _dmesg_cmd or ["dmesg", "--level=err,warn", "-T"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {
                "error_count": 0,
                "recent_errors": [],
                "error": result.stderr.strip() or f"dmesg exited with {result.returncode}",
            }
        lines = result.stdout.strip().splitlines() if result.stdout else []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("dmesg failed: %s", exc)
        return {
            "error_count": 0,
            "recent_errors": [],
            "error": str(exc),
        }

    errors: List[Dict[str, str]] = []
    for line in lines[-max_lines:]:
        # dmesg -T format: [Mon Mar 25 12:00:00 2026] message text
        ts, _, msg = line.partition("] ")
        if ts.startswith("["):
            ts = ts[1:]  # strip leading [
        errors.append({"timestamp": ts.strip(), "message": msg.strip()})

    return {
        "error_count": len(lines),
        "recent_errors": errors,
    }


# ---------------------------------------------------------------------------
# Uptime — /proc/uptime
# ---------------------------------------------------------------------------

def collect_uptime(procfs_root: str = "/proc") -> float:
    """Read system uptime in seconds from /proc/uptime."""
    uptime_path = Path(procfs_root) / "uptime"
    try:
        with open(uptime_path) as f:
            return float(f.read().split()[0])
    except (FileNotFoundError, ValueError, IndexError):
        return 0.0


# ---------------------------------------------------------------------------
# Hostname
# ---------------------------------------------------------------------------

def collect_hostname() -> str:
    """Return the system hostname."""
    try:
        with open("/etc/hostname") as f:
            return f.read().strip()
    except FileNotFoundError:
        import socket
        return socket.gethostname()
