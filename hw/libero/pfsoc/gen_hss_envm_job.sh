#!/usr/bin/env bash
# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
#
# DS-P2.145 (PF-L0e) — build the HSS eNVM-client programming job (the missing
# artifact every prior attempt named).
#
# WHY THIS EXISTS
# ---------------
# The PolarFire SoC E51 boot vector is eNVM ALWAYS (microchip-pfsoc-bringup). To
# make the E51 boot OUR Hart Software Services instead of the factory diagnostic,
# HSS must be written into eNVM. `rr program pfsoc hss-envm` drives the FlashPro
# `fpgenprog` backend against a programming job that embeds an **HSS eNVM client**
# — and the only staged job (out/pfsoc_mss.job, DS-P2.142/PF-L0c) carries Fabric +
# sNVM Stage-1 DDR-init ONLY, NO eNVM component (proven: Libero's
# `export_prog_job -bitstream_file_components` couples FABRIC+SNVM and cannot emit
# an eNVM component — PF-L0c Finding 2; and there is no `configure_envm` Designer
# TCL verb on this install). So the fabric/Libero export can NEVER carry eNVM.
#
# THE SUPPORTED HEADLESS PATH is the FlashPro `fpgenprog` programming-project
# builder (subcommands: new_project / envm_client / mss_boot_info /
# generate_bitstream / run_action). It builds a standalone PDB+PPD that "programs
# eNVM" — independent of the fabric Libero flow. This is the same engine
# `_LivePfsocEnvmTransport` resolves for `rr program pfsoc hss-envm`.
#
# WHAT THIS PRODUCES
# ------------------
# A programming project at $OUT_DIR (default: build/pfsoc_hss_envm) whose
# proj_fp/target.ppd programs the HSS eNVM-WRAPPER image
# (sw/linux/pfsoc/out/pfsoc_datalogger/hss-envm-wrapper.bin — the upstream HSS
# build's boot-mode-1 image, linked at eNVM page 1 = 0x20220100, which inflates a
# DEFLATE-compressed HSS from eNVM to L2-LIM at runtime) into eNVM as a
# data-storage client at eNVM page 1, in MSS boot mode 1 (non-secure boot from
# eNVM — the sanctioned HSS deployment mode; boot mode 0 is IDLE boot and does not
# execute eNVM). The 5 hart reset vectors (U_MSS_BOOTCFG) are set to the wrapper
# entry 0x20220100 via mss_boot_info. Program it on the bench with:
#   fpgenprog run_action --location <OUT>/project --action PROGRAM
# (see hw/libero/pfsoc/mss/README.md "HSS eNVM-client job" section).
#
# It is deterministic given the same hss.bin; the eNVM component bitstream digest
# is pinned in the sidecar (out/pfsoc_hss_envm_job.digest) and the guard test
# tools/tests/test_pfsoc_hss_envm_job_ds_p2_145.py.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
LIBERO="${LIBERO:-/opt/microchip/libero_soc/Libero_SoC}"
FP="$LIBERO/Designer/bin64/fpgenprog"
# BM1 CONTRACT (DS-P2.145 wrapper iteration): the E51 boots MODE1 (non-secure
# boot from eNVM). The image MUST be the HSS eNVM-WRAPPER (linked at eNVM page 1
# = 0x20220100; upstream build/hss-envm-wrapper.bin), NOT the raw reset trampoline.
# The prior attempt programmed the wrapper bytes at page 0 with bootmode 0 (idle);
# because the wrapper is LINKED for 0x20220100 its absolute references were then
# 256 B (one eNVM page) low, so the E51 trapped Illegal-Instruction at first fetch
# (silicon-proven, docs/hardware/pf-l0e/evidence/hss_envm_boot_illegal_instr_2026-09-15.txt).
HSS_BIN="${HSS_BIN:-$REPO_ROOT/sw/linux/pfsoc/out/pfsoc_datalogger/hss-envm-wrapper.bin}"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/build/pfsoc_hss_envm}"
DIE="${DIE:-MPFS095T}"
PACKAGE="${PACKAGE:-FCSG325}"
CLIENT_NAME="${CLIENT_NAME:-hss_boot}"
# MODE1 boot: the eNVM-wrapper is linked at page 1 (0x20220100), and eNVM page 0
# is reserved. All 5 harts are vectored to the wrapper entry; the wrapper itself
# marshals the U54s. START_PAGE 1 places the wrapper's _start exactly at the
# reset-vector address, so absolute references resolve correctly (fixing the trap).
START_PAGE="${START_PAGE:-1}"
BOOT_MODE="${BOOT_MODE:-1}"
BOOT_ENTRY="${BOOT_ENTRY:-0x20220100}"   # eNVM page 1 (wrapper ENTRY / link base)

