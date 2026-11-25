#!/usr/bin/env bash
# rr unknown-llm (AI-generated)
# Regenerate the regbank artifacts deepskopion's build depends on.
#
# Runs the three routertl-side generators in dependency order with the
# project-canonical output paths + `--library tich`. Wired as a
# pre_build hook in project.yml so synth/sim never reads a stale tree.
#
# Companion to tools/datalogger-builder/datalogger_gather_gen.py.
#
# Why not `rr regbank generate`? That sanctioned CLI (RTL-P3.502) covers
# only steps 1+2 (manifest + types). The build-metadata pkg (step 3,
# gen_build_metadata.py) and the axi-lite entity (step 4, rr regbank
# axilite) are still discrete CLIs, so the orchestration here stays
# project-side. Revisit once `rr regbank generate` grows the metadata
# + entity stages.
#
# Per DS-P2.34, the config-tier hex (config_schema.hex encoding which
# dataloggers are baked into the build) is emitted by datalogger-builder
# directly — not by this script. Treated as project-side concern.

set -euo pipefail

usage() {
    cat <<'EOF'
usage: tools/regen_regbank.sh [--output-dir DIR] [--regbank-dir DIR]
                                [--library NAME] [--stage-stamp PATH]
                                [--rom-info-only]

--rom-info-only regenerates only rom_info_pkg.vhd, from the tracked regbank
memory images; it is the pre_sim generator (DS-P2.231: the file is a
gitignored build output, so a fresh clone has none until a hook writes it).

All inputs are repository-relative. Output directories may be redirected for
the DS-X2 scratch reconstruction check; generated VHDL still names the
canonical clone-relative memory images consumed by the design.
EOF
}

OUTPUT_DIR="hw/rtl/pkg"
REGBANK_DIR="hw/rtl/regbank"
LIBRARY="tich"
STAGE_STAMP=""
ROM_INFO_ONLY=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --output-dir)
            [ "$#" -ge 2 ] || { echo "ERROR: --output-dir needs a value" >&2; exit 2; }
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --regbank-dir)
            [ "$#" -ge 2 ] || { echo "ERROR: --regbank-dir needs a value" >&2; exit 2; }
            REGBANK_DIR="$2"
            shift 2
            ;;
        --library)
            [ "$#" -ge 2 ] || { echo "ERROR: --library needs a value" >&2; exit 2; }
            LIBRARY="$2"
            shift 2
            ;;
        --stage-stamp)
            [ "$#" -ge 2 ] || { echo "ERROR: --stage-stamp needs a value" >&2; exit 2; }
            STAGE_STAMP="$2"
            shift 2
            ;;
        --rom-info-only)
            ROM_INFO_ONLY=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --neutral-paths)
            # Kept as a no-op compatibility spelling after DS-X2. Outputs are
            # now clone-relative on every run, including normal builds.
            shift
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

# Resolve project root + the routertl SDK regbank-utils dir.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON="${PYTHON:-python3}"

# DS-X6 (DS-P1.37): the SDK generators are reached by MODULE import, never by
# a source-file path under `rr sdk-path`. The published routertl wheel ships
# them compiled (*.so), so a literal payload_encoder.py exists only on a
# source checkout — the old path check here is exactly what killed the first
# off-bench clean-clone build. tools/hooks/run_sdk_generator.py holds the one
# import-and-call bridge; this probe fails loudly if the SDK is absent.
SDK_GEN="$ROOT_DIR/tools/hooks/run_sdk_generator.py"
if ! "$PYTHON" -c "import sdk.generators.regbank.payload_encoder" 2>/dev/null; then
    echo "ERROR: routertl SDK not importable from this python3 — is routertl installed?" >&2
    echo "       (pip install routertl; a venv must be active if one was used.)" >&2
    exit 2
fi

