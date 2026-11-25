#!/usr/bin/env bash
# rr unknown-llm (AI-generated)
# DS-P2.231: generate the build-time VHDL package `rr lint` reads but no hook
# writes before it.
#
# hw/rtl/pkg/rom_info_pkg.vhd is a gitignored build output (pre_build
# regbank-regen and pre_sim rom-info-sim write it). `rr lint` runs no hooks, so
# on a fresh clone deepskopion_core.vhd and system_regbank_wrapper.vhd would
# name a unit that does not exist ("rom_info_pkg not found in library tich").
# The pre-commit gate and CI call this before `rr lint`; it writes the same file
# the hooks write (tools/regen_regbank.sh --rom-info-only), and the build and
# sim hooks overwrite it with their own stamp.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"
bash tools/regen_regbank.sh --rom-info-only \
    --output-dir hw/rtl/pkg --regbank-dir hw/rtl/regbank --library tich
