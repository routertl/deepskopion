#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.264: refuse a built de25_p2writer rootfs that lacks a command the
writer or its /init runs, before any board step.

  check_applets.py <rootfs dir>     exit 0 = every command present, 1 = refused

A command is present when <rootfs>/{bin,sbin,usr/bin,usr/sbin}/<name> exists
(busybox installs one link per applet). The list is the external commands of
overlay/init and overlay/usr/sbin/p2write.sh; shell builtins are not listed.
"""
from __future__ import annotations

import sys
from pathlib import Path

REQUIRED = (
    # overlay/init
    "sh", "mount", "mkdir", "ip", "seq", "sleep", "cat", "tr", "sed",
    # overlay/usr/sbin/p2write.sh
    "tail", "grep", "awk", "blockdev", "sha256sum", "cut", "nc", "tftp", "dd",
    "head", "sync",
)
DIRS = ("bin", "sbin", "usr/bin", "usr/sbin")


def missing(rootfs: Path) -> list[str]:
    return [name for name in REQUIRED
            if not any((rootfs / d / name).exists() or (rootfs / d / name).is_symlink()
                       for d in DIRS)]


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not Path(argv[1]).is_dir():
        print("usage: check_applets.py <rootfs dir>")
        return 1
    gone = missing(Path(argv[1]))
    if gone:
        print("P2W-APPLETS REFUSE missing: " + " ".join(gone))
        return 1
    print(f"P2W-APPLETS OK {len(REQUIRED)} commands present")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
