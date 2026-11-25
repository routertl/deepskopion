# rr unknown-llm (AI-generated)
"""DS-P2.155 regression guard: the DE25 fabric runs at 100 MHz via a fabric IOPLL.

THE DEFECT this pins. Before DS-P2.155 the DE25 fabric was driven directly from
CLOCK2_50 (``clk_sys <= CLOCK2_50``, 50 MHz) while ip.yml already declared
``fabric_clk_mhz: 100.0`` (EXPLICIT_CLOCK_RATE) — the HPS was told 100 MHz but
fed 50 (the RTL-P3.1755 mismatch). deepskopion_core was therefore instantiated
with ``G_SYS_CLK_HZ => 50_000_000``, so every clock-scaled quantity ran on the
50 MHz assumption: the UART-datalogger edge-timestamp resolution was 20 ns (one
50 MHz tick) instead of the validated 10 ns, and the CTU-aperture watchdog's
wall-clock timeout was pinned to a 50 MHz cycle count. This also left the three
legs (PolarFire PF-F0, Zynq, DE25) on non-uniform clocks, breaking the DS-D.45
shared-core count arithmetic.

THE FIX. A fabric IOPLL (de25_fabric_pll, altera_iopll, CLOCK2_50 x2 ->
100 MHz; since DS-P2.196 declared in ip.yml ``vendor_ip_variants`` and made
by rr in the build tree) produces clk_sys; it feeds BOTH the core and the
hps_subsys fabric_clk conduit; the reset synchroniser is held until the PLL
locks AND the SDM completes; the core generic and the CTU watchdog cycle count
are re-derived for 100 MHz.

These are SOURCE-CONTRACT guards on deepskopion_de25_top.vhd — the top
instantiates a generated HPS/PLL black box, so it cannot be GHDL-elaborated in
CI; the elaboration-level proof of the derived UART prescaler at 100 MHz lives
in test_uart_datalogger_clk_parametrised_ds_p2_154.py. This is the same guard class
the codebase already relies on (test_core_sys_clk_generic_guard,
test_de25_shell_contract).
"""

import os
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[2]
DE25_TOP = ROOT / "hw" / "rtl" / "de25" / "deepskopion_de25_top.vhd"
DE25_RESET = ROOT / "hw" / "rtl" / "de25" / "de25_reset_ctrl.vhd"
# DS-P2.196: rr makes the IOPLL variant from ip.yml ``vendor_ip_variants`` in
# the build tree; the source tree holds no generated file.
IP_YML = ROOT / "ip.yml"
# DS-P3.193: tools/post_synth_guard.py points this at the newest DE25 build tree
# (another worktree's build/rr_ip_regen) through DS_FABRIC_PLL_DIR.
REGEN_PLL_DIR = Path(os.environ.get("DS_FABRIC_PLL_DIR")
                     or ROOT / "build" / "rr_ip_regen" / "ip" / "de25_fabric_pll")
# The GENERATED altera_iopll primitive — the file that actually fixes the M/N/C
# dividers into silicon. If the IOPLL is reconfigured to any other frequency,
# these constants (not the declared parameters alone) are what change.
PLL_PRIMITIVE_GLOB = "de25_fabric_pll/altera_iopll_*/synth/de25_fabric_pll_altera_iopll_*.v"
DATALOGGER_TGT = ROOT / "targets" / "de25_datalogger.yml"
import sys  # noqa: E402
sys.path.insert(0, str(ROOT / "tools"))
from target_files import target_file, with_bench  # noqa: E402  (DS-P1.103)


def _u_core_generic_map(top: str) -> str:
    m = re.search(
        r"u_core\s*:\s*entity\b.*?generic map\s*\((.*?)\)\s*port map",
        top,
        re.DOTALL | re.IGNORECASE,
    )
    assert m, "u_core instance was not found in deepskopion_de25_top.vhd"
    return m.group(1)


def test_de25_top_drives_core_at_100mhz():
    """The selected test-power guard. clk_sys is the 100 MHz IOPLL product, so
    the core's clock-scaled math must be derived for 100 MHz, not 50."""
    seam = _u_core_generic_map(DE25_TOP.read_text())
    assert re.search(r"G_SYS_CLK_HZ\s*=>\s*100_000_000", seam, re.IGNORECASE), (
        "DS-P2.155: DE25 top must pass G_SYS_CLK_HZ => 100_000_000 (clk_sys is "
        "the CLOCK2_50 x2 IOPLL product); at 50_000_000 the UART datalogger runs "
        "20 ns edge timestamps and the CTU watchdog timeout is mis-scaled"
    )
    assert not re.search(r"G_SYS_CLK_HZ\s*=>\s*50_000_000", seam, re.IGNORECASE), (
        "DS-P2.155 regression: DE25 top still passes G_SYS_CLK_HZ => 50_000_000"
    )


