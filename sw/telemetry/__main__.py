# SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT

"""CLI entry point for the telemetry exporter.
"""

from __future__ import annotations

import argparse
import sys
import time

from sw.telemetry.exporter import collect_snapshot, format_snapshot


def main():
    parser = argparse.ArgumentParser(
        description="Deepskopion PS-side telemetry exporter."
    )
    parser.add_argument(
        "--interval",
        type=float,
        help="Continuous mode — snapshot every N seconds",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="Output file path. Defaults to stdout.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON",
    )
    parser.add_argument(
        "--procfs",
        default="/proc",
        help="Override /proc root (for development/testing)",
    )
    parser.add_argument(
        "--sysfs",
        default="/sys",
        help="Override /sys root",
    )

    args = parser.parse_args()

    try:
        if args.interval:
            while True:
                t0 = time.monotonic()
                snapshot = collect_snapshot(
                    procfs_root=args.procfs,
                    sysfs_root=args.sysfs,
                )
                output = format_snapshot(snapshot, pretty=args.pretty)
                
                if args.output:
                    with open(args.output, "a") as f:
                        f.write(output + "\n")
                else:
                    print(output)
                    sys.stdout.flush()
                
                elapsed = time.monotonic() - t0
                time.sleep(max(0, args.interval - elapsed))
        else:
            snapshot = collect_snapshot(
                procfs_root=args.procfs,
                sysfs_root=args.sysfs,
            )
            output = format_snapshot(snapshot, pretty=args.pretty)
            
            if args.output:
                with open(args.output, "w") as f:
                    f.write(output)
            else:
                print(output)

    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
