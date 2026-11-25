# rr unknown-llm (AI-generated)
"""
UART Datalogger — explicit-config / disable-calibration latency path (DS-T3.9)

When the host sets autoSync=0 and an explicit byte format in the CONFIG word, the
datalogger must decode using that format and SKIP the stop-finder calibration replay
entirely — so a short burst (a few chars) decodes immediately instead of waiting
for the stop-finder to lock on enough frames. Baud is still auto-measured from
the buffered edges; only the FORMAT (data bits / parity / stop bits) is taken
from config.

Discriminator (non-vacuous): with autoSync=0 the stop-finder must NEVER be
enabled (`stopFinderEn_r` stays 0 for the whole run). That is what distinguishes
the explicit-config latency path from auto-calibration — auto-calibration would
assert stopFinderEn_r during DETECT_STOP_BIT. The burst must also decode to the
exact hard-coded bytes.

Builds on DS-T2.27 (the buffer idle-timeout flush must be reachable, else a short
burst never flushes in either path).

Run via the sanctioned executor (ROUTERTL-001). NOTE for this repo: use
`rm -rf hw/sim/work` before re-running after any RTL edit — `rr sim --clean` does
NOT clean the NVC custom-library here (RTL-P2.671).
"""

import sys
import os
from pathlib import Path

import cocotb
from cocotb.triggers import Timer, ClockCycles, RisingEdge

test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")

import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    setup_dut, GENERICS, CUSTOM_LIBRARIES, UartSource, FastSignalCollector
)

BAUD_RATE = 115_200
DATA_BITS = 8
STOP_BITS = 1
PARITY = "none"

# The user's "6 chars" case — a short burst, hard-coded (ROUTERTL-002).
# 0x55/0xAA give adjacent-bit edges so the auto-measured baud (baudRateMin_r)
# locks onto the true bit-time.
SHORT_BURST = [0x55, 0xAA, 0x12, 0x34, 0xC3, 0x3C]


def cfg_word(dev_id, *, enable, auto_sync, nbits, parity_en, parity_type, stop2):
    """Build the 16-bit CONFIG word per the uart_datalogger decode map:
       [15:11] dev_id | [10:7] stopBitsDef | [6] parityType | [5] parityEn
       | [4:2] dataBits(nbits-5) | [1] autoSync | [0] enable
    """
    payload = (
        (enable & 1)
        | ((auto_sync & 1) << 1)
        | (((nbits - 5) & 0x7) << 2)
        | ((parity_en & 1) << 5)
        | ((parity_type & 1) << 6)
        | ((1 if stop2 else 0) << 7)  # stopBitsDef(0)
    )
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


async def apply_explicit_config(dut, dev_id, word):
    """Latch an explicit CONFIG word: prime the ID match, then commit on change."""
    dut.CONFIG.value = (dev_id & 0x1F) << 11   # enable=0 → primes ID, no commit
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = word                      # word-change on matched id → latch
    await ClockCycles(dut.CLK, 5)


@cocotb.test()
async def explicit_config_short_burst_no_calibration(dut):
    """autoSync=0: decode a short burst via explicit config, stop-finder never runs (DS-T3.9)."""
    dev_id = GENERICS["G_UART_DEV_ID"]
    word = cfg_word(dev_id, enable=1, auto_sync=0, nbits=DATA_BITS,
                    parity_en=0, parity_type=0, stop2=False)
    dut._log.info(f"explicit CONFIG word = 0x{word:04x} (autoSync=0, 8N1)")

    await setup_dut(dut)
    await apply_explicit_config(dut, dev_id, word)
    await ClockCycles(dut.CLK, 100)

    # Discriminator: the stop-finder must NEVER be enabled on the explicit path.
    seen = {"stop_finder": False}

    async def watch_stop_finder():
        while True:
            await RisingEdge(dut.CLK)
            if dut.stopFinderEn_r.value == 1:
                seen["stop_finder"] = True

    cocotb.start_soon(watch_stop_finder())

    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    uart_source = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=DATA_BITS,
                             stop_bits=STOP_BITS, parity=PARITY)
    dut._log.info(f"Sending short burst: {[hex(x) for x in SHORT_BURST]}")
    await uart_source.write_bytes(SHORT_BURST)

    # Idle settle: DS-T2.27 idle-timeout flush (~16 bit-times) → immediate
    # calibrationDone (no stop-finder) → frame_gen replay at measured baud.
    bit_time_ns = 1e9 / BAUD_RATE
    replay_ns = bit_time_ns * (1 + DATA_BITS + STOP_BITS) * (len(SHORT_BURST) + 4)
    await Timer(int(replay_ns), units="ns")
    await Timer(2_000_000, units="ns")

    cal_done = int(dut.calibrationDone_r.value)
    cal_fail = int(dut.calibrationFail_r.value)
    dut._log.info(f"calibrationDone_r={cal_done} calibrationFail_r={cal_fail} "
                  f"stopFinderEverEnabled={seen['stop_finder']}")

    # 1. The latency path was actually taken — calibration completed WITHOUT the
    #    stop-finder replay.
    assert cal_done == 1, "calibrationDone_r never asserted — short burst never flushed/completed"
    assert cal_fail == 0, "calibration reported failure on the explicit-config path"
    assert seen["stop_finder"] is False, (
        "stopFinderEn_r asserted — the stop-finder replay RAN, so this was "
        "auto-calibration, not the autoSync=0 explicit-config latency path (DS-T3.9)"
    )

    # 2. The burst decoded to the exact bytes (format honoured).
    captured = [item.value & 0xFF for item in collector._buf if ((item.value >> 24) & 0x7) == 2]
    dut._log.info(f"Captured {len(collector._buf)} msgs, {len(captured)} payload bytes: "
                  f"{[hex(x) for x in captured]}")

    match = -1
    n = len(SHORT_BURST)
    for i in range(len(captured) - n + 1):
        if captured[i:i + n] == SHORT_BURST:
            match = i
            break
    assert match >= 0, (
        f"explicit-config decode wrong — expected {[hex(x) for x in SHORT_BURST]} "
        f"as a subsequence of {[hex(x) for x in captured]} (DS-T3.9)"
    )
    dut._log.info(f"✓ explicit-config short burst decoded at index {match}, stop-finder never ran")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_explicit_config",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
