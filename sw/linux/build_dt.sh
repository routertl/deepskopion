#!/usr/bin/env bash
# rr unknown-llm (AI-generated)
# build_dt.sh — DEPRECATED (retired 2026-05-25 by RTL-P3.592 + DS dt_nodes
# migration).
#
# This wrapper used to run `rr linux dtb` and then fdtput-patch a spidev child
# node under /axi/spi@e0006000 (DS-P3.48), sourcing fdtput from the buildroot
# host tree. That post-patch was invisible to anyone running a plain
# `rr linux dtb`, so every regen silently dropped spidev (it bit the
# RTL-P3.590 bench on 2026-05-25).
#
# The spidev child is now DECLARED in project.yml `linux.dt_nodes` and injected
# automatically by `rr linux dtb` (RTL-P3.581) — via fdtput when present, or a
# dtc round-trip when it isn't (RTL-P3.592). No wrapper, no buildroot-fdtput
# dependency, no silent drop. The legacy devicetree.dtb mirror is handled by
# `rr linux sdcard` staging.
#
# Just run:  rr linux dtb
set -euo pipefail
echo "build_dt.sh is DEPRECATED — spidev is now declared in project.yml" >&2
echo "linux.dt_nodes and injected by 'rr linux dtb' (RTL-P3.592)." >&2
echo "Forwarding to: rr linux dtb --refresh" >&2
exec rr linux dtb --refresh "$@"
