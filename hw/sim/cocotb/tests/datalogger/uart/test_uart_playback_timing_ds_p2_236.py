# rr unknown-llm (AI-generated)
"""DS-P2.236: edge-dense frames decode at 1 Mbaud, inside the declared range.

uart_pkg.vhd declares C_UART_MAX_BAUDRATE = 1 Mbps ("validated range 9600..1M
@ 100 MHz"). At 1 Mbaud (C_PRESCALER 4, 25 ticks/bit) test_integration_1M_8N1
at seed 1794125872 lost 7 of 100 payload bytes to STOP_ERROR statuses and
cleared the MSB of 3 more. Every dropped byte had MSB = 0 and many edges.

Measured cause, three parts that offset each other:
- the edge recorder dropped the prescaler tick of the edge cycle, so at the
  prescaler phase where edges land on the tick every interval recorded one
  tick short (k x 25 - 1), and calibration took BR = 24;
- uart_baudrate_gen advanced BR_CYCLES + 1 cycles per bit;
- uart_playback replayed each interval one cycle long, so a rebuilt frame
  grew with its number of edges and edge-dense frames lost their stop bit.
With the replay made exact alone, BR = 25 (no shortfall) put the sampler at
26 per bit: the stop sample came so late that format detection and the
decoder were back in WAIT_START only after the next back-to-back start edge,
missed it, and resynchronised on a data edge (calibration failure, garbled
calibration burst). Recording, replay and sampler are now all exact.
Whether a frame failed depended on the calibrated BR as well as the data,
which is why the seed's own calibration bytes are replayed here.

No seed: the bytes below are the ones that seed generated, hard-coded.
"""

import random

import cocotb
from cocotb.triggers import ClockCycles, Timer

import sim.cocotb  # noqa: F401  (SDK path setup, as the other UART tests)

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    GENERICS, CUSTOM_LIBRARIES, setup_dut, apply_config, FastSignalCollector,
)
from rr_cocotb_tb.drivers.uart_master import UartSource

MSG_TYPE_PAYLOAD = 0b010
MSG_TYPE_STATUS = 0b100
EXPLICIT_8N1 = 0x0D        # enable | autoSync=0 | 8 data bits: the format is given, the baud is measured

