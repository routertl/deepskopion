# rr unknown-llm (AI-generated)
"""
UART Datalogger — short-burst flush regression (DS-T2.27)

Guard for the dead buffer idle-timeout flush. Before DS-T2.27, the
`pr_fsm_buffer_statistics` idle-timeout compared `relativeDistCnt_r`
(15-bit, max 32767 ticks) against `C_BUFFER_TIMEOUT_LIMIT`
(= C_UART_MIN_BAUDRATE*16 = 153600) — never true. The only working
bufferFinish trigger was the FIFO-fill path (>= 2^(C_FIFO_DEPTH-3)-1 = 127
edges ≈ 25 bytes). So any UART burst shorter than ~25 bytes never flushed
to the decoder: an 8-byte burst yielded 0 payloads.

This test sends ONE short burst (8 bytes, no long calibration preamble),
relies on the idle line afterwards to trigger the now-reachable idle-timeout
flush, and asserts the bytes decode. It would have FAILED (0 payloads) before
the fix and PASSES after.

Run via the sanctioned executor only (ROUTERTL-001):
    rr sim run test_uart_short_burst --clean
"""

import sys
import os
from pathlib import Path

import cocotb
from cocotb.triggers import Timer, ClockCycles

# Wave dir relative to this test
test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")

# SDK cocotb path setup (pip-installed package)
import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    setup_dut, apply_config, GENERICS, CUSTOM_LIBRARIES, UartSource, FastSignalCollector
)

# Test parameters — a SHORT burst, well under the ~25-byte FIFO-fill threshold.
BAUD_RATE = 115_200
DATA_BITS = 8
STOP_BITS = 1
PARITY = "none"

# Hard-coded expected payload (ROUTERTL-002 — fixed, not derived at runtime).
# Includes 0x55/0xAA so adjacent-bit edges give baudRateMin_r the true bit-time.
SHORT_BURST = [0x55, 0xAA, 0x12, 0x34, 0xDE, 0xAD, 0xBE, 0xEF]


@cocotb.test()
async def short_burst_decodes(dut):
    """An 8-byte burst (no long preamble) must flush + decode (DS-T2.27)."""
    assert len(SHORT_BURST) < 25, "burst must stay below the FIFO-fill flush path"

    # 1. Setup + enable
    await setup_dut(dut)
    await apply_config(dut, GENERICS["G_UART_DEV_ID"])
    await ClockCycles(dut.CLK, 100)

    # 2. Monitor
    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    # 3. Send ONE short burst, then leave the line idle (UartSource holds high).
    uart_source = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=DATA_BITS,
                             stop_bits=STOP_BITS, parity=PARITY)
    dut._log.info(f"Sending short burst: {[hex(x) for x in SHORT_BURST]}")
    await uart_source.write_bytes(SHORT_BURST)

    # 4. Idle settle: the idle-timeout flush fires after ~16 bit-times (139 us at
    #    115200). Then calibration replays the buffered edges and frame_gen emits
    #    the payload at the detected baud. Allow generous margin.
    bit_time_ns = 1e9 / BAUD_RATE
    frame_bits = 1 + DATA_BITS + STOP_BITS
    replay_ns = bit_time_ns * frame_bits * (len(SHORT_BURST) + 4)
    await Timer(int(replay_ns), units="ns")
    await Timer(2_000_000, units="ns")  # 2 ms settle (flush + calibration + replay)

    # 5. Mechanism check: the flush must have driven the calibration chain.
    cal_done = int(dut.calibrationDone_r.value)
    cal_fail = int(dut.calibrationFail_r.value)
    dut._log.info(f"calibrationDone_r={cal_done} calibrationFail_r={cal_fail}")
    assert cal_fail == 0, "calibration FAILED on the short burst"
    assert cal_done == 1, (
        "calibrationDone_r never asserted — the short burst never flushed "
        "(DS-T2.27 idle-timeout still dead)"
    )

    # 6. Decode check: extract type-2 payload bytes.
    #    Format: [31:27 ID] [26:24 Type] [23:8 Timestamp] [7:0 Data]; Type 2 = payload.
    captured = []
    for item in collector._buf:
        val = item.value
        if ((val >> 24) & 0x7) == 2:
            captured.append(val & 0xFF)

    dut._log.info(f"Captured {len(collector._buf)} msgs, {len(captured)} payload bytes: "
                  f"{[hex(x) for x in captured]}")

    # The sent burst must appear as a contiguous subsequence.
    match = -1
    n = len(SHORT_BURST)
    for i in range(len(captured) - n + 1):
        if captured[i:i + n] == SHORT_BURST:
            match = i
            break

    assert match >= 0, (
        f"short burst not decoded — expected {[hex(x) for x in SHORT_BURST]} "
        f"as a subsequence of {[hex(x) for x in captured]} (DS-T2.27)"
    )
    dut._log.info(f"✓ short burst decoded at index {match}")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_short_burst",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
