"""Cheap static guards for DS-P2.110's testbench-side DDR base-address fix.

The sibling ``tools/tests/test_core_ddr_base_generic_guard.py`` forbids an
INTEGER ``G_DDR_BASE_ADDR`` on the *core* (RTL) seam. This module is its
testbench analogue: it forbids reverting ``hw/sim/tb_datalogger_to_ddr.vhd``'s
``G_DDR_BASE_ADDR`` back to INTEGER, and it pins the product-geometry write-path
guard into a target tag so the ~100 s NVC regression cannot silently drop out of
every batch/CI lane (the "working detector nobody called" failure mode the
landing reviewer refused on).

Why this exists at all: the DE25 product base ``0xA0000000`` = 2_684_354_560
overflows a 32-bit signed VHDL INTEGER ('high = 2_147_483_647), so an INTEGER
generic is STRUCTURALLY incapable of carrying it — it degrades to 0 / a toy DDR
width and the tb silently runs the wrong geometry. These asserts live in the
sub-second pytest lane; the expensive proof is
``test_datalogger_to_ddr_product_base`` itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
TB = ROOT / "hw" / "sim" / "tb_datalogger_to_ddr.vhd"
DE25_DATALOGGER_TARGET = ROOT / "targets" / "de25_datalogger.yml"
PRODUCT_TEST = (
    ROOT
    / "hw"
    / "sim"
    / "cocotb"
    / "tests"
    / "datalogger"
    / "infra"
    / "full_data_path"
    / "test_datalogger_to_ddr_product_base.py"
)
PRODUCT_MODULE = PRODUCT_TEST.stem


def _base_generic_declaration() -> str:
    source = TB.read_text(encoding="utf-8")
    match = re.search(r"G_DDR_BASE_ADDR\s*:\s*([^;:=]+?)\s*(?::=|;)", source)
    assert match, "tb_datalogger_to_ddr no longer declares G_DDR_BASE_ADDR"
    return " ".join(match.group(1).split())


def test_tb_base_generic_is_full_width_vector_not_integer() -> None:
    """The tb base generic must accept every 32-bit base, 0xA0000000 included.

    Reverting it to INTEGER re-opens the DS-P2.110 scaled-sim trap: the DE25
    product base overflows a signed 32-bit INTEGER and silently degrades to 0.
    """
    decl = _base_generic_declaration()
    assert decl == "std_logic_vector(31 downto 0)", (
        f"tb_datalogger_to_ddr G_DDR_BASE_ADDR must be "
        f"'std_logic_vector(31 downto 0)', found {decl!r} — an INTEGER (or any "
        f"narrower type) cannot carry the DE25 product base 0xA0000000"
    )
    assert "integer" not in decl.lower(), (
        "DS-P2.110 regression: G_DDR_BASE_ADDR reverted toward INTEGER"
    )


def test_product_geometry_guard_is_pinned_into_a_runnable_de25_tag() -> None:
    """The guard must live in a target tag, not only a hand-typed rr sim run.

    It belongs in its own DE25-specific tag (256-bit / 0xA0000000), NOT in
    de25_baseline, which is equality-pinned to canfd_demo's xilinx_baseline
    (tools/tests/test_de25_core_baseline_contract.py). This assert keeps the
    membership from silently drifting back out.
    """
    target = yaml.safe_load(DE25_DATALOGGER_TARGET.read_text(encoding="utf-8"))
    tags = target["simulation"]["tags"]
    hosting = [name for name, members in tags.items() if PRODUCT_MODULE in members]
    assert hosting, (
        f"{PRODUCT_MODULE} is in no de25_datalogger tag — it can only be run by "
        f"hand ('working detector nobody called'). Add it to a DE25 tag."
    )
    assert "de25_baseline" not in hosting, (
        f"{PRODUCT_MODULE} must NOT be in de25_baseline: that tag is "
        f"equality-pinned to the Xilinx baseline and this test is DE25-only "
        f"(256-bit, 0xA0000000)."
    )


def test_product_test_drives_the_exact_de25_product_generics() -> None:
    """The guard is only meaningful if it elaborates at DE25 geometry.

    A guard that quietly ran at base 0 / 128-bit would be the very trap
    DS-P2.110 closes, re-opened one layer up.
    """
    source = PRODUCT_TEST.read_text(encoding="utf-8")
    assert '"G_DDR_BASE_ADDR":' in source and 'x"A0000000"' in source, (
        "product test no longer drives base 0xA0000000"
    )
    assert re.search(r'"G_DDR_DATA_WIDTH":\s*256', source), (
        "product test no longer drives the 256-bit f2sdram DDR width"
    )
    assert re.search(r'"G_DDR_ADDR_WIDTH":\s*32', source), (
        "product test no longer drives the 32-bit DDR address width"
    )
