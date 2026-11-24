#!/usr/bin/env python3
"""DS-P1.71 — drive ALL FOUR EDGE dataloggers with a known stimulus, ON the board.

Why this exists. LT-P1.24 fixed RouteWave's edge decoding (65.536 ms phantom
wrap, shared cross-channel previous timestamp, readback drawn as a transition,
late HELO names), and its only evidence was a replay of a recorded file with
ONE active edge channel and seven events. A live capture of all four channels
is the proof. On the DE25 nothing external drives the EDGE pins (the PMOD
``jb[5:2]`` header is unconnected on this bench), so the stimulus has to come
from the fabric:

  * ``EMIO_MUX_SEL`` bit[2] routes the internal SPI bus, bit[3] (DS-P3.71) feeds
    that bus into the four edge dataloggers: id 1 <- SCLK, id 2 <- MOSI,
    id 3 <- MISO, id 6 <- CS.
  * ``ds_stimulus_gen``'s SPI master (``STIM_SPI_*``) drives SCLK/MOSI/CS.
  * the on-chip SPI responder (``SPI_INJ_*``) drives MISO from its TX FIFO.

Every edge is therefore a deterministic function of the bytes written here and
of ``STIM_SPI_CLK_DIV`` (``ds_stimulus_gen.vhd`` ``p_spi``), and the host-side
judge (``edge4_live_verdict.expected_transfer``) rebuilds the exact waveform.

Two phases, as the ticket asks:

  * ``sparse`` — ``STIM_SPI_CLK_DIV`` = 25 000 000 cycles (250 ms at 100 MHz).
    One transfer lasts 18 half-periods (4.5 s) and every edge on every channel
    is >= ~250 ms from its neighbour on the same channel: a few edges per
    second, each 3.8 wrap windows (65.536 ms) apart, interleaved across the
    four channels.
  * ``dense`` — ``STIM_SPI_CLK_DIV`` = 200 cycles (2 us), 600 transfers paced
    1 ms apart: ~0.6 s of continuous traffic that wraps the 65.536 ms window
    nine times.

Each transfer k carries a distinct MOSI byte and a distinct MISO byte, so the
judge identifies every transfer from its own edges (missing, duplicated or
reordered transfers cannot hide).

Regbank WRITES plus a few status reads only — it never touches the DDR ring, so
the stream daemon keeps streaming (Haz.59). Runs on the board:

    python3 -u -m sw.stream.testbench.edge4_live_stimulus sparse
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence, Tuple

# Regbank byte offsets (sw/stream/regbank.py ``Regs``; project_regbank.yml).
GIT_HASH = 0x0000
DATALOGGER_CONFIG = 0x0020
EMIO_MUX_SEL = 0x0024
SPI_INJ_CONTROL = 0x0040
SPI_INJ_DATAIN = 0x0044
SPI_INJ_STATUS = 0x0048
STIM_SPI_CONTROL = 0x0238
STIM_SPI_CLK_DIV = 0x023C
STIM_SPI_DATA = 0x0240
PKT_CNT_EDGE = 0x00E0
ERR_CNT_EDGE = 0x0100
RING_WRITE_PTR = 0x0150
RING_COMMIT_PTR = 0x0164
DDR_RESET_STATUS = 0x0050

# EMIO_MUX_SEL: bit[2] internal SPI bus, bit[3] EDGE x4 <- full SPI set.
EMIO_MUX_EDGE4_SPI = 0x4 | 0x8

# Edge datalogger device_ids (datalogger_config.yml ``Edge``) and the SPI line
# each one observes under EMIO_MUX_EDGE4_SPI (deepskopion_core.vhd
# capture_pin(42..45)).
EDGE_LINES: Dict[int, str] = {1: "sclk", 2: "mosi", 3: "miso", 6: "cs"}
EDGE_IDS: Tuple[int, ...] = tuple(EDGE_LINES)

# edge_datalogger.vhd CONFIG[1:0]: "01" capture, "10" capture + one read
# trigger. "10" makes the logger emit one S=1 READBACK word with the current
# line level at enable — the word LT-P1.24 must not draw as a transition.
EDGE_CFG_DISABLE = 0x0
EDGE_CFG_ENABLE_READBACK = 0x2

SPI_INJ_FIFO_RESET = 1 << 9
SPI_INJ_ENABLE = 0x1

CLK_HZ = 100_000_000


# Margin after a transfer's CS rise (18 half-periods after its start write) before the next MISO byte is queued.
MIN_PRELOAD_GAP_S = 20e-6


@dataclass(frozen=True)
class Phase:
    name: str
    clk_div: int                       # STIM_SPI_CLK_DIV, clk_sys cycles per half-period
    transfers: Tuple[Tuple[int, int], ...]  # (mosi_byte, miso_byte) per transfer
    period_s: float                    # transfer-start spacing
    settle_s: float = 0.5              # quiet time after enable, and after the last transfer

    @property
    def transfer_s(self) -> float:
        """Wall time of one transfer: CS low for 18 half-periods (p_spi)."""
        return 18 * self.clk_div / CLK_HZ


def _dense_transfers(n: int) -> Tuple[Tuple[int, int], ...]:
    return tuple(((k & 0xFF), ((k * 37 + 11) & 0xFF)) for k in range(n))


PHASES: Dict[str, Phase] = {
    "sparse": Phase(
        name="sparse",
        clk_div=25_000_000,
        transfers=((0xA5, 0x3C), (0x5A, 0xC3)),
        period_s=18 * 25_000_000 / CLK_HZ + 0.75,
    ),
    "dense": Phase(
        name="dense",
        clk_div=200,
        transfers=_dense_transfers(600),
        period_s=0.001,
    ),
}


def datalogger_word(device_id: int, cfg: int) -> int:
    """DATALOGGER_CONFIG broadcast: [15:11]=device_id, [10:0]=config."""
    return ((device_id & 0x1F) << 11) | (cfg & 0x7FF)


@dataclass
class RunLog:
    phase: str
    clk_div: int
    clk_hz: int = CLK_HZ
    mux: int = EMIO_MUX_EDGE4_SPI
    edge_lines: Dict[int, str] = field(default_factory=lambda: dict(EDGE_LINES))
    enable_mono_ns: int = 0
    transfers: List[dict] = field(default_factory=list)
    regs_before: Dict[str, int] = field(default_factory=dict)
    regs_after: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "phase": self.phase, "clk_div": self.clk_div, "clk_hz": self.clk_hz,
            "emio_mux_sel": self.mux,
            "edge_lines": {str(k): v for k, v in self.edge_lines.items()},
            "enable_mono_ns": self.enable_mono_ns,
            "transfers": self.transfers,
            "regs_before": self.regs_before, "regs_after": self.regs_after,
        }


_SNAPSHOT = (
    ("GIT_HASH", GIT_HASH), ("EMIO_MUX_SEL", EMIO_MUX_SEL),
    ("PKT_CNT_EDGE", PKT_CNT_EDGE), ("ERR_CNT_EDGE", ERR_CNT_EDGE),
    ("RING_WRITE_PTR", RING_WRITE_PTR), ("RING_COMMIT_PTR", RING_COMMIT_PTR),
    ("DDR_RESET_STATUS", DDR_RESET_STATUS), ("SPI_INJ_STATUS", SPI_INJ_STATUS),
)


def snapshot(rb) -> Dict[str, int]:
    return {name: int(rb.read_u32(off)) for name, off in _SNAPSHOT}


def arm(rb, phase: Phase, sleep: Callable[[float], None] = time.sleep) -> None:
    """Route the SPI set into the four edge loggers WITHOUT a level change on
    any observed line, then re-arm the loggers.

    Order matters for the "no glitch at enable" check: the loggers are off while
    the mux moves (the mux switch is a real line change), the SPI master is
    enabled while its lines idle (SS=1, SCLK=0 — p_spi drives nothing until a
    DATA write), and only then are the loggers enabled, each emitting one S=1
    readback of an idle line."""
    for dev in EDGE_IDS:
        rb.write_u32(DATALOGGER_CONFIG, datalogger_word(dev, EDGE_CFG_DISABLE))
    rb.write_u32(STIM_SPI_CONTROL, 0x0)
    rb.write_u32(SPI_INJ_CONTROL, SPI_INJ_FIFO_RESET)
    rb.write_u32(SPI_INJ_CONTROL, SPI_INJ_ENABLE)
    rb.write_u32(EMIO_MUX_SEL, EMIO_MUX_EDGE4_SPI)
    rb.write_u32(STIM_SPI_CLK_DIV, phase.clk_div)
    rb.write_u32(STIM_SPI_CONTROL, 0x1)
    sleep(0.05)
    for dev in EDGE_IDS:
        rb.write_u32(DATALOGGER_CONFIG, datalogger_word(dev, EDGE_CFG_ENABLE_READBACK))


def run(rb, phase: Phase,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep) -> RunLog:
    """Arm, then issue every transfer on an absolute schedule.

    Before each transfer the responder FIFO receives the MISO byte alone. No
    trailing 0x00 is queued (DS-P2.386): with an empty FIFO the responder
    reloads zeros at the 8th SCLK falling edge (spi_slave.vhd DS-T2.8), so
    MISO returns low during CS hold. Once DS-T2.102 moves the pop to the first
    SCLK rising edge of a byte, a queued pad survives its own transfer and, at
    the FIFO head, is returned as the next transfer's data, displacing its
    MISO byte."""
    log = RunLog(phase=phase.name, clk_div=phase.clk_div)
    log.regs_before = snapshot(rb)
    arm(rb, phase, sleep)
    log.enable_mono_ns = clock_ns()
    sleep(phase.settle_s)
    t0 = clock_ns()
    prev_start_ns = None
    for k, (mosi, miso) in enumerate(phase.transfers):
        due = t0 + int(k * phase.period_s * 1e9)
        if prev_start_ns is not None:
            # DS-P2.386: a MISO byte queued while the previous transfer is still running is consumed at that
            # transfer's trailing SCLK edge (spi_slave.vhd, until DS-T2.102) and lost; the old trailing 0x00 pad hid
            # this. A late loop therefore never preloads before the previous transfer's CS has risen.
            due = max(due, prev_start_ns + int((phase.transfer_s + MIN_PRELOAD_GAP_S) * 1e9))
        now = clock_ns()
        if due > now:
            sleep((due - now) / 1e9)
        rb.write_u32(SPI_INJ_DATAIN, miso)
        t_write = clock_ns()
        rb.write_u32(STIM_SPI_DATA, mosi)
        # Taken AFTER the start write returns: the transfer may already be underway by then, so the deadline derived
        # from it is conservative; a stall before the store cannot make the next preload early (DS-P2.386 review).
        prev_start_ns = clock_ns()
        log.transfers.append({"k": k, "mosi": mosi, "miso": miso, "mono_ns": t_write})
    sleep(phase.transfer_s + phase.settle_s)
    log.regs_after = snapshot(rb)
    return log


BEGIN_MARK = "EDGE4-LOG-BEGIN"
END_MARK = "EDGE4-LOG-END"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("phase", choices=sorted(PHASES))
    ap.add_argument("--mmio-base", type=lambda s: int(s, 0), default=0x20000000,
                    help="regbank base (DE25 lwh2f window, ds_mmio_base)")
    args = ap.parse_args(argv)
    from sw.stream.regbank import MmioRegbank
    rb = MmioRegbank(base_addr=args.mmio_base)
    try:
        log = run(rb, PHASES[args.phase])
    finally:
        rb.close()
    print(BEGIN_MARK)
    print(json.dumps(log.as_dict(), separators=(",", ":")))
    print(END_MARK)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
