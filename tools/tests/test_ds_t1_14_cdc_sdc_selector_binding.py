"""DS-T1.14: the scoped rr_ddr_stream CDC constraint must still BIND to the RTL
the de25_datalogger build compiles — a silently unconstrained cross-clock path
is the exact silicon-only failure class this ticket investigated.

WHY THIS GUARD EXISTS
---------------------
DS-T1.12/T1.13 traced the rr_ddr_stream 0.3.2 write-plane wedge (COMMIT stuck at
0, no AW ever issued) that passes simulation but wedges on DE25 silicon. Step (4)
of DS-T1.14 reads the 0.3.2 ``.sta.rpt`` to adjudicate whether the metastability
synchronisers are TIMED as false paths or reported as normal — and the decisive
trap named there is:

    a 0-match ``get_registers`` glob is SILENTLY unconstrained, and Quartus then
    reports the crossing "met" as an ordinary same-edge path.

That is not hypothetical here. The 0.3.1 -> 0.3.2 repin RENAMED the CDC primitive
``sync_cdc`` -> ``ddr_sync_cdc`` (entity + file). The scoped SDC
(``ddr_stream_cdc_scoped.sdc``) selects the synchroniser flops by their INSTANCE
label glob ``*i_sync_cdc_*stages_r[0]`` and the internal signal ``stages_r`` — NOT
by entity name. The repin kept the instance labels (``i_sync_cdc_empty`` …) and
the ``stages_r`` signal, so the selector still binds; the RTL comments in
``ddr_stream_axi.vhd`` say the instance names were chosen deliberately to keep the
selector alive. But NOTHING guarded that: a future repin that renames those
instance labels or ``stages_r`` would orphan the selector, leave every
empty/full/busy/pointer synchroniser unconstrained, and reproduce a fresh
sim-green / silicon-wedge exactly like the one under investigation. The IP ships
its own drift guard (``tests/test_cdc_xdc_drift.py``) but that test is NOT vendored
into this consumer (``libs/routertl/rr_ddr_stream/`` contains only rtl/,
constraints/, ip.yml), so the deepskopion build had no equivalent.

WHAT IS ASSERTED, AT THE BUILD SURFACE
--------------------------------------
The assertions parse the ACTUAL SDC file the build feeds Quartus (wired in via
``ip.lock`` ``scoped_constraints: {kind: sdc, ref: ddr_stream_axi}``) and check its
``get_registers`` selectors against the ACTUAL tracked rr_ddr_stream RTL the build
compiles — the deployed surfaces, not a helper return value. If the SDC selector
and the RTL identifier drift apart, the crossing goes unconstrained and this test
fails BEFORE a bitstream is ever built.

POWER
-----
* BASELINE (this test vs the tree before it existed): the deepskopion build had
  NO guard binding the scoped-SDC selectors to the RTL — the ``sync_cdc`` rename
  could have orphaned ``*i_sync_cdc_*stages_r`` and shipped an unconstrained CDC
  with a green suite. This test closes that gap; its value is prospective, proven
  by the MUTATION below (a green run on today's tree is not itself a "catch").
* MUTATION (proves the guard has teeth): rename ``i_sync_cdc_`` -> ``i_syncX_`` in
  ``ddr_stream_axi.vhd`` (OR in ``ddr_fifo_intf.vhd``), or ``stages_r`` -> ``stages_q``
  in ``ddr_sync_cdc.vhd``, and the section-D binding assertion FAILS — that mutation
  is precisely a repin that silently unconstrains the empty/full/busy synchronisers.
  The instance-label assertion is PER COMPILED FILE, not corpus-wide: the AVMM
  backend TOP (``ddr_stream_avmm.vhd``) also carries ``i_sync_cdc_*`` labels but is
  NOT compiled by the AXI de25_datalogger build, so it is excluded from the corpus —
  otherwise renaming the labels in the compiled ``ddr_stream_axi.vhd`` alone would
  leave the guard green (the exact hole the LAND review caught).

The section-D CDC selectors and the pointer MCP selectors live entirely in the
git-TRACKED rr_ddr_stream sources, so this guard runs in any lane (fresh clone,
cron, CI) with no ``rr pkg install``. The section-A/B selectors reach into the
gitignored open-logic sources; those assertions are skipped with an explicit
message when open-logic is not materialised, rather than passing vacuously.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(
    os.environ.get("DE25_TEST_ROOT", Path(__file__).resolve().parents[2])
).resolve()

DDR = ROOT / "libs/routertl/rr_ddr_stream"
SDC = DDR / "constraints/ddr_stream_cdc_scoped.sdc"
DDR_RTL = DDR / "rtl"
OLO = ROOT / "libs/open-logic"

# The tracked rr_ddr_stream RTL the de25_datalogger build compiles. All
# section-D (CDC) and section-C/E (pointer/base MCP) selector anchors live here.
TRACKED_RTL_GLOB = "**/*.vhd"

# The de25_datalogger build has hardware.top_module deepskopion_de25_top, which
# instantiates the AXI backend TOP ``ddr_stream_axi``. rr_ddr_stream packages BOTH
# backend TOPs (ddr_stream_axi + ddr_stream_avmm); VHDL elaboration prunes the
# unused one, so the AVMM-only sources are NEVER in this build's compile set. They
# still carry ``i_sync_cdc_*`` labels, so including them in the corpus makes the
# section-D guard VACUOUS: a repin that renames the labels in the compiled AXI
# datapath (the DS-T1.12 wedge class) leaves the AVMM copy matching and the guard
# green. Exclude them so the corpus reflects what Quartus actually compiles.
# (Reproduced by the LAND reviewer: ``sed 's/i_sync_cdc_/i_syncX_/' ddr_stream_axi.vhd``
# left the pre-fix corpus-wide assertion green because ddr_stream_avmm.vhd matched.)
AVMM_ONLY_RTL = {"ddr_stream_avmm.vhd"}
AVMM_ONLY_DIRS = {"avmm"}

# The compiled AXI datapath files that carry the section-D CDC synchroniser
# instances (``i_sync_cdc_* : entity rr.ddr_sync_cdc``). Both are in the
# de25_datalogger AXI closure, so BOTH must individually keep the label — renaming
# the prefix in either one silently unconstrains that file's synchronisers.
AXI_CDC_INSTANCE_FILES = ("ddr_stream_axi.vhd", "ddr_fifo_intf.vhd")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _sdc_text() -> str:
    assert SDC.is_file(), f"scoped CDC SDC missing at {SDC} — the build feeds this to Quartus"
    return _read(SDC)


def _compiled_rtl_files() -> list[Path]:
    """The rr_ddr_stream RTL the de25_datalogger AXI build actually compiles.

    Drops the AVMM-backend-only sources (``ddr_stream_avmm.vhd`` and the ``avmm/``
    directory) — they are pruned by elaboration and must not prop up a selector the
    compiled AXI datapath has dropped.
    """
    out: list[Path] = []
    for f in sorted(DDR_RTL.glob(TRACKED_RTL_GLOB)):
        if f.name in AVMM_ONLY_RTL:
            continue
        if AVMM_ONLY_DIRS & set(f.relative_to(DDR_RTL).parts):
            continue
        out.append(f)
    assert out, f"no rr_ddr_stream RTL under {DDR_RTL} — it is git-tracked and must exist"
    return out


def _tracked_rtl_corpus() -> str:
    return "\n".join(_read(f) for f in _compiled_rtl_files())


def _get_registers_patterns(sdc: str) -> list[str]:
    """Every brace-delimited ``get_registers { ... }`` selector body in the SDC.

    Returns the raw inner text (may hold several space-separated globs).
    ``get_registers *`` (the section-D ``-from``) is intentionally excluded: it is
    an all-registers wildcard with no literal anchor to bind.
    """
    return re.findall(r"get_registers\s*\{([^}]*)\}", sdc)


def _literal_anchors(glob: str) -> list[str]:
    """Identifier fragments of a Quartus glob, with wildcards/index stripped.

    ``*i_sync_cdc_*stages_r[0]`` -> ['i_sync_cdc_', 'stages_r']
    ``*wr_ptr_addr_sys[*]``      -> ['wr_ptr_addr_sys']
    """
    body = re.sub(r"\[[^\]]*\]", "", glob.strip())  # drop [*] / [0]
    frags = [f for f in body.split("*") if f]
    # keep only real identifier fragments
    return [f for f in frags if re.fullmatch(r"[A-Za-z0-9_]+", f)]


# ── the load-bearing guard: section-D CDC selector still binds ──────────────


def test_section_d_cdc_synchroniser_selector_binds_to_tracked_rtl() -> None:
    """``*i_sync_cdc_*stages_r[0]`` must resolve to real instance labels + signal.

    This is the crossing the 0.3.1->0.3.2 repin renamed (``sync_cdc`` ->
    ``ddr_sync_cdc``). If a future repin renames the ``i_sync_cdc_*`` instances or
    the ``stages_r`` signal, this glob matches 0 registers, the empty/full/busy
    synchronisers go unconstrained, and Quartus reports the crossing "met" — the
    silicon-wedge class under investigation. MUTATION: rename ``i_sync_cdc_`` in the
    compiled ``ddr_stream_axi.vhd`` (or ``ddr_fifo_intf.vhd``), or ``stages_r`` in
    ``ddr_sync_cdc.vhd``, and this fails — the instance-label check is asserted per
    compiled file, so the excluded AVMM variant cannot mask a repin.
    """
    sdc = _sdc_text()
    corpus = _tracked_rtl_corpus()

    # The SDC really does select the synchronisers by this glob.
    assert "i_sync_cdc_" in sdc and "stages_r" in sdc, (
        "section-D selector text drifted out of the SDC — the CDC synchronisers "
        "would no longer be false-pathed"
    )

    # Instance-label anchor, PER COMPILED FILE. The `i_sync_cdc_* : entity` labels
    # live in ddr_stream_axi.vhd (write-plane wr_ptr/empty/full/busy synchronisers)
    # AND ddr_fifo_intf.vhd — both in the AXI closure. A corpus-wide match is not
    # enough: a repin that renames the labels in ONE compiled file orphans that
    # file's synchronisers while the other still matches. Assert each individually
    # so renaming the prefix in either file FAILS. (The AVMM variant, which also
    # carries the labels, is already excluded from the corpus — it is not compiled.)
    for name in AXI_CDC_INSTANCE_FILES:
        f = DDR_RTL / name
        assert f.is_file(), (
            f"{name} missing from the compiled AXI datapath — the section-D CDC "
            "instance carrier is gone"
        )
        inst = re.findall(r"(i_sync_cdc_\w+)\s*:\s*entity", _read(f))
        assert inst, (
            f"SDC selects *i_sync_cdc_*stages_r but NO `i_sync_cdc_* : entity` "
            f"instance exists in the compiled {name} — that file's CDC crossings are "
            "now SILENTLY UNCONSTRAINED (the DS-T1.12 silicon-wedge class)."
        )

    # Signal anchor: `stages_r` is the synchroniser shift register the glob indexes.
    assert re.search(r"\bstages_r\b", corpus), (
        "SDC selects *stages_r[0] but no `stages_r` signal exists in the compiled "
        "RTL — the synchroniser flops cannot be constrained."
    )

    # And the primitive those instances point at must still declare stages_r.
    cdc_prim = DDR_RTL / "support/ddr_sync_cdc.vhd"
    assert cdc_prim.is_file(), (
        "ddr_sync_cdc.vhd missing — the 0.3.2 CDC primitive the instances bind to"
    )
    assert re.search(r"\bstages_r\b", _read(cdc_prim)), (
        "ddr_sync_cdc no longer declares stages_r — the SDC index [0] is orphaned"
    )


def test_section_ce_pointer_and_base_mcp_selectors_bind() -> None:
    """Section-C base-addr MCP and section-E pointer MCP selectors bind to RTL.

    These false-path + net-delay-bound the clk_sys->clk_ddr address crossings.
    Section E may legitimately match 0 REGISTERS in a folded netlist, but the RTL
    SIGNAL names it selects must still exist (else the selector is dead text). A
    rename here unbounds the address crossing.
    """
    sdc = _sdc_text()
    corpus = _tracked_rtl_corpus()

    required_signals = [
        "rd_base_addr_sys",
        "wr_base_addr_sys",
        "rd_base_addr_meta",
        "wr_base_addr_meta",
        "rd_ptr_addr_sys",
        "wr_ptr_addr_sys",
        "rd_ptr_addr_ddr",
        "wr_ptr_addr_ddr",
    ]
    for sig in required_signals:
        assert sig in sdc, f"section-C/E selector for {sig} vanished from the SDC"
        assert re.search(rf"\bsignal\s+{sig}\b", corpus), (
            f"SDC selects *{sig} but no `signal {sig}` is declared in the compiled "
            f"rr_ddr_stream RTL — the address MCP crossing is unbounded."
        )


def test_section_b_reset_request_crossing_bounds_present() -> None:
    """DS-P2.129 / DDR-T1.3: section B of ddr_stream_cdc_scoped.sdc must BOUND
    the olo_base_cc_reset request crossings (RstALatch -> RstRqstA2B and
    RstBLatch -> RstRqstB2A) with set_net_delay and set_max_skew.

    A bare false path on the reset request crossing leaves routing delay and
    inter-bit skew unbounded. 55bb0af added set_net_delay -max and set_max_skew
    to enforce deterministic reset release.
    """
    sdc = _sdc_text()

    # Both reset request endpoints and launch latches must be defined
    assert "RstALatch" in sdc and "RstRqstA2B" in sdc, (
        "section-B RstA crossing missing from SDC"
    )
    assert "RstBLatch" in sdc and "RstRqstB2A" in sdc, (
        "section-B RstB crossing missing from SDC"
    )

    # Both crossings must have set_false_path, set_net_delay -max, and set_max_skew
    for prefix in ("_rr_ds_rstA", "_rr_ds_rstB"):
        from_var = f"${prefix}_from"
        to_var = f"${prefix}_to"

        assert re.search(
            rf"^[ \t]*set_false_path\s+-from\s+{re.escape(from_var)}\s+-to\s+{re.escape(to_var)}",
            sdc,
            re.MULTILINE,
        ), f"section-B missing set_false_path for {prefix}"

        assert re.search(
            rf"^[ \t]*set_net_delay\s+-from\s+{re.escape(from_var)}\s+-to\s+{re.escape(to_var)}\s+-max",
            sdc,
            re.MULTILINE,
        ), f"section-B missing set_net_delay -max for {prefix} (DDR-T1.3 bound)"

        assert re.search(
            rf"^[ \t]*set_max_skew\s+-from\s+{re.escape(from_var)}\s+-to\s+{re.escape(to_var)}",
            sdc,
            re.MULTILINE,
        ), f"section-B missing set_max_skew for {prefix} (DDR-T1.3 bound)"



def test_every_tracked_selector_anchor_resolves() -> None:
    """Blanket sweep: every literal anchor of every SDC ``get_registers`` glob
    resolves in either the tracked rr_ddr_stream RTL or (when materialised) the
    open-logic sources. Catches a rename this file did not enumerate by name.
    """
    sdc = _sdc_text()
    corpus = _tracked_rtl_corpus()

    olo_corpus = ""
    if OLO.is_dir():
        olo_corpus = "\n".join(_read(f) for f in sorted(OLO.glob("**/*.vhd")))

    # anchors that live in open-logic, not rr_ddr_stream (section A/B)
    OLO_ANCHORS = {"RegIn", "Reg0", "RstALatch", "RstBLatch", "RstRqstA2B", "RstRqstB2A", "i_rst_cc"}

    unresolved: list[str] = []
    skipped_olo: list[str] = []
    for pat in _get_registers_patterns(sdc):
        for glob in pat.split():
            for anchor in _literal_anchors(glob):
                if anchor in corpus:
                    continue
                if anchor in OLO_ANCHORS or anchor in olo_corpus:
                    if not olo_corpus:
                        skipped_olo.append(anchor)
                    elif anchor not in olo_corpus:
                        unresolved.append(anchor)
                    continue
                unresolved.append(anchor)

    assert not unresolved, (
        "scoped-CDC-SDC selector anchors with NO matching RTL identifier "
        f"(orphaned constraint -> unconstrained crossing): {sorted(set(unresolved))}"
    )
    if skipped_olo:
        pytest.skip(
            "open-logic not materialised (no `rr pkg install`); section-A/B anchors "
            f"not verified this lane: {sorted(set(skipped_olo))}"
        )
