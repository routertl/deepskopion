# rr unknown-llm (AI-generated)
"""Unit test for error_event_emitter (DS-P3.70 piece 3).

Proves the coalescing in-band error reporter: one rate-limited zs_msg ERROR
frame per flush window, carrying the lost_count DELTA, with correct msg_type and
no lost accounting under back-pressure."""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

def _find_root(start):
    for p in [start, *start.parents]:
        if (p / "project.yml").exists() and (p / "hw" / "rtl").is_dir():
            return p
    raise RuntimeError("deepskopion root not found")


ROOT = _find_root(Path(__file__).resolve())
RTL = ROOT / "hw" / "rtl"

FLUSH = 20
# SEV_ERROR(0x2) | DOMAIN_EDGE(0x6) | ERR_EVT_INTERNAL_FIFO_OVERFLOW(0xF0)
EXPECT_MSG_TYPE = (0x2 << 12) | (0x6 << 8) | 0xF0   # 0x26F0
CLASS_STATUS = 0x01


async def _await_valid(dut, limit=3 * FLUSH):
    for _ in range(limit):
        await RisingEdge(dut.CLK)
        if int(dut.FR_VALID.value) == 1:
            return True
    return False


@cocotb.test()
async def test_coalesced_error_reports(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.RST.value = 1
    dut.ERR_CNT.value = 0
    dut.TIMESTAMP.value = 0
    dut.FR_READY.value = 1
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0

    # 1. No movement → no report across several flush windows.
    await ClockCycles(dut.CLK, 3 * FLUSH)
    assert int(dut.FR_VALID.value) == 0, "emitted a report with ERR_CNT unchanged at 0"

    # 2. 5 losses → exactly one report, payload=5, correct msg_type/class.
    dut.ERR_CNT.value = 5
    assert await _await_valid(dut), "no report after ERR_CNT advanced"
    assert int(dut.FR_PAYLOAD.value) == 5, f"delta wrong: {int(dut.FR_PAYLOAD.value)}"
    assert int(dut.FR_MSG_TYPE.value) == EXPECT_MSG_TYPE, f"msg_type {int(dut.FR_MSG_TYPE.value):#06x}"
    assert int(dut.FR_MSG_CLASS.value) == CLASS_STATUS
    assert int(dut.FR_WORDS.value) == 1
    await RisingEdge(dut.CLK)                       # FR_READY=1 → handshake
    assert int(dut.FR_VALID.value) == 0, "report not retired after handshake"

    # 3. Coalescing: a big jump in one window → ONE report carrying the full delta.
    dut.ERR_CNT.value = 5 + 88                      # 88 more losses
    assert await _await_valid(dut)
    assert int(dut.FR_PAYLOAD.value) == 88, f"coalesced delta wrong: {int(dut.FR_PAYLOAD.value)}"
    await RisingEdge(dut.CLK)

    # 4. Back-pressure: FR_READY=0 holds the report; further losses must NOT be
    #    lost — they show up in the next report once the framer accepts.
    dut.FR_READY.value = 0
    dut.ERR_CNT.value = 93 + 100                    # 100 more, report can't go out yet
    assert await _await_valid(dut)
    held = int(dut.FR_PAYLOAD.value)
    assert held == 100, f"held report delta wrong: {held}"
    dut.ERR_CNT.value = 193 + 50                    # 50 MORE while the report is stuck
    await ClockCycles(dut.CLK, 5)
    assert int(dut.FR_VALID.value) == 1 and int(dut.FR_PAYLOAD.value) == 100, \
        "pending report changed under back-pressure"
    dut.FR_READY.value = 1                          # framer accepts the 100-report
    await RisingEdge(dut.CLK)
    # next window must report the 50 that accrued while stuck (no lost accounting)
    assert await _await_valid(dut)
    assert int(dut.FR_PAYLOAD.value) == 50, f"post-backpressure delta wrong: {int(dut.FR_PAYLOAD.value)}"
    dut._log.info("error_event_emitter coalescing + back-pressure accounting: PASS")


def main():
    run_simulation(
        top_level="error_event_emitter",
        module="test_error_event_emitter",
        custom_libraries={"tich": [
            f"{RTL}/pkg/zs_msg_pkg.vhd",
            f"{RTL}/datalogger/infra/error_event_emitter/error_event_emitter.vhd",
        ]},
        generics={"G_FLUSH_CYCLES": FLUSH},
        waves=True,
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
