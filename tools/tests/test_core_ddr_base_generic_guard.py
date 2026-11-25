# rr unknown-llm (AI-generated)
"""Regression guard for DS-T3.32's full-width DDR base-address path."""

from pathlib import Path
import re
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "hw" / "rtl" / "deepskopion_core.vhd"
DDR_AXI = ROOT / "libs" / "routertl" / "rr_ddr_stream" / "rtl" / "ddr_stream_axi.vhd"
DDR_AVMM = ROOT / "libs" / "routertl" / "rr_ddr_stream" / "rtl" / "ddr_stream_avmm.vhd"
sys.path.insert(0, str(ROOT / "tools" / "tests"))
from fetched_packages import ensure_fetched  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _rr_ddr_stream_fetched():
    """DS-P2.360: rr_ddr_stream is fetched at the ip.lock pin, not tracked; fail (never skip) without it."""
    ensure_fetched("routertl/rr_ddr_stream")


def test_core_passes_full_ddr_base_without_integer_conversion():
    """The production core seam must retain all bits of xA0000000."""
    core = CORE.read_text()
    generic_map = re.search(
        r"u_ddr_burst\s*:.*?generic map\s*\((.*?)\)\s*port map",
        core,
        re.DOTALL | re.IGNORECASE,
    )
    assert generic_map, "deepskopion_core's production DDR instance was not found"
    seam = generic_map.group(1)
    assert re.search(
        r"ddr_base_addr_g\s*=>\s*std_logic_vector\s*\(\s*G_DDR_BASE_ADDR\s*\)",
        seam,
        re.IGNORECASE,
    ), "production core must pass the complete unsigned DDR base as a vector"
    assert not re.search(r"to_integer\s*\(\s*G_DDR_BASE_ADDR", seam, re.IGNORECASE), (
        "DS-T3.32 regression: xA0000000 cannot be represented by a signed "
        "32-bit VHDL INTEGER during core elaboration"
    )


def _assert_full_width_generic(path: Path):
    source = path.read_text()
    declaration = re.search(
        r"ddr_base_addr_g\s*:\s*([^;]+);", source, re.IGNORECASE
    )
    assert declaration, f"{path.name} base-address generic was not found"
    assert "std_logic_vector(31 downto 0)" in " ".join(declaration.group(1).split()), (
        f"{path.name} must accept every 32-bit base address without INTEGER narrowing"
    )


def test_ddr_axi_generic_accepts_the_full_32_bit_address_domain():
    """The AXI callee must not narrow the core's vector back to INTEGER."""
    _assert_full_width_generic(DDR_AXI)


def test_ddr_avmm_generic_accepts_the_full_32_bit_address_domain():
    """The backend-symmetric AVMM API must retain the same address domain."""
    _assert_full_width_generic(DDR_AVMM)
