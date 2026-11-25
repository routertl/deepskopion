#!/usr/bin/env bash
# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
#
# DS-P2.145 (PF-L0e) — build the FACTORY-RESTORE eNVM programming job.
#
# WHY THIS EXISTS
# ---------------
# The OWNER DECISION (differential-against-factory) requires RESTORING the factory
# diagnostic eNVM image from the committed backup so its known-good banner can be
# captured on MMUART0 and its live register state snapshotted as the golden
# reference. `rr program pfsoc hss-envm --factory-recovery --factory-backup <bin>`
# treats the backup only as a *restorability guarantee check* (the backup must
# exist on disk) — the bytes it actually writes come from the resolved Libero
# programming JOB, NOT from the backup binary. So there is no built-in "flash these
# backup bytes back" path; this generator builds exactly that job.
#
# The factory backup is a raw 128 KiB dump of the eNVM window at 0x20220000
# (docs/hardware/pf-l0e/evidence/envm_backup_0x20220000_128k_2026-09-15.bin,
# sha256 eace05c6…). Its structure (measured):
#   * offset 0x000: `6f 00 00 10` = RISC-V `j 0x100` (reset trampoline at page 0)
#   * offset 0x008: 5x reset vectors, each `00 01 22 20` = 0x20220100 (LE in image)
#   * offset 0x100: the diagnostic code (page 1)
# So it is a page-0 image booted in MSS boot mode 1 (non-secure boot from eNVM),
# with the 5 hart reset vectors at the diagnostic entry 0x20220100.
#
# This restores the factory image FAITHFULLY: full 128 KiB as the eNVM client at
# page 0, boot mode 1, reset vectors 0x20220100 (matching the image's own).
#
# Deterministic given the same backup binary. Uses the SAME fpgenprog builder as
# gen_hss_envm_job.sh (new_project / envm_client / mss_boot_info /
# generate_bitstream / run_action) — the sanctioned FlashPro programming path.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
LIBERO="${LIBERO:-/opt/microchip/libero_soc/Libero_SoC}"
FP="$LIBERO/Designer/bin64/fpgenprog"

# The committed factory eNVM backup (the FULL 128 KiB image, page 0).
FACTORY_BIN="${FACTORY_BIN:-$REPO_ROOT/docs/hardware/pf-l0e/evidence/envm_backup_0x20220000_128k_2026-09-15.bin}"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/build/pfsoc_factory_restore}"
DIE="${DIE:-MPFS095T}"
PACKAGE="${PACKAGE:-FCSG325}"
CLIENT_NAME="${CLIENT_NAME:-factory_diag}"
# The factory image is a page-0 image (reset trampoline `j 0x100` lives at the
# eNVM base). Restore the WHOLE image at page 0; boot mode 1 with the image's own
# reset vectors (0x20220100).
START_PAGE="${START_PAGE:-0}"
BOOT_MODE="${BOOT_MODE:-1}"
BOOT_ENTRY="${BOOT_ENTRY:-0x20220100}"   # the factory image's own 5 hart vectors

export LM_LICENSE_FILE="${LM_LICENSE_FILE:-1702@localhost}"   # DS-P2.360: set LM_LICENSE_FILE to your Libero licence server (port@host)

if [[ ! -x "$FP" ]]; then
    echo "ERROR: fpgenprog not found at $FP (set LIBERO=...)" >&2
    exit 2
fi
if [[ ! -f "$FACTORY_BIN" ]]; then
    echo "ERROR: factory eNVM backup image not found: $FACTORY_BIN" >&2
    exit 2
fi

BOOTCFG="$(python3 -c "import sys; v=int('${BOOT_ENTRY}',16); sys.stdout.write(('%08x' % v)*5)")"

NB="$(stat -c%s "$FACTORY_BIN")"
PROJ="$OUT_DIR/project"
HEX="$OUT_DIR/factory.hex"

echo "== DS-P2.145 FACTORY-RESTORE eNVM job =="
echo "   factory image : $FACTORY_BIN ($NB bytes, sha256 $(sha256sum "$FACTORY_BIN" | cut -d' ' -f1))"
echo "   part          : $DIE-$PACKAGE   client: $CLIENT_NAME"
echo "   boot mode     : $BOOT_MODE (non-secure boot from eNVM)   start_page: $START_PAGE"
echo "   bootcfg       : $BOOTCFG (5x $BOOT_ENTRY reset vectors)"
echo "   out           : $OUT_DIR"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

python3 "$REPO_ROOT/hw/libero/pfsoc/bin2intelhex.py" "$FACTORY_BIN" "$HEX"

"$FP" --subcommand=new_project --location "$PROJ" \
      --target_die "$DIE" --target_package "$PACKAGE"

"$FP" --subcommand=envm_client --location "$PROJ" \
      --number_of_bytes "$NB" \
      --content_file_format intel-hex --content_file "$HEX" \
      --start_page "$START_PAGE" --client_name "$CLIENT_NAME" \
      --mem_file_base_address 00000000

"$FP" --subcommand=mss_boot_info --location "$PROJ" \
      --u_mss_bootmode "$BOOT_MODE" --u_mss_bootcfg "$BOOTCFG"

"$FP" --subcommand=generate_bitstream --location "$PROJ" --format PPD

PPD="$PROJ/proj_fp/target.ppd"
echo "== generated: $PPD =="
echo "   ppd sha256: $(sha256sum "$PPD" | cut -d' ' -f1)"
echo "   ppd size  : $(stat -c%s "$PPD")"
grep -E 'eNVM component bitstream digest|Entire bitstream digest' "$PROJ/proj_fp/target_ppd.digest" 2>/dev/null || true
echo "== To program on the bench (board via ANEL outlet 6, board OFF after): =="
echo "   $FP --subcommand=run_action --location $PROJ --action PROGRAM"
