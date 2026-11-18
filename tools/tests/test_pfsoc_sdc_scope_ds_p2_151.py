"""PF-F0 (DS-P2.151): the PolarFire SoC (Libero) fit must associate a
Libero-loadable SDC set — and ONLY it.

MEASURED 2026-09-20 running the first SDC-ASSOCIATED timing-driven fit for
``pfsoc_datalogger`` (``rr synth run`` → ``rr synth check`` → PLACEROUTE /
VERIFYTIMING, Libero SoC 2025.2). Two distinct walls, both invisible to PF-H0
(which only ever ran SYNTHESIZE, where a bad SDC line is a NON-fatal warning):

  1. LEAK. ``paths.constraints`` auto-discovers every ``*.sdc`` under the dir,
     RECURSIVELY. The pfsoc datalogger pointed it at the flat shared ``hw/sdc/``,
     which also holds the DE25 Quartus SDC (``hw/sdc/de25/de25_timing.sdc`` —
     ``get_registers`` / ``get_collection_size``) and the known-answer image's
     ``pfsoc_ka_timing.sdc``. Associated with Libero SmartTime they abort the
     run (SDC0061 "Unknown parameter"/"invalid command name", SDC0001). This is
     the exact symmetric twin of the DE25 leak fixed in DS-T1.15 — the shared
     flat ``hw/sdc/`` is the trap BOTH legs fall into.

  2. DIALECT. ``pfsoc_timing.sdc`` itself was written in the Vivado accessor
     form ``get_cells -hierarchical -filter {name =~ "..."}``. Libero SmartTime
     ``get_cells`` has no ``-filter`` parameter (SDC0061) — it takes a positional
     wildcard NAME pattern. Left unparsed, the async capture-source CDC arcs the
     file exists to declare simply never load.

The fix scopes the datalogger to ``hw/sdc/pfsoc/`` and re-expresses the arcs in
the Libero positional form. These guards assert on the FILES the build's
auto-discovery actually globs (the exact surface Libero ``read_sdc`` /
``organize_tool_files`` reads), not a helper's opinion — so widening the path
back to the shared root OR reintroducing a Vivado/Quartus accessor is caught.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"

# Accessor tokens Libero SmartTime `read_sdc` / SmartTime rejects. `-filter` and
# `-hierarchical` are the Vivado/Synplify object-query form; `get_registers` and
# `get_collection_size` are the Quartus idiom (both live in de25_timing.sdc, the
# file the flat-dir leak dragged in). Matched only on a non-comment `get_*`/query
# line so an SDC comment mentioning one is not a false positive.
_LIBERO_HOSTILE_TOKENS = ("-filter", "-hierarchical", "get_registers",
                          "get_collection_size")

# Receipt intended-assertion marker. Assembled by concatenation so the contiguous
# string exists NOWHERE in this source file — a pytest traceback echoes test
# source into stdout, and a source-echoed marker would let a wrong-reason red
# still satisfy a fort_test_power intended-assertion match. Only a genuine
# hostile-SDC failure message carries it.
_LEAK_MARKER = "pfsoc fit associates a non-Libero" + "-dialect SDC"


def _target_cfg() -> dict:
    return yaml.safe_load(TARGET.read_text(encoding="utf-8"))


def _constraints_dir() -> Path:
    return ROOT / _target_cfg()["paths"]["constraints"]


def _associated_sdcs() -> list[Path]:
    """The SDC set the Libero fit associates: every *.sdc discovered RECURSIVELY
    under paths.constraints (auto_sources), UNION the explicit sources.sdc — the
    same union gen_project imports and synth_check.tcl globs from the project
    constraint dir."""
    cfg = _target_cfg()
    found: set[Path] = set()
    cdir = ROOT / cfg["paths"]["constraints"]
    if cdir.is_dir():
        found.update(p.resolve() for p in cdir.rglob("*.sdc"))
    for rel in (cfg.get("sources", {}) or {}).get("sdc", []) or []:
        p = (ROOT / rel).resolve()
        if p.is_file():
            found.add(p)
    return sorted(found)


def _hostile_lines(sdc: Path) -> list[str]:
    hits = []
    for i, line in enumerate(sdc.read_text(errors="replace").splitlines(), 1):
        code = line.split("#", 1)[0]
        if "get_" not in code and "sizeof_collection" not in code:
            continue
        for tok in _LIBERO_HOSTILE_TOKENS:
            if tok in code:
                hits.append(f"{sdc.relative_to(ROOT)}:{i}: {line.strip()}")
                break
    return hits


def test_pfsoc_constraints_dir_is_scoped_not_the_shared_root() -> None:
    """paths.constraints must NOT glob the flat hw/sdc/ that also holds the
    DE25 Quartus SDC and the known-answer SDC — that flat dir is the leak."""
    cdir = _constraints_dir()
    assert cdir != (ROOT / "hw" / "sdc"), (
        f"{_LEAK_MARKER}: pfsoc paths.constraints points at the shared hw/sdc/ "
        "— it recursively auto-discovers the DE25 Quartus SDC (get_registers) "
        "and the KA SDC, and the Libero timing run aborts on read_sdc. Scope it "
        "to the Libero-only subdir (hw/sdc/pfsoc)."
    )
    assert cdir.is_dir(), f"pfsoc constraints dir {cdir} does not exist"


def test_every_associated_sdc_is_libero_loadable() -> None:
    """Every SDC the pfsoc fit associates must be free of the Vivado/Quartus
    accessor tokens Libero SmartTime rejects (SDC0061/SDC0001)."""
    sdcs = _associated_sdcs()
    assert sdcs, "no SDC discovered for the pfsoc target"
    offenders: list[str] = []
    for sdc in sdcs:
        offenders.extend(_hostile_lines(sdc))
    assert not offenders, (
        f"{_LEAK_MARKER} — the fit would associate SDC line(s) carrying a "
        "Vivado/Quartus accessor Libero read_sdc rejects (PF-F0 / DS-P2.151):\n  "
        + "\n  ".join(offenders)
    )


def test_pfsoc_timing_sdc_constrains_the_design_not_vacuously() -> None:
    """Anti-vacuity: the Libero-dialect pfsoc_timing.sdc must actually create a
    clock and declare the async capture arcs (spi/i2c/uart) — an SDC that parses
    but constrains nothing is the 'unconstrained-but-met' trap PF-F0 must fail
    on. The CAN arc is deliberately absent (CAN-free target: an empty
    set_false_path collection is a HARD SDC0023 error in Libero SmartTime)."""
    sdc_rels = (_target_cfg().get("sources", {}) or {}).get("sdc", []) or []
    assert sdc_rels, "pfsoc target declares no sources.sdc"
    code = _tcl_code(ROOT / sdc_rels[0])
    assert "create_clock" in code and "ref_clk_50mhz" in code, (
        "pfsoc_timing.sdc must create the ref_clk_50mhz source clock"
    )
    capture_selector = (
        "set_false_path -to [get_cells "
        "{*u_core*u_datalogger_gather*i_cond*meta_r*}]"
    )
    assert capture_selector in code, (
        "pfsoc_timing.sdc lost the implemented gather async-capture "
        "false-path arc — the CDC metastability hazard would be unconstrained"
    )
    # The unconditional CAN arc would SDC0023 on this CAN-free netlist.
    assert "set_false_path -to [get_cells {*u_core*can_edge_meta_r*}]" not in code, (
        "an unconditional CAN false-path arc is present, but this target is "
        "CAN-free — Libero set_false_path -to <empty> is a hard SDC0023 error"
    )


def test_pfsoc_derives_component_timing_after_the_mss_builder() -> None:
    """The CCC-generated clk_sys is only TIMED when derive_constraints_sdc runs
    (and associates the component SDCs). It must be a gen_project hook ordered
    AFTER the MSS builder (which generates the CCC/MSS components it reads).
    Without it the fit is 78394/78401 setup-UNconstrained — 'timing met' is
    vacuous (PF-P0 §6)."""
    hooks = ((_target_cfg().get("tcl_hooks", {}) or {}).get("gen_project", []) or [])
    derive = "hw/libero/pfsoc/pfsoc_derive_timing.tcl"
    builder = "hw/libero/pfsoc/pfsoc_mss_subsys.tcl"
    assert derive in hooks, (
        f"{_LEAK_MARKER}: the gen_project flow does not run {derive} — the "
        "CCC-generated clk_sys stays unconstrained and any timing signoff is "
        "vacuous (PF-F0 / DS-P2.151)"
    )
    assert builder in hooks and hooks.index(derive) > hooks.index(builder), (
        f"{derive} must run AFTER {builder} (it reads the component SDCs the "
        "MSS builder generates)"
    )
    assert (ROOT / derive).is_file()


def _tcl_code(path: Path) -> str:
    """The Tcl source with `#`-comment tails stripped, so a token mentioned only
    in a comment does not satisfy (or trip) a content assertion below."""
    return "\n".join(line.split("#", 1)[0] for line in
                     path.read_text(errors="replace").splitlines())


def test_pfsoc_derive_hook_actually_derives_and_does_not_swallow_failure() -> None:
    """Wall 3, on the ALWAYS-ON path (the DS-P2.151 land-refusal fix).

    Listing the hook is not enough: the mutants the earlier receipt missed are a
    derive hook replaced by a NO-OP, or one that ``catch``-es
    ``derive_constraints_sdc`` and only ``puts`` a warning (the shipped bug) —
    both leave clk_sys UNCONSTRAINED yet let gen_project (and the fit) proceed to
    a vacuous "timing met". A skip-gated bench coverage check cannot guard the
    default suite. So we assert on the hook's own source:

      * it must actually invoke ``derive_constraints_sdc`` (a no-op hook fails); and
      * if it ``catch``-es that call, the failure path must ABORT — re-raise via
        ``error`` / ``return -code error`` — never merely ``puts`` and fall
        through (a swallowing hook fails).

    This goes RED on origin/main (no hook at all), RED on a no-op hook, and RED
    on the pre-fix swallow-and-warn hook — the three wall-3 mutants."""
    hook = ROOT / "hw" / "libero" / "pfsoc" / "pfsoc_derive_timing.tcl"
    assert hook.is_file(), f"the wall-3 derive hook {hook} is missing"
    code = _tcl_code(hook)
    assert "derive_constraints_sdc" in code, (
        f"{_LEAK_MARKER}: {hook.name} does not call derive_constraints_sdc — a "
        "no-op/replaced hook leaves the CCC clk_sys unconstrained and 'timing "
        "met' vacuous (PF-F0 / DS-P2.151)"
    )
    if "catch" in code:
        aborts = ("error" in code) or ("return -code error" in code) or ("exit 1" in code)
        assert aborts, (
            f"{_LEAK_MARKER}: {hook.name} catches derive_constraints_sdc but "
            "never aborts (it swallows the failure with only a puts) — a failed "
            "derive would leave clk_sys UNCONSTRAINED and the fit would proceed "
            "to a vacuous timing signoff (PF-P0 §6). The failure path must "
            "re-raise (error / return -code error / exit 1)."
        )


# NOTE: the end-to-end Libero fit (which SKIPS where Libero is absent) lives in
# tools/tests/test_pfsoc_fit_e2e_ds_p2_151.py — deliberately a SEPARATE file so
# THIS suite stays skip-free (the fort_test_power affected-suite must be green
# with no skip/xfail).
