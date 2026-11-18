# SPDX-License-Identifier: MIT
"""DS-T2.65 — structural guard for the DS-T2.60 AXI3 write-burst cap.

The cocotb test_axi3_burst_cap_under_backpressure runs against tb_datalogger_to_ddr's
own u_ddr instance. Before DS-T2.65 the tb hardcoded max_burst_len_g=>16 as a
SEPARATE mirror of deepskopion_top's u_ddr_burst, so reverting the *design* cap
left the cocotb test GREEN — the classic green-test-proves-the-mirror-not-the-
effect trap.

Fix: single-source the cap as axi_pkg.C_ZYNQ7_S_AXI_HP_MAX_BURST, referenced by
BOTH the design (deepskopion_top via deepskopion_core) and the tb, so they
cannot diverge — and this guard asserts (a) the constant is <= the AXI3 16-beat
max, and (b) both instances reference the constant rather than a re-introduced
literal. (DS-T2.76: re-pointed to judge live u_ddr_burst inside deepskopion_core
and u_core in deepskopion_top after the DE25 portable core split). Fast, no sim.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AXI_PKG = ROOT / "hw" / "rtl" / "pkg" / "axi_pkg.vhd"
TOP = ROOT / "hw" / "rtl" / "deepskopion_top.vhd"
CORE = ROOT / "hw" / "rtl" / "deepskopion_core.vhd"
TB = ROOT / "hw" / "sim" / "tb_datalogger_to_ddr.vhd"

CONST = "C_ZYNQ7_S_AXI_HP_MAX_BURST"
AXI3_MAX_BEATS = 16


def _generic_map(src: str, inst: str) -> str:
    """The generic-map body of `<inst> : entity ... generic map ( ... ) port map`."""
    m = re.search(
        rf"{re.escape(inst)}\s*:\s*entity\s+\S+\s+generic\s+map\s*\((.*?)\)\s*port\s+map",
        src, re.IGNORECASE | re.DOTALL,
    )
    assert m, f"{inst} instantiation not found (moved/renamed?)"
    return m.group(1)


def test_cap_constant_is_within_axi3_max():
    m = re.search(rf"constant\s+{CONST}\s*:\s*integer\s*:=\s*(\d+)", AXI_PKG.read_text())
    assert m, f"{CONST} not defined in axi_pkg.vhd"
    beats = int(m.group(1))
    assert beats <= AXI3_MAX_BEATS, (
        f"{CONST}={beats} exceeds the Zynq-7 S_AXI_HP AXI3 16-beat max (DS-T2.60): "
        f"AWLEN>15 into the AXI3 HP port → SmartConnect split/drop → silent data loss."
    )


def test_deepskopion_top_u_ddr_burst_uses_the_constant():
    # DS-T2.76: following the DE25 port, u_ddr_burst moved into deepskopion_core,
    # which generic-maps max_burst_len_g => G_AXI_MAX_BURST. deepskopion_top
    # binds u_core's G_AXI_MAX_BURST => C_ZYNQ7_S_AXI_HP_MAX_BURST.
    # The guard asserts the entire live chain from top constant to live adapter.
    top_gmap = _generic_map(TOP.read_text(), "u_core")
    assert re.search(rf"G_AXI_MAX_BURST\s*=>\s*{CONST}\b", top_gmap), (
        "deepskopion_top's u_core must set G_AXI_MAX_BURST => "
        f"{CONST} (single-sourced). A missing generic (AXI4 default 256) or a "
        "re-introduced literal reintroduces DS-T2.60 while the cocotb TB mirror "
        "stays green."
    )
    core_gmap = _generic_map(CORE.read_text(), "u_ddr_burst")
    assert re.search(r"max_burst_len_g\s*=>\s*G_AXI_MAX_BURST\b", core_gmap), (
        "deepskopion_core's u_ddr_burst must set max_burst_len_g => G_AXI_MAX_BURST "
        "so the top-level burst cap reaches the live DDR burst adapter."
    )


def test_tb_u_ddr_uses_the_same_constant():
    gmap = _generic_map(TB.read_text(), "u_ddr")
    assert re.search(rf"max_burst_len_g\s*=>\s*{CONST}\b", gmap), (
        "tb_datalogger_to_ddr's u_ddr must reference the SAME "
        f"{CONST} as the design, so the AXI3 back-pressure test cannot diverge "
        "from deepskopion_top's real cap (DS-T2.65)."
    )