# test_integration_1M_8N1 at seed 1794125872: its 150 calibration bytes and
# 100 payload bytes.
SEED_CAL = [
    0xFB, 0x7E, 0x47, 0xF9, 0x2C, 0x16, 0xBC, 0x1D, 0x50, 0x96, 0x11, 0x12,
    0xDF, 0xCA, 0x57, 0x95, 0xEA, 0x22, 0x23, 0x25, 0x4A, 0xF2, 0x4C, 0xEC,
    0x7E, 0x94, 0x2A, 0xF5, 0x05, 0x2A, 0x76, 0x93, 0xD5, 0x95, 0x7E, 0x9F,
    0x8F, 0x9B, 0x24, 0x03, 0x12, 0xB0, 0x35, 0xF8, 0xDD, 0x93, 0x81, 0x91,
    0x2D, 0x58, 0x31, 0x6E, 0x5B, 0x6B, 0x28, 0xB1, 0xBC, 0xCA, 0x7C, 0xE0,
    0x49, 0x1A, 0x7E, 0x1D, 0xB9, 0xCE, 0xD1, 0x23, 0xE4, 0x8F, 0x77, 0x17,
    0x38, 0xEA, 0x28, 0x1A, 0xD3, 0x1B, 0x00, 0xDD, 0x9F, 0x58, 0xC6, 0x55,
    0xDD, 0x95, 0xC4, 0x7D, 0xA6, 0xDE, 0x22, 0x83, 0xC0, 0xE2, 0x63, 0x04,
    0xF9, 0xC5, 0x3F, 0x6A, 0x63, 0x6C, 0x9C, 0xE0, 0x60, 0x80, 0xB4, 0x83,
    0x86, 0x73, 0x34, 0xE6, 0x8B, 0xF2, 0xA3, 0xDB, 0xDD, 0xA1, 0x70, 0xC7,
    0x1E, 0x40, 0xC2, 0x4D, 0xB3, 0x26, 0x80, 0xB6, 0xD0, 0x84, 0xE1, 0x70,
    0xD2, 0xBB, 0xB8, 0x20, 0xA4, 0x03, 0x40, 0xE7, 0x1A, 0x62, 0x78, 0x44,
    0xBE, 0x88, 0xD5, 0x12, 0x1C, 0xDC,
]
SEED_PAYLOAD = [
    0xCE, 0xB2, 0x04, 0x01, 0x13, 0x9B, 0xD1, 0x7D, 0x2C, 0xCD, 0x7A, 0xA6,
    0xD1, 0xE2, 0xCB, 0xFA, 0xA8, 0x92, 0xA6, 0x10, 0x33, 0xBE, 0x3D, 0x94,
    0x97, 0xCF, 0xF5, 0xEC, 0x01, 0x1E, 0xB9, 0x51, 0x63, 0x5B, 0xC2, 0x43,
    0x63, 0xE6, 0xA5, 0x71, 0x23, 0x3F, 0x6F, 0xFB, 0x38, 0x2B, 0xB9, 0xB0,
    0x1A, 0x79, 0xEB, 0xF2, 0xA3, 0xCA, 0x20, 0xAC, 0xF1, 0x31, 0xC8, 0xC4,
    0x36, 0x03, 0x56, 0x32, 0xE1, 0x32, 0xF1, 0xA9, 0x7B, 0xC4, 0x69, 0xE4,
    0xEA, 0x68, 0x9D, 0x38, 0x26, 0xB8, 0x6B, 0x37, 0x07, 0xEA, 0xB7, 0x99,
    0x63, 0x41, 0x0C, 0xF6, 0x4A, 0xD5, 0x87, 0x25, 0x79, 0x74, 0x8D, 0xB8,
    0x64, 0xDF, 0xCE, 0x43,
]

# The bytes that seed's 1 Mbaud decode dropped (MSB = 0, edge-dense) and
# corrupted (MSB cleared: 0xA5 -> 0x25, 0xA9 -> 0x29, 0xD5 -> 0x95).
DROPPED = [0x51, 0x5B, 0x2B, 0x56, 0x69, 0x6B, 0x25, 0x49, 0x55, 0x6A, 0x4D]
CORRUPTED = [0xA5, 0xA9, 0xD5]
PATTERN = (DROPPED + CORRUPTED) * 4


CLK_HZ = 100_000_000


async def _exact_uart(dut, data, baud):
    """Bit-bang 8N1 on UART_IN, every transition on a CLK edge, with no BFM:
    bit k ends at round((k+1) * CLK_HZ / baud) clocks, so every interval is
    exact (1 Mbaud: 100 clocks per bit, no first-bit shortfall)."""
    acc = 0.0
    edge = 0
    per_bit = CLK_HZ / baud
    for byte in data:
        bits = [0] + [(byte >> i) & 1 for i in range(8)] + [1]
        for bit in bits:
            dut.UART_IN.value = bit
            acc += per_bit
            nxt = round(acc)
            await ClockCycles(dut.CLK, nxt - edge)
            edge = nxt


