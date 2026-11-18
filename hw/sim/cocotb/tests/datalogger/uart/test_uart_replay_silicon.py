"""DS-T2.30: replay the REAL silicon UART waveform into uart_datalogger (cocotb).

Reads uart_silicon_capture.jsonl (recorded on the board by
sw/stream/testbench/uart_capture_for_replay.py — EDGE#1 raw-line transitions of
a cal+payload burst that GARBLES on silicon) and drives those exact transitions
onto UART_IN via EdgeReplaySource. If the bare datalogger garbles the SAME way in
sim, the HW failure is deterministically reproduced and debuggable with full
waveform visibility; if it decodes, the HW-only factor is upstream of the
captured line (conditioning / mux / clocking).

Sweeps the format config so we see whether ANY config decodes the real waveform:
  autoSync=1 (full auto), explicit 8N1 (0x600D), explicit 5N1 (0x6001 = REQ-5).

Run:  rm -rf hw/sim/work && rr sim run test_uart_replay_silicon
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

_DS_ROOT = test_dir.parents[5]
sys.path.insert(0, str(_DS_ROOT / "sw" / "stream"))
sys.path.insert(0, str(_DS_ROOT / "hw" / "sim" / "vip" / "drivers"))

from engine.simulation import run_simulation
from uart_datalogger_test_utils import setup_dut, GENERICS, CUSTOM_LIBRARIES, FastSignalCollector
from edge_capture import EdgeCapture
from edge_replay_source import EdgeReplaySource

EDGE_INSTANCE = 1
PAYLOAD = b"DEEPSKOPION-DS-T9-UART-READBACK!"
MSG_TYPE_PAYLOAD = 0b010
CAP_FILE = str(test_dir / "uart_silicon_capture.jsonl")


def cfg_word(dev_id, *, enable, auto_sync, nbits, parity_en=0, parity_type=0, stop2=False):
    payload = ((enable & 1) | ((auto_sync & 1) << 1) | (((nbits - 5) & 0x7) << 2)
               | ((parity_en & 1) << 5) | ((parity_type & 1) << 6) | ((1 if stop2 else 0) << 7))
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


async def latch_config(dut, dev_id, word):
    dut.CONFIG.value = (dev_id & 0x1F) << 11
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = word
    await ClockCycles(dut.CLK, 5)


async def _replay_once(dut, dev_id, word, label):
    await setup_dut(dut)
    await latch_config(dut, dev_id, word)
    await ClockCycles(dut.CLK, 100)
    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    cap = EdgeCapture.read(CAP_FILE)
    src = EdgeReplaySource(dut.UART_IN, clk_mhz=100)
    n = await src.replay_capture(cap, instance=EDGE_INSTANCE, idle_level=1)
    await Timer(3_000_000, units="ns")

    got = bytes(it.value & 0xFF for it in list(collector._buf)
                if ((it.value >> 24) & 0x7) == MSG_TYPE_PAYLOAD)
    found = PAYLOAD in got
    dut._log.info(f"[{label}] cfg=0x{word:04x} replayed {n} transitions → "
                  f"{len(got)} bytes, payload {'FOUND ✓' if found else 'GARBLE ✗'}; "
                  f"sample={got[:24].hex()}")
    return found, n, got


@cocotb.test()
async def replay_silicon_capture(dut):
    dev_id = GENERICS["G_UART_DEV_ID"]
    cap = EdgeCapture.read(CAP_FILE)
    dut._log.info(f"silicon capture: {len(cap.words)} edge words, instances={cap.instances()}, "
                  f"meta={cap.meta if hasattr(cap, 'meta') else '?'}")

    # 9600 replay is slow in sim; run one config per invocation (env-selectable).
    # CFG=auto|8n1|5n1 (default auto). Each ~156 ms sim ≈ several minutes real.
    sel = os.environ.get("CFG", "auto").lower()
    cfgs = {
        "auto": (cfg_word(dev_id, enable=1, auto_sync=1, nbits=8), "autoSync=1"),
        "8n1":  (cfg_word(dev_id, enable=1, auto_sync=0, nbits=8), "explicit-8N1"),
        "5n1":  (cfg_word(dev_id, enable=1, auto_sync=0, nbits=5), "explicit-5N1(REQ-5)"),
    }
    word, label = cfgs.get(sel, cfgs["auto"])
    results = {label: await _replay_once(dut, dev_id, word, label)}

    decoded = results[label][0]
    dut._log.info("=== VERDICT ===")
    # DIAGNOSTIC (no hard assert yet). DS-T2.30: the relativeDistCnt prescaled-
    # tick saturation fix resolves the long-INITIAL-idle garble (see
    # test_uart_initial_idle_repro, which passes). The REAL silicon waveform also
    # has long MID-STREAM gaps (the ~254-bit cal→payload gap); this replay still
    # garbles to a constant byte, so either a second gap-handling facet remains
    # in the datalogger OR EdgeReplaySource mishandles very large inter-transition
    # gaps. The on-silicon self-test (PS-LOOP-REQ-5) is the authoritative check.
    # NB: the capture host-decodes correctly (faithful 9600 8N1), and the level
    # polarity in uart_capture_for_replay.py is corrected.
    dut._log.info(f"  real silicon waveform {label}: "
                  f"{'DECODES ✓' if decoded else 'GARBLES ✗ (gap facet / replay artifact — see REQ-5)'}")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_replay_silicon",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
