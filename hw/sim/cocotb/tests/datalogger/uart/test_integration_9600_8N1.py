"""
UART Datalogger Integration Test - 9600 8N1 (low-end bound of the 9600..1M range)

DS-T2.40: bookend to test_integration_1M_8N1. Verifies the datalogger still
calibrates + decodes byte-for-byte at the SLOWEST supported baud after the
prescaler-4 retune (the prescaler widens the distance counters; 9600's bit is
the largest distance, so this exercises the wide end of the range).
"""

import sys
import random
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, ClockCycles, RisingEdge
from cocotb.utils import get_sim_time

current_dir = Path(__file__).resolve().parent
import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    DEEPSKOPION_ROOT, DEEPSKOPION_RTL, GENERICS, CUSTOM_LIBRARIES,
    setup_dut, apply_config,
    FastSignalCollector, UartDataloggerMsgReassembler
)
from rr_cocotb_tb.drivers.uart_master import UartSource


# Test Parameters
BAUD_RATE = 9600   # slowest supported baud
DATA_BITS = 8
PARITY = "none"
STOP_BITS = 1

# 9600 is ~100x slower than 1 Mbps in sim wall-clock (~1 ms/byte), so keep the
# byte counts lean — enough to calibrate (FIFO-fill path) and verify a payload.
CAL_BYTES = 32
PAYLOAD_BYTES = 4

# Decode-latency bound (DS-P2.241). The calibration replay and the streamer
# both play one recorded bit per baudRateMin_r clock cycles, i.e. REPLAY_RATE
# times the line rate (prescaler 4 at 100 MHz: baudRateMin_r = 2604 ticks for a
# 10417-cycle bit). The whole-buffer format decision replays the calibration
# buffer, then settles 2 replay bits; the streamer then replays every edge
# since the first one. In line bit times T, counted from the first start bit:
#   lock      <= F + F / R + 2 / R
#   last byte <= max(lock + E / R + 10 / R, E) + IDLE_FLUSH + MARGIN
# F = buffer close (bufferFinish_r), E = end of the last stop bit on the line,
# R = REPLAY_RATE, MARGIN = one frame. The first term of the max holds while the
# streamer is still behind the line at its end, the second once it has caught
# up. Either way the last byte is flushed by the post-calibration idle timeout,
# IDLE_FLUSH = 16 bit times (pr_timeout_idle, relativeDistCnt_r >=
# baudRateMin_r & "0000", uart_datalogger.vhd:476).
LINE_BIT_NS = 1e9 / BAUD_RATE
REPLAY_RATE = 4
EXPECTED_BAUD_RATE_MIN = 2604      # ticks: 10417-cycle bit / prescaler 4
MARGIN_BITS = 10
IDLE_FLUSH_BITS = 16


