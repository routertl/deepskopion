# rr unknown-llm (AI-generated)
"""DS-P2.101 — regression guard for `entity work.<tich-unit>` library
mismatches that silently blocked every de25_datalogger Quartus bitstream build.

Two instances of the SAME defect class were found and fixed by this ticket:

  1. `deepskopion_core.vhd`'s `u_ring_watermark : entity work.ring_watermark`
     (landed by DS-P3.60, commit 9981d59) — the only instantiation in that
     file naming the literal library `work` instead of `tich`; every sibling
     internal instantiation (`u_emio_mux`, `u_datalogger_gather`, `u_regbank`,
     `u_stim_gen`, ...) uses `entity tich.<unit>` explicitly.
  2. `deepskopion_de25_top.vhd`'s `u_core : entity work.deepskopion_core`
     (pre-existing) — the only instantiation in that file naming `work` for
     a `tich`-library unit; the same file correctly uses
     `u_rea : entity rr_rea.rr_rea_intel` two lines later (DS-P2.168: the REA
     package compiles into its own library `rr_rea`, so that unit's literal
     library is `rr_rea`, not `tich`).

In a single-library NVC/GHDL sim compile this is harmless (VHDL's `work`
denotes "whatever library this file is analyzed into", and every file in a
sim compile order lands in the same library), so DS-P2.100's cocotb suite
never exercised the difference and stayed green. Quartus's multi-library
synthesis does NOT collapse work/tich this way: `tools/hooks`'s
DependencyResolver attributes a file to the literal library named in ITS
CONSUMER's `entity work.X` clause (RTL-P2.646) rather than resolving `work`
as "the consumer's own library", so `ring_watermark.vhd` landed in library
`work` while `deepskopion_core.vhd` landed in `tich` (and separately,
`deepskopion_core.vhd` landed in `tich` while `deepskopion_de25_top.vhd`
landed in `work`) — Quartus then fails elaboration with, e.g.:

    Error (13785): VHDL Use Clause error ... design library "tich" does not
    contain primary unit "ring_watermark"
    Error (13785): VHDL Use Clause error ... design library "altera_work"
    does not contain primary unit "deepskopion_core"

Both were discovered when DS-P2.101 queued the FIRST-EVER de25_datalogger
Quartus build to include DS-P3.60's ring_watermark and DS-P2.100's stimulus
generator together (`rr queue submit bitstream de25_datalogger`; jobs
b47cb8125b21 / 63831c296395 / 464d275f37d2) — real, silicon-build-blocking
regressions, not workaround targets.

This is a pure, no-simulation, line-anchored/regex structural test on the
PRODUCTION sources: it cannot invoke Quartus (no bench-independent way to
run Analysis & Synthesis in a unit test), so it instead asserts the exact
property that broke synthesis in both files — every internal
`entity <lib>.<unit>` instantiation of a `tich`-library module names the
literal library `tich`, never `work`.

BASELINE (measured before this ticket's fix, i.e. against
`entity work.ring_watermark` / `entity work.deepskopion_core`): FAILS —
these are the exact regressions the Quartus builds hit.
MUTATION (measured by reverting either fix back to `entity work.<unit>` and
rerunning): FAILS, proving the guard is not vacuous for either file.
Current code (post-fix, `entity tich.<unit>` in both places): PASSES.

Run:  pytest tools/tests/test_ds_p2_101_ring_watermark_library_guard.py
"""
from __future__ import annotations

import re
from typing import Dict, Tuple

import pytest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "hw" / "rtl" / "deepskopion_core.vhd"
TOP = ROOT / "hw" / "rtl" / "de25" / "deepskopion_de25_top.vhd"

# (file, {internal-unit-name -> must-be-instantiated}) — units this file
# instantiates that live in the `tich` library (as opposed to a library
# vendored/placed alongside the consumer itself, e.g. `work.axil_apb_shim`
# for deepskopion_de25_top.vhd, which is intentionally NOT `tich`).
_TICH_UNITS_BY_FILE: Dict[Path, Tuple[str, ...]] = {
    CORE: (
        "ring_watermark",
        "ds_emio_mux",
        "datalogger_gather",
        "edge_batcher_aggregator",
        "error_event_bank",
        "maxfreq_event_bank",
        "zs_msg_framer",
        "system_regbank_wrapper",
        "i2c_injector",
        "spi_injector",
        "ds_stimulus_gen",
    ),
    TOP: (
        "deepskopion_core",
    ),
}

# DS-P2.168: units a file instantiates from a package-owned library. The
# routertl/rea package's generated build-id output is locked to `rr_rea`, so the
# REA units cannot sit in `tich`; the literal must still match where they land.
_PACKAGE_UNITS_BY_FILE: Dict[Path, Dict[str, str]] = {
    TOP: {"rr_rea_intel": "rr_rea"},
}

_ENTITY_INSTANTIATION_RE = re.compile(
    r"entity\s+(\w+)\.(\w+)\b", re.IGNORECASE)


@pytest.mark.parametrize("path", [CORE, TOP], ids=lambda p: p.name)
def test_every_tich_entity_instantiation_names_the_tich_library(path):
    units = _TICH_UNITS_BY_FILE[path]
    text = path.read_text()
    found = {}
    for lib, unit in _ENTITY_INSTANTIATION_RE.findall(text):
        if unit in units:
            found[unit] = lib

    missing = [u for u in units if u not in found]
    assert not missing, (
        f"expected to find `entity <lib>.<unit>` instantiations for "
        f"{missing} in {path} — did the instance get renamed/removed?")

    wrong_library = {u: lib for u, lib in found.items() if lib != "tich"}
    assert not wrong_library, (
        "the following modules (all compiled into the `tich` library) are "
        f"instantiated in {path.name} with a literal library other than "
        f"'tich': {wrong_library!r}. Quartus's multi-library synthesis "
        "takes the named library literally (unlike a single-library "
        "NVC/GHDL sim compile, where 'work' silently aliases whatever "
        "library the file happens to land in) — a non-'tich' literal here "
        "(typically the VHDL convention `work`, used elsewhere in this "
        "same file to mean 'my own library') is exactly the DS-P2.101 "
        "defect class: 'design library \"tich\" does not contain primary "
        "unit \"<unit>\"' at Quartus elaboration.")


@pytest.mark.parametrize("path", sorted(_PACKAGE_UNITS_BY_FILE, key=str), ids=lambda p: p.name)
def test_every_package_entity_instantiation_names_its_package_library(path):
    expected = _PACKAGE_UNITS_BY_FILE[path]
    found = {unit: lib for lib, unit in _ENTITY_INSTANTIATION_RE.findall(path.read_text())
             if unit in expected}
    assert found == expected, (
        f"{path.name} must instantiate package units with their package library "
        f"literally (Quartus takes the named library as written): found {found!r}, "
        f"expected {expected!r}")