# Libero licence (the bench-wide LM_LICENSE_FILE is Altera-only; add 1702@).
export LM_LICENSE_FILE="${LM_LICENSE_FILE:-1702@localhost}"   # DS-P2.360: set LM_LICENSE_FILE to your Libero licence server (port@host)

if [[ ! -x "$FP" ]]; then
    echo "ERROR: fpgenprog not found at $FP (set LIBERO=...)" >&2
    exit 2
fi
if [[ ! -f "$HSS_BIN" ]]; then
    echo "ERROR: HSS eNVM-wrapper image not found: $HSS_BIN" >&2
    echo "       Build it first (rr linux hss produces build/hss-envm-wrapper.bin;" >&2
    echo "       the firmware-leg wrapper stages it to out/pfsoc_datalogger/)." >&2
    exit 2
fi

# Compute the 20-byte U_MSS_BOOTCFG for MODE1: 5 hart reset vectors, each the
# wrapper entry as a 4-byte BIG-ENDIAN hex word. fpgenprog stores the hex-string
# bytes verbatim and the System Controller reads each 4-byte reset vector
# big-endian — VERIFIED on silicon: a little-endian-packed 0x20220100 (hex
# 00012220) made the E51 boot to 0x00012220 (the bytes read big-endian). So the
# hex must be the address in natural order (20220100), repeated for all 5 harts.
BOOTCFG="$(python3 -c "import sys; v=int('${BOOT_ENTRY}',16); sys.stdout.write(('%08x' % v)*5)")"

NB="$(stat -c%s "$HSS_BIN")"
# The fpgenprog project folder ($PROJ) must be EMPTY at new_project time, so the
# Intel-HEX (and any sidecar input) lives in $OUT_DIR, one level ABOVE $PROJ.
PROJ="$OUT_DIR/project"
HEX="$OUT_DIR/hss.hex"

echo "== DS-P2.145 HSS eNVM-client job =="
echo "   HSS image : $HSS_BIN ($NB bytes, sha256 $(sha256sum "$HSS_BIN" | cut -d' ' -f1))"
echo "   part      : $DIE-$PACKAGE   client: $CLIENT_NAME"
echo "   boot mode : $BOOT_MODE (non-secure boot from eNVM)   start_page: $START_PAGE"
echo "   bootcfg   : $BOOTCFG (5x $BOOT_ENTRY reset vectors)"
echo "   out       : $OUT_DIR"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"   # $PROJ is created empty by new_project (must not pre-exist)

# 1. wrapper .bin (raw binary) -> Intel HEX. fpgenprog's content_file formats are
#    {intel-hex, microsemi-binary-8-16-32, motorola-s, simple-hex};
#    microsemi-binary-8-16-32 is an ASCII 0/1 word format (NOT raw bytes), so we
#    use intel-hex, base 0 (the client is placed at eNVM page $START_PAGE by the
#    tool; base_addr 0 = start of the client's user data).
python3 "$REPO_ROOT/hw/libero/pfsoc/bin2intelhex.py" "$HSS_BIN" "$HEX"

# 2. New standalone FlashPro programming project for THIS part.
"$FP" --subcommand=new_project --location "$PROJ" \
      --target_die "$DIE" --target_package "$PACKAGE"

# 3. Add the HSS eNVM data-storage client at page $START_PAGE (page 1 for MODE1:
#    page 0 is reserved and the wrapper is linked at page 1 = $BOOT_ENTRY).
"$FP" --subcommand=envm_client --location "$PROJ" \
      --number_of_bytes "$NB" \
      --content_file_format intel-hex --content_file "$HEX" \
      --start_page "$START_PAGE" --client_name "$CLIENT_NAME" \
      --mem_file_base_address 00000000

# 4. MSS boot mode $BOOT_MODE (non-secure boot from eNVM) with the 5 hart reset
#    vectors (U_MSS_BOOTCFG) set to the wrapper entry $BOOT_ENTRY. This is what
#    vectors the E51 at reset to the wrapper's _start (matching its link address),
#    the fix for the prior MODE0/page-0 illegal-instruction trap.
"$FP" --subcommand=mss_boot_info --location "$PROJ" \
      --u_mss_bootmode "$BOOT_MODE" --u_mss_bootcfg "$BOOTCFG"

# 5. Generate the PPD programming data (this is where "file programs eNVM" prints
#    and the eNVM component bitstream digest is emitted).
"$FP" --subcommand=generate_bitstream --location "$PROJ" --format PPD

PPD="$PROJ/proj_fp/target.ppd"
echo "== generated: $PPD =="
echo "   ppd sha256: $(sha256sum "$PPD" | cut -d' ' -f1)"
echo "   ppd size  : $(stat -c%s "$PPD")"
grep -E 'eNVM component bitstream digest|Entire bitstream digest' "$PROJ/proj_fp/target_ppd.digest" || true
echo "== To program on the bench (board via ANEL outlet 6, board OFF after): =="
echo "   $FP --subcommand=run_action --location $PROJ --action PROGRAM"
