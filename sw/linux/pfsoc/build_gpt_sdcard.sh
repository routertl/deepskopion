#!/usr/bin/env bash
# rr unknown-llm (AI-generated)
# DS-P2.149 — deterministic build job: rebuild the DS-P2.146 payload onto the
# DEPLOYED GPT layout as a single flashable sdcard.img.
#
# WHY THIS EXISTS
# ---------------
# DS-P2.146 assembled the daemon payload with an ad-hoc sequence of `rr linux`
# steps + `pfsoc_boot_media.py assemble`, and it laid the pre-DS-P2.149
# 2-partition MBR (payload on FAT, rootfs on p2). DS-P2.149 reconciled the
# in-tree assembler onto the >=3-partition GPT the deployed card and the
# RTL-P2.1271 `rr linux sdcard` path use (P1 raw HSS payload / P2 FAT boot /
# P3 ext4 rootfs, root=/dev/mmcblk0p3). This script is the committed,
# reproducible recipe that turns the SAME DS-P2.146 payload baseline into that
# GPT image, so "rebuild the payload onto GPT" is a one-command job, not a
# remembered sequence. It is the deliverable-(2) build job named in the ticket.
#
# It is a SOFTWARE build only. It does NOT program the board.
#
# INPUTS (the DS-P2.146 payload baseline):
#   * sw/linux/pfsoc/python_overlay.tar.gz  — riscv64 python3.12 + libs (git-lfs)
#   * sw/stream/                            — the deepskopion daemon package
#   * sw/linux/pfsoc/rootfs_overlay/        — S99deepskopion-stream + overlay
# The kernel Image / U-Boot / DTB / boot.scr come from `rr linux` (the RISC-V
# chain, NOT the fabric bitstream). The HSS payload generator is built from the
# HSS source (`rr linux hss` clones it) — payload.bin is produced by the real
# generate_payload caller, never a raw copy.
#
# USAGE
#   sw/linux/pfsoc/build_gpt_sdcard.sh --from <dir>    # assemble from a prebuilt
#                                                        staging dir (chain already
#                                                        built) — deterministic,
#                                                        no board-side toolchain
#   sw/linux/pfsoc/build_gpt_sdcard.sh --from <dir> --out <img> --hss-dir <hss>
#
# There is no full mode (DS-P2.402): without --from the script exits 2 and points
# to the guide (docs/getting_started/polarfire_soc_discovery.md).
#
# The `--from` path is what CI / a fresh clone runs to reproduce the image from
# a captured chain without a 1-3 h kernel build.
set -euo pipefail

THIS_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../.." && pwd)"
cd "$REPO_ROOT"

STAGING="$REPO_ROOT/sw/linux/pfsoc/out/pfsoc_datalogger"
OUT="$STAGING/sdcard.img"
HSS_DIR="${PFSOC_HSS_DIR:-$REPO_ROOT/sw/linux/pfsoc/src/hart-software-services}"
FROM_STAGING=""

while [ $# -gt 0 ]; do
  case "$1" in
    --from)     FROM_STAGING="$2"; shift 2 ;;
    --out)      OUT="$2"; shift 2 ;;
    --hss-dir)  HSS_DIR="$2"; shift 2 ;;
    # No effect. Accepted so an old full-mode command reaches the refusal below.
    --size-mb)  shift; [ $# -eq 0 ] || shift ;;
    -h|--help)  sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

log() { printf '\n=== %s ===\n' "$*"; }

if [ -n "$FROM_STAGING" ]; then
  # Deterministic fast path: the rr linux chain was built elsewhere and handed
  # to us. Stage the daemon overlay into that tree, then assemble.
  log "assemble from prebuilt staging: $FROM_STAGING"
  STAGING="$(cd "$FROM_STAGING" && pwd)"
  OUT="${OUT:-$STAGING/sdcard.img}"
  STAGE_ROOTFS="$STAGING/rootfs"
  if [ ! -d "$STAGE_ROOTFS" ]; then
    echo "build_gpt_sdcard.sh: $STAGE_ROOTFS missing — --from needs a base rootfs" \
         "tree from 'rr linux rootfs' (the riscv64 python_overlay lives there)" >&2
    exit 2
  fi
  # DS-T3.40: a prebuilt `--from` staging tree from `rr linux rootfs` already
  # carries the riscv64 python_overlay (`usr/bin/python3.12`) inside
  # `$STAGE_ROOTFS`. Calling `fetch_python_overlay.sh` unconditionally against
  # the repo checkout broke `--from` in fresh clones and CI containers where
  # `sw/linux/pfsoc/python_overlay.tar.gz` is an unfetched git-lfs pointer (or
  # omitted by the public export). Only fetch and merge `python_overlay` from
  # the repo when `$STAGE_ROOTFS` does not yet carry `python3*`.
  if [ ! -x "$STAGE_ROOTFS/usr/bin/python3.12" ] && ! ls "$STAGE_ROOTFS"/usr/bin/python3* >/dev/null 2>&1; then
    log "resolve python_overlay into staging rootfs (DS-P2.146 payload baseline)"
    "$THIS_DIR/fetch_python_overlay.sh"
    rsync -a "$THIS_DIR/python_overlay/" "$STAGE_ROOTFS/"
  fi
  # Stage the daemon through the deployment contract. stage_daemon.sh writes the
  # overlay to sw/linux/pfsoc/rootfs_overlay (== sw/linux/rootfs_overlay) — NOT
  # into $STAGING/rootfs, which is the tree pfsoc_boot_media.py packs as the P3
  # ext4. In the FULL path `rr linux rootfs` bridges that gap (its pre_rootfs
  # hook stages, then it copies the overlay into the rootfs it bakes); the
  # `--from` fast path has no such step, so without the merge below the daemon
  # (S99deepskopion-stream, sw/stream, the wave_stream contract) never reaches
  # the packed rootfs and assemble refuses with "dropped the overlay: rootfs is
  # missing etc/init.d/S99deepskopion-stream ..." (the fast path was inert —
  # DS-P2.149 landing finding). Merge the staged overlay into $STAGING/rootfs so
  # the fast path deterministically emits a daemon-carrying GPT image regardless
  # of whether the handed-in base rootfs already carried it.
  "$REPO_ROOT/sw/linux/stage_daemon.sh"
  log "merge daemon overlay into staging rootfs (fast-path bridge)"
  rsync -a "$REPO_ROOT/sw/linux/pfsoc/rootfs_overlay/" "$STAGE_ROOTFS/"
  python3 tools/pfsoc_boot_media.py assemble \
      --from "$STAGING" --out "$OUT" --hss-dir "$HSS_DIR"
else
  cat >&2 <<'EOF_REFUSE'
build_gpt_sdcard.sh: full mode is retired; this script only assembles from a prebuilt staging tree (--from <dir>).
To build the card image, follow docs/getting_started/polarfire_soc_discovery.md.
EOF_REFUSE
  exit 2
fi

# Verify + fingerprint the produced GPT image.
log "inspect (offline GPT/payload/rootfs layout)"
python3 tools/pfsoc_boot_media.py inspect "$OUT"
log "sha256"
sha256sum "$OUT"
echo
echo "built GPT sdcard.img: $OUT"
