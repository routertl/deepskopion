"""PF-H0 (DS-P2.124): PolarFire SoC product-geometry write-path guards.

The DS-P2.110 pattern extended to the PolarFire SoC MSS-FIC backend. Its two
siblings own the DE25 leg:

  * tools/tests/test_core_ddr_base_generic_guard.py       — core RTL seam
  * tools/tests/test_tb_datalogger_ddr_base_generic_guard.py — tb + DE25 tag

This module guards the three things the PFSoC port adds, each cross-checked
against the live RTL / target so a mutation to nonsense fails:

  1. the shared core carries a DEFAULT-OFF `G_DDR_BACKEND` generate arm that
     selects rr.ddr_stream_fic — and keeps the Zynq/DE25 `u_ddr_burst` axi arm
     byte-identical (the invariance the whole "no behaviour change" claim rests
     on);
  2. the tb has the matching backend-select arm so a fic body can be elaborated;
  3. the PFSoC FIC product test is pinned into a RUNNABLE PFSoC-only tag (not
     pfsoc_baseline, which is equality-pinned to the Xilinx set) and actually
     drives the fic body at the product geometry (64-bit, base 0xA0000000).

These are the sub-second static half; the expensive proof is
`test_datalogger_to_ddr_product_base_fic` itself, run via the
`pfsoc_product_geometry` tag.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "hw" / "rtl" / "deepskopion_core.vhd"
TB = ROOT / "hw" / "sim" / "tb_datalogger_to_ddr.vhd"
PFSOC_TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"
FIC_TEST = (
    ROOT
    / "hw"
    / "sim"
    / "cocotb"
    / "tests"
    / "datalogger"
    / "infra"
    / "full_data_path"
    / "test_datalogger_to_ddr_product_base_fic.py"
)
FIC_MODULE = FIC_TEST.stem


def test_core_carries_default_off_fic_backend_arm() -> None:
    """The core's G_DDR_BACKEND must default to "axi" and gate a fic arm.

    Default-off is the whole safety argument: Zynq/DE25 elaborate the "axi" arm
    unchanged. A default of "fic" (or a missing default) would silently swing
    every existing target onto the PolarFire body.
    """
    core = CORE.read_text(encoding="utf-8")
    decl = re.search(r"G_DDR_BACKEND\s*:\s*string\s*:=\s*\"([a-z]+)\"", core)
    assert decl, "deepskopion_core no longer declares a G_DDR_BACKEND string generic"
    assert decl.group(1) == "axi", (
        f"G_DDR_BACKEND default must be \"axi\" (Zynq/DE25-preserving), found "
        f"{decl.group(1)!r} — a non-axi default swings the existing targets onto "
        f"the fic body"
    )
    assert re.search(
        r"gen_ddr_fic\s*:\s*if\s+G_DDR_BACKEND\s*=\s*\"fic\"\s+generate",
        core,
    ), "core has no `gen_ddr_fic : if G_DDR_BACKEND = \"fic\" generate` arm"
    assert re.search(r"entity\s+rr\.ddr_stream_fic", core), (
        "core's fic arm no longer instantiates rr.ddr_stream_fic (PF-P0 §3.4 "
        "forbids swapping in ddr_stream_avmm — the wrong tool for the MSS ring)"
    )
    assert "ddr_stream_avmm" not in core, (
        "core must not reach for ddr_stream_avmm (fabric-EMIF Avalon-MM) — the "
        "MSS cannot see it at a stable CPU address (PF-P0 §3.4)"
    )
    # The axi arm must stay intact: same label, same full-width base pass-through.
    axi_gen = re.search(
        r"gen_ddr_axi\s*:\s*if\s+G_DDR_BACKEND\s*=\s*\"axi\"\s+generate(.*?)end generate gen_ddr_axi;",
        core,
        re.DOTALL,
    )
    assert axi_gen, "core's `gen_ddr_axi` arm was removed or renamed"
    assert "u_ddr_burst : entity rr.ddr_stream_axi" in axi_gen.group(1), (
        "the Zynq/DE25 axi arm must keep instantiating ddr_stream_axi as u_ddr_burst"
    )
    assert re.search(
        r"ddr_base_addr_g\s*=>\s*std_logic_vector\s*\(\s*G_DDR_BASE_ADDR\s*\)",
        axi_gen.group(1),
    ), "the axi arm must keep passing the full 32-bit base as a vector (DS-T3.32)"


def test_fic_arm_passes_full_ddr_base_without_integer_conversion() -> None:
    """The fic arm must retain all 32 bits of 0xA0000000, same as the axi arm."""
    core = CORE.read_text(encoding="utf-8")
    fic_gen = re.search(
        r"gen_ddr_fic\s*:\s*if.*?generate(.*?)end generate gen_ddr_fic;",
        core,
        re.DOTALL,
    )
    assert fic_gen, "core's `gen_ddr_fic` arm was removed"
    seam = fic_gen.group(1)
    assert re.search(
        r"ddr_base_addr_g\s*=>\s*std_logic_vector\s*\(\s*G_DDR_BASE_ADDR\s*\)",
        seam,
    ), "fic arm must pass the complete unsigned DDR base as a vector"
    assert not re.search(r"to_integer\s*\(\s*G_DDR_BASE_ADDR", seam), (
        "0xA0000000 cannot be represented by a signed 32-bit VHDL INTEGER during "
        "fic-arm elaboration"
    )


def test_tb_has_backend_select_arm_defaulting_to_axi() -> None:
    """The tb must be able to elaborate the fic body, default-off."""
    tb = TB.read_text(encoding="utf-8")
    decl = re.search(r"G_DDR_BACKEND\s*:\s*string\s*:=\s*\"([a-z]+)\"", tb)
    assert decl and decl.group(1) == "axi", (
        "tb_datalogger_to_ddr must declare G_DDR_BACKEND string default \"axi\" "
        "so existing axi-geometry tests are byte-unchanged"
    )
    assert re.search(
        r"gen_ddr_fic\s*:\s*if\s+G_DDR_BACKEND\s*=\s*\"fic\"\s+generate",
        tb,
    ), "tb has no fic arm to elaborate the PolarFire body"
    assert "entity rr.ddr_stream_fic" in tb, (
        "tb fic arm no longer instantiates rr.ddr_stream_fic"
    )


def test_pfsoc_product_guard_is_pinned_into_a_runnable_pfsoc_tag() -> None:
    """The guard must live in a target tag, not only a hand-typed rr sim run.

    It belongs in its own PFSoC-specific tag (64-bit fic / 0xA0000000), NOT in
    pfsoc_baseline, which is equality-pinned to canfd_demo's xilinx_baseline.
    """
    target = yaml.safe_load(PFSOC_TARGET.read_text(encoding="utf-8"))
    tags = target["simulation"]["tags"]
    hosting = [name for name, members in tags.items() if FIC_MODULE in members]
    assert hosting, (
        f"{FIC_MODULE} is in no pfsoc_datalogger tag — it could only run by hand "
        f"('working detector nobody called'). Add it to a PFSoC tag."
    )
    assert "pfsoc_baseline" not in hosting, (
        f"{FIC_MODULE} must NOT be in pfsoc_baseline: that tag is equality-pinned "
        f"to the Xilinx baseline and this test is PFSoC-only (64-bit fic, "
        f"0xA0000000)."
    )


def test_pfsoc_product_test_drives_the_exact_fic_product_generics() -> None:
    """The guard is only meaningful if it elaborates the fic body at PFSoC geometry.

    A guard that quietly ran the axi body at base 0 / 128-bit would be the very
    trap DS-P2.110 closes, re-opened on the wrong backend.
    """
    source = FIC_TEST.read_text(encoding="utf-8")
    assert '"G_DDR_BASE_ADDR":' in source and 'x"A0000000"' in source, (
        "fic product test no longer drives base 0xA0000000"
    )
    assert re.search(r'"G_DDR_DATA_WIDTH":\s*64', source), (
        "fic product test no longer drives the 64-bit FIC DDR width (PF-P0 §3.4)"
    )
    assert re.search(r'"G_DDR_BACKEND":\s*"fic"', source), (
        "fic product test no longer selects the fic backend — it would silently "
        "run the axi body and prove nothing about the PolarFire ring"
    )
    assert re.search(r'"G_DDR_ADDR_WIDTH":\s*32', source), (
        "fic product test no longer drives the 32-bit DDR address width"
    )
