"""DS-T1.15: the DE25 (Quartus) constraints dir must be Quartus-only.

MEASURED 2026-09-07 building a from-head ``de25_datalogger`` bitstream for the
DS-T1.15 silicon witness: the Fitter died with

    Error (332000): Unknown option: "-filter", or
      More than 1 positional argument specified: "-filter", "name =~ ..."
      "get_cells -hierarchical -filter {name =~ "*u_core*spi_edge_meta_r*"}"
    Critical Warning (332008): Read_sdc failed due to errors in the SDC file
      (file "../../../hw/sdc/pfsoc_timing.sdc" line 50)
    Error: Quartus Prime Fitter was unsuccessful.

``paths.constraints`` AUTO-DISCOVERS every ``*.sdc`` under the dir.  The DE25
(Quartus_Pro) target and the PolarFire SoC (Libero/Synplify) targets both
pointed ``paths.constraints`` at a FLAT shared ``hw/sdc/``.  When PF-H0/PF-T9
(DS-P2.124/132) dropped ``pfsoc_timing.sdc`` / ``pfsoc_ka_timing.sdc`` — which
use Vivado/Synplify ``get_cells -hierarchical -filter {...}`` — into that dir,
Quartus ``read_sdc`` in the DE25 fit choked on syntax it does not accept, and
the DE25 product bitstream fit was RED on main from that commit on (no DE25 fit
had run since DS-P1.45).  This is not a CAN defect; it just blocked the witness.

The fix scopes the DE25 target to ``hw/sdc/de25/`` (de25_timing.sdc uses Quartus
``get_registers``).  This guard fails if the DE25 constraints dir ever again
contains an SDC carrying the Vivado/Synplify token Quartus ``read_sdc`` rejects
— whether by widening the path back to the shared dir OR by dropping such a file
into the de25 dir.  It asserts on the FILES the build's auto-discovery actually
globs, i.e. the exact surface the Fitter reads, not a helper's opinion.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "de25_datalogger.yml"

# The precise token that broke Quartus read_sdc: a `get_*  ... -filter` — the
# Synopsys/Synplify/Vivado object-query filter form. Quartus SDC uses
# get_registers / get_pins with explicit collections, never `-filter {name =~}`.
_QUARTUS_HOSTILE = "-filter"


def _constraints_dir() -> Path:
    cfg = yaml.safe_load(TARGET.read_text())
    rel = cfg["paths"]["constraints"]
    return ROOT / rel


def test_de25_constraints_dir_is_scoped_not_the_shared_root():
    """The DE25 target must NOT glob the flat hw/sdc/ that also holds the
    PolarFire SoC (Synplify) SDCs — that flat dir is what leaked -filter."""
    cdir = _constraints_dir()
    assert cdir != (ROOT / "hw" / "sdc"), (
        "de25 paths.constraints points at the shared hw/sdc/ — it will "
        "auto-discover the pfsoc Synplify SDCs and the Quartus fit will die on "
        "read_sdc. Scope it to a Quartus-only subdir (hw/sdc/de25)."
    )
    assert cdir.is_dir(), f"de25 constraints dir {cdir} does not exist"


def test_every_de25_sdc_is_quartus_safe():
    """Every SDC the DE25 auto-discovery globs must be free of the Vivado/
    Synplify `-filter` object-query Quartus read_sdc rejects."""
    cdir = _constraints_dir()
    sdcs = sorted(cdir.glob("*.sdc"))
    assert sdcs, f"no SDC discovered under {cdir}"
    offenders = []
    for sdc in sdcs:
        text = sdc.read_text(errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.split("#", 1)[0]
            if _QUARTUS_HOSTILE in stripped and "get_" in stripped:
                offenders.append(f"{sdc.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "DE25 (Quartus) constraints carry Vivado/Synplify `get_* -filter` "
        "syntax that read_sdc rejects (DS-T1.15):\n  " + "\n  ".join(offenders)
    )


def test_pfsoc_synplify_sdc_is_not_in_the_de25_dir():
    """The specific files that caused the regression must not live where the
    DE25 build would glob them."""
    cdir = _constraints_dir()
    for leaked in ("pfsoc_timing.sdc", "pfsoc_ka_timing.sdc"):
        assert not (cdir / leaked).exists(), (
            f"{leaked} is inside the DE25 constraints dir {cdir} — it is a "
            "Libero/Synplify constraint and breaks the Quartus fit."
        )
