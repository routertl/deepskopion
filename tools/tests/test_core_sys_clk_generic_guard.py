"""Regression guard for DS-T1.10: the datalogger-path clock-scaled generics must
thread G_SYS_CLK_HZ, never a bare literal.

The bug: deepskopion_core instantiated u_datalogger_gather (G_SYS_CLK) and
u_i2c_injector (G_CLK_FREQ_HZ) with a hardcoded 100_000_000, even though the
core exposes G_SYS_CLK_HZ and the DE25 top drove it to 50 MHz (clk_sys =
CLOCK2_50). Result: on the 50 MHz DE25 the datalogger baud / glitch-filter /
SCL-stuck-low math ran 2x fast while the Zynq (100 MHz default) looked fine.
Any clk-scaled generic in the datalogger path fed a literal is this class of bug.

DS-P2.155 update: the DE25 clk_sys is now a 100 MHz IOPLL product (CLOCK2_50
x2), so the DE25 top must drive G_SYS_CLK_HZ => 100_000_000. The threading rule
(never a bare literal) is unchanged; only the DE25 value moved.
"""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "hw" / "rtl" / "deepskopion_core.vhd"
DE25_TOP = ROOT / "hw" / "rtl" / "de25" / "deepskopion_de25_top.vhd"

# Generics that scale by the system clock and so must be driven by G_SYS_CLK_HZ.
CLK_SCALED_GENERICS = ("G_SYS_CLK", "G_CLK_FREQ_HZ", "G_CLK_HZ")


def _generic_map(source: str, instance: str) -> str:
    """Return the generic-map body of a named instance."""
    m = re.search(
        rf"{instance}\s*:\s*entity\b.*?generic map\s*\((.*?)\)\s*port map",
        source,
        re.DOTALL | re.IGNORECASE,
    )
    assert m, f"{instance} instance was not found in deepskopion_core.vhd"
    return m.group(1)


def test_datalogger_gather_threads_the_system_clock_generic():
    seam = _generic_map(CORE.read_text(), "u_datalogger_gather")
    assert re.search(r"G_SYS_CLK\s*=>\s*G_SYS_CLK_HZ", seam, re.IGNORECASE), (
        "DS-T1.10: u_datalogger_gather must take G_SYS_CLK => G_SYS_CLK_HZ so the "
        "datalogger baud math follows the real clock (50 MHz on DE25)"
    )
    assert not re.search(r"G_SYS_CLK\s*=>\s*\d", seam), (
        "DS-T1.10 regression: u_datalogger_gather G_SYS_CLK is a bare literal"
    )


def test_i2c_injector_threads_the_system_clock_generic():
    seam = _generic_map(CORE.read_text(), "u_i2c_injector")
    assert re.search(r"G_CLK_FREQ_HZ\s*=>\s*G_SYS_CLK_HZ", seam, re.IGNORECASE), (
        "DS-T1.10: u_i2c_injector must take G_CLK_FREQ_HZ => G_SYS_CLK_HZ so the "
        "SCL-stuck-low timeout scales with the real clock"
    )
    assert not re.search(r"G_CLK_FREQ_HZ\s*=>\s*\d", seam), (
        "DS-T1.10 regression: u_i2c_injector G_CLK_FREQ_HZ is a bare literal"
    )


def test_no_clk_scaled_generic_in_core_is_a_bare_literal():
    """Broad guard: every clk-scaled generic in the core threads G_SYS_CLK_HZ."""
    core = CORE.read_text()
    offenders = []
    for generic in CLK_SCALED_GENERICS:
        for m in re.finditer(rf"{generic}\s*=>\s*([^,\s)]+)", core, re.IGNORECASE):
            value = m.group(1)
            if re.fullmatch(r"[\d_]+", value):  # a numeric literal, not a name
                offenders.append(f"{generic} => {value}")
    assert not offenders, (
        "DS-T1.10: clk-scaled generic(s) fed a bare literal instead of "
        f"G_SYS_CLK_HZ: {offenders}"
    )


def test_de25_top_drives_the_core_at_100mhz():
    """DS-P2.155: the DE25 clk_sys is now the CLOCK2_50 x2 IOPLL product
    (100 MHz), so the top must drive G_SYS_CLK_HZ to 100e6. Was 50e6 while
    clk_sys was the bare CLOCK2_50; the fabric moved to a 100 MHz IOPLL to
    unify all three legs (DS-D.45) and clock the UART datalogger at 10 ns
    edge-timestamp resolution. The full 100 MHz contract (PLL source, reset
    gating, CTU re-derive) is guarded in
    test_de25_fabric_pll_100mhz_ds_p2_155.py."""
    top = DE25_TOP.read_text()
    m = re.search(
        r"u_core\s*:\s*entity\b.*?generic map\s*\((.*?)\)\s*port map",
        top,
        re.DOTALL | re.IGNORECASE,
    )
    assert m, "u_core instance was not found in deepskopion_de25_top.vhd"
    assert re.search(r"G_SYS_CLK_HZ\s*=>\s*100_000_000", m.group(1), re.IGNORECASE), (
        "DS-P2.155: DE25 top must pass G_SYS_CLK_HZ => 100_000_000 (clk_sys is "
        "the CLOCK2_50 x2 IOPLL product); without it the datalogger/datalogger math "
        "runs on the stale 50 MHz assumption"
    )
    assert not re.search(r"G_SYS_CLK_HZ\s*=>\s*50_000_000", m.group(1), re.IGNORECASE), (
        "DS-P2.155 regression: DE25 top still passes the pre-IOPLL 50_000_000"
    )
