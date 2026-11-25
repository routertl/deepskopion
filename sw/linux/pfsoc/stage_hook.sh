#!/usr/bin/env bash
# rr unknown-llm (AI-generated)
# PolarFire staging. sw/linux/stage_daemon.sh runs this before it stages the
# daemon. It lives here because the public export drops sw/linux/pfsoc/.
set -euo pipefail

THIS_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# DS-P2.141: materialize the riscv64 pfsoc python_overlay from its committed
# git-lfs tarball before rootfs packing consumes the declared directory
# (targets/pfsoc_datalogger.yml python_overlay). GUARDED: this script is the
# shared stage_command for every Linux target (zybo/de25 included), so a
# pfsoc-only concern must never fail the shared path — fetch only when the
# real tarball is present (not an unfetched lfs pointer); otherwise note and
# continue (pfsoc rootfs itself still fails loudly later if it needs it).
if [ -s "$THIS_DIR/pfsoc/python_overlay.tar.gz" ]    && ! head -c 20 "$THIS_DIR/pfsoc/python_overlay.tar.gz" | grep -q "^version http"; then
  "$THIS_DIR/pfsoc/fetch_python_overlay.sh"
elif [ ! -d "$THIS_DIR/pfsoc/python_overlay" ]; then
  echo "note: pfsoc python_overlay not materialized (run 'git lfs pull' for pfsoc targets); non-pfsoc staging continues."
fi

# DS-P2.157 (MEASURED on the kit 2026-09-23): this riscv64 glibc loader searches
# /lib64 and /usr/lib (`ld-linux-riscv64-lp64d.so.1 --help`), never /lib, and the
# overlay ships libc/libm in /lib with no /lib64 link (Buildroot's skeleton makes
# that link; the overlay took lib/ only). python3 then died "libm.so.6: cannot
# open shared object file" until `ln -s lib /lib64` was added on the card.
# Recreated here, every run, like the rootfs_overlay link below. NB: `rr linux
# rootfs` copies overlays with followlinks, so the image carries /lib64 as a
# real copy of lib (~2 MiB), which the loader resolves the same way.
PFSOC_PY="$THIS_DIR/pfsoc/python_overlay"
if [[ -d "$PFSOC_PY/lib" ]]; then
  if [[ -e "$PFSOC_PY/lib64" && ! -L "$PFSOC_PY/lib64" ]]; then
    echo "stage_daemon.sh: $PFSOC_PY/lib64 exists and is not a symlink —" \
         "refusing to shadow the overlay's /lib (DS-P2.157)" >&2
    exit 1
  fi
  ln -sfn lib "$PFSOC_PY/lib64"
fi

# DS-P2.146: `rr linux rootfs` copies `<memory-map-parent>/rootfs_overlay`.
# PolarFire's map is sw/linux/pfsoc/system_memory_map.yml, so the sibling it
# actually reads is sw/linux/pfsoc/rootfs_overlay — not THIS_DIR/rootfs_overlay
# where S99deepskopion-stream lives. Point the sibling at the one SW-C0 tree.
# Do NOT commit this link: a tracked `../` symlink fails Python tarfile's
# data_filter in the clean-clone gate (DS-P3.150). Recreate it here, every
# run; refuse a real directory so we never drift a second copy (DS-P2.81).
PFSOC_OVERLAY="$THIS_DIR/pfsoc/rootfs_overlay"
if [[ -e "$PFSOC_OVERLAY" && ! -L "$PFSOC_OVERLAY" ]]; then
  echo "stage_daemon.sh: $PFSOC_OVERLAY exists and is not a symlink —" \
       "refusing a second overlay copy (DS-P2.81 / DS-P2.146)" >&2
  exit 1
fi
ln -sfn ../rootfs_overlay "$PFSOC_OVERLAY"