async def _run_exact(dut, baud, cal, payload, phase=0, explicit=True):
    """As _run, but both bursts come from _exact_uart, started *phase* clocks
    later (the 4-clock prescaler phase the edges fall on)."""
    await setup_dut(dut)
    if explicit:
        await _arm_explicit_8n1(dut)
    else:
        await apply_config(dut, GENERICS["G_UART_DEV_ID"])      # autoSync
    await ClockCycles(dut.CLK, 100 + phase)
    raw = []
    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT,
                                    value_fn=lambda v: raw.append(int(v)))
    task = cocotb.start_soon(collector.start())
    await _exact_uart(dut, cal, baud)
    for _ in range(200):
        if dut.calibrationDone_r.value == 1:
            break
        await ClockCycles(dut.CLK, 100)
    assert dut.calibrationDone_r.value == 1, f"[{baud}] calibration never locked"
    assert dut.calibrationFail_r.value == 0, f"[{baud}] calibration failed"
    br = int(dut.baudRateMin_r.value)
    n_cal = len(raw)
    await _exact_uart(dut, payload, baud)
    await Timer(15, unit="ms")
    task.kill()
    after = raw[n_cal:]
    status = [v for v in after if (v >> 24) & 0x7 == MSG_TYPE_STATUS]
    data = [v & 0xFF for v in after if (v >> 24) & 0x7 == MSG_TYPE_PAYLOAD]
    dut._log.info(f"[{baud}] exact source: BR={br}; after calibration: {len(data)} payload, {len(status)} status")
    assert not status, f"[{baud}] BR={br}: {len(status)} STATUS frame(s) in place of data"
    assert data[-len(payload):] == payload, (
        f"[{baud}] BR={br}: decoded {[hex(b) for b in data[-len(payload):]]}")
    _assert_whole_stream(raw, cal, payload, baud, br)
    return br


def _assert_whole_stream(raw, cal, payload, baud, br):
    """The calibration burst decodes too: on silicon at 1 Mbaud it was the
    calibration burst that came out garbled (run (b), 2026-11-08)."""
    status = [v for v in raw if (v >> 24) & 0x7 == MSG_TYPE_STATUS]
    data = [v & 0xFF for v in raw if (v >> 24) & 0x7 == MSG_TYPE_PAYLOAD]
    assert not status, f"[{baud}] BR={br}: STATUS frame(s) in the stream: {[f'{v:08X}' for v in status]}"
    sent = list(cal) + list(payload)
    assert data == sent, (
        f"[{baud}] BR={br}: decoded stream differs from the sent bytes "
        f"({len(data)} decoded, {len(sent)} sent; first difference at "
        f"{next((i for i, (a, b) in enumerate(zip(data, sent)) if a != b), min(len(data), len(sent)))})")


async def _arm_explicit_8n1(dut):
    """Latch explicit 8N1 (as test_uart_baud_limit's arm): only the baud is
    calibrated, so a decode loss cannot come from format detection."""
    dev = GENERICS["G_UART_DEV_ID"] & 0x1F
    dut.CONFIG.value = dev << 11
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = (dev << 11) | EXPLICIT_8N1
    await ClockCycles(dut.CLK, 5)


async def _run(dut, baud, cal, payload, explicit, gap_bits=0):
    """Calibrate with *cal*, send *payload*; gap_bits idle bit-times after each byte."""
    await setup_dut(dut)
    if explicit:
        await _arm_explicit_8n1(dut)
    else:
        await apply_config(dut, GENERICS["G_UART_DEV_ID"])      # autoSync, as test_integration_1M_8N1
    await ClockCycles(dut.CLK, 100)
    raw = []
    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT,
                                    value_fn=lambda v: raw.append(int(v)))
    task = cocotb.start_soon(collector.start())
    src = UartSource(dut.UART_IN, baud=baud, bits=8, stop_bits=1, parity="none")

    await src.write_bytes(cal)
    for _ in range(200):
        if dut.calibrationDone_r.value == 1:
            break
        await ClockCycles(dut.CLK, 100)
    assert dut.calibrationDone_r.value == 1, f"[{baud}] calibration never locked"
    assert dut.calibrationFail_r.value == 0, f"[{baud}] calibration failed"
    br = int(dut.baudRateMin_r.value)
    n_cal = len(raw)

    if gap_bits:
        for b in payload:
            await src.write_bytes([b])
            await Timer(gap_bits * src.bit_time_ns, unit="ns")
    else:
        await src.write_bytes(payload)
    await Timer(15, unit="ms")          # buffer-timeout flush, as test_integration_1M_8N1
    task.kill()

    after = raw[n_cal:]
    status = [v for v in after if (v >> 24) & 0x7 == MSG_TYPE_STATUS]
    data = [v & 0xFF for v in after if (v >> 24) & 0x7 == MSG_TYPE_PAYLOAD]
    dut._log.info(f"[{baud}] BR={br}; after calibration: {len(data)} payload, {len(status)} status")
    assert not status, (
        f"[{baud}] BR={br}: {len(status)} STATUS frame(s) in place of data: "
        f"{[f'{v:08X}' for v in status]}")
    tail = data[-len(payload):]
    assert tail == payload, (
        f"[{baud}] BR={br}: decoded bytes differ: sent {[hex(b) for b in payload]}, "
        f"got {[hex(b) for b in tail]}")
    _assert_whole_stream(raw, cal, payload, baud, br)