def test_clk_sys_is_the_pll_product_not_bare_clock2_50():
    """clk_sys must come from the fabric IOPLL output, never the bare 50 MHz
    board oscillator."""
    top = DE25_TOP.read_text()
    assert not re.search(r"clk_sys\s*<=\s*CLOCK2_50\s*;", top), (
        "DS-P2.155: clk_sys must NOT be assigned CLOCK2_50 directly — it is the "
        "100 MHz IOPLL product (u_fabric_pll outclk_0)"
    )
    assert re.search(r"outclk_0\s*=>\s*clk_sys", top, re.IGNORECASE), (
        "DS-P2.155: the fabric IOPLL (u_fabric_pll) must drive clk_sys via "
        "outclk_0 => clk_sys"
    )
    assert re.search(r"refclk\s*=>\s*CLOCK2_50", top, re.IGNORECASE), (
        "DS-P2.155: the fabric IOPLL reference must be CLOCK2_50 (50 MHz -> "
        "100 MHz x2)"
    )


_BUSY_WORDS = {"not", "and", "or", "xor", "nand", "nor", "ninit_done", "pll_locked"}


def _eval_config_busy(expr: str, ninit_done: int, pll_locked: int) -> int:
    """Evaluate the VHDL ``config_busy`` expression. Precedence matches VHDL:
    ``not`` binds tightest, then ``and``/``nand``/``nor``, then ``or``/``xor``.
    """
    src = re.sub(r"--.*", "", expr).strip().lower()
    tokens: list[str] = []
    i = 0
    while i < len(src):
        if src[i].isspace():
            i += 1
            continue
        if src[i] in "()":
            tokens.append(src[i])
            i += 1
            continue
        word = re.match(r"[a-z_]+", src[i:])
        if word is None or word.group(0) not in _BUSY_WORDS:
            raise ValueError(f"unrecognized token in config_busy expr: {src[i:]!r}")
        tokens.append(word.group(0))
        i += len(word.group(0))
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def eat(kind: str) -> None:
        nonlocal pos
        if peek() != kind:
            raise ValueError(f"expected {kind} in {tokens}")
        pos += 1

    def parse_or() -> int:
        val = parse_and()
        while peek() in {"or", "xor"}:
            op = peek()
            eat(op)
            rhs = parse_and()
            val = (val ^ rhs) if op == "xor" else (val or rhs)
        return val

    def parse_and() -> int:
        val = parse_not()
        while peek() in {"and", "nand", "nor"}:
            op = peek()
            eat(op)
            rhs = parse_not()
            if op == "and":
                val = val and rhs
            elif op == "nand":
                val = int(not (val and rhs))
            else:
                val = int(not (val or rhs))
        return val

    def parse_not() -> int:
        nonlocal pos
        if peek() == "not":
            eat("not")
            return int(not parse_not())
        return parse_primary()

    def parse_primary() -> int:
        nonlocal pos
        tok = peek()
        if tok == "(":
            eat("(")
            val = parse_or()
            eat(")")
            return val
        if tok == "ninit_done":
            eat(tok)
            return ninit_done
        if tok == "pll_locked":
            eat(tok)
            return pll_locked
        raise ValueError(f"bad primary {tok!r} in config_busy expr")

    val = parse_or()
    if pos != len(tokens):
        raise ValueError(f"trailing tokens in config_busy expr: {tokens[pos:]}")
    return int(val)


