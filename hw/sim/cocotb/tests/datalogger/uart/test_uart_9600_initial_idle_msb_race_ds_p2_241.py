"""DS-P2.241: at 9600 8N1, after a 3 ms initial idle, the 7-bit stop-finder
instance matches seven frame-aligned frames because their top data bit is 1.

The 30 calibration bytes and 32 payload bytes below are the ones
test_uart_initial_idle_repro drew at seed 1794154559. The first seven
calibration bytes (AF F0 AF DC D4 E1 D1) all have d7 = 1, so the 7-bit instance
(GEN_STOP_FINDER(2), NBITS 7) reads d7 as its stop bit and matches every frame
with a 10-bit spacing; 0x3F (d7 = 0) refutes it at the eighth. This is the
DS-P2.242 race (test_uart_autosync_msb_race_ds_p2_242 covers it at 1 Mbaud
without an idle); this test pins it at 9600 with the long initial idle and the
mid-stream gap of test_uart_initial_idle_repro.

At 97073018 the detector took the first instance to reach G_MIN_MATCHES and
locked 7 data bits, so the payload was not found. Since df1d12c4 the 8-bit
instance wins.
"""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, Edge, First, RisingEdge, Timer
from cocotb.utils import get_sim_time

current_dir = Path(__file__).resolve().parent
import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    GENERICS, CUSTOM_LIBRARIES, setup_dut, apply_config,
    FastSignalCollector, UartDataloggerMsgReassembler,
)
from rr_cocotb_tb.drivers.uart_master import UartSource

BAUD_RATE = 9600
CLK_PERIOD_NS = 10
INITIAL_IDLE_US = 3000
MID_GAP_US = 3000
CAL_BYTES = [
    0xAF, 0xF0, 0xAF, 0xDC, 0xD4, 0xE1, 0xD1, 0x3F, 0xD8, 0x04, 0x40, 0xC3,
    0x06, 0x95, 0x4D, 0xD6, 0x84, 0x5F, 0x96, 0xEB, 0x37, 0xE7, 0x49, 0xF8,
    0xF0, 0x0E, 0xA1, 0x72, 0x05, 0xD0,
]
PAYLOAD_BYTES = [
    0xCC, 0x6F, 0x5E, 0x81, 0x7E, 0x88, 0xA3, 0xDF, 0xD3, 0xF5, 0xA1, 0xB4,
    0xC2, 0x82, 0x46, 0x63, 0x95, 0xF7, 0x9A, 0x4F, 0xCC, 0x83, 0x2B, 0x7C,
    0x23, 0x29, 0x93, 0xBB, 0x22, 0x5C, 0xED, 0xCA,
]
# Stop samples of the 7-bit instance, in replay bits from its first one.
EXPECTED_7BIT_MATCH_BITS = [0, 10, 20, 30, 40, 50, 60]


LOCK_CEILING_MS = 10


async def _rise_time(sig, out):
    """Record the sim time of the first rise of sig."""
    await RisingEdge(sig)
    out.append(get_sim_time("ns"))


async def _wait_lock(dut, buffer_finish_ns):
    """Wait for calibrationDone after the last cal byte, ceiling LOCK_CEILING_MS.

    Logs the lock time and the buffer-close to lock (drain) latency.
    """
    t0 = get_sim_time("ns")
    if int(dut.calibrationDone_r.value) != 1:
        await First(RisingEdge(dut.calibrationDone_r), Timer(LOCK_CEILING_MS, units="ms"))
    now = get_sim_time("ns")
    elapsed_ms = (now - t0) / 1e6
    assert int(dut.calibrationDone_r.value) == 1, (
        f"calibrationDone not set {elapsed_ms:.3f} ms after the last cal byte "
        f"(ceiling {LOCK_CEILING_MS} ms)")
    drain = (f"{(now - buffer_finish_ns[0]) / 1e6:.4f} ms after bufferFinish"
             if buffer_finish_ns else "bufferFinish not seen")
    dut._log.info(f"calibrationDone at {now / 1e6:.4f} ms: {elapsed_ms:.4f} ms after "
                  f"the last cal byte, {drain}")


async def _record_matches(inst, times_ns):
    """Append the sim time of every rise of cntMatches_r."""
    prev = int(inst.cntMatches_r.value)
    while True:
        await Edge(inst.cntMatches_r)
        cur = int(inst.cntMatches_r.value)
        if cur > prev:
            times_ns.append(get_sim_time("ns"))
        prev = cur


@cocotb.test()
async def test_initial_idle_msb_race_9600(dut):
    assert len(CAL_BYTES) == 30 and len(PAYLOAD_BYTES) == 32
    await setup_dut(dut)
    await apply_config(dut, GENERICS["G_UART_DEV_ID"])          # autoSync on

    seven_bit = dut.GEN_STOP_FINDER[2].i_uart_stop_finder
    assert int(seven_bit.G_CONF_NBITS.value) == 7
    buffer_finish_ns = []
    cocotb.start_soon(_rise_time(dut.bufferFinish_r, buffer_finish_ns))
    match_ns = []
    rec = cocotb.start_soon(_record_matches(seven_bit, match_ns))

    reassembler = UartDataloggerMsgReassembler(8, dut._log)
    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT,
                                    value_fn=lambda v: reassembler.push(v))
    collector_coro = cocotb.start_soon(collector.start())
    uart = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=8, stop_bits=1, parity="none")

    dut.UART_IN.value = 1
    await Timer(INITIAL_IDLE_US, units="us")
    await ClockCycles(dut.CLK, 100)
    await uart.write_bytes(CAL_BYTES)
    await _wait_lock(dut, buffer_finish_ns)
    lock = (int(dut.calibrationDone_r.value), int(dut.calibrationFail_r.value),
            int(dut.nbitsConf_r.value))
    rec.kill()

    br_cycles = int(dut.baudRateMin_r.value)
    bit_ns = br_cycles * CLK_PERIOD_NS
    match_bits = [round((t - match_ns[0]) / bit_ns, 2) for t in match_ns] if match_ns else []
    dut._log.info(f"baudRateMin_r={br_cycles} lock(done,fail,nbits)={lock} "
                  f"7-bit stop samples (replay bits)={match_bits}")

    # The stimulus exercises the frame-aligned path: the 7-bit instance's
    # first seven matches are one frame (10 replay bits) apart.
    assert match_bits[:7] == EXPECTED_7BIT_MATCH_BITS, (
        f"7-bit instance match bits {match_bits[:7]}, expected {EXPECTED_7BIT_MATCH_BITS}")

    assert lock == (1, 0, 8), f"(done, fail, nbits) = {lock}, expected (1, 0, 8)"

    dut.UART_IN.value = 1
    await Timer(MID_GAP_US, units="us")
    await uart.write_bytes(PAYLOAD_BYTES)
    await Timer(4, units="ms")
    collector_coro.kill()
    results = reassembler.get_results()
    dut._log.info(f"Reassembled {len(results)} bytes (sent 62): {bytes(results).hex()}")
    assert results == CAL_BYTES + PAYLOAD_BYTES, (
        f"decoded {len(results)} bytes: {bytes(results).hex()}")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_9600_initial_idle_msb_race_ds_p2_241",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
