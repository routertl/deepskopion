# rr unknown-llm (AI-generated)
"""DS-P2.242: autoSync on the word-length aliases.

On the wire, an n-bit frame with its top data bit 1 and one stop bit is the
same waveform as an (n-1)-bit frame with two stop bits, and a gapped n-bit
frame is the same waveform as an (n+1)-bit frame with its top data bit 1.

This records what the detector reports for each alias class, 16 frames each,
at 1 Mbaud, autoSync on:

  gapped_8n1      8N1, 3 idle bits between frames, mixed top bits
  b2b_7n2         7N2 back to back, mixed data
  b2b_8n1_top1    8N1 back to back, every frame with its top bit 1
"""

import cocotb
from cocotb.triggers import ClockCycles

import sim.cocotb  # noqa: F401  (SDK path setup, as the other UART tests)

from engine.simulation import run_simulation
from uart_datalogger_test_utils import GENERICS, CUSTOM_LIBRARIES, setup_dut

CLK_HZ = 100_000_000
BAUD = 1_000_000
AUTOSYNC = 0x03            # enable | autoSync

MIXED_8 = [0xA3, 0x5C, 0x71, 0x0E, 0xFF, 0x42, 0x00, 0x96,
           0x3C, 0xC1, 0x2E, 0x87, 0x55, 0x6A, 0xD2, 0x19]
MIXED_7 = [0x23, 0x5C, 0x71, 0x0E, 0x7F, 0x42, 0x00, 0x16,
           0x3C, 0x41, 0x2E, 0x07, 0x55, 0x6A, 0x52, 0x19]
TOP1_8 = [0xA3, 0xDC, 0xF1, 0x8E, 0xFF, 0xC2, 0x80, 0x96,
          0xBC, 0xC1, 0xAE, 0x87, 0xD5, 0xEA, 0xD2, 0x99]

# name -> (data bits, stop bits, idle bits after each frame, words)
CASES = {
    "gapped_8n1": (8, 1, 3, MIXED_8),
    "b2b_7n2": (7, 2, 0, MIXED_7),
    "b2b_8n1_top1": (8, 1, 0, TOP1_8),
}


async def _send(dut, word, nbits, stops, idle):
    per_bit = round(CLK_HZ / BAUD)
    for bit in [0] + [(word >> i) & 1 for i in range(nbits)] + [1] * (stops + idle):
        dut.UART_IN.value = bit
        await ClockCycles(dut.CLK, per_bit)


async def _calibrate(dut, name):
    """Re-arm the logger, send one alias case, return (fail, nbits, stopBitsConf)."""
    nbits, stops, idle, words = CASES[name]
    dev = GENERICS["G_UART_DEV_ID"] & 0x1F
    dut.CONFIG.value = dev << 11
    await ClockCycles(dut.CLK, 20)
    dut.CONFIG.value = (dev << 11) | AUTOSYNC
    await ClockCycles(dut.CLK, 200)
    for word in words:
        await _send(dut, word, nbits, stops, idle)
    for _ in range(2000):
        if int(dut.calibrationDone_r.value) == 1:
            break
        await ClockCycles(dut.CLK, 100)
    got = (int(dut.calibrationFail_r.value), int(dut.nbitsConf_r.value),
           int(dut.stopBitsConf_r.value))
    dut._log.info(f"{name}: fail={got[0]} nbits={got[1]} stopBitsConf={got[2]}")
    return got


@cocotb.test()
async def autosync_word_length_aliases(dut):
    """Each alias class resolves to the shorter word (DS-P2.242 rule)."""
    await setup_dut(dut)
    seen = {name: (await _calibrate(dut, name))[:2] for name in CASES}
    expected = {"gapped_8n1": (0, 8), "b2b_7n2": (0, 7), "b2b_8n1_top1": (0, 7)}
    assert seen == expected, (
        f"DS-P2.242: (fail, nbits) per alias class {seen}; expected {expected}")


# Expected failure, DS-T2.92: on a two-stop line the next start comes exactly
# 1.5 bit-times after the stop sample, the stop-bit threshold at
# uart_stop_finder.vhd:120 is "< 1.5 bit" and its counter starts 3 replay
# cycles late, so 7N2 is reported with one stop bit. DS-T2.92's done-when
# removes this marker when the fix lands.
@cocotb.test(expect_fail=True)
async def autosync_reports_two_stop_bits_on_7n2_ds_t2_92(dut):
    await setup_dut(dut)
    _, _, stop_bits = await _calibrate(dut, "b2b_7n2")
    # stopBitsConf: 1 = one stop bit, 0 = two (uart_datalogger_protocol.yml).
    assert stop_bits == 0, f"DS-T2.92: 7N2 reported stopBitsConf={stop_bits}"


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_autosync_aliases_ds_p2_242",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