case "$OUTPUT_DIR" in /*) PKG_DIR="$OUTPUT_DIR" ;; *) PKG_DIR="$ROOT_DIR/$OUTPUT_DIR" ;; esac
case "$REGBANK_DIR" in /*) REGBANK_OUT_DIR="$REGBANK_DIR" ;; *) REGBANK_OUT_DIR="$ROOT_DIR/$REGBANK_DIR" ;; esac

REGBANK_YAML="$ROOT_DIR/project_regbank.yml"
REGBANK_HEX="$REGBANK_OUT_DIR/regbank_schema.hex"
REGBANK_BIN="$REGBANK_OUT_DIR/regbank_schema.bin"
CONFIG_HEX="$REGBANK_OUT_DIR/config_schema.hex"
SYSTEM_REGBANK_PKG="$PKG_DIR/system_regbank_pkg.vhd"
ROM_INFO_PKG="$PKG_DIR/rom_info_pkg.vhd"

mkdir -p "$(dirname "$REGBANK_HEX")" "$PKG_DIR"

if [ "$ROM_INFO_ONLY" = 0 ]; then
# 1. Encode project_regbank.yml → regbank_schema.hex/.bin
echo "[regen_regbank] encoding regbank manifest..."
"$PYTHON" "$SDK_GEN" payload-encoder \
    "$REGBANK_YAML" \
    --hex "$REGBANK_HEX" \
    --bin "$REGBANK_BIN"

# 2. Generate system_regbank_pkg.vhd (constants/types) in library `tich`.
echo "[regen_regbank] generating system_regbank_pkg.vhd..."
"$PYTHON" "$SDK_GEN" regbank-pkg \
    "$REGBANK_YAML" "$SYSTEM_REGBANK_PKG" \
    --library "$LIBRARY"

else
    for img in "$REGBANK_HEX"; do
        [ -f "$img" ] || { echo "ERROR: --rom-info-only needs $img" >&2; exit 2; }
    done
fi

# 3. Refresh rom_info_pkg.vhd with build metadata (git hash, build date,
#    REGBANK_VERSION) + word counts from the hex files generated above.
#    The config-hex side stays empty until datalogger_gather_gen.py learns
#    to emit it — gen_build_metadata reports C_CONFIG_WORDS = 0 in the
#    interim, which is truthful, not a lie.
echo "[regen_regbank] regenerating rom_info_pkg.vhd..."
#    RTL-P2.653: pass our real design-source dirs so the baked dirty bit
#    reflects uncommitted edits to hw/rtl + libs. Without these the
#    generator would scan the whole worktree (still honest, but noisier);
#    with them the stamp's dirty bit tracks exactly the synthesised RTL.
METADATA_TMP="$(mktemp -d)"
AXILITE_TMP="$(mktemp -d)"
cleanup() {
    rm -rf "$METADATA_TMP" "$AXILITE_TMP"
}
trap cleanup EXIT

"$PYTHON" "$SDK_GEN" build-metadata \
    "$ROOT_DIR" \
    --language vhdl \
    --output-dir "$METADATA_TMP" \
    --regbank-yml "$REGBANK_YAML" \
    --regbank-hex "$REGBANK_HEX" \
    --config-hex  "$CONFIG_HEX" \
    --dirty-path  hw/rtl \
    --dirty-path  libs

# DS-X2 / DS-P2.156: install a clone-relative candidate and preserve the first
# stage's explicitly-declared volatile build fields only against a same-build
# stage stamp, never against the committed file.
INSTALL_STAGE_STAMP_ARGS=()
if [ -n "$STAGE_STAMP" ]; then
    INSTALL_STAGE_STAMP_ARGS+=(--stage-stamp "$STAGE_STAMP")
fi
"$PYTHON" "$ROOT_DIR/tools/generated_source_contract.py" install \
    --kind rom-info \
    --candidate "$METADATA_TMP/rom_info_pkg.vhd" \
    --output "$ROM_INFO_PKG" \
    "${INSTALL_STAGE_STAMP_ARGS[@]}"

if [ "$ROM_INFO_ONLY" = 1 ]; then
    echo "[regen_regbank] done (rom_info_pkg.vhd only)."
    exit 0
fi

# 4. AXI-Lite regbank entity. RTL-P3.503 added --entity-name so the
#    post-hoc sed rename is gone; tich.system_regbank lines up with
#    the system_regbank_pkg naming.
AXILITE_VHD="$REGBANK_OUT_DIR/system_regbank.vhd"
AXILITE_CANDIDATE="$AXILITE_TMP/system_regbank.vhd"
echo "[regen_regbank] generating system_regbank.vhd..."
rr regbank axilite "$REGBANK_YAML" \
    -o "$AXILITE_CANDIDATE" \
    -l "$LIBRARY" \
    --entity-name system_regbank >/dev/null
"$PYTHON" "$ROOT_DIR/tools/generated_source_contract.py" install \
    --kind axilite \
    --candidate "$AXILITE_CANDIDATE" \
    --stamp-candidate "$AXILITE_CANDIDATE.producer.json" \
    --output "$AXILITE_VHD"

echo "[regen_regbank] done."
