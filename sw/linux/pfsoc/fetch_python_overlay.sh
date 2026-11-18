#!/bin/bash
# DS-P2.141 — resolve the riscv64 PolarFire python_overlay for a fresh consumer.
#
# WHY THIS EXISTS — distribution PARITY, not footgun-prevention. Be precise about
# what problem this closes, because an earlier draft of this header (and the
# DS-P2.141 tests) claimed it stopped `rr linux rootfs` from "silently baking a
# python-less rootfs". That claim is FALSE and was corrected on review: the bake
# path already fails CLOSED. `sdk/cli/commands/linux_steps/rootfs.py`
# `_resolve_python_overlay` raises FileNotFoundError when the declared
# `linux.rootfs.python_overlay` path is not a directory, and `rr linux rootfs`
# maps that to exit 1 — so a missing pfsoc overlay aborts the bake loudly on its
# own, with or without this script.
#
# The gap this closes (DS-P2.141): a fresh clone / CI runner / isolated dispatch
# worktree gets the PolarFire overlay in seconds from the git-lfs-tracked tarball.
# (The armhf Zynq overlay is always built: its tarball left the tree, DS-P1.106.) The PolarFire riscv64 overlay
# (sw/linux/pfsoc/python_overlay, declared by targets/pfsoc_datalogger.yml as
# linux.rootfs.python_overlay) had NO such path: its only producer was the 1-3 h
# sw/linux/pfsoc/build_python_overlay.sh. This script is the missing riscv64
# distribution entrypoint, the sibling of the armhf one, behaving identically:
#   * overlay already present  -> nothing to do (exit 0);
#   * git-lfs tarball present   -> extract it (the fast path — seconds, no rebuild);
#   * neither                    -> refuse (exit 1) with the ONE actionable next
#                                   step (git lfs pull, or the 1-3 h build). This
#                                   is a clearer message than rootfs.py's generic
#                                   FileNotFoundError, not a substitute for it.
#
# Usage:  sw/linux/pfsoc/fetch_python_overlay.sh
#   then  rr linux rootfs --python-overlay sw/linux/pfsoc/python_overlay   (or via
#         linux.rootfs.python_overlay in targets/pfsoc_datalogger.yml).
set -euo pipefail
cd "$(dirname "$0")"

OVERLAY="python_overlay"
TARBALL="python_overlay.tar.gz"

# python3.12 is the interpreter build_python_overlay.sh extracts (Buildroot
# 2024.11.x). Its presence is the "overlay is real" tell, mirroring the armhf
# fetch script's python3.12 probe.
if [ -x "$OVERLAY/usr/bin/python3.12" ] || ls "$OVERLAY"/usr/bin/python3* >/dev/null 2>&1; then
  echo "pfsoc python_overlay present ($(du -sh "$OVERLAY" | cut -f1)) — nothing to do."
  exit 0
fi

if [ ! -f "$TARBALL" ]; then
  echo "ERROR: pfsoc python_overlay is absent and $TARBALL does not exist." >&2
  echo "  The tarball is not published. Build the overlay from upstream sources (1-3 h):" >&2
  echo "      ./sw/linux/pfsoc/build_python_overlay.sh" >&2
  echo "  A private checkout that tracks it in git-lfs can instead run: git lfs pull" >&2
  echo "  Without the overlay, 'rr linux rootfs' for pfsoc_datalogger would abort" >&2
  echo "  anyway (rootfs.py _resolve_python_overlay -> exit 1); this just says so" >&2
  echo "  earlier, with the build step to run next." >&2
  exit 1
fi

# Guard against a non-fetched git-lfs POINTER (a few hundred bytes of text).
if [ "$(stat -c %s "$TARBALL")" -lt 1000000 ]; then
  echo "ERROR: $TARBALL looks like an unfetched git-lfs pointer." >&2
  echo "  Run 'git lfs pull' to download the real tarball," >&2
  echo "  or rebuild from source with ./sw/linux/pfsoc/build_python_overlay.sh (1-3 h)." >&2
  exit 1
fi

echo "Extracting $TARBALL -> $OVERLAY/ ..."
mkdir -p "$OVERLAY"
tar xzf "$TARBALL" -C "$OVERLAY"
echo "OK: pfsoc python_overlay ready ($(du -sh "$OVERLAY" | cut -f1))."
