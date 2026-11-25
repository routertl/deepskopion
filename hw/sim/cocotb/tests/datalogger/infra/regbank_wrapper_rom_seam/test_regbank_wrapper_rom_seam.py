# rr unknown-llm (AI-generated)
"""test_regbank_wrapper_rom_seam — DS-T4.1 contract.

system_regbank_wrapper's two BRAM-init ROMs (regbank manifest + build-config)
are loaded by regbank_rom.vhd via a plain TEXTIO ``file ... open read_mode is
G_HEX_FILE``. G_HEX_FILE used to be hardwired, inside the wrapper, to the
clone-relative constants baked into rom_info_pkg (``hw/rtl/regbank/
regbank_schema.hex`` etc.) — paths a simulator resolves against its OWN
process cwd at elaboration time, never against the source file's location.
Every other regbank seam tb in this tree dodges the problem by instantiating
the INNER system_regbank instead of the wrapper (see their "not
system_regbank_wrapper" comments) — a workaround, not a fix, and one that
only exists because nothing here elaborated the wrapper's ROMs at all.

The fix: system_regbank_wrapper now exposes G_REGBANK_HEX_FILE /
G_CONFIG_HEX_FILE generics (default = the same clone-relative constants, so
nothing that doesn't override them changes behaviour). This test is the seam
that DOES override them — with ABSOLUTE paths computed from this test
module's own ``__file__`` (never from ``os.getcwd()``) — and then proves the
wrapper elaborates and its ROMs actually contain the real manifests by
reading the first streamed word of each through the real AXI-Lite REGBANK_
DATA / CONFIG_DATA protocol and comparing it to the .hex file's own first
line. (The companion *_NUM_WORDS slots are deliberately NOT asserted here:
they're populated by TELEMETRY_IN, which only deepskopion_core.vhd drives —
this bare wrapper tb ties TELEMETRY_IN to all-zero by design, same as
tb_regbank_axilite_mon. Out of scope for this ticket, which is the ROM file
path, not the telemetry fan-in.) Run this suite from an arbitrary temp cwd
(as the harness does below, and as `rr sim run test_regbank_wrapper_rom_seam`
does when invoked from anywhere) and it must still pass — that is the whole
point of the ticket.

Run:  rr sim run test_regbank_wrapper_rom_seam
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

# Routertl SDK cocotb path (matches every sibling deepskopion test).
import routertl_core as _rrc

sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

# ── Absolute paths, derived from THIS FILE's location — never from cwd. ──
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"
REGBANK_HEX = DEEPSKOPION_RTL / "regbank" / "regbank_schema.hex"
CONFIG_HEX = DEEPSKOPION_RTL / "regbank" / "config_schema.hex"

# Byte-offset slots (== C_INDEX_* in system_regbank_pkg.vhd). The pointer
# for each ROM starts at 0 out of reset, so the first DATA read (with no
# preceding NUM_WORDS read) already returns word 0 of the .hex file.
SLOT_REGBANK_DATA = 0x14  # word 5
SLOT_CONFIG_DATA = 0x1C   # word 7

CLK_PERIOD_NS = 10  # 100 MHz clk_sys

GENERICS = {
    "G_REGBANK_HEX_FILE": str(REGBANK_HEX),
    "G_CONFIG_HEX_FILE": str(CONFIG_HEX),
}


def _first_word(hex_path: Path) -> int:
    with hex_path.open("r", encoding="ascii") as f:
        return int(f.readline().strip(), 16)


async def reset(dut):
    dut.RST.value = 1
    dut.S_AXI_AWADDR.value = 0
    dut.S_AXI_AWVALID.value = 0
    dut.S_AXI_WDATA.value = 0
    dut.S_AXI_WSTRB.value = 0
    dut.S_AXI_WVALID.value = 0
    dut.S_AXI_BREADY.value = 0
    dut.S_AXI_ARADDR.value = 0
    dut.S_AXI_ARVALID.value = 0
    dut.S_AXI_RREADY.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 4)


async def axi_read(dut, addr: int) -> int:
    dut.S_AXI_ARADDR.value = addr
    dut.S_AXI_ARVALID.value = 1
    while True:
        await RisingEdge(dut.CLK)
        if int(dut.S_AXI_ARREADY.value) == 1:
            break
    dut.S_AXI_ARVALID.value = 0

    while True:
        await RisingEdge(dut.CLK)
        if int(dut.S_AXI_RVALID.value) == 1:
            break
    data = int(dut.S_AXI_RDATA.value)
    dut.S_AXI_RREADY.value = 1
    await RisingEdge(dut.CLK)
    dut.S_AXI_RREADY.value = 0
    return data


@cocotb.test()
async def test_wrapper_elaborates_and_roms_load_real_manifests(dut):
    """The seam: elaborate the REAL system_regbank_wrapper (ROMs included)
    with hex paths supplied as absolute generics, and prove each ROM was
    genuinely loaded — not left at the BAD00BAD sentinel — by reading its
    first streamed word over AXI-Lite and comparing against the actual
    .hex file content on disk."""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await reset(dut)

    first_regbank_word = await axi_read(dut, SLOT_REGBANK_DATA)
    expected_regbank_word = _first_word(REGBANK_HEX)
    assert first_regbank_word == expected_regbank_word, (
        f"regbank ROM first word 0x{first_regbank_word:08X} != file content "
        f"0x{expected_regbank_word:08X} — either the ROM never loaded "
        f"(cwd-relative G_HEX_FILE couldn't find the file from this cwd) or "
        f"it loaded the wrong file."
    )

    first_config_word = await axi_read(dut, SLOT_CONFIG_DATA)
    expected_config_word = _first_word(CONFIG_HEX)
    assert first_config_word == expected_config_word, (
        f"config ROM first word 0x{first_config_word:08X} != file content "
        f"0x{expected_config_word:08X}."
    )

    dut._log.info(
        "✓ DS-T4.1 — system_regbank_wrapper elaborated with absolute ROM "
        "hex paths and both ROMs contain the real manifests, independent of "
        "the invoking process's cwd"
    )


# ─────────────────────────────────────────────────────────────────────────
#  Standalone Execution (ROUTERTL-001 — engine.simulation.run_simulation only)
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from engine.simulation import run_simulation

    run_simulation(
        top_level="tb_regbank_wrapper_rom_seam",
        module="test_regbank_wrapper_rom_seam",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/rom_info_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/system_regbank_pkg.vhd",
                f"{DEEPSKOPION_RTL}/regbank/system_regbank.vhd",
                f"{DEEPSKOPION_RTL}/regbank/regbank_rom.vhd",
                f"{DEEPSKOPION_RTL}/regbank/system_regbank_wrapper.vhd",
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_regbank_wrapper_rom_seam.vhd",
            ],
        },
    )
