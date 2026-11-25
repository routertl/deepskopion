# rr unknown-llm (AI-generated)
"""DS-T1.19 — DE25 board and JTAG clock contract evidence and regression guards.

Audit (DS-P1.72):
1. Board master.sdc creates CLOCK0_50/CLOCK1_50 on ports the design lacks (TMC-20026),
   and CLOCK2_50 is duplicated by hw/sdc/de25/de25_timing.sdc:12.
2. REA's altera_reserved_tck clocks real registers without a signoff clock; a
   Fitter-only default_jtag.sdc is not signoff evidence.

Resolution:
- Board file half (RTL-P1.175): boards/de25-standard/master.sdc wraps each
  create_clock in a port-existence check (`if {[get_collection_size [get_ports -nowarn ...]] > 0}`).
  deepskopion_de25_top instantiates CLOCK0_50 and CLOCK2_50; absent CLOCK1_50 emits nothing.
- Project clock half: hw/sdc/de25/de25_timing.sdc guards CLOCK2_50 creation via
  `if {[get_collection_size $_ds_ref_clocks] == 0}`, eliminating duplicate clock warnings.
- Package-owned REA JTAG constraint: routertl/rea 1.12.0 package provides
  libs/routertl/rea/constraints/rr_rea_scoped.sdc (registered in ip.lock scoped_constraints
  with kind: sdc and emitted via PACKAGE_SDC_FILES / g_package_sdc).
  In targets/de25_datalogger.yml, sources.sdc explicitly orders rr_rea_scoped.sdc
  before de25_timing.sdc, and the project-local create_clock and _ds_rea_s1 bounds
  are removed from de25_timing.sdc so REA's clock is owned solely by the package.
- Evidence documented in docs/DS-T1.19-de25-constraint-evidence.md and linked from
  docs/DS-P1.72-timing-constraint-audit.md.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "de25_datalogger.yml"
TOP = ROOT / "hw" / "rtl" / "de25" / "deepskopion_de25_top.vhd"
AUDIT_DOC = ROOT / "docs" / "DS-P1.72-timing-constraint-audit.md"
EVIDENCE_DOC = ROOT / "docs" / "DS-T1.19-de25-constraint-evidence.md"

_TCK_MARKER = "de25 REA JTAG " + "clock unconstrained"
_BOARD_MARKER = "de25 board clock " + "contract broken"


def _code(path: Path) -> str:
    """Return file content stripped of comment-only lines."""
    return "\n".join(
        line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
    )


def _assert_no_project_local_rea_tck(root: Path) -> None:
    timing_sdc = _code(root / "hw" / "sdc" / "de25" / "de25_timing.sdc")
    assert not re.search(r"create_clock\s+-name\s+\{?altera_reserved_tck\}?", timing_sdc), (
        f"{_TCK_MARKER}: de25_timing.sdc contains project-local create_clock for altera_reserved_tck; "
        "tck clock must be owned solely by package SDC (libs/routertl/rea/constraints/rr_rea_scoped.sdc)"
    )
    assert "_ds_rea_s1" not in timing_sdc, (
        f"{_TCK_MARKER}: de25_timing.sdc contains project-local _ds_rea_s1 bounds; "
        "REA s1 stage net delay must be owned solely by package SDC (rr_rea_scoped.sdc)"
    )

    # Verify that in targets/de25_datalogger.yml, sources.sdc explicitly orders rr_rea_scoped.sdc before de25_timing.sdc
    target_data = yaml.safe_load((root / "targets" / "de25_datalogger.yml").read_text(encoding="utf-8"))
    sources_sdc = (target_data.get("sources") or {}).get("sdc") or []
    rea_sdc_rel = "libs/routertl/rea/constraints/rr_rea_scoped.sdc"
    de25_sdc_rel = "hw/sdc/de25/de25_timing.sdc"
    assert rea_sdc_rel in sources_sdc, (
        f"{_TCK_MARKER}: targets/de25_datalogger.yml sources.sdc must list {rea_sdc_rel}"
    )
    assert de25_sdc_rel in sources_sdc, (
        f"{_TCK_MARKER}: targets/de25_datalogger.yml sources.sdc must list {de25_sdc_rel}"
    )
    assert sources_sdc.index(rea_sdc_rel) < sources_sdc.index(de25_sdc_rel), (
        f"{_TCK_MARKER}: {rea_sdc_rel} must be ordered before {de25_sdc_rel} so tck clock exists when de25_timing.sdc cuts HPS CDC paths"
    )


def _assert_rea_package_sdc_applied(root: Path) -> None:
    target_data = yaml.safe_load((root / "targets" / "de25_datalogger.yml").read_text(encoding="utf-8"))
    packages = target_data.get("packages") or {}
    assert "routertl/rea" in packages, (
        f"{_TCK_MARKER}: de25_datalogger.yml does not pull routertl/rea, so no package SDC applies"
    )

    lock = yaml.safe_load((root / "ip.lock").read_text(encoding="utf-8"))
    rea_entry = lock.get("routertl/rea") or {}
    scoped_sdc_entries = [
        rec for rec in rea_entry.get("scoped_constraints") or []
        if rec.get("kind") == "sdc" and str(rec.get("path", "")).endswith("/rr_rea_scoped.sdc")
    ]
    assert scoped_sdc_entries, (
        f"{_TCK_MARKER}: ip.lock has no scoped_constraints entry applying "
        "rr_rea_scoped.sdc with kind: sdc"
    )

    from routertl_core.constraints_resolver import resolve_package_scoped_sdc

    applied = resolve_package_scoped_sdc(root, vendor="altera", config=target_data)
    matching = [p for p in applied if str(p).endswith("rr_rea_scoped.sdc")]
    assert matching, (
        f"{_TCK_MARKER}: resolve_package_scoped_sdc did not resolve rr_rea_scoped.sdc: {applied}"
    )

    scoped_sdc = (root / matching[0]).read_text(encoding="utf-8")
    assert re.search(
        r"create_clock\s+-name\s+\{?altera_reserved_tck\}?\s+-period\s+([\d.]+)",
        scoped_sdc,
    ), f"{_TCK_MARKER}: rr_rea_scoped.sdc has no create_clock for altera_reserved_tck"

    assert re.search(
        r"set_net_delay\s+-from\s+\[get_registers \*\]\s+-to\s+\$_rr_rea_s1\s+-max[\s\\]+"
        r"-get_value_from_clock_period\s+min_clock_period\s+-value_multiplier\s+0\.5\b",
        scoped_sdc,
    ), f"{_TCK_MARKER}: rr_rea_scoped.sdc does not bound REA s1 synchronizers with set_net_delay"


def _assert_board_clocks_guarded(root: Path) -> None:
    # Check routertl board master.sdc port existence guards (RTL-P1.175)
    import routertl
    try:
        from sdk.engine.board_pinout import board_pinout_path
        board_master_sdc = board_pinout_path("de25-standard").parent / "master.sdc"
    except Exception:
        board_master_sdc = Path(routertl.__file__).parent.parent / "boards" / "de25-standard" / "master.sdc"
    if not board_master_sdc.is_file():
        board_master_sdc = Path(routertl.__file__).resolve().parents[2] / "boards" / "de25-standard" / "master.sdc"
    assert board_master_sdc.is_file(), f"{_BOARD_MARKER}: master.sdc not found at {board_master_sdc}"
    _assert_master_sdc_clocks_guarded(board_master_sdc.read_text(encoding="utf-8"))
    _assert_top_board_clock_ports(root)


def _assert_master_sdc_clocks_guarded(master_text: str) -> None:
    # The guard is either inline (RTL-P1.175) or a proc whose first statement
    # returns 0 for an absent port (RTL-P2.1680's rr_board_clock_free, which
    # also skips a port an IP SDC already clocks). Either way the create_clock
    # must sit directly inside it.
    guard_procs = [
        m.group(1) for m in re.finditer(
            r"proc\s+(\w+)\s+\{(\w+)\}\s*\{\s*\n\s*"
            r"if\s*\{\[get_collection_size\s+\[get_ports\s+-nowarn\s+\$\2\]\]\s*==\s*0\}\s*"
            r"\{\s*return\s+0\s*\}",
            master_text)
    ]
    for clk in ("CLOCK0_50", "CLOCK1_50", "CLOCK2_50"):
        guards = [rf"get_collection_size\s+\[get_ports\s+-nowarn\s+\{{{clk}\}}\]\]\s*>\s*0"]
        guards += [rf"{re.escape(proc)}\s+\{{{clk}\}}\]" for proc in guard_procs]
        pattern = (rf"if\s*\{{\[(?:{'|'.join(guards)})\}}\s*\{{\s*\n"
                   rf"\s*create_clock\s+-name\s+{clk}\b")
        assert re.search(pattern, master_text), (
            f"{_BOARD_MARKER}: master.sdc create_clock for {clk} is not guarded on port existence (RTL-P1.175)"
        )


def _assert_top_board_clock_ports(root: Path) -> None:
    # Check top-level port contract
    top_vhd = (root / "hw" / "rtl" / "de25" / "deepskopion_de25_top.vhd").read_text(encoding="utf-8")
    # Top declares CLOCK0_50 and CLOCK2_50
    assert re.search(r"^\s*CLOCK0_50\s*:\s*in\s+std_logic\b", top_vhd, re.M), (
        f"{_BOARD_MARKER}: deepskopion_de25_top does not declare CLOCK0_50 port"
    )
    assert re.search(r"^\s*CLOCK2_50\s*:\s*in\s+std_logic\b", top_vhd, re.M), (
        f"{_BOARD_MARKER}: deepskopion_de25_top does not declare CLOCK2_50 port"
    )
    # CLOCK1_50 must NOT be declared as a port
    assert not re.search(r"^\s*CLOCK1_50\s*:\s*in\s+std_logic\b", top_vhd, re.M), (
        f"{_BOARD_MARKER}: deepskopion_de25_top declares CLOCK1_50 port unexpectedly"
    )


def _assert_project_clock_guarded(root: Path) -> None:
    timing_sdc = _code(root / "hw" / "sdc" / "de25" / "de25_timing.sdc")
    query = "set _ds_ref_clocks [get_clocks -nowarn -of_objects $_ds_ref_port]"
    guard = "if {[get_collection_size $_ds_ref_clocks] == 0} {"
    create = "create_clock -name CLOCK2_50 -period 20.000 $_ds_ref_port"

    assert query in timing_sdc, f"{_BOARD_MARKER}: de25_timing.sdc missing clock query {query!r}"
    assert guard in timing_sdc, f"{_BOARD_MARKER}: de25_timing.sdc missing emptiness guard {guard!r}"
    assert create in timing_sdc, f"{_BOARD_MARKER}: de25_timing.sdc missing create_clock {create!r}"

    q_idx = timing_sdc.index(query)
    g_idx = timing_sdc.index(guard)
    c_idx = timing_sdc.index(create)
    assert q_idx < g_idx < c_idx, (
        f"{_BOARD_MARKER}: de25_timing.sdc statements out of order (query -> guard -> create)"
    )


def _assert_hps_cdc_falsepath_is_order_independent(root: Path) -> None:
    """The HPS JTAG CDC false paths must NOT depend on the altera_reserved_tck clock object.

    rr_rea_scoped.sdc (which creates altera_reserved_tck) is a g_package_sdc, emitted
    AFTER this project SDC — the measured read order (job 387f024a415d) is
    master.sdc -> de25_timing.sdc -> rr_rea_scoped.sdc. The previous revision guarded
    the whole HPS CDC block on `[get_clocks altera_reserved_tck]` being non-empty, so
    under the package-owned clock the guard was always false, the block was silently
    skipped, and 9 setup + 5 recovery clk_sys<->tck sink_crosser crossings were left
    timed (setup WNS -2.633 ns, recovery -2.695 ns). The cut must therefore be
    expressed order-independently: clk_sys->tck keeps `-from $_ds_sys_clk` (clk_sys is
    created first), and tck->clk_sys is cut by destination register set only (no -from
    clock). This guard would have caught the skip.
    """
    timing_sdc = _code(root / "hw" / "sdc" / "de25" / "de25_timing.sdc")
    assert "altera_reserved_tck" not in timing_sdc, (
        f"{_TCK_MARKER}: de25_timing.sdc code references the altera_reserved_tck clock object; "
        "the HPS JTAG CDC false paths must not depend on it — it is created by a package SDC read "
        "AFTER this file, so any such dependency silently skips the block and leaves the "
        "clk_sys<->tck crossings timed (DS-T1.19 regression)"
    )
    assert "_ds_tck_clk" not in timing_sdc, (
        f"{_TCK_MARKER}: de25_timing.sdc still derives _ds_tck_clk from the tck clock; "
        "remove the clock-existence dependency so the false-path block applies whatever the SDC read order"
    )
    assert re.search(r"set_false_path\s+-from\s+\$_ds_sys_clk\s+-to\s+\$_ds_to_tck", timing_sdc), (
        f"{_TCK_MARKER}: de25_timing.sdc missing the clk_sys->tck HPS CDC false path (-from $_ds_sys_clk -to $_ds_to_tck)"
    )
    assert re.search(r"set_false_path\s+-to\s+\$_ds_to_sys\b", timing_sdc), (
        f"{_TCK_MARKER}: de25_timing.sdc missing the order-independent tck->clk_sys HPS CDC false path "
        "(-to $_ds_to_sys with no -from clock)"
    )


def test_de25_timing_sdc_has_no_project_local_rea_tck_or_bounds() -> None:
    """de25_timing.sdc must not duplicate altera_reserved_tck or REA s1 bounds, and package SDC is ordered first."""
    _assert_no_project_local_rea_tck(ROOT)


def test_de25_resolves_package_scoped_sdc() -> None:
    """Target de25_datalogger pulls routertl/rea and resolves rr_rea_scoped.sdc."""
    _assert_rea_package_sdc_applied(ROOT)


def test_master_sdc_and_top_conform_to_rtl_p1_175_port_contract() -> None:
    """master.sdc guards all board clocks on port existence and top leaves CLOCK1_50 absent."""
    _assert_board_clocks_guarded(ROOT)


def _installed_board_master_sdc() -> str:
    from sdk.engine.board_pinout import board_pinout_path
    return (board_pinout_path("de25-standard").parent / "master.sdc").read_text(encoding="utf-8")


def test_board_clock_guard_fails_when_proc_drops_port_check() -> None:
    """Mutant check (DS-P1.75): a guard proc that no longer tests port existence goes red.

    RTL-P2.1680 moved the board guard into rr_board_clock_free; the check accepts
    that proc only because its first statement returns 0 for an absent port.
    """
    master_text = _installed_board_master_sdc()
    _assert_master_sdc_clocks_guarded(master_text)
    mutant = re.sub(
        r"\n\s*if\s*\{\[get_collection_size\s+\[get_ports\s+-nowarn\s+\$port\]\]\s*==\s*0\}\s*\{\s*return\s+0\s*\}",
        "", master_text, count=1)
    assert mutant != master_text, "mutant precondition: proc port-existence check not found"
    with pytest.raises(AssertionError, match="CLOCK0_50 is not guarded on port existence"):
        _assert_master_sdc_clocks_guarded(mutant)


def test_board_clock_guard_fails_when_create_clock_leaves_the_guard() -> None:
    """Mutant check (DS-P1.75): a create_clock moved out of its guard goes red."""
    master_text = _installed_board_master_sdc()
    mutant = re.sub(r"if\s*\{\[rr_board_clock_free\s+\{CLOCK2_50\}\]\}\s*\{",
                    "if {1} {", master_text, count=1)
    assert mutant != master_text, "mutant precondition: CLOCK2_50 guard not found"
    with pytest.raises(AssertionError, match="CLOCK2_50 is not guarded on port existence"):
        _assert_master_sdc_clocks_guarded(mutant)


def test_de25_timing_sdc_guards_clock2_50_to_prevent_duplicate_clock() -> None:
    """de25_timing.sdc guards CLOCK2_50 to ensure zero duplicate/ignored board clocks."""
    _assert_project_clock_guarded(ROOT)


def test_de25_hps_cdc_falsepath_is_order_independent() -> None:
    """de25_timing.sdc HPS JTAG CDC false paths apply regardless of SDC read order (DS-T1.19)."""
    _assert_hps_cdc_falsepath_is_order_independent(ROOT)


def test_hps_cdc_order_independence_guard_fails_when_gated_on_tck_clock(tmp_path: Path) -> None:
    """Mutant check: reintroducing the altera_reserved_tck clock-existence dependency goes red.

    This is the exact regression of the first DS-T1.19 landing: the package-owned clock
    does not exist when de25_timing.sdc is read, so gating the HPS CDC false paths on it
    skips them silently.
    """
    sdc_text = (ROOT / "hw" / "sdc" / "de25" / "de25_timing.sdc").read_text(encoding="utf-8")
    mutant_sdc = sdc_text.replace(
        "set _ds_sys_clk [get_clocks -nowarn {u_fabric_pll|de25_fabric_pll_outclk0}]",
        "set _ds_sys_clk [get_clocks -nowarn {u_fabric_pll|de25_fabric_pll_outclk0}]\n"
        "set _ds_tck_clk [get_clocks -nowarn {altera_reserved_tck}]",
        1,
    )
    assert mutant_sdc != sdc_text, "mutant precondition: _ds_sys_clk assignment not found"
    dst = tmp_path / "hw" / "sdc" / "de25" / "de25_timing.sdc"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(mutant_sdc, encoding="utf-8")

    with pytest.raises(AssertionError, match="altera_reserved_tck clock object|_ds_tck_clk"):
        _assert_hps_cdc_falsepath_is_order_independent(tmp_path)


def test_project_local_rea_tck_guard_fails_when_local_create_clock_added(tmp_path: Path) -> None:
    """Mutant check: adding project-local create_clock for altera_reserved_tck causes the guard to fail."""
    for rel in ("targets/de25_datalogger.yml",):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((ROOT / rel).read_text(encoding="utf-8"))

    sdc_text = (ROOT / "hw" / "sdc" / "de25" / "de25_timing.sdc").read_text(encoding="utf-8")
    mutant_sdc = sdc_text + "\ncreate_clock -name {altera_reserved_tck} -period 33.333 [get_ports altera_reserved_tck]\n"
    dst = tmp_path / "hw" / "sdc" / "de25" / "de25_timing.sdc"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(mutant_sdc, encoding="utf-8")

    with pytest.raises(AssertionError, match="contains project-local create_clock for altera_reserved_tck"):
        _assert_no_project_local_rea_tck(tmp_path)


def test_project_local_rea_tck_guard_fails_when_package_sdc_not_ordered_first(tmp_path: Path) -> None:
    """Mutant check: if sources.sdc does not order rr_rea_scoped.sdc first, guard fails."""
    for rel in ("hw/sdc/de25/de25_timing.sdc",):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((ROOT / rel).read_text(encoding="utf-8"))

    cfg = yaml.safe_load((ROOT / "targets" / "de25_datalogger.yml").read_text(encoding="utf-8"))
    cfg["sources"]["sdc"] = ["hw/sdc/de25/de25_timing.sdc"]
    dst = tmp_path / "targets" / "de25_datalogger.yml"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    with pytest.raises(AssertionError, match="sources.sdc must list libs/routertl/rea/constraints/rr_rea_scoped.sdc"):
        _assert_no_project_local_rea_tck(tmp_path)


def test_rea_package_sdc_guard_fails_when_lock_entry_is_dropped(tmp_path: Path) -> None:
    """Mutant check: removing rr_rea_scoped.sdc from ip.lock causes the guard to fail."""
    for rel in ("targets/de25_datalogger.yml",):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((ROOT / rel).read_text(encoding="utf-8"))

    lock = yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))
    rea_entry = lock.get("routertl/rea", {})
    rea_entry["scoped_constraints"] = [
        rec for rec in rea_entry.get("scoped_constraints") or []
        if not (rec.get("kind") == "sdc" and str(rec.get("path", "")).endswith("/rr_rea_scoped.sdc"))
    ]
    (tmp_path / "ip.lock").write_text(yaml.safe_dump(lock), encoding="utf-8")

    with pytest.raises(AssertionError, match="no scoped_constraints entry applying rr_rea_scoped.sdc"):
        _assert_rea_package_sdc_applied(tmp_path)


def test_board_clock_guard_fails_when_top_instantiates_absent_clock(tmp_path: Path) -> None:
    """Mutant check: if top declared CLOCK1_50, the guard goes red."""
    top_text = (ROOT / "hw" / "rtl" / "de25" / "deepskopion_de25_top.vhd").read_text(encoding="utf-8")
    mutant_top = top_text.replace(
        "CLOCK0_50 : in  std_logic;",
        "CLOCK0_50 : in  std_logic;\n    CLOCK1_50 : in  std_logic;",
    )
    dst = tmp_path / "hw" / "rtl" / "de25" / "deepskopion_de25_top.vhd"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(mutant_top, encoding="utf-8")

    with pytest.raises(AssertionError, match="declares CLOCK1_50 port unexpectedly"):
        _assert_board_clocks_guarded(tmp_path)


def test_project_clock_guard_fails_on_unconditional_clock2_50(tmp_path: Path) -> None:
    """Mutant check: if de25_timing.sdc removes the emptiness guard, the guard goes red."""
    sdc_text = (ROOT / "hw" / "sdc" / "de25" / "de25_timing.sdc").read_text(encoding="utf-8")
    # Mutate to baseline uncoordinated duplicate create_clock
    mutant_sdc = sdc_text.replace(
        "if {[get_collection_size $_ds_ref_clocks] == 0} {",
        "if {1} {",
    )
    dst = tmp_path / "hw" / "sdc" / "de25" / "de25_timing.sdc"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(mutant_sdc, encoding="utf-8")

    with pytest.raises(AssertionError, match="missing emptiness guard"):
        _assert_project_clock_guarded(tmp_path)


_DOC_MARKER = "de25 constraint evidence " + "missing or unlinked"


def _assert_evidence_doc_linked(root: Path) -> None:
    ev_path = root / "docs" / "DS-T1.19-de25-constraint-evidence.md"
    assert ev_path.is_file(), f"{_DOC_MARKER}: {ev_path} not found on disk"
    ev_text = ev_path.read_text(encoding="utf-8")

    assert "6ebaa3de8130" in ev_text, f"{_DOC_MARKER}: evidence doc missing queue job 6ebaa3de8130"
    assert "24ba18cb" in ev_text, f"{_DOC_MARKER}: evidence doc missing provenance SHA 24ba18cb"
    assert "altera_reserved_tck" in ev_text, f"{_DOC_MARKER}: evidence doc missing altera_reserved_tck evidence"
    assert "RTL-P1.175" in ev_text, f"{_DOC_MARKER}: evidence doc missing RTL-P1.175 citation"
    assert "+4.358" in ev_text, f"{_DOC_MARKER}: evidence doc missing WNS +4.358 ns"
    assert "57.9" in ev_text, f"{_DOC_MARKER}: evidence doc missing fmax 57.9 MHz"
    assert "332174" in ev_text, f"{_DOC_MARKER}: evidence doc missing Warning 332174 citation"
    assert "332049" in ev_text, f"{_DOC_MARKER}: evidence doc missing Warning 332049 citation"
    assert "CLOCK0_50" in ev_text, f"{_DOC_MARKER}: evidence doc missing CLOCK0_50 citation"
    assert "CLOCK1_50" in ev_text, f"{_DOC_MARKER}: evidence doc missing CLOCK1_50 citation"

    # Reject fabricated or contradictory claims
    unsupported_markers = [
        "Ignored board/project clocks: 0",
        "zero ignored board/project clocks",
        "0 ignored board/project clocks",
        "0 ignored board clocks",
    ]
    for marker in unsupported_markers:
        assert marker.lower() not in ev_text.lower(), (
            f"{_DOC_MARKER}: evidence doc contains unsupported claim '{marker}' contradicted by job 6ebaa3de8130 log"
        )

    audit_path = root / "docs" / "DS-P1.72-timing-constraint-audit.md"
    assert audit_path.is_file(), f"{_DOC_MARKER}: {audit_path} not found on disk"
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "[DS-T1.19 DE25 constraint evidence](DS-T1.19-de25-constraint-evidence.md)" in audit_text, (
        f"{_DOC_MARKER}: audit doc does not link DS-T1.19-de25-constraint-evidence.md"
    )
