# rr unknown-llm (AI-generated)
"""DS-T2.94: at 9600 8N1 the 5-bit stop-finder instance loses frame alignment
and still collects G_MIN_MATCHES matches by re-syncing on in-frame edges.

The 32 calibration bytes below are the ones test_integration_9600_8N1 drew at
seed 1794146657. The 5-bit instance (GEN_STOP_FINDER(0), NBITS 5) samples d5
as its stop bit on frames 0-2 (FF E7 66). In 0x66, d6 = 1 and d7 = 0, so it
takes the 1-to-0 edge at d7 as a start bit. From there it starts on in-frame
falling edges, and each false frame's stop sample reads 1 until replay bit 72.
Its stop samples fall at replay bits 0, 10, 20, 28, 35, 42, 50 (counted from
its first stop sample); the 8/7/7/8 spacing is the misalignment, a frame being
10 bits. The frame-aligned MSB race of DS-P2.242 instead has a 10-bit spacing
throughout.

At 97073018 the detector took the first instance to reach G_MIN_MATCHES and
locked 5 data bits (23/36 bytes reassembled). Since df1d12c4 the word length
is decided on the whole calibration buffer: the 5-bit instance is refuted at
bit 72 and the 8-bit instance wins.

The replay runs one bit per baudRateMin_r clock cycles, so the spacing is
measured in units of baudRateMin_r cycles.
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
CAL_BYTES = [
    0xFF, 0xE7, 0x66, 0xAC, 0xDD, 0x37, 0x0D, 0x65, 0x57, 0xB9, 0x60, 0x0A,
    0x25, 0x0E, 0x5D, 0x34, 0xED, 0x02, 0xF7, 0x58, 0xD9, 0xF4, 0x74, 0x53,
    0x6A, 0x64, 0x41, 0x0D, 0x39, 0x99, 0x72, 0x4E,
]
PAYLOAD_BYTES = [0x9E, 0xE4, 0x94, 0xD1]
# Stop samples of the 5-bit instance, in replay bits from its first one.
EXPECTED_5BIT_MATCH_BITS = [0, 10, 20, 28, 35, 42, 50]


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
async def test_misaligned_resync_9600(dut):
    assert len(CAL_BYTES) == 32 and len(PAYLOAD_BYTES) == 4
    await setup_dut(dut)
    await apply_config(dut, GENERICS["G_UART_DEV_ID"])          # autoSync on
    await ClockCycles(dut.CLK, 100)

    five_bit = dut.GEN_STOP_FINDER[0].i_uart_stop_finder
    assert int(five_bit.G_CONF_NBITS.value) == 5
    buffer_finish_ns = []
    cocotb.start_soon(_rise_time(dut.bufferFinish_r, buffer_finish_ns))
    match_ns = []
    rec = cocotb.start_soon(_record_matches(five_bit, match_ns))

    reassembler = UartDataloggerMsgReassembler(8, dut._log)
    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT,
                                    value_fn=lambda v: reassembler.push(v))
    collector_coro = cocotb.start_soon(collector.start())
    uart = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=8, stop_bits=1, parity="none")

    await uart.write_bytes(CAL_BYTES)
    await _wait_lock(dut, buffer_finish_ns)
    lock = (int(dut.calibrationDone_r.value), int(dut.calibrationFail_r.value),
            int(dut.nbitsConf_r.value))
    rec.kill()

    br_cycles = int(dut.baudRateMin_r.value)
    bit_ns = br_cycles * CLK_PERIOD_NS
    match_bits = [round((t - match_ns[0]) / bit_ns, 2) for t in match_ns] if match_ns else []
    dut._log.info(f"baudRateMin_r={br_cycles} lock(done,fail,nbits)={lock} "
                  f"5-bit stop samples (replay bits)={match_bits}")

    # The stimulus exercises the misalignment path: the 5-bit instance's
    # first seven matches are at these replay bits, not every 10 bits.
    assert match_bits[:7] == EXPECTED_5BIT_MATCH_BITS, (
        f"5-bit instance match bits {match_bits[:7]}, expected {EXPECTED_5BIT_MATCH_BITS}")

    assert lock == (1, 0, 8), f"(done, fail, nbits) = {lock}, expected (1, 0, 8)"

    await uart.write_bytes(PAYLOAD_BYTES)
    await Timer(4, units="ms")
    collector_coro.kill()
    results = reassembler.get_results()
    dut._log.info(f"Reassembled {len(results)} bytes (expected 36).")
    assert results == CAL_BYTES + PAYLOAD_BYTES, (
        f"decoded {len(results)} bytes: {bytes(results).hex()}")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_9600_misaligned_resync_ds_t2_94",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
