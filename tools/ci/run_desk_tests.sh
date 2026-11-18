#!/usr/bin/env bash
# The desk tests public CI runs: no vendor tool, no hardware.
# Lightweight unit sims in the desk suite (test_edge_rle_sim_ds_p2_16,
# test_isolated_unit_sim_fresh_tree_p2_138) shell out to `rr sim run` under nvc;
# tools/ci/desk_test_exclusions.txt names what is left out and why.
set -euo pipefail
cd "$(dirname "$0")/../.."

for tool in rr nvc; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: $tool not on PATH; desk-checks requires rr and nvc (DS-T3.34)" >&2
        exit 1
    fi
done

deselect=()
# DS-P2.300: a checkout with bench/ (not exported) also leaves out the bench-only tests its own list names.
for list in tools/ci/desk_test_exclusions.txt bench/ci/desk_test_exclusions.txt; do
    [ -f "$list" ] || continue
    while read -r node _; do
        case "$node" in
            ''|'#'*) continue ;;
        esac
        deselect+=("--deselect=$node")
    done < "$list"
done

python -m pytest tools/tests sw/stream/tests tools/datalogger-builder/tests \
    sw/linux/tests \
    -q -p no:cacheprovider -m 'not vendor_tools' --tb=short "${deselect[@]}" "$@"