def test_fabric_reset_is_gated_by_pll_locked():
    """Reset stays asserted until SDM init completes AND the PLL locks.

    DS-P1.80 moved the gate into de25_reset_ctrl, which synchronises each
    source on its own chain and combines the chain outputs. The combination
    ``init_sync_r(last) or not lock_sync_r(last)`` is evaluated here with the
    chain outputs standing for ninit_done and pll_locked. A De Morgan flip
    (``... and lock_sync_r(...)``) still mentions both chains and would release
    reset while the PLL is unlocked; the truth table below is red on that
    mutant. The behaviour over time is covered by the cocotb test
    hw/sim/cocotb/tests/infra/de25_reset_ctrl.
    """
    top = DE25_TOP.read_text()
    inst = re.search(r"u_reset\s*:\s*entity\s+work\.de25_reset_ctrl(.*?);", top, re.S)
    assert inst, "DS-P1.80: deepskopion_de25_top must instantiate de25_reset_ctrl"
    ports = inst.group(1)
    for formal, actual in (("ninit_done_i", "ninit_done"), ("pll_locked_i", "pll_locked"),
                           ("clk_i", "clk_sys"), ("rst_o", "rst_sys")):
        assert re.search(rf"\b{formal}\s*=>\s*{actual}\b", ports), (
            f"DS-P1.80: de25_reset_ctrl port {formal} must be driven by/drive {actual}"
        )
    body = DE25_RESET.read_text()
    m = re.search(r"\brst_r\s*<=\s*([^;]+);", body)
    assert m, "rst_r assignment not found in de25_reset_ctrl.vhd"
    expr = re.sub(r"init_sync_r\s*\(\s*G_STAGES\s*-\s*1\s*\)", "ninit_done", m.group(1))
    expr = re.sub(r"lock_sync_r\s*\(\s*G_STAGES\s*-\s*1\s*\)", "pll_locked", expr)
    # (ninit_done, pll_locked) -> reset. Release (0) only when init is
    # done AND the PLL is locked.
    expected = {
        (0, 0): 1,
        (0, 1): 0,
        (1, 0): 1,
        (1, 1): 1,
    }
    for (ninit_done, pll_locked), want in expected.items():
        try:
            got = _eval_config_busy(expr, ninit_done, pll_locked)
        except ValueError as exc:
            raise AssertionError(
                "DS-P2.155/DS-P1.80: reset must be ninit_done OR NOT pll_locked "
                f"(reset held until init done AND pll locked); {exc}; "
                f"expr={expr.strip()}"
            ) from exc
        assert got == want, (
            "DS-P2.155/DS-P1.80: reset must be ninit_done OR NOT pll_locked "
            "(reset held until init done AND pll locked); "
            f"ninit_done={ninit_done} pll_locked={pll_locked} => {got}, "
            f"want {want}; expr={expr.strip()}"
        )


def test_hps_fabric_clk_is_fed_by_100mhz_clk_sys():
    """The HPS fabric bridges must be clocked at the 100 MHz clk_sys the ip.yml
    fabric_clk_mhz declares — not the bare 50 MHz CLOCK2_50."""
    top = DE25_TOP.read_text()
    m = re.search(r"fabric_clk_clk\s*=>\s*(\w+)", top)
    assert m, "hps_subsys fabric_clk_clk port map not found"
    assert m.group(1) == "clk_sys", (
        "DS-P2.155: hps_subsys.fabric_clk_clk must be fed clk_sys (100 MHz IOPLL "
        f"product), matching ip.yml fabric_clk_mhz=100; found => {m.group(1)}"
    )


def test_ctu_aperture_watchdog_rederived_for_100mhz():
    """The CTU-aperture watchdog timeout is a wall-clock bound (~82 us). At
    100 MHz that is 8192 cycles; left at the 50 MHz value (4096) it silently
    halves to 41 us."""
    top = DE25_TOP.read_text()
    m = re.search(r"axil_demux3\s*generic map\s*\(\s*wd_cycles_g\s*=>\s*(\d+)",
                  top, re.DOTALL | re.IGNORECASE)
    assert m, "axil_demux3 wd_cycles_g generic not found in deepskopion_de25_top.vhd"
    assert m.group(1) == "8192", (
        "DS-P2.155: axil_demux3 wd_cycles_g must be re-derived to 8192 for the "
        f"100 MHz clk_sys (~82 us aperture timeout); found {m.group(1)} "
        "(4096 = the un-scaled 50 MHz value -> 41 us)"
    )


def _pll_variant():
    from sdk.engine.hps_regen import load_variant_decls

    decls = {d.name: d for d in load_variant_decls(ROOT)}
    assert "de25_fabric_pll" in decls, (
        "DS-P2.155/DS-P2.196: ip.yml vendor_ip_variants must declare de25_fabric_pll "
        "(the entity u_fabric_pll instantiates)"
    )
    return decls["de25_fabric_pll"]


