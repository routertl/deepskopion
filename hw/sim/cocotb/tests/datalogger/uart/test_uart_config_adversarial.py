# rr unknown-llm (AI-generated)
"""
UART Datalogger — adversarial CONFIG-path coverage (DS-T3.9 hardening).

Motivated by a live Zybo Z7-20 capture where the UART decoded to garbage
(0x07/0x1E/0x1F + START/STOP framing errors) and the user's hypothesis that the
DS-T3.9 explicit-format enabler may have regressed the core.

What the RTL actually shows (read before writing these — uart_datalogger.vhd):
  * The frame-format mux selects the auto-calibrated *Conf_r when autoSync='1'
    and the host config ONLY when autoSync='0'. So host config CANNOT leak into
    the auto path through the mux.
  * The GEN_STOP_FINDER calibration matchers take only ENABLE/BR_CYCLES/WAVE —
    NO host config. Calibration is purely waveform-driven; it tries data-bit
    hypotheses 5..9 (G_CONF_NBITS => 5+i) and locks the one whose stop bit is a
    consistent one-hot. nbitsConf_r resets to 0 and is set only on lock.

Therefore the live garbage is NOT a DS-T3.9 regression. Two real causes:
  1. The board CONFIG was degenerate: CFG=0x6001/0x6003 sets dataBits=0
     → explicit nbits = dataBits+5 = 5 (not 8), or autoSync=1 before lock
     (nbitsConf_r=0). The correct explicit 8N1 word is dataBits=3 → CFG=0x600D.
  2. Auto-calibration (autoSync=1) can silently lock on the WRONG hypothesis
     (5 data bits) for real ASCII traffic that lacks an alternating-edge
     preamble — producing garbage with calibrationDone=1 and no error flag.

These tests pin the RELIABLE contract (explicit config) and ADVERSARIALLY attack
the auto path (bare ASCII) so the silent mis-lock is caught, not masked.

Message format: [31:27 ID][26:24 Type][23:8 TS][7:0 Data]. Type 2 = payload,
Type 4 = status; parity_error = type 4 data 0x02.

Run (ROUTERTL-001):  rr sim run test_uart_config_adversarial
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

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    setup_dut, GENERICS, CUSTOM_LIBRARIES, UartSource, FastSignalCollector
)

BAUD_RATE = 115_200
MSG_TYPE_PAYLOAD = 0b010
MSG_TYPE_STATUS = 0b100
UART_PARITY_ERROR = 0x02

# Calibration preamble. It must (a) carry enough adjacent-bit edges to lock the
# auto-measured baud, AND (b) be UNAMBIGUOUS in data width. A pure 0x55/0xAA
# stream is NOT a valid 8N1-only sequence — 0x55 is also a valid 7E1 frame
# (0x55 & 0x7F = 0x55, even parity = 0), so the auto-calibrator can legitimately
# lock 7E1 on it. We mix in bytes whose 7-bit parity is WRONG (e.g. 0x34, 0x80,
# 0x12) so the 7E1/7O1 hypotheses fail their parity check and 8N1 wins.
PREAMBLE = [0x55, 0xAA, 0x12, 0x34, 0xC3, 0x80, 0xFF, 0x3C] * 4


def cfg_word(dev_id, *, enable, auto_sync, nbits, parity_en, parity_type, stop2):
    """[15:11] dev_id | [10:7] stopBitsDef | [6] parityType | [5] parityEn
       | [4:2] dataBits(nbits-5) | [1] autoSync | [0] enable"""
    payload = (
        (enable & 1)
        | ((auto_sync & 1) << 1)
        | (((nbits - 5) & 0x7) << 2)
        | ((parity_en & 1) << 5)
        | ((parity_type & 1) << 6)
        | ((1 if stop2 else 0) << 7)
    )
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


async def latch_config(dut, dev_id, word):
    dut.CONFIG.value = (dev_id & 0x1F) << 11
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = word
    await ClockCycles(dut.CLK, 5)


def _payload_bytes(items):
    return [it.value & 0xFF for it in items if ((it.value >> 24) & 0x7) == MSG_TYPE_PAYLOAD]


def _parity_errors(items):
    return sum(1 for it in items
               if ((it.value >> 24) & 0x7) == MSG_TYPE_STATUS and (it.value & 0xFF) == UART_PARITY_ERROR)


def _contains(haystack, needle):
    n = len(needle)
    return any(haystack[i:i + n] == needle for i in range(max(0, len(haystack) - n + 1)))


async def _settle(nbytes, bits, parity, stop):
    bit_ns = 1e9 / BAUD_RATE
    frame_bits = 1 + bits + (1 if parity != "none" else 0) + stop
    await Timer(int(bit_ns * frame_bits * (nbytes + 8)), units="ns")
    await Timer(2_000_000, units="ns")


# ── 1. RELIABLE PATH: explicit 8N1 decodes real ASCII (the correct live config) ─
@cocotb.test()
async def explicit_8n1_ascii(dut):
    """autoSync=0, dataBits=3 (8N1) — the correct explicit config (CFG payload
    0x0D, the board should use CFG=0x600D not 0x6001). Decodes real ASCII payload
    with ZERO false parity errors. This is the path real traffic must use."""
    dev_id = GENERICS["G_UART_DEV_ID"]
    word = cfg_word(dev_id, enable=1, auto_sync=0, nbits=8, parity_en=0, parity_type=0, stop2=False)
    dut._log.info(f"explicit 8N1 CONFIG = 0x{word:04x} (payload 0x{word & 0x7FF:03x})")

    await setup_dut(dut)
    await latch_config(dut, dev_id, word)
    await ClockCycles(dut.CLK, 100)

    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    payload = list(b"DEEPSKOPION-DS9!")  # real ASCII, includes 'D'=0x44 etc (8-bit)
    src = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=8, stop_bits=1, parity="none")
    await src.write_bytes(payload)
    await _settle(len(payload), 8, "none", 1)

    got = _payload_bytes(list(collector._buf))
    perr = _parity_errors(list(collector._buf))
    dut._log.info(f"8N1 explicit decoded {len(got)} bytes: {bytes(got[:24])!r}, parity_errors={perr}")
    assert _contains(got, payload), (
        f"explicit 8N1 did not decode the ASCII payload — got {bytes(got)!r}"
    )
    assert perr == 0, f"{perr} spurious parity errors on clean 8N1 explicit config (DS-T3.5 family)"
    dut._log.info("✓ explicit 8N1 decoded real ASCII, zero false parity — the reliable path")


# ── 2. FORMAT COVERAGE: explicit 7E1, no false parity ─────────────────────────
@cocotb.test()
async def explicit_config_7e1(dut):
    """autoSync=0, 7 data bits + EVEN parity steer the decode; a CORRECT parity
    stream yields ZERO parity errors."""
    dev_id = GENERICS["G_UART_DEV_ID"]
    word = cfg_word(dev_id, enable=1, auto_sync=0, nbits=7, parity_en=1, parity_type=0, stop2=False)
    dut._log.info(f"explicit 7E1 CONFIG = 0x{word:04x}")

    await setup_dut(dut)
    await latch_config(dut, dev_id, word)
    await ClockCycles(dut.CLK, 100)

    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    burst = [0x55, 0x2A, 0x10, 0x7F, 0x01, 0x3C]
    src = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=7, stop_bits=1, parity="even")
    await src.write_bytes(burst)
    await _settle(len(burst), 7, "even", 1)

    got = _payload_bytes(list(collector._buf))
    perr = _parity_errors(list(collector._buf))
    dut._log.info(f"7E1 decoded {[hex(x) for x in got]}, parity_errors={perr}")
    assert _contains(got, burst), f"7E1 decode wrong — expected {[hex(x) for x in burst]} in {[hex(x) for x in got]}"
    assert perr == 0, f"{perr} spurious parity errors on a CORRECT 7E1 stream"
    dut._log.info("✓ explicit 7E1 decoded with zero false parity errors")


# ── 3. ADVERSARIAL: mid-stream reconfigure between explicit formats ───────────
# Red until RTL-P2.2215: UartSource cannot retire its strict monitor, so src8's monitor fails src7's frames (DS-P3.222).
@cocotb.test()
async def midstream_reconfig_switches_format(dut):
    """Latch explicit 8N1, decode a burst; re-latch explicit 7-bit mid-run and
    decode a second burst. The new format must take effect with no wedge."""
    dev_id = GENERICS["G_UART_DEV_ID"]
    await setup_dut(dut)
    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    # Phase 1 — explicit 8N1.
    w8 = cfg_word(dev_id, enable=1, auto_sync=0, nbits=8, parity_en=0, parity_type=0, stop2=False)
    await latch_config(dut, dev_id, w8)
    await ClockCycles(dut.CLK, 100)
    burst1 = [0x41, 0x42, 0x43, 0x44, 0x45, 0x46]
    src8 = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=8, stop_bits=1, parity="none")
    await src8.write_bytes(burst1)
    await _settle(len(burst1), 8, "none", 1)
    n_after_p1 = len(_payload_bytes(list(collector._buf)))   # plain int — deque can't slice
    got1 = _payload_bytes(list(collector._buf))
    dut._log.info(f"phase1 (8N1) decoded: {[hex(x) for x in got1]}")
    assert _contains(got1, burst1), f"phase1 8N1 burst not decoded: {[hex(x) for x in got1]}"

    # Phase 2 — reconfigure to explicit 7-bit mid-operation.
    w7 = cfg_word(dev_id, enable=1, auto_sync=0, nbits=7, parity_en=0, parity_type=0, stop2=False)
    await latch_config(dut, dev_id, w7)
    await ClockCycles(dut.CLK, 100)
    burst2 = [0x21, 0x22, 0x33, 0x44, 0x55, 0x66]
    src7 = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=7, stop_bits=1, parity="none")
    await src7.write_bytes(burst2)
    await _settle(len(burst2), 7, "none", 1)

    got2 = _payload_bytes(list(collector._buf))[n_after_p1:]   # bytes decoded after the reconfig
    dut._log.info(f"phase2 (7-bit after reconfig) decoded: {[hex(x) for x in got2]}")
    assert int(dut.calibrationFail_r.value) == 0, "mid-stream reconfig drove calibrationFail_r=1"
    assert _contains(got2, burst2), (
        f"mid-stream reconfig to 7-bit did not take effect — expected "
        f"{[hex(x) for x in burst2]} in {[hex(x) for x in got2]}"
    )
    dut._log.info("✓ mid-stream reconfig switched format cleanly")


# ── 4. REGRESSION GUARD: autoSync=1 (with preamble) ignores host config ───────
@cocotb.test()
async def autosync1_preamble_ignores_host_config(dut):
    """autoSync=1 with a calibration preamble must lock the auto-calibrated 8N1
    format and IGNORE a poisoned host config (5 data bits / parity on / 2 stop).
    If host config leaked, the payload would mis-decode or show false parity."""
    dev_id = GENERICS["G_UART_DEV_ID"]
    word = cfg_word(dev_id, enable=1, auto_sync=1, nbits=5, parity_en=1, parity_type=0, stop2=True)
    dut._log.info(f"autoSync=1 + poisoned host config = 0x{word:04x} (must be ignored)")

    await setup_dut(dut)
    await latch_config(dut, dev_id, word)
    await ClockCycles(dut.CLK, 100)

    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    payload = list(b"NET8N1OK")  # 8-bit ASCII, distinct from preamble
    src = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=8, stop_bits=1, parity="none")
    await src.write_bytes(PREAMBLE + payload)

    for _ in range(400):
        if int(dut.calibrationDone_r.value) == 1:
            break
        if int(dut.calibrationFail_r.value) == 1:
            assert False, "auto-cal FAILED even with a 32-byte alternating preamble (autoSync=1)"
        await ClockCycles(dut.CLK, 100)
    assert int(dut.calibrationDone_r.value) == 1, "autoSync=1 never calibrated with preamble"
    await _settle(len(PREAMBLE) + len(payload), 8, "none", 1)

    got = _payload_bytes(list(collector._buf))
    perr = _parity_errors(list(collector._buf))
    nbits = int(dut.nbitsConf_r.value)
    dut._log.info(f"autoSync=1 nbitsConf_r={nbits} decoded {bytes(got[-24:])!r} parity_errors={perr}")
    assert nbits == 8, (
        f"autoSync=1 calibrated to {nbits} data bits on an 8N1 stream (expected 8) — "
        f"the auto path mis-locked OR host dataBits leaked into calibration"
    )
    assert _contains(got, payload), (
        f"autoSync=1 did not decode the 8N1 payload despite a clean preamble — "
        f"host config leaked or auto-cal wrong (got {bytes(got)!r})"
    )
    assert perr == 0, f"{perr} parity errors with autoSync=1 on a no-parity stream — host parity_en leaked"
    dut._log.info("✓ autoSync=1 locked 8N1 from the preamble and ignored the poisoned host config")


# ── 5. ADVERSARIAL: autoSync=1 on BARE real ASCII must not silently mis-lock ──
@cocotb.test()
async def autosync1_bare_ascii_no_silent_mislock(dut):
    """The live failure mode: autoSync=1 on real ASCII with NO alternating-edge
    preamble. The contract we WANT: the datalogger either decodes the ASCII
    correctly, or does NOT claim a successful calibration (calibrationDone with a
    wrong nbits and garbage output is a SILENT mis-lock — the live 0x07/0x1E/0x1F
    bug). A clean host CONFIG is used so this isolates the auto path itself."""
    dev_id = GENERICS["G_UART_DEV_ID"]
    word = cfg_word(dev_id, enable=1, auto_sync=1, nbits=8, parity_en=0, parity_type=0, stop2=False)
    await setup_dut(dut)
    await latch_config(dut, dev_id, word)
    await ClockCycles(dut.CLK, 100)

    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    payload = list(b"DEEPSKOPION-DS-T9-UART-READBACK!")  # the exact live payload, no preamble
    src = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=8, stop_bits=1, parity="none")
    await src.write_bytes(payload)
    await _settle(len(payload), 8, "none", 1)

    got = _payload_bytes(list(collector._buf))
    cal = int(dut.calibrationDone_r.value)
    nbits = int(dut.nbitsConf_r.value)
    decoded_ok = _contains(got, payload)
    dut._log.info(f"bare-ASCII autoSync=1: calDone={cal} nbitsConf_r={nbits} "
                  f"decoded_ok={decoded_ok} got={bytes(got[:24])!r}")

    # SILENT MIS-LOCK = claimed calibration (calDone=1, nbits!=8) AND wrong output.
    silent_mislock = (cal == 1 and nbits != 8 and not decoded_ok and len(got) > 0)
    assert not silent_mislock, (
        f"SILENT MIS-LOCK: autoSync=1 reported calibrationDone with nbitsConf_r={nbits} "
        f"and emitted garbage {bytes(got[:24])!r} on a clean 8N1 ASCII stream — reproduces "
        f"the Zybo live 0x07/0x1E/0x1F decode. The auto-calibrator must not claim success "
        f"on the wrong frame model. Real traffic without a preamble must use autoSync=0 "
        f"explicit config (DS-T3.9), and the auto path must fail-LOUD, not silently mis-lock."
    )
    if decoded_ok:
        dut._log.info("✓ autoSync=1 decoded bare ASCII correctly")
    else:
        dut._log.info("✓ autoSync=1 did not claim a successful calibration (failed loud, no garbage)")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_config_adversarial",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
