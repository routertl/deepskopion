# rr unknown-llm (AI-generated)
# SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT

"""Deepskopion PS-side telemetry exporter.

Reads CPU load, memory, XADC temperature, IRQ rate, and kernel errors
from Linux /proc and IIO sysfs.  Zero RTL changes — pure software.

Usage::

    from sw.telemetry import collect_snapshot

    snapshot = collect_snapshot()
    print(json.dumps(snapshot, indent=2))

Or from the command line::

    python -m sw.telemetry --pretty
"""

__version__ = "0.1.0"

from sw.telemetry.exporter import collect_snapshot  # noqa: F401

__all__ = ["collect_snapshot", "__version__"]
