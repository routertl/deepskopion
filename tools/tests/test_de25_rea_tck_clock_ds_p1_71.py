# rr unknown-llm (AI-generated)
"""DS-P1.71: the DE25 constraints must clock REA's JTAG domain.

MEASURED 2026-10-02 building ``de25_datalogger`` at ``cda264a`` for the DS-P1.71
live witness: Quartus reported "Timing requirements were met", then rr's
sign-off gate (RTL-P2.1382) refused the bitstream:

    ERROR: — TimeQuest reports unconstrained clocks (report_ucp).
      NO CLOCK: unconstrained clock altera_reserved_tck

``deepskopion_de25_top`` instantiates ``rr_rea_intel``, whose sld_virtual_jtag
runs on ``altera_reserved_tck``. Quartus does not clock that port on its own,
and deepskopion's local REA fork (``libs/local/rr_rea`` 0.7.0-ds-fork) predates
upstream rr-rea's ``constraints/rr_rea_scoped.sdc`` (REA-P2.12), so no SDC
declared it and every DE25 bitstream run on main stopped at the gate.

The guard reads the SDC files the DE25 build auto-discovers (``paths.constraints``)
— the exact surface Quartus ``read_sdc`` sees — and pins four separate
mechanisms, one test each:

(a) a ``create_clock`` on ``altera_reserved_tck``;
(b) REA's tck <-> clk_sys synchronizer first stages bounded with
    ``set_net_delay`` (a clock group would waive the crossing without bounding
    it);
(c) the Intel JTAG-to-Avalon master's CDC waiver, one test per direction:
    clocking tck made STA time the handshake / reset-synchronizer crossings
    inside ``jtag_phy_embedded_in_jtag_master`` (every failing path of the
    cda264a build), and the SDC cuts exactly those named destination registers.
    clk_sys -> tck keeps its named launch clock (``-from $_ds_sys_clk``, read
    first); tck -> clk_sys is cut by destination register set only, with NO
    ``-from`` clock, because DS-T1.19 moved altera_reserved_tck into a package
    SDC (rr_rea_scoped.sdc) emitted AFTER this file — a ``-from altera_reserved_tck``
    cut would be read before the clock exists and silently skip (job
    387f024a415d: recovery WNS -2.695 ns). Deleting a direction, or widening or
    dropping a destination selector, fails;
(d) every ``set_false_path`` in the file lands on a named register collection,
    so the waiver above cannot be broadened into a whole-clock cut.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "de25_datalogger.yml"
TOP = ROOT / "hw" / "rtl" / "de25" / "deepskopion_de25_top.vhd"

SYS_CLK = "u_fabric_pll|de25_fabric_pll_outclk0"
TCK_CLK = "altera_reserved_tck"
REA_S1 = {"*|rr_rea_sync_word:*|s1*", "*|rr_rea_pulse_xfer:*|s1*"}

# The JTAG-to-Avalon master instance inside hps_subsys (f2s_term_master).
_JP = "*|jtag_phy_embedded_in_jtag_master|normal.jtag_dc_streaming|"
# clk_sys -> tck: handshake capture, its toggle synchronizer, the tck-side
# reset synchronizers.
JTAG_CUT_TO_TCK = {
    _JP + "sink_crosser|out_data_buffer*",
    _JP + "sink_crosser|in_to_out_synchronizer|din_s1",
    _JP + "synchronizer|dreg*",
    _JP + "sink_crosser|alt_rst_req_sync_out_rst|*",
}
# tck -> clk_sys: the return toggle synchronizer and the clk_sys-side reset
# synchronizers.
JTAG_CUT_TO_SYS = {
    _JP + "sink_crosser|out_to_in_synchronizer|din_s1",
    _JP + "sink_crosser|alt_rst_req_sync_in_rst|*",
    _JP + "jtag_streaming|clock_sense_reset_n_synchronizer|dreg*",
}


def _de25_sdc_files() -> list[Path]:
    cfg = yaml.safe_load(TARGET.read_text())
    files: list[Path] = []
    if "sources" in cfg and "sdc" in cfg["sources"]:
        for rel in cfg["sources"]["sdc"]:
            p = ROOT / rel
            if p.is_file() and p not in files:
                files.append(p)
    elif "paths" in cfg and "constraints" in cfg["paths"]:
        cdir = ROOT / cfg["paths"]["constraints"]
        files.extend(sorted(cdir.rglob("*.sdc")))
    try:
        from routertl_core.constraints_resolver import resolve_package_scoped_sdc
        for rel in resolve_package_scoped_sdc(ROOT, vendor="altera", config=cfg):
            if "rr_rea" in rel:
                p = ROOT / rel
                if p.is_file() and p not in files:
                    files.append(p)
    except ImportError:
        pass
    assert files, f"no SDC found for target {TARGET}"
    return files


def _de25_sdc_text() -> str:
    """The live SDC the DE25 build reads: comments dropped, continuations joined.

    A Tcl comment ending in a backslash swallows the next line too, so a
    commented-out first line of a multi-line command removes the whole command.
    """
    live = []
    for f in _de25_sdc_files():
        in_comment = False
        for line in f.read_text().splitlines():
            if in_comment or line.lstrip().startswith("#"):
                in_comment = line.rstrip().endswith("\\")
                continue
            live.append(line.split("#", 1)[0])
    return re.sub(r"\\\n\s*", " ", "\n".join(live))


def _clock_vars(sdc: str) -> dict[str, str]:
    return dict(re.findall(r"set\s+(_\w+)\s+\[get_clocks\s+-nowarn\s+\{([^}]*)\}\]", sdc))


def _register_vars(sdc: str) -> dict[str, set[str]]:
    """``set _x [get_registers -nowarn {...}|[list "..." ...]]`` -> selectors."""
    jp = re.search(r"set\s+_ds_jp\s+\{([^}]*)\}", sdc)
    out: dict[str, set[str]] = {}
    for var, braced, listed in re.findall(
            r"set\s+(_\w+)\s+\[get_registers\s+-nowarn\s+"
            r"(?:\{([^}]*)\}|\[list\s+((?:\"[^\"]*\"\s*)+)\])\]", sdc):
        sels = braced.split() if braced else re.findall(r'"([^"]*)"', listed)
        if jp:
            sels = [s.replace("${_ds_jp}", jp.group(1)) for s in sels]
        out[var] = set(sels)
    return out


def _jtag_master_cuts(sdc: str) -> dict[str, set[str]]:
    """Launch clock name -> destination selectors, for every false path whose
    destination collection lies inside the JTAG-to-Avalon master."""
    clocks, regs = _clock_vars(sdc), _register_vars(sdc)
    cuts: dict[str, set[str]] = {}
    for src, dst in re.findall(r"set_false_path\s+-from\s+\$(\w+)\s+-to\s+\$(\w+)", sdc):
        sels = regs.get(dst, set())
        if sels and all("|jtag_phy_embedded_in_jtag_master|" in s for s in sels):
            cuts.setdefault(clocks.get(src, f"${src}"), set()).update(sels)
    return cuts


def _jtag_master_to_only_cuts(sdc: str) -> set[str]:
    """Destination selectors of every ``set_false_path -to $var`` that names NO
    launch clock (no ``-from``) and whose collection lies inside the
    JTAG-to-Avalon master.

    DS-T1.19: altera_reserved_tck is created by rr_rea_scoped.sdc, a g_package_sdc
    emitted AFTER de25_timing.sdc, so a ``-from altera_reserved_tck`` cut is read
    before the clock exists and is silently skipped. A destination-only cut applies
    whatever the SDC read order; cutting into these dedicated Intel synchronizer /
    reset-sync keepers is the standard async-CDC treatment and needs no launch clock.
    """
    regs = _register_vars(sdc)
    out: set[str] = set()
    for dst in re.findall(r"set_false_path\s+-to\s+\$(\w+)\s*$", sdc, re.M):
        sels = regs.get(dst, set())
        if sels and all("|jtag_phy_embedded_in_jtag_master|" in s for s in sels):
            out.update(sels)
    return out


def test_de25_top_has_the_rea_jtag_domain():
    assert "rr_rea_intel" in TOP.read_text(), (
        "the DE25 top no longer instantiates rr_rea_intel; re-check whether "
        "altera_reserved_tck still exists before keeping this guard")


def test_de25_sdc_declares_altera_reserved_tck():
    sdc = _de25_sdc_text()
    assert re.search(r"create_clock\s+-name\s+\{?altera_reserved_tck\}?\s+-period\s+[\d.]+", sdc), (
        "no create_clock for altera_reserved_tck in the DE25 SDC: Quartus leaves the "
        "REA JTAG clock Unconstrained and rr's sign-off gate refuses the bitstream")


def test_de25_sdc_bounds_rea_crossings_instead_of_waiving_them():
    sdc = _de25_sdc_text()
    m = re.search(r"set_net_delay\s+-from\s+\[get_registers \*\]\s+-to\s+\$(\w+)\s+-max\s+"
                  r"-get_value_from_clock_period\s+min_clock_period\s+-value_multiplier\s+0\.5\b", sdc)
    assert m, (
        "REA's tck <-> clk_sys synchronizer first stages are not bounded with set_net_delay "
        "(-max, half the faster clock period)")
    assert _register_vars(sdc).get(m.group(1)) == REA_S1, (
        f"the set_net_delay bound targets ${m.group(1)}, not exactly REA's s1 stages {sorted(REA_S1)}")
    assert not re.search(r"set_clock_groups[^\n]*altera_reserved_tck", sdc), (
        "a clock group on altera_reserved_tck waives the REA crossings without bounding them")


def test_de25_sdc_cuts_jtag_master_clk_sys_to_tck_crossings_by_name():
    cuts = _jtag_master_cuts(_de25_sdc_text())
    assert SYS_CLK in cuts, (
        "no clk_sys -> altera_reserved_tck set_false_path into the JTAG-to-Avalon master: "
        f"STA times Intel's handshake/reset synchronizers and fails (cuts seen: {cuts})")
    assert cuts[SYS_CLK] == JTAG_CUT_TO_TCK, (
        "the clk_sys -> tck JTAG-to-Avalon waiver selectors drifted: "
        f"extra={sorted(cuts[SYS_CLK] - JTAG_CUT_TO_TCK)} missing={sorted(JTAG_CUT_TO_TCK - cuts[SYS_CLK])}")


def test_de25_sdc_cuts_jtag_master_tck_to_clk_sys_crossings_by_name():
    """DS-T1.19: the tck -> clk_sys crossings are cut by DESTINATION register set only.

    altera_reserved_tck is created by rr_rea_scoped.sdc, a package SDC emitted AFTER
    de25_timing.sdc (measured read order, job 387f024a415d), so the former
    ``-from altera_reserved_tck`` cut was read before the clock existed and silently
    skipped, leaving these return synchronizer / reset-chain crossings timed (recovery
    WNS -2.695 ns, 5 failing endpoints). The cut is now destination-only so it applies
    whatever the SDC read order; the selector set must still be exactly these named
    collections (no widening, no dropping a direction) so it cannot become a
    whole-clock waiver.
    """
    sdc = _de25_sdc_text()
    to_only = _jtag_master_to_only_cuts(sdc)
    assert to_only == JTAG_CUT_TO_SYS, (
        "the tck -> clk_sys JTAG-to-Avalon return syncs/reset chains are not cut by a "
        "destination-only set_false_path (no -from clock, so it applies whatever the SDC "
        f"read order): extra={sorted(to_only - JTAG_CUT_TO_SYS)} missing={sorted(JTAG_CUT_TO_SYS - to_only)}")

    # The ORDER-INDEPENDENCE invariant (replaces the former `-from altera_reserved_tck`
    # assertion, which encoded the broken form this build disproved): the return-sync
    # cut must carry NO launch-clock dependency. altera_reserved_tck is created by a
    # package SDC read AFTER this file, so any `-from` on it is read before the clock
    # exists and is silently skipped — exactly the DS-T1.19 regression. Assert it
    # explicitly rather than merely dropping the old check.
    from_cuts = _jtag_master_cuts(sdc)
    targeted_by_from = set().union(*from_cuts.values()) if from_cuts else set()
    assert targeted_by_from.isdisjoint(JTAG_CUT_TO_SYS), (
        "the tck -> clk_sys return-sync registers are targeted by a `-from` launch "
        "clock; that reintroduces the order-dependent form (the launch clock may not "
        f"exist yet at read time): {sorted(targeted_by_from & JTAG_CUT_TO_SYS)}")

    tck_clock_vars = {var for var, sel in _clock_vars(sdc).items() if TCK_CLK in sel}
    from_launch_vars = set(re.findall(r"set_false_path\s+-from\s+\$(\w+)\b", sdc))
    assert from_launch_vars.isdisjoint(tck_clock_vars), (
        f"altera_reserved_tck is used as a `-from` launch clock (vars "
        f"{sorted(from_launch_vars & tck_clock_vars)}); the package SDC that creates it "
        "is read after this file, so the cut would be silently skipped")
    assert not re.search(r"-from\s+\[get_clocks[^\]]*" + re.escape(TCK_CLK), sdc), (
        "a set_false_path references altera_reserved_tck directly in a `-from` "
        "[get_clocks ...]; the tck -> clk_sys cut must be destination-only so it is "
        "order-independent")


def test_de25_sdc_false_paths_land_only_on_named_registers():
    sdc = _de25_sdc_text()
    regs = _register_vars(sdc)
    broad = [stmt.strip() for stmt in re.findall(r"set_false_path[^\n]*", sdc)
             if (m := re.search(r"-to\s+\$(\w+)\s*$", stmt.strip())) is None or m.group(1) not in regs]
    assert not broad, (
        "a DE25 set_false_path does not land on a named register collection, so it waives "
        f"whole-clock or unnamed crossings: {broad}")
    assert "set_clock_groups" not in sdc, "a DE25 clock group waives every crossing between its clocks"