def test_fabric_pll_is_declared_and_registered_from_the_build_tree(tmp_path):
    """The altera_iopll variant is declared in ip.yml, rr's real pre-synth step
    makes and generates it in the build tree and registers that .qip, and no DE25
    target lists a source-tree .qip any more (DS-P2.196). The variant name is the
    entity name, so the top's u_fabric_pll binding is unchanged."""
    import shutil
    import subprocess

    from sdk.engine.hps_regen import read_ip, regenerate_vendor_variants, variant_qip

    decl = _pll_variant()
    assert decl.component == "altera_iopll"
    params = dict(decl.parameters)
    # Pin the FREQUENCY, not merely the presence of the parameter: the whole
    # point of DS-P2.155 is a 100 MHz clk_sys from a 50 MHz reference.
    assert float(params["gui_reference_clock_frequency"]) == 50.0, (
        "DS-P2.155: the IOPLL reference must be 50.0 MHz (CLOCK2_50)")
    assert int(params["gui_number_of_clocks"]) == 1
    assert float(params["gui_output_clock_frequency0"]) == 100.0, (
        "DS-P2.155: the IOPLL outclk_0 must be 100.0 MHz")
    assert "component de25_fabric_pll is" in DE25_TOP.read_text()

    # DS-P1.103: the DE25 product always; the bench CTU target only where bench/ exists.
    for tgt in [target_file(n, ROOT) for n in sorted(with_bench({"de25_datalogger"}, {"de25_canfd"}, ROOT))]:
        assert "de25_fabric_pll" not in tgt.read_text(), (
            f"{tgt.name} still names a source-tree de25_fabric_pll file; rr "
            "registers the regenerated .qip itself (DS-P2.196)")

    shutil.copy2(IP_YML, tmp_path / "ip.yml")
    calls = []

    def stand_in(argv, cwd=None, **_kw):
        argv = [str(a) for a in argv]
        calls.append(argv)
        tool = Path(argv[0]).name
        opts = dict(a[2:].split("=", 1) for a in argv[1:] if a.startswith("--") and "=" in a)
        if tool == "ip-deploy":
            out = Path(opts["output-directory"]) / f"{opts['output-name']}.ip"
            out.write_text(
                '<?xml version="1.0"?><ipxact:component xmlns:ipxact='
                '"http://www.accellera.org/XMLSchema/IPXACT/1685-2014" xmlns:altera='
                '"http://www.altera.com/XMLSchema/IPXact2014/extensions"><ipxact:name>'
                'de25_fabric_pll</ipxact:name><ipxact:vendorExtensions>'
                '<altera:altera_module_parameters><ipxact:parameters>'
                + "".join(
                    f'<ipxact:parameter parameterId="{k}"><ipxact:name>{k}</ipxact:name>'
                    f"<ipxact:value>{v}</ipxact:value></ipxact:parameter>"
                    for k, v in params.items())
                + "</ipxact:parameters></altera:altera_module_parameters>"
                "</ipxact:vendorExtensions></ipxact:component>", encoding="utf-8")
        elif tool == "qsys-generate":
            qip = Path(opts["output-directory"]) / "de25_fabric_pll.qip"
            qip.parent.mkdir(parents=True, exist_ok=True)
            qip.write_text("# test fixture\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    qips = regenerate_vendor_variants(
        tmp_path, [decl], qsys_bin_dir=Path("/q/quartus/sopc_builder/bin"),
        part="A5ED013BB32AE4SCS", runner=stand_in, version="25.3.1.100",
    )
    assert qips == [variant_qip(tmp_path, "de25_fabric_pll")]
    assert qips[0].relative_to(tmp_path).as_posix() == (
        "build/rr_ip_regen/ip/de25_fabric_pll/de25_fabric_pll/de25_fabric_pll.qip")
    made = read_ip(tmp_path / "build/rr_ip_regen/ip/de25_fabric_pll/de25_fabric_pll.ip")
    assert float(made.top["gui_output_clock_frequency0"]) == 100.0
    assert [Path(c[0]).name for c in calls].count("qsys-generate") == 1


def test_generated_pll_primitive_locks_50mhz_ref_to_100mhz_out():
    """The GENERATED altera_iopll primitive is what fixes the dividers into
    silicon. Pin ref=50 MHz, out=100 MHz and the exact M/C dividers (x64 / /32 =
    x2) so a reconfiguration to any other frequency — the failure mode the
    declared-parameter guard alone misses — fails the receipt. Since DS-P2.196
    the primitive exists only after ``rr synth run`` of a DE25 target has made
    it in the build tree."""
    prims = sorted(REGEN_PLL_DIR.glob(PLL_PRIMITIVE_GLOB))
    if not prims:
        pytest.skip("DS-P3.193: the IOPLL primitive is generated by `rr synth run` of a "
                    "DE25 target into build/rr_ip_regen/; none has run in this checkout")
    assert len(prims) == 1, f"more than one generated IOPLL primitive: {prims}"
    prim = prims[0].read_text(encoding="utf-8", errors="replace")
    assert re.search(r"\.out_clk_0_freq\(36'd100000000\)", prim), (
        "DS-P2.155: generated IOPLL out_clk_0_freq must be 100 MHz (100000000)"
    )
    assert re.search(r"\.ref_clk_0_freq\(32'd50000000\)", prim), (
        "DS-P2.155: generated IOPLL ref_clk_0_freq must be 50 MHz (CLOCK2_50)"
    )
    assert re.search(r"\.fb_clk_m_div\(64\)", prim) and re.search(
        r"\.out_clk_0_c_div\(32\)", prim), (
        "DS-P2.155: generated IOPLL dividers must be M=64 / C=32 (net x2, "
        "50 MHz -> 100 MHz)"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
