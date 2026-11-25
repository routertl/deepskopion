# rr unknown-llm (AI-generated)
"""
DS-P3.53 regression — a SPARSE-traffic last byte must flush.

The ticket (filed 2026-05-28, before DS-T2.27) reported uart_datalogger's playback
holding the LAST byte of a burst until subsequent edges push it through frame_gen,
so for sparse traffic the final byte was captured but never emitted —
test_datalogger_to_ddr and the DS-T2.10 PS fix papered over it with a trailing flush
burst.

VERDICT (reproduced contract-first 2026-06-04): the gap is RESOLVED. DS-T2.27
(making the buffer idle-timeout flush reachable) + the existing timeOutEdgeDetected
synthetic-idle edge now flush the last byte: the idle-timeout flushes the buffer
including the synthetic idle period, the playback regenerates the last byte's stop
bit, and frame_gen emits it. No new IDLE_TIMEOUT feature is needed; this test locks
the behaviour so it can't silently regress (and so the trailing-flush workarounds
can be retired as a follow-up).

autoSync=0 (explicit 8N1, DS-T3.9) — a single byte can't auto-calibrate (the
stop-finder needs ~7 frames), so we use the explicit-config path; this isolates
the *playback flush* question from calibration.

Run:  rr sim run test_uart_sparse_flush   (use rm -rf hw/sim/work first)
"""
import sys
import os
from pathlib import Path

import cocotb
from cocotb.triggers import Timer, ClockCycles

test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")

import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation
from sdk.cocotb_helpers import requires
from uart_datalogger_test_utils import (
    setup_dut, GENERICS, CUSTOM_LIBRARIES, UartSource, FastSignalCollector
)

BAUD_RATE = 115_200
DATA_BITS = 8


def cfg_word(dev_id, *, enable, auto_sync, nbits, parity_en, parity_type, stop2):
    payload = ((enable & 1) | ((auto_sync & 1) << 1) | (((nbits - 5) & 0x7) << 2)
               | ((parity_en & 1) << 5) | ((parity_type & 1) << 6)
               | ((1 if stop2 else 0) << 7))
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


async def apply_explicit_config(dut, dev_id, word):
    dut.CONFIG.value = (dev_id & 0x1F) << 11
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = word
    await ClockCycles(dut.CLK, 5)


def captured_payloads(collector):
    return [item.value & 0xFF for item in collector._buf if ((item.value >> 24) & 0x7) == 2]


@cocotb.test()
@requires("UART-REQ-13")
async def single_byte_flushes(dut):
    """ONE byte then idle — the extreme sparse case. Does the only byte emit?"""
    dev_id = GENERICS["G_UART_DEV_ID"]
    word = cfg_word(dev_id, enable=1, auto_sync=0, nbits=DATA_BITS,
                    parity_en=0, parity_type=0, stop2=False)
    await setup_dut(dut)
    await apply_explicit_config(dut, dev_id, word)
    await ClockCycles(dut.CLK, 100)

    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    src = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=DATA_BITS, stop_bits=1, parity="none")
    dut._log.info("Sending a SINGLE byte 0x5A then long idle")
    await src.write_bytes([0x5A])

    await Timer(3_000_000, units="ns")   # 3 ms idle — way past any flush timeout

    caps = captured_payloads(collector)
    dut._log.info(f"captured payloads: {[hex(x) for x in caps]}")
    assert 0x5A in caps, (
        f"single sparse byte 0x5A never emitted (got {[hex(x) for x in caps]}) — "
        f"DS-P3.53 reproduced: the playback holds the last byte without a trailing edge"
    )
    dut._log.info("✓ single byte flushed")


@cocotb.test()
@requires("UART-REQ-13")
async def byte_gap_byte_both_flush(dut):
    """Two bytes separated by a long idle gap. Both must emit — the byte BEFORE
    the gap is the classic 'last byte of a burst' that DS-P3.53 says stalls."""
    dev_id = GENERICS["G_UART_DEV_ID"]
    word = cfg_word(dev_id, enable=1, auto_sync=0, nbits=DATA_BITS,
                    parity_en=0, parity_type=0, stop2=False)
    await setup_dut(dut)
    await apply_explicit_config(dut, dev_id, word)
    await ClockCycles(dut.CLK, 100)

    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    src = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=DATA_BITS, stop_bits=1, parity="none")
    await src.write_bytes([0x55, 0xC3])   # prime baud with a couple bits first
    await src.write_bytes([0xA1])
    dut._log.info("Sent 0x55,0xC3,0xA1; long idle gap")
    await Timer(2_000_000, units="ns")
    await src.write_bytes([0xB2])
    await Timer(2_000_000, units="ns")

    caps = captured_payloads(collector)
    dut._log.info(f"captured payloads: {[hex(x) for x in caps]}")
    assert 0xA1 in caps, f"byte before the gap (0xA1) never emitted: {[hex(x) for x in caps]}"
    assert 0xB2 in caps, f"byte after the gap (0xB2) never emitted: {[hex(x) for x in caps]}"
    dut._log.info("✓ both gap-separated bytes flushed")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_sparse_flush",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
