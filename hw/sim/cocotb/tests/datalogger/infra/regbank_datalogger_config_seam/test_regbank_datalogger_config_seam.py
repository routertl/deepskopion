# rr unknown-llm (AI-generated)
"""
test_regbank_datalogger_config_seam — DS-P3.73 contract.

Locks the writable per-datalogger CONFIG seam: a PS AXI-Lite write to the
DATALOGGER_CONFIG regbank register (0x0020) drives `config_word`, which every
datalogger's config_control demuxes by id — only the ADDRESSED datalogger latches the
payload (CONFIG[10:0]); others are untouched. This is the v1.1 addressed model
(no cfg_valid): CONFIG[15:11]=device_id, CONFIG[10:0]=payload.

The seam under test = regbank.DATALOGGER_CONFIG (RW) → wrapper output (line 269) →
config_word → config_control mux-by-id. The SW applier (DS-P3.72,
apply_datalogger_config) writes exactly this register; this proves the HW half
actually delivers it to the right datalogger.

Hard-coded expected values (ROUTERTL-002).

Run:  rr sim run test_regbank_datalogger_config_seam
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

# Routertl SDK cocotb path (matches every sibling deepskopion test).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

SLOT_DATALOGGER_CONFIG = 0x20      # regbank word 8

# Must match the testbench generics G_DEV_A / G_DEV_B.
DEV_A = 3
DEV_B = 7

# Hard-coded 11-bit payloads (≤ 0x7FF), distinct, not derived at runtime.
PAYLOAD_A = 0x123
PAYLOAD_B = 0x4AB

CLK_PERIOD_NS = 10              # 100 MHz clk_sys


def cfg_word(dev_id: int, payload: int) -> int:
    """Build the 16-bit addressed CONFIG word: [15:11]=dev_id, [10:0]=payload."""
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


async def reset(dut):
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 4)


async def make_axi_master(dut):
    bus = AxiLiteBus.from_prefix(dut, "S_AXI")
    return AxiLiteMaster(bus, dut.CLK, dut.RST)


async def write_config(dut, axi, word: int):
    await axi.write(SLOT_DATALOGGER_CONFIG, word.to_bytes(4, "little"))
    # AXI BVALID → REGS_OUT slot update → config_control latch pipeline (~3 cyc).
    await ClockCycles(dut.CLK, 12)


@cocotb.test()
async def test_addressed_write_latches_only_target(dut):
    """A write addressed to DEV_A latches PAYLOAD_A at datalogger A only; DEV_B is
    untouched. A subsequent write addressed to DEV_B latches PAYLOAD_B at B and
    does NOT disturb A's previously-latched value (the addressed-mux property)."""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await reset(dut)
    axi = await make_axi_master(dut)

    # Post-reset: both config_controls hold 0 (no word seen yet).
    assert int(dut.OBS_CONFIG_OUT_A.value) == 0
    assert int(dut.OBS_CONFIG_OUT_B.value) == 0

    # ── Write addressed to DEV_A. ───────────────────────────────────────
    wa = cfg_word(DEV_A, PAYLOAD_A)
    await write_config(dut, axi, wa)
    word = int(dut.OBS_CONFIG_WORD.value)
    a = int(dut.OBS_CONFIG_OUT_A.value)
    b = int(dut.OBS_CONFIG_OUT_B.value)
    dut._log.info(f"[A] wrote 0x{wa:04X} → CONFIG_WORD=0x{word:04X} A=0x{a:03X} B=0x{b:03X}")
    assert word == wa, f"regbank did not fan out the written word: 0x{word:04X} != 0x{wa:04X}"
    assert a == PAYLOAD_A, f"addressed datalogger A did not latch payload: 0x{a:03X} != 0x{PAYLOAD_A:03X}"
    assert b == 0, f"non-addressed datalogger B latched 0x{b:03X} — mux-by-id leaked"

    # ── Write addressed to DEV_B — A must be undisturbed. ───────────────
    wb = cfg_word(DEV_B, PAYLOAD_B)
    await write_config(dut, axi, wb)
    word = int(dut.OBS_CONFIG_WORD.value)
    a = int(dut.OBS_CONFIG_OUT_A.value)
    b = int(dut.OBS_CONFIG_OUT_B.value)
    dut._log.info(f"[B] wrote 0x{wb:04X} → CONFIG_WORD=0x{word:04X} A=0x{a:03X} B=0x{b:03X}")
    assert word == wb, f"regbank did not fan out the second word: 0x{word:04X} != 0x{wb:04X}"
    assert b == PAYLOAD_B, f"addressed datalogger B did not latch payload: 0x{b:03X} != 0x{PAYLOAD_B:03X}"
    assert a == PAYLOAD_A, (
        f"datalogger A's latch was disturbed by a write addressed to B: "
        f"0x{a:03X} != 0x{PAYLOAD_A:03X} — config_control latched on a non-matching id"
    )

    dut._log.info("✓ DS-P3.73 — addressed DATALOGGER_CONFIG write reaches only the target datalogger")


@cocotb.test()
async def test_unaddressed_id_latches_nothing(dut):
    """A write whose device_id matches NEITHER instance latches at neither —
    config_control only commits on its own id match."""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    await reset(dut)
    axi = await make_axi_master(dut)

    w = cfg_word(15, 0x2CD)   # id 15 ≠ DEV_A(3) ≠ DEV_B(7)
    await write_config(dut, axi, w)
    a = int(dut.OBS_CONFIG_OUT_A.value)
    b = int(dut.OBS_CONFIG_OUT_B.value)
    dut._log.info(f"[none] wrote id=15 → A=0x{a:03X} B=0x{b:03X} (both must stay 0)")
    assert a == 0 and b == 0, f"a non-matching id was latched: A=0x{a:03X} B=0x{b:03X}"

    dut._log.info("✓ DS-P3.73 — a CONFIG write to an absent id latches nowhere")


if __name__ == "__main__":
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH  # noqa: F401  (parity with siblings)

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
    DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

    run_simulation(
        top_level="tb_regbank_datalogger_config_seam",
        module="test_regbank_datalogger_config_seam",
        top_level_lang="vhdl",
        waves=False,
        work_lib="tich",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/rom_info_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/system_regbank_pkg.vhd",
                f"{DEEPSKOPION_RTL}/regbank/system_regbank.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_regbank_datalogger_config_seam.vhd",
            ],
        },
    )
