"""Unit test for maxfreq_event_emitter (DS-P3.117).

Proves the IN-BAND per-line max-frequency reporter: on a MAX_NEW pulse (a new,
smaller min edge-to-edge interval was latched) or a RE_ARM pulse (host re-ping),
the emitter frames a zs_msg with msg_type = SEV_INFO|DOMAIN_EDGE|EVT_EDGE_MAXFREQ
(0x0602), the latched interval in the payload, and the 8-byte payload laid out
byte-exact to the SW sw/stream/zs_msg.py EDGE_MAXFREQ_STRUCT (<IBBH>).
"""
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

# Hard-coded contract values (ROUTERTL-002 — not derived from the DUT at runtime).
# G_DATALOGGER_ID is an slv generic — left at its default (0) rather than overridden
# (passing an int to an slv generic is not portable across simulators; the
# per-channel id wiring is exercised by the gather/top integration, not here).
DATALOGGER_ID = 0
IVAL_WIDTH = 24
# SEV_INFO(0x0) | DOMAIN_EDGE(0x6) | EVT_EDGE_MAXFREQ(0x02)
EXPECT_MSG_TYPE = (0x0 << 12) | (0x6 << 8) | 0x02   # 0x0602
CLASS_DATA = 0x00
SENTINEL = (1 << IVAL_WIDTH) - 1                     # 0x00FFFFFF


def _expected_payload(interval: int) -> int:
    """The 64-bit FR_PAYLOAD = {word1, word0} the emitter must drive.

    word0 = min_interval (32b). word1 = flags(16=0) | ival_width(8) | channel(8).
    This is the byte-exact image of the SW <IBBH> EDGE_MAXFREQ payload: word0 is
    the LE u32 interval; word1 low byte = channel, next = ival_width, top u16 =
    flags. Hard-coded layout (ROUTERTL-002)."""
    word0 = interval & 0xFFFFFFFF
    word1 = (0x0000 << 16) | (IVAL_WIDTH << 8) | DATALOGGER_ID
    return (word1 << 32) | word0


async def _settle(dut):
    await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)


@cocotb.test()
async def test_emit_on_max_new(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.RST.value = 1
    dut.MAX_INTERVAL.value = SENTINEL
    dut.MAX_NEW.value = 0
    dut.RE_ARM.value = 0
    dut.TIMESTAMP.value = 0
    dut.FR_READY.value = 1
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 3)

    # No trigger → no frame.
    assert int(dut.FR_VALID.value) == 0, "emitted with no MAX_NEW/RE_ARM trigger"

    # A new min interval of 50 ticks is latched → emit EVT_EDGE_MAXFREQ.
    dut.MAX_INTERVAL.value = 50
    dut.TIMESTAMP.value = 0xABCD
    dut.MAX_NEW.value = 1
    await RisingEdge(dut.CLK)
    dut.MAX_NEW.value = 0
    await RisingEdge(dut.CLK)   # registered snapshot now visible

    assert int(dut.FR_VALID.value) == 1, "no frame after MAX_NEW pulse"
    assert int(dut.FR_MSG_TYPE.value) == EXPECT_MSG_TYPE, \
        f"msg_type {int(dut.FR_MSG_TYPE.value):#06x} != 0x0602"
    assert int(dut.FR_MSG_CLASS.value) == CLASS_DATA
    assert int(dut.FR_DATALOGGER_ID.value) == DATALOGGER_ID
    assert int(dut.FR_WORDS.value) == 2, "EDGE_MAXFREQ payload is 2 words (8 B)"
    assert int(dut.FR_TIMESTAMP.value) == 0xABCD, "timestamp not snapshotted"
    assert int(dut.FR_PAYLOAD.value) == _expected_payload(50), \
        f"payload {int(dut.FR_PAYLOAD.value):#018x} != {_expected_payload(50):#018x}"

    # FR_READY=1 → the frame retires next cycle.
    await RisingEdge(dut.CLK)
    assert int(dut.FR_VALID.value) == 0, "frame not retired after handshake"


@cocotb.test()
async def test_emit_on_re_arm_sends_sentinel(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.RST.value = 1
    dut.MAX_INTERVAL.value = 50
    dut.MAX_NEW.value = 0
    dut.RE_ARM.value = 0
    dut.TIMESTAMP.value = 0
    dut.FR_READY.value = 1
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 3)

    # Host re-arm: the datalogger cleared the latch to the all-ones sentinel and the
    # RE_ARM pulse fires. The emitter must report the sentinel so the consumer
    # learns the line was re-armed (freq → None on the SW side).
    dut.MAX_INTERVAL.value = SENTINEL
    dut.RE_ARM.value = 1
    await RisingEdge(dut.CLK)
    dut.RE_ARM.value = 0
    await RisingEdge(dut.CLK)

    assert int(dut.FR_VALID.value) == 1, "no frame after RE_ARM pulse"
    assert int(dut.FR_MSG_TYPE.value) == EXPECT_MSG_TYPE
    assert int(dut.FR_PAYLOAD.value) == _expected_payload(SENTINEL), \
        "re-arm frame must carry the all-ones sentinel interval"


@cocotb.test()
async def test_refresh_to_latest_under_backpressure(dut):
    """FR_READY held low: a second MAX_NEW must REFRESH the pending snapshot to
    the newest (smaller) interval — the value is a running minimum, so the latest
    is the one worth sending; no accounting is lost."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.RST.value = 1
    dut.MAX_INTERVAL.value = SENTINEL
    dut.MAX_NEW.value = 0
    dut.RE_ARM.value = 0
    dut.TIMESTAMP.value = 0
    dut.FR_READY.value = 0      # framer stalled
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 3)

    # First new-min = 80.
    dut.MAX_INTERVAL.value = 80
    dut.MAX_NEW.value = 1
    await RisingEdge(dut.CLK)
    dut.MAX_NEW.value = 0
    await RisingEdge(dut.CLK)
    assert int(dut.FR_VALID.value) == 1
    assert int(dut.FR_PAYLOAD.value) == _expected_payload(80)

    # A smaller min = 40 arrives while still stalled → snapshot refreshes to 40.
    dut.MAX_INTERVAL.value = 40
    dut.MAX_NEW.value = 1
    await RisingEdge(dut.CLK)
    dut.MAX_NEW.value = 0
    await RisingEdge(dut.CLK)
    assert int(dut.FR_VALID.value) == 1, "frame dropped under back-pressure"
    assert int(dut.FR_PAYLOAD.value) == _expected_payload(40), \
        "pending snapshot did not refresh to the newest min under back-pressure"

    # Release the framer → frame retires.
    dut.FR_READY.value = 1
    await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)
    assert int(dut.FR_VALID.value) == 0, "frame not retired after release"
    dut._log.info("maxfreq_event_emitter emit + sentinel + refresh: PASS")


def main():
    run_simulation(
        top_level="maxfreq_event_emitter",
        module="test_maxfreq_event_emitter",
        custom_libraries={"tich": [
            f"{RTL}/pkg/zs_msg_pkg.vhd",
            f"{RTL}/datalogger/infra/maxfreq_event_emitter/maxfreq_event_emitter.vhd",
        ]},
        generics={"G_IVAL_WIDTH": IVAL_WIDTH},
        waves=True,
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