@cocotb.test()
async def seed_bytes_decode_at_1mbaud_autosync(dut):
    """The failing seed's calibration and payload, as test_integration_1M_8N1 sends them."""
    await _run(dut, 1_000_000, SEED_CAL, SEED_PAYLOAD, explicit=False)


@cocotb.test()
async def seed_bytes_decode_at_1mbaud_explicit(dut):
    await _run(dut, 1_000_000, SEED_CAL, SEED_PAYLOAD, explicit=True)


@cocotb.test()
async def edge_dense_pattern_decodes_at_1mbaud_after_seed_calibration(dut):
    await _run(dut, 1_000_000, SEED_CAL, PATTERN, explicit=True)


@cocotb.test()
async def edge_dense_pattern_decodes_at_1mbaud_with_idle_gaps(dut):
    """The loss is inside each frame, so idle time between frames (as a
    register-strobed board stimulus leaves) does not hide it."""
    await _run(dut, 1_000_000, SEED_CAL, PATTERN, explicit=True, gap_bits=5)


@cocotb.test()
async def edge_dense_pattern_decodes_at_230400(dut):
    await _run(dut, 230_400, SEED_CAL, PATTERN, explicit=True)


@cocotb.test()
async def exact_source_decodes_at_every_prescaler_phase_at_1mbaud(dut):
    """Every interval exact (100 clocks, 25 ticks), every edge on one phase of
    the 4-clock tick prescaler. The recorder must measure 25 at every phase
    (it measured 24 where the edge landed on the tick), and decode, the
    buffered calibration burst included, must pass at all four phases."""
    brs = []
    for phase in range(4):
        brs.append(await _run_exact(dut, 1_000_000, SEED_CAL, PATTERN, phase=phase))
    dut._log.info(f"BR by prescaler phase: {brs}")
    # Pins the calibration estimate: the true bit time at every phase. The
    # sampler period equals BR_CYCLES (test_uart_baudrate_gen).
    assert brs == [25, 25, 25, 25], f"calibration by prescaler phase: {brs}, expected 25 at every phase"


@cocotb.test()
async def exact_source_autosync_locks_and_decodes_at_every_prescaler_phase(dut):
    """Format detection (uart_stop_finder) on back-to-back exact frames: at
    BR = 25 its 8-bit instance re-armed for the next start edge two clocks
    after that edge, resynchronised on a data edge and stuck in NOT_A_MATCH,
    so calibration failed and nothing was emitted."""
    for phase in range(4):
        await _run_exact(dut, 1_000_000, SEED_CAL, PATTERN, phase=phase, explicit=False)


@cocotb.test()
async def no_shortfall_calibration_decodes_at_921600(dut):
    """921600 at 100 MHz is 108.51 clocks per bit (27.13 ticks), so the exact
    source alternates 108/109-clock bits and calibration takes their floor."""
    await _run_exact(dut, 921_600, SEED_CAL, PATTERN)


@cocotb.test()
async def autosync_calibration_with_another_burst_locks_at_1mbaud(dut):
    """At 1 Mbaud an autoSync calibration on this 150-byte burst ended with
    calibrationFail_r = 1 and nbits = 0, and the datalogger emitted nothing:
    format detection reads the same rebuilt waveform."""
    rng = random.Random(0x236)
    cal = [rng.randint(0, 255) for _ in range(150)]
    await _run(dut, 1_000_000, cal, PATTERN, explicit=False)


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_playback_timing_ds_p2_236",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
