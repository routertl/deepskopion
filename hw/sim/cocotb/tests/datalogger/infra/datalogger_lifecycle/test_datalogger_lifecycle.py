"""DS-P3.90 Phase 1: datalogger_lifecycle — symmetric ENABLED/DISABLED emitter.

Verifies the always-on lifecycle component emits exactly one ENABLED frame on
the CONFIG_EN rising edge and one DISABLED frame on the falling edge, with the
correct frame layout (DEV_ID | TYPE | TIMESTAMP | code), and is silent while
CONFIG_EN is steady. Uses the entity-default generics (5b dev id, 3b type
"100", 16b ts, 8b codes 0x03/0x00 -> 32b frame); only G_DEV_ID is overridden.

Run:  rm -rf hw/sim/work && rr sim run test_datalogger_lifecycle
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation
from sdk.cocotb_helpers import requires

DEEPSKOPION_RTL = Path(__file__).resolve().parents[7] / "hw" / "rtl"

DEV_ID = 12
TYPE = 0b100
TS = 0xABCD
ENABLED = 0x03
DISABLED = 0x00

GENERICS = {"G_DEV_ID": DEV_ID}


def frame(code):
    return (DEV_ID << 27) | (TYPE << 24) | (TS << 8) | (code & 0xFF)


async def _capture_next_vld(dut, max_cycles=20):
    """Return MSG_OUT on the next cycle MSG_VLD is high, else None."""
    for _ in range(max_cycles):
        await RisingEdge(dut.CLK)
        if int(dut.MSG_VLD.value) == 1:
            return int(dut.MSG_OUT.value)
    return None


@requires("SL-REQ-1", "SL-REQ-2", "SL-REQ-3", "SL-REQ-4")
@cocotb.test()
async def lifecycle_enable_disable(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.RST.value = 1
    dut.CONFIG_EN.value = 0
    dut.TIMESTAMP.value = TS
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 3)

    # No spurious frame while disabled+steady
    quiet = await _capture_next_vld(dut, 8)
    assert quiet is None, f"spurious frame while CONFIG_EN steady-low: {quiet:#010x}"

    # Rising edge -> exactly one ENABLED frame
    dut.CONFIG_EN.value = 1
    got = await _capture_next_vld(dut, 6)
    assert got == frame(ENABLED), f"ENABLED frame wrong: {got:#010x} != {frame(ENABLED):#010x}"
    dut._log.info(f"ENABLED frame OK: {got:#010x}")
    # ...and then silence while held enabled
    quiet = await _capture_next_vld(dut, 8)
    assert quiet is None, f"spurious frame while CONFIG_EN steady-high: {quiet:#010x}"

    # Falling edge -> exactly one DISABLED frame
    dut.CONFIG_EN.value = 0
    got = await _capture_next_vld(dut, 6)
    assert got == frame(DISABLED), f"DISABLED frame wrong: {got:#010x} != {frame(DISABLED):#010x}"
    dut._log.info(f"DISABLED frame OK: {got:#010x}")

    # Re-enable after a global RST still emits ENABLED (en_r cleared by RST)
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 3)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 2)
    dut.CONFIG_EN.value = 1
    got = await _capture_next_vld(dut, 6)
    assert got == frame(ENABLED), f"post-RST ENABLED wrong: {got:#010x}"
    dut._log.info("post-RST re-enable emits ENABLED OK")


def main():
    run_simulation(
        top_level="datalogger_lifecycle",
        module="test_datalogger_lifecycle",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
            ]
        },
        generics=GENERICS,
        waves=False,
    )


if __name__ == "__main__":
    main()
