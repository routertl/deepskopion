"""DS-P2.242: autoSync format detection locks the true word length when the
first G_MIN_MATCHES calibration frames all have their top data bit set.

The stop finder runs one instance per word length (5..9 data bits). On an n-bit
line whose first 7 frames all have data bit n-1 = 1, the (n-1)-bit instance
samples that bit where its stop bit would be, matches every one of them and
reaches G_MIN_MATCHES=7 one bit before the n-bit instance. The detector took the
first single winner, so it locked n-1 data bits; the (n-1)-bit instance failed
only on frame 8 (top bit 0), one frame too late, and kept its MATCH.

Observed 2026-11-08 at seed 1794167572 of test_integration_1M_8N1: calibration
bytes F6 E8 99 94 DF 87 E8 (all MSB = 1) locked 7N1 on an 8N1 line.

Each case below sends, back to back at 1 Mbaud, 7 frames with the top data bit
set, one frame with it clear, then 8 more frames, with autoSync on, and
requires the detector to report the line's own word length. n = 5 has no
shorter instance to race it, so the cases are n = 6, 7, 8 and 9.

The fix decides the format once the calibration replay has drained, not on the
first instance to match. Each case also pins the calibration-complete timing:
calibrationDone rises no later than two bit-times (2 x baudRateMin_r cycles)
after the playback FIFO went empty, plus the registered hand-off.
"""

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import sim.cocotb  # noqa: F401  (SDK path setup, as the other UART tests)

from engine.simulation import run_simulation
from uart_datalogger_test_utils import GENERICS, CUSTOM_LIBRARIES, setup_dut

CLK_HZ = 100_000_000
BAUD = 1_000_000
AUTOSYNC = 0x03            # enable | autoSync
HANDOFF_CYCLES = 3         # WAIT_FOR_DETECTION -> CALIBRATION_DONE -> calibrationDone_r

# n -> (7 frames with data bit n-1 set, the 8th frame with it clear, 8 more).
# Hard-coded (ROUTERTL-002); the 8-bit row is the observed seed's bytes.
CASES = {
    6: ([0x36, 0x28, 0x39, 0x34, 0x3F, 0x27, 0x28], 0x1C,
        [0x15, 0x2A, 0x0F, 0x30, 0x03, 0x3C, 0x11, 0x2E]),
    7: ([0x76, 0x68, 0x59, 0x54, 0x5F, 0x47, 0x68], 0x3C,
        [0x55, 0x2A, 0x0F, 0x70, 0x03, 0x3C, 0x51, 0x2E]),
    8: ([0xF6, 0xE8, 0x99, 0x94, 0xDF, 0x87, 0xE8], 0x7C,
        [0x55, 0xAA, 0x0F, 0xF0, 0x03, 0x3C, 0xC1, 0x2E]),
    9: ([0x1F6, 0x1E8, 0x199, 0x194, 0x1DF, 0x187, 0x1E8], 0x07C,
        [0x155, 0x0AA, 0x10F, 0x0F0, 0x003, 0x13C, 0x0C1, 0x12E]),
}


async def _send(dut, word, nbits):
    """Bit-bang one nN1 frame on UART_IN, every transition on a CLK edge."""
    per_bit = round(CLK_HZ / BAUD)
    for bit in [0] + [(word >> i) & 1 for i in range(nbits)] + [1]:
        dut.UART_IN.value = bit
        await ClockCycles(dut.CLK, per_bit)


async def _watch_done_timing(dut, out: dict):
    """Cycle of the last pbEmpty rise before calibrationDone_r rises, and of
    that rise."""
    cycle, empty_prev, done_prev = 0, 1, 0
    while True:
        await RisingEdge(dut.CLK)
        cycle += 1
        empty = int(dut.pbEmpty.value)
        done = int(dut.calibrationDone_r.value)
        if empty and not empty_prev:
            out["empty_rise"] = cycle
        if done and not done_prev:
            out["done_rise"] = cycle
            out["br"] = int(dut.baudRateMin_r.value)
            return
        empty_prev, done_prev = empty, done


@cocotb.test()
async def autosync_locks_the_line_word_length_when_the_top_bit_races(dut):
    await setup_dut(dut)
    dev = GENERICS["G_UART_DEV_ID"] & 0x1F
    seen = {}
    late = {}
    for nbits, (lead, breaker, tail) in CASES.items():
        dut.CONFIG.value = dev << 11                    # disable: reset the logger
        await ClockCycles(dut.CLK, 20)
        dut.CONFIG.value = (dev << 11) | AUTOSYNC
        await ClockCycles(dut.CLK, 200)
        timing: dict = {}
        watcher = cocotb.start_soon(_watch_done_timing(dut, timing))
        for word in lead + [breaker] + tail:
            await _send(dut, word, nbits)
        for _ in range(2000):
            if int(dut.calibrationDone_r.value) == 1:
                break
            await ClockCycles(dut.CLK, 100)
        watcher.kill()
        if "done_rise" in timing and "empty_rise" in timing:
            delay = timing["done_rise"] - timing["empty_rise"]
            bound = 2 * timing["br"] + HANDOFF_CYCLES
            dut._log.info(f"{nbits}N1 line: done {delay} cycles after pbEmpty "
                          f"(bound {bound}, BR {timing['br']})")
            if delay > bound:
                late[nbits] = (delay, bound)
        else:   # the measurement must prove it ran; a missing edge is not "on time"
            late[nbits] = f"no pbEmpty/calibrationDone rise observed: {timing}"
        seen[nbits] = (int(dut.calibrationDone_r.value),
                       int(dut.calibrationFail_r.value),
                       int(dut.nbitsConf_r.value))
        dut._log.info(f"{nbits}N1 line: done={seen[nbits][0]} "
                      f"fail={seen[nbits][1]} nbits={seen[nbits][2]}")

    expected = {n: (1, 0, n) for n in CASES}
    assert seen == expected, (
        f"DS-P2.242: (calibrationDone, calibrationFail, nbits) per line word "
        f"length {seen}; expected {expected}")
    assert not late, (
        f"DS-P2.242: calibrationDone later than pbEmpty + 2 bit-times "
        f"(+{HANDOFF_CYCLES} hand-off cycles): {late}")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_autosync_msb_race_ds_p2_242",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