@cocotb.test()
async def test_integration_9600_8N1(dut):
    """Integration test at 9600 8N1: calibrate, send payload, verify byte-for-byte."""
    await setup_dut(dut)
    dev_id = GENERICS["G_UART_DEV_ID"]
    await apply_config(dut, dev_id)
    await ClockCycles(dut.CLK, 100)

    dut._log.info(f"Integration Test: {BAUD_RATE}bps, {DATA_BITS}N1")

    reassembler = UartDataloggerMsgReassembler(DATA_BITS, dut._log)
    count_ns = []                       # sim time at which result k+1 appeared

    def _push(v):
        before = len(reassembler.get_results())
        reassembler.push(v)
        if len(reassembler.get_results()) > before:
            count_ns.append(get_sim_time("ns"))

    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT,
                                    value_fn=_push)
    buffer_finish_ns = []

    async def _buffer_finish():
        await RisingEdge(dut.bufferFinish_r)
        buffer_finish_ns.append(get_sim_time("ns"))

    cocotb.start_soon(_buffer_finish())
    collector_coro = cocotb.start_soon(collector.start())

    uart_source = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=DATA_BITS,
                             stop_bits=STOP_BITS, parity=PARITY)

    # Phase 1: Calibration
    dut._log.info(f"Phase 1: Sending {CAL_BYTES} calibration bytes...")
    cal_data = [random.randint(0, 255) for _ in range(CAL_BYTES)]
    t_first_ns = get_sim_time("ns")     # the first start bit begins now
    await uart_source.write_bytes(cal_data)

    # Wait for calibration lock, up to the lock term of the bound above. Until
    # the buffer closes, the limit is the buffer's 16-bit idle close plus MARGIN.
    t_cal_end_ns = get_sim_time("ns")
    calibration_locked = False
    lock_limit_ns = t_cal_end_ns + (16 + MARGIN_BITS) * LINE_BIT_NS
    while True:
        if dut.calibrationDone_r.value == 1:
            calibration_locked = True
            break
        if buffer_finish_ns:
            f0 = (buffer_finish_ns[0] - t_first_ns) / LINE_BIT_NS
            lock_limit_ns = t_first_ns + (f0 + f0 / REPLAY_RATE + 2 / REPLAY_RATE
                                          + MARGIN_BITS) * LINE_BIT_NS
        if get_sim_time("ns") >= lock_limit_ns:
            break
        await Timer(10, units="us")
    lock_ms = (get_sim_time("ns") - t_cal_end_ns) / 1e6
    bound_lock_ms = (lock_limit_ns - t_cal_end_ns) / 1e6

    if calibration_locked:
        dut._log.info(f"Calibration LOCKED via calibrationDone_r, {lock_ms:.3f} ms after "
                      f"the last cal byte (bound {bound_lock_ms:.3f} ms).")
    else:
        dut._log.error("Calibration FAILED - calibrationDone_r never went high.")
        dut._log.info(f"Collector captured {collector.count} pulses so far.")
        assert False, (f"Calibration timeout: calibrationDone_r not set {lock_ms:.3f} ms "
                       f"after the last cal byte (bound {bound_lock_ms:.3f} ms)")

    # Phase 2: Payload
    dut._log.info(f"Phase 2: Sending {PAYLOAD_BYTES} payload bytes...")
    payload_data = [random.randint(0, 255) for _ in range(PAYLOAD_BYTES)]
    await uart_source.write_bytes(payload_data)

    t_end_ns = get_sim_time("ns")       # end of the last stop bit on the line

    # Wait until every byte is decoded, up to the bound above.
    assert int(dut.baudRateMin_r.value) == EXPECTED_BAUD_RATE_MIN, (
        f"baudRateMin_r={int(dut.baudRateMin_r.value)}, the bound assumes "
        f"{EXPECTED_BAUD_RATE_MIN}")
    assert buffer_finish_ns, "bufferFinish_r never rose"
    f_bits = (buffer_finish_ns[0] - t_first_ns) / LINE_BIT_NS
    e_bits = (t_end_ns - t_first_ns) / LINE_BIT_NS
    lock_bits = f_bits + f_bits / REPLAY_RATE + 2 / REPLAY_RATE
    last_bits = (max(lock_bits + e_bits / REPLAY_RATE + 10 / REPLAY_RATE, e_bits)
                 + IDLE_FLUSH_BITS + MARGIN_BITS)
    deadline_ns = t_first_ns + last_bits * LINE_BIT_NS
    bound_ms = (deadline_ns - t_end_ns) / 1e6
    expected = CAL_BYTES + PAYLOAD_BYTES
    while len(count_ns) < expected and get_sim_time("ns") < deadline_ns:
        await Timer(10, units="us")
    if len(count_ns) >= expected:
        latency_ms = (count_ns[expected - 1] - t_end_ns) / 1e6
        dut._log.info(f"Line end to byte {expected}: {latency_ms:.3f} ms "
                      f"(bound {bound_ms:.3f} ms; F={f_bits:.1f} T, E={e_bits:.1f} T)")
        assert latency_ms <= bound_ms, (
            f"byte {expected} decoded {latency_ms:.3f} ms after the line end, "
            f"bound {bound_ms:.3f} ms")
    else:
        elapsed_ms = (get_sim_time("ns") - t_end_ns) / 1e6
        assert False, (f"{len(count_ns)}/{expected} bytes decoded {elapsed_ms:.3f} ms "
                       f"after the line end (bound {bound_ms:.3f} ms)")

    collector_coro.kill()
    results = reassembler.get_results()

    dut._log.info(f"Collector captured {collector.count} total pulses.")
    dut._log.info(f"Reassembled {len(results)} bytes (expected {CAL_BYTES + PAYLOAD_BYTES}).")

    if len(results) >= PAYLOAD_BYTES:
        received_payload = results[-PAYLOAD_BYTES:]
        for i, (exp, got) in enumerate(zip(payload_data, received_payload)):
            assert exp == got, f"Payload mismatch at byte {i}: expected 0x{exp:02X}, got 0x{got:02X}"
        dut._log.info(f"✓ INTEGRATION TEST PASSED: All {PAYLOAD_BYTES} payload bytes verified.")
    else:
        assert False, f"Not enough data captured: {len(results)} < {PAYLOAD_BYTES}"


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_integration_9600_8N1",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
