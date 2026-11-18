#!/usr/bin/env bash
# Stage sw/stream/ into rootfs_overlay/usr/local/share/deepskopion/ so
# `rr linux rootfs` bakes the daemon into the image, and check the overlay
# against the deployment contract (docs/DEPLOYMENT_CONTRACT.md) before it does.
#
# THIS IS THE ONLY STAGING SCRIPT (DS-P2.81). There used to be a second one,
# refresh_daemon_overlay.sh, doing the same rsync with a DIFFERENT result:
# it omitted the top-level sw/__init__.py that makes `python -m sw.stream`
# resolve. Which of the two an agent happened to run decided whether the
# daemon imported on the board. If you are tempted to add a third, don't —
# extend this one.
#
# The staged tree is gitignored (it really is now — it was tracked for months
# while three separate places claimed otherwise). The only on-disk truth is
# sw/stream/. Re-run this after ANY change under sw/stream/.
#
# You should not normally have to run it by hand: project.yml wires it as a
# pre_rootfs hook so `rr linux rootfs` stages the daemon itself. Running it
# directly is still fine and idempotent.
#
# Prerequisite: a Python-capable rootfs target. The default `rr linux rootfs`
# is busybox-only (no python3, no msgpack) — hence the python_overlay.

set -euo pipefail

THIS_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../.." && pwd)"
SRC="$REPO_ROOT/sw/stream"
SHARE="$THIS_DIR/rootfs_overlay/usr/local/share/deepskopion"

# A board kept out of the public export adds its own staging here.
for hook in "$THIS_DIR"/*/stage_hook.sh; do
  [[ -f "$hook" ]] || continue
  bash "$hook"
done

DST="$SHARE/sw/stream"

if [[ ! -d "$SRC" ]]; then
  echo "stage_daemon.sh: source not found: $SRC" >&2
  exit 1
fi

# DS-P2.262: remove bytecode from the staged tree BEFORE the contract check
# (which refuses any bytecode in the overlay). The rsync excludes below keep
# bytecode from being COPIED, but bytecode already in the staged tree survives
# them (rsync --delete protects excluded files), and sw/__pycache__ sits outside
# the rsync destination. A host import of the staged tree (measured:
# sw/linux/tests/test_deployed_tree_self_contained.py before it ran with -B) left
# three cpython-310 .pyc files that rr linux rootfs then baked into a published
# image.
if [[ -d "$SHARE" ]]; then
  find "$SHARE" -name '__pycache__' -type d -prune -exec rm -rf {} +
  find "$SHARE" -name '*.pyc' -type f -delete
fi

# SW-C0 (DS-P1.46): the deployment contract, checked HERE because this is the
# one script every Linux target runs (linux.rootfs.stage_command) immediately
# before the overlay is packed. An overlay that starts something the contract
# does not declare must fail the build, not ship: a stowaway systemd unit rode
# into every image for months precisely because nothing looked at the overlay
# as a whole (DS-P2.82).
python3 "$THIS_DIR/deployment_contract.py" --check

mkdir -p "$DST"

# --delete so a file REMOVED from sw/stream/ also disappears from the image.
# Without it the overlay accumulates files the source no longer has and they
# ride onto the board indefinitely.
#
# The excludes are not cosmetic (DS-P2.83): rr's _apply_overlay_tree copies
# every file it walks, so __pycache__/*.pyc left here get baked into a
# tmpfs-resident initramfs — noise at best, and stale bytecode shadowing
# freshly-staged source at worst. tests/ stays out; the board is not where we
# run them.
# A built libds_edge_drain.so is for the CPU that built it; a board image gets
# its own build (DS-P2.210), never a developer's host copy from sw/stream/native.
rsync -a --delete \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache/' \
  --exclude='tests/' \
  --exclude='*.so' \
  "$SRC/" "$DST/"

# Top-level sw/__init__.py so Python resolves the package under
# WorkingDirectory=/usr/local/share/deepskopion. rsync --delete above only
# touches sw/stream/, so this survives; write it every time regardless.
touch "$SHARE/sw/__init__.py"

# DS-X5: the Wave Stream v1 contract package, staged because the DAEMON
# IMPORTS IT. sw/stream/wave_stream_guard.py (DS-P2.89, 2026-08-12) does
#   sys.path.insert(0, <share>/contracts/wave_stream_v1)
#   import wave_stream_contract
# resolving relative to its own parents[2] — i.e. inside the staged tree, not
# the repo. Staging only sw/stream/ therefore shipped an image whose daemon
# dies on import with ModuleNotFoundError: No module named
# 'wave_stream_contract'. Measured on the Z7-20 the first time a post-2026-08-12
# tree was deployed (DS-P1.36): the board came up, the PL was correct, every
# PS-LOOP requirement passed — and S99deepskopion-stream could not start.
# tests/ and fixtures/ are dev-side and stay out; the board needs the module.
CONTRACT_SRC="$REPO_ROOT/contracts/wave_stream_v1"
CONTRACT_DST="$SHARE/contracts/wave_stream_v1"
if [[ ! -f "$CONTRACT_SRC/wave_stream_contract.py" ]]; then
  echo "stage_daemon.sh: missing $CONTRACT_SRC/wave_stream_contract.py — the" \
       "daemon imports it and will not start without it" >&2
  exit 1
fi
mkdir -p "$CONTRACT_DST"
rsync -a --delete \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache/' \
  --exclude='tests/' \
  --exclude='fixtures/' \
  "$CONTRACT_SRC/" "$CONTRACT_DST/"

echo "✅ staged $SRC → $DST"
echo "   $(find "$DST" -type f | wc -l) file(s); next: rr linux rootfs"
