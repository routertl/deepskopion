# rr unknown-llm (AI-generated)
"""UART datalogger decodes a REPLAYED edge capture (DS-P2.60).

The dogfood loop end-to-end, in sim:

    UART waveform → EDGE_EVENT words → capture FILE → read back →
    EdgeReplaySource drives UART_IN → uart_datalogger decodes the payload.

This proves the capture file format + the replay driver faithfully reproduce a
signal: the SAME EdgeCapture.read() + EdgeReplaySource path is what consumes a
REAL silicon capture recorded by the daemon's --capture-edges hook. Here we
synthesise the words so the fixture is deterministic and hard-coded
(ROUTERTL-002), but the replay machinery is identical for a field capture.

Run (ROUTERTL-001):  rr sim run test_uart_edge_replay
NOTE: rm -rf hw/sim/work before re-running after any RTL edit (RTL-P2.671).
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

# deepskopion roots: sw/stream codec + the replay driver under hw/sim/vip/drivers.
_DS_ROOT = test_dir.parents[5]   # deepskopion/
sys.path.insert(0, str(_DS_ROOT / "sw" / "stream"))
sys.path.insert(0, str(_DS_ROOT / "hw" / "sim" / "vip" / "drivers"))

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    setup_dut, GENERICS, CUSTOM_LIBRARIES, FastSignalCollector
)
from edge_capture import EdgeCapture                       # noqa: E402
from edge_replay_source import EdgeReplaySource, uart_waveform_words  # noqa: E402

BAUD_RATE = 115_200
EDGE_INSTANCE = 1            # EDGE0 (the datalogger instance the UART was routed to)
PAYLOAD = b"EDGECAP!"        # hard-coded expected (ROUTERTL-002)
MSG_TYPE_PAYLOAD = 0b010


def cfg_word(dev_id, *, enable, auto_sync, nbits, parity_en, parity_type, stop2):
    payload = ((enable & 1) | ((auto_sync & 1) << 1) | (((nbits - 5) & 0x7) << 2)
               | ((parity_en & 1) << 5) | ((parity_type & 1) << 6)
               | ((1 if stop2 else 0) << 7))
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


async def latch_config(dut, dev_id, word):
    dut.CONFIG.value = (dev_id & 0x1F) << 11
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = word
    await ClockCycles(dut.CLK, 5)


@cocotb.test()
async def decode_replayed_edge_capture(dut):
    """Replay a captured-to-file edge waveform into the datalogger and decode it."""
    dev_id = GENERICS["G_UART_DEV_ID"]

    # 1. Synthesise EDGE_EVENT words for an 8N1 PAYLOAD waveform, persist to a
    #    capture file, and read it back — exercising the real file codec.
    words = uart_waveform_words(PAYLOAD, BAUD_RATE, instance=EDGE_INSTANCE, clk_mhz=100)
    cap = EdgeCapture(clk_mhz=100, meta={"src": "synthetic", "fixture": "uart-8n1", "baud": BAUD_RATE})
    cap.extend(words)
    cap_path = str(test_dir / "_edge_replay_fixture.jsonl")
    cap.write(cap_path)
    cap_back = EdgeCapture.read(cap_path)
    assert cap_back.words == cap.words, "capture file round-trip corrupted the words"
    assert cap_back.instances() == [EDGE_INSTANCE]
    dut._log.info(f"capture: {len(cap_back.words)} edge words, instances={cap_back.instances()}")

    # 2. Explicit 8N1 config (autoSync=0, dataBits=3) — the deterministic path.
    await setup_dut(dut)
    word = cfg_word(dev_id, enable=1, auto_sync=0, nbits=8, parity_en=0, parity_type=0, stop2=False)
    await latch_config(dut, dev_id, word)
    await ClockCycles(dut.CLK, 100)

    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    # 3. Replay the captured edges onto UART_IN (EDGE0 instance → the wire).
    src = EdgeReplaySource(dut.UART_IN, clk_mhz=100)
    n = await src.replay_capture(cap_back, instance=EDGE_INSTANCE, idle_level=1)
    dut._log.info(f"replayed {n} transitions onto UART_IN")
    assert n > 0, "no transitions replayed — capture decode/filter failed"

    # 4. Let the datalogger flush + decode (DS-T2.27 idle-timeout).
    bit_ns = 1e9 / BAUD_RATE
    await Timer(int(bit_ns * 10 * (len(PAYLOAD) + 8)), units="ns")
    await Timer(2_000_000, units="ns")

    got = [it.value & 0xFF for it in list(collector._buf)
           if ((it.value >> 24) & 0x7) == MSG_TYPE_PAYLOAD]
    dut._log.info(f"datalogger decoded {len(got)} bytes from the replayed capture: {bytes(got)!r}")

    target = list(PAYLOAD)
    found = any(got[i:i + len(target)] == target
                for i in range(max(0, len(got) - len(target) + 1)))
    assert found, (
        f"replayed capture did NOT decode to {PAYLOAD!r} — got {bytes(got)!r}. "
        f"The capture→file→replay→DUT dogfood loop is broken."
    )
    dut._log.info(f"✓ DS-P2.60: replayed edge capture decoded back to {PAYLOAD!r}")

    try:
        os.remove(cap_path)
    except OSError:
        pass


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_edge_replay",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
