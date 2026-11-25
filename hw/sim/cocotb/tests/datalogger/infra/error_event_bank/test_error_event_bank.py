# rr unknown-llm (AI-generated)
"""Unit test for error_event_bank (DS-T3.18 — per-channel loss provenance).

Proves the N-channel bank: each EDGE channel's running loss count produces a
SEV_ERROR frame tagged with THAT channel's datalogger_id and THAT channel's
lost_count delta (independent per-channel baselines), and the fixed-priority
N:1 merge serializes concurrent reports without merging their deltas.

The TB (tb_error_event_bank.vhd) fixes 4 channels with distinct ids:
    ch0=0x11  ch1=0x22  ch2=0x33  ch3=0x44   (packed low-byte-first)

Hard-coded driven values + independent expected ids (ROUTERTL-002)."""
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
CH_IDS = {0: 0x11, 1: 0x22, 2: 0x33, 3: 0x44}


def _pack(counts: dict) -> int:
    """Pack {channel: count} into the 128-bit ERR_CNTS bus (ch k in [k*32 +: 32])."""
    word = 0
    for ch, val in counts.items():
        word |= (val & 0xFFFFFFFF) << (ch * 32)
    return word


async def _await_valid(dut, limit=3 * FLUSH):
    for _ in range(limit):
        await RisingEdge(dut.CLK)
        if int(dut.FR_VALID.value) == 1:
            return True
    return False


@cocotb.test()
async def test_per_channel_attribution(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.RST.value = 1
    dut.ERR_CNTS.value = 0
    dut.TIMESTAMP.value = 0
    dut.FR_READY.value = 1
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0

    # 1. No movement on any channel → no report.
    await ClockCycles(dut.CLK, 3 * FLUSH)
    assert int(dut.FR_VALID.value) == 0, "emitted a report with all channels idle"

    # 2. Overflow channel 2 ONLY → one report tagged with ch2's id (0x33), delta=7.
    counts = {2: 7}
    dut.ERR_CNTS.value = _pack(counts)
    assert await _await_valid(dut), "no report after channel 2 advanced"
    assert int(dut.FR_DATALOGGER_ID.value) == CH_IDS[2], \
        f"ch2 report tagged {int(dut.FR_DATALOGGER_ID.value):#04x}, expected {CH_IDS[2]:#04x}"
    assert int(dut.FR_PAYLOAD.value) == 7, f"ch2 delta wrong: {int(dut.FR_PAYLOAD.value)}"
    assert int(dut.FR_MSG_TYPE.value) == EXPECT_MSG_TYPE
    assert int(dut.FR_MSG_CLASS.value) == CLASS_STATUS
    assert int(dut.FR_WORDS.value) == 1
    await RisingEdge(dut.CLK)                       # FR_READY=1 → handshake
    assert int(dut.FR_VALID.value) == 0, "report not retired after handshake"

    # 3. Overflow channel 0 ONLY → independent baseline: ch0 report id 0x11, delta=20.
    counts[0] = 20
    dut.ERR_CNTS.value = _pack(counts)
    assert await _await_valid(dut), "no report after channel 0 advanced"
    assert int(dut.FR_DATALOGGER_ID.value) == CH_IDS[0], \
        f"ch0 report tagged {int(dut.FR_DATALOGGER_ID.value):#04x}, expected {CH_IDS[0]:#04x}"
    assert int(dut.FR_PAYLOAD.value) == 20, \
        f"ch0 delta wrong (baseline leaked across channels?): {int(dut.FR_PAYLOAD.value)}"
    await RisingEdge(dut.CLK)
    assert int(dut.FR_VALID.value) == 0

    # 4. Concurrent overflow on ch1 AND ch3 in the same window. The merge must
    #    serialize: ch1 (lower index) wins first with ITS delta+id, then ch3 —
    #    NOT a single merged frame, NOT a summed delta.
    counts[1] = 11        # +11 on ch1 (baseline 0)
    counts[3] = 44        # +44 on ch3 (baseline 0)
    dut.ERR_CNTS.value = _pack(counts)
    assert await _await_valid(dut), "no report after concurrent ch1+ch3 overflow"
    # First: ch1
    assert int(dut.FR_DATALOGGER_ID.value) == CH_IDS[1], \
        f"first concurrent report tagged {int(dut.FR_DATALOGGER_ID.value):#04x}, expected ch1 {CH_IDS[1]:#04x}"
    assert int(dut.FR_PAYLOAD.value) == 11, \
        f"ch1 delta wrong (merged with ch3?): {int(dut.FR_PAYLOAD.value)}"
    await RisingEdge(dut.CLK)                       # ch1 handshakes
    # Second: ch3 follows immediately (still pending, now selected)
    assert int(dut.FR_VALID.value) == 1, "ch3 report dropped after ch1 retired"
    assert int(dut.FR_DATALOGGER_ID.value) == CH_IDS[3], \
        f"second concurrent report tagged {int(dut.FR_DATALOGGER_ID.value):#04x}, expected ch3 {CH_IDS[3]:#04x}"
    assert int(dut.FR_PAYLOAD.value) == 44, f"ch3 delta wrong: {int(dut.FR_PAYLOAD.value)}"
    await RisingEdge(dut.CLK)
    assert int(dut.FR_VALID.value) == 0, "spurious report after both concurrent frames drained"

    dut._log.info("error_event_bank per-channel attribution + concurrent merge: PASS")


def main():
    run_simulation(
        top_level="tb_error_event_bank",
        module="test_error_event_bank",
        custom_libraries={"tich": [
            f"{RTL}/pkg/zs_msg_pkg.vhd",
            f"{RTL}/datalogger/infra/error_event_emitter/error_event_emitter.vhd",
            f"{RTL}/datalogger/infra/error_event_bank/error_event_bank.vhd",
            f"{ROOT}/hw/sim/cocotb/tb/tb_error_event_bank.vhd",
        ]},
        generics={"G_FLUSH_CYCLES": FLUSH},
        waves=True,
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
