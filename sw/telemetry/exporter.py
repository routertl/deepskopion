# rr unknown-llm (AI-generated)
# SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT

"""Telemetry exporter — orchestrates metric collection into a JSON snapshot.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from typing import Any, Dict, Optional

from sw.telemetry.collectors import (
    collect_cpu_load_from_snapshots,
    collect_hostname,
    collect_kernel_errors,
    collect_memory,
    collect_uptime,
    collect_xadc_temperature,
    compute_irq_rate_from_snapshots,
    parse_interrupts,
    read_cpu_times,
)

logger = logging.getLogger(__name__)


def collect_snapshot(
    procfs_root: str = "/proc",
    sysfs_root: str = "/sys",
    irq_interval_s: float = 1.0,
    cpu_interval_s: float = 0.1,
    max_dmesg_lines: int = 20,
) -> Dict[str, Any]:
    """Collect all Phase 1 metrics and return a timestamped snapshot.

    Args:
        procfs_root: Path to /proc (default: /proc).
        sysfs_root: Path to /sys (default: /sys).
        irq_interval_s: Interval for IRQ rate calculation.
        cpu_interval_s: Interval for CPU load calculation.
        max_dmesg_lines: Max recent kernel errors to include.

    Returns:
        A dictionary containing all collected metrics.
    """
    t_start = time.monotonic()
    cpu_t1 = read_cpu_times(procfs_root)
    irq_t1 = parse_interrupts(procfs_root)

    # We can collect instant metrics while we wait (takes negligible time)
    mem = collect_memory(procfs_root)
    temp = collect_xadc_temperature(sysfs_root)
    errors = collect_kernel_errors(max_dmesg_lines)

    elapsed = time.monotonic() - t_start
    target_sleep = max(cpu_interval_s, irq_interval_s)
    if elapsed < target_sleep:
        time.sleep(target_sleep - elapsed)

    actual_interval = time.monotonic() - t_start

    cpu_t2 = read_cpu_times(procfs_root)
    irq_t2 = parse_interrupts(procfs_root)

    cpu = collect_cpu_load_from_snapshots(cpu_t1, cpu_t2)
    irq_rate = compute_irq_rate_from_snapshots(irq_t1, irq_t2, actual_interval)

    snapshot = {
        "schema_version": 1,
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "hostname": collect_hostname(),
        "uptime_seconds": collect_uptime(procfs_root),
        "metrics": {
            "cpu": cpu,
            "memory": mem,
            "temperature": temp,
            "irq_rate": irq_rate,
            "kernel_errors": errors,
        },
    }

    return snapshot


def format_snapshot(snapshot: Dict[str, Any], pretty: bool = False) -> str:
    """Format a snapshot dictionary as a JSON string."""
    indent = 2 if pretty else None
    return json.dumps(snapshot, indent=indent)
