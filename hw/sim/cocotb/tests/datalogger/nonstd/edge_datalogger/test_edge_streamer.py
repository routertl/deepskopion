# rr unknown-llm (AI-generated)
"""DS-P2.16 — edge_datalogger Stream-RLE (differential) mode.

CONFIG[4:3] = "10" switches a capture from one 32-bit word per edge to
8-bit edge-to-edge deltas, three per PACK word. The stream is anchored by a
SYNC word (line level + TIMESTAMP[23:0]) and carries LONG (one edge after
256+ ticks) and IDLE (no edge for 2**G_RLE_CNT_WIDTH-1 ticks) words. See the
RLE block in edge_datalogger.vhd and edge_datalogger_protocol.yml.

Every expected word below is hard-coded from that format (ROUTERTL-002). The
one value the bench cannot know in advance, the first delta after a SYNC (it
depends on the config_control latch latency), is pinned by a relation the
format guarantees: SYNC timestamp + delta == the TIMESTAMP an event-mode word
stamps on the same edge, which test_event_mode_stamp_lag pins to the drive
cycle + STAMP_LAG.

Run:  rr sim run test_edge_streamer   (from the deepskopion dir)
"""

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH
except ImportError:
    print("Error: Could not import engine. Run via `rr sim run test_edge_streamer`.")
    sys.exit(1)

from sdk.cocotb_helpers import requires
from hw.sim.portable_fifo_sources import olo_fifo_sources

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

DEV_ID = 28
RLE_CNT_WIDTH = 12          # IDLE after 4095 ticks, reachable in sim
GENERICS = {
    "G_ED_ID_WIDTH": 5,
    "G_ED_DEV_ID": DEV_ID,
    "G_TIMESTAMP_WIDTH": 24,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_ENABLE_INFILTER": 0,
    "G_ILA_EN": 0,
    "G_MAXFREQ_WIDTH": 24,
    "G_MAXFREQ_MIN_INTERVAL": 2,
    "G_RLE_CNT_WIDTH": RLE_CNT_WIDTH,
}

# ── Hard-coded format constants (edge_datalogger_protocol.yml rle_*) ─────────
RLE_SYNC_LO, RLE_SYNC_HI, RLE_PACK, RLE_LONG, RLE_IDLE = 0b000, 0b001, 0b101, 0b110, 0b111
IDLE_TICKS = 4095           # 2**RLE_CNT_WIDTH - 1
# An edge driven onto EDGE_IN right after the clock edge at which TIMESTAMP was
# set to T is stamped T + 1 by event mode: edge_detector samples it at the next
# clock and reports it one clock later, when TIMESTAMP reads T + 1.
STAMP_LAG = 1

CFG_DISABLED = DEV_ID << 11
CFG_EVENT = (DEV_ID << 11) | 0b01
CFG_RESERVED_01 = (DEV_ID << 11) | (0b01 << 3) | 0b01
CFG_RLE = (DEV_ID << 11) | (0b10 << 3) | 0b01


def word(kind, payload):
    return (DEV_ID << 27) | (kind << 24) | (payload & 0xFFFFFF)


def pack(d0, d1=0, d2=0):
    return word(RLE_PACK, (d2 << 16) | (d1 << 8) | d0)


def kind_of(w):
    return (w >> 24) & 0x7


def main():
    run_simulation(
        top_level="edge_datalogger",
        module="test_edge_streamer",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/nonstd/edge_datalogger/edge_datalogger.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )


class Bench:
    """One clock, a TIMESTAMP that counts clocks, a scheduled EDGE_IN driver and
    a monitor of every accepted ED_MSG_OUT word.

    TIMESTAMP and EDGE_IN are written by the same coroutine right after each
    clock edge, so 'the edge driven at T' has an exact meaning: EDGE_IN changed
    in the cycle TIMESTAMP reads T."""

    def __init__(self, dut):
        self.dut = dut
        self.cyc = 0
        self.schedule = {}      # cycle -> EDGE_IN level
        self.words = []         # (word, stream, lifecycle, cycle)
        self.loss_pulses = 0
        self.stream_flag_on_vld_low = 0

    async def start(self, rdy=1):
        dut = self.dut
        cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
        dut.RST.value = 1
        dut.EDGE_IN.value = 0
        dut.ED_MSG_RDY.value = rdy
        dut.CONFIG.value = 0
        dut.TIMESTAMP.value = 0
        cocotb.start_soon(self._drive())
        cocotb.start_soon(self._monitor())
        await ClockCycles(dut.CLK, 5)
        dut.RST.value = 0
        await ClockCycles(dut.CLK, 64)          # FIFO reset-busy clears

    async def _drive(self):
        while True:
            await RisingEdge(self.dut.CLK)
            self.cyc += 1
            self.dut.TIMESTAMP.value = self.cyc & 0xFFFFFF
            if self.cyc in self.schedule:
                self.dut.EDGE_IN.value = self.schedule.pop(self.cyc)

    async def _monitor(self):
        dut = self.dut
        while True:
            await RisingEdge(dut.CLK)
            await ReadOnly()
            vld = int(dut.ED_MSG_VLD.value)
            stream = int(dut.ED_MSG_STREAM.value)
            if vld and int(dut.ED_MSG_RDY.value):
                self.words.append((int(dut.ED_MSG_OUT.value), stream,
                                   int(dut.ED_MSG_LIFECYCLE.value), self.cyc))
            if stream and not vld:
                self.stream_flag_on_vld_low += 1
            self.loss_pulses += int(dut.ED_ERRORS.value) & 1

    async def config(self, value):
        self.dut.CONFIG.value = value
        await ClockCycles(self.dut.CLK, 4)

    def edges_at(self, start, deltas):
        """Schedule one EDGE_IN toggle at `start`, then one after each delta.
        Returns the drive cycles. The line starts from its current level."""
        level = int(self.dut.EDGE_IN.value)
        pending = [lvl for _c, lvl in sorted(self.schedule.items())]
        if pending:
            level = pending[-1]
        cycles = [start]
        for d in deltas:
            cycles.append(cycles[-1] + d)
        for c in cycles:
            level ^= 1
            self.schedule[c] = level
        return cycles

    async def run_until(self, cycle):
        while self.cyc < cycle:
            await RisingEdge(self.dut.CLK)

    def stream_words(self):
        return [w for w, s, _lc, _c in self.words if s]

    def check_flags(self):
        for w, s, lc, _c in self.words:
            assert not (s and lc), f"word {w:#010x} flagged both STREAM and LIFECYCLE"
        assert self.stream_flag_on_vld_low == 0, (
            f"ED_MSG_STREAM high on {self.stream_flag_on_vld_low} cycle(s) with ED_MSG_VLD low")


def check_anchor(sync_word, first_delta, drive_cycle, label):
    """SYNC timestamp + first delta == event-mode stamp of that edge."""
    ts = sync_word & 0xFFFFFF
    assert ts + first_delta == drive_cycle + STAMP_LAG, (
        f"{label}: SYNC ts {ts} + first delta {first_delta} = {ts + first_delta}, "
        f"but the edge driven at {drive_cycle} is stamped {drive_cycle + STAMP_LAG} "
        "in event mode — the RLE time base is off")
    assert 1 <= first_delta <= 255, f"{label}: first delta {first_delta} out of range"


@cocotb.test()
@requires("EDGE-REQ-63")
async def test_event_mode_stamp_lag(dut):
    """EDGE-REQ-63: with CONFIG[4:3] = "00" and the reserved "01" the datalogger
    stays in event mode: one S=0 word per edge, ED_MSG_STREAM low, stamped
    TIMESTAMP(drive) + STAMP_LAG. Pins the lag the RLE anchor checks rely on."""
    b = Bench(dut)
    await b.start()
    for cfg in (CFG_EVENT, CFG_RESERVED_01):
        b.words.clear()
        await b.config(cfg)
        drives = b.edges_at(b.cyc + 20, [9, 4])
        await b.run_until(drives[-1] + 30)
        events = [(w, s) for w, s, lc, _c in b.words if not lc]
        assert [s for _w, s in events] == [0, 0, 0], (
            f"cfg {cfg:#06x}: want three event words with ED_MSG_STREAM=0, got "
            f"{[(hex(w), s) for w, s in events]}")
        stamps = [w & 0xFFFFFF for w, _s in events]
        assert stamps == [c + STAMP_LAG for c in drives], (
            f"cfg {cfg:#06x}: stamps {stamps}, want {[c + STAMP_LAG for c in drives]}")
        assert all(((w >> 26) & 1) == 0 for w, _s in events), "event word with S=1"
        await b.config(CFG_DISABLED)
        await ClockCycles(dut.CLK, 10)
    b.check_flags()


@cocotb.test()
@requires("EDGE-REQ-59")
async def test_rle_pack_words(dut):
    """EDGE-REQ-59: in Stream-RLE mode the capture is one SYNC_LO anchor and then
    three deltas per PACK word, delta_0 the oldest, 1..255 ticks each (1 =
    back-to-back edges, 255 = the longest short delta). Every such word has
    ED_MSG_STREAM=1; the lifecycle frame keeps ED_MSG_STREAM=0. PROHIBITION: no
    event-mode word is emitted for a Stream-RLE capture."""
    b = Bench(dut)
    await b.start()
    await b.config(CFG_RLE)
    drives = b.edges_at(b.cyc + 30, [7, 3, 1, 255, 12])
    await b.run_until(drives[-1] + 600)

    got = b.stream_words()
    assert len(got) == 3, f"want SYNC + 2 PACK, got {[hex(w) for w in got]}"
    assert got[0] >> 24 == (DEV_ID << 3) | RLE_SYNC_LO, f"anchor {got[0]:#010x} is not SYNC_LO"
    d0 = got[1] & 0xFF
    check_anchor(got[0], d0, drives[0], "pack")
    assert got[1:] == [pack(d0, 7, 3), pack(1, 255, 12)], (
        f"PACK words {[hex(w) for w in got[1:]]}, want "
        f"{[hex(pack(d0, 7, 3)), hex(pack(1, 255, 12))]}")
    others = [(hex(w), lc) for w, s, lc, _c in b.words if not s]
    assert len(others) == 1 and others[0][1] == 1 and int(others[0][0], 16) & 0xFF == 0x03, (
        f"only the ENABLED lifecycle frame may be a non-stream word, got {others}")
    b.check_flags()


@cocotb.test()
@requires("EDGE-REQ-60")
async def test_rle_long_words(dut):
    """EDGE-REQ-60: an edge 256 or more ticks after the previous one is a LONG
    word carrying the full delta (256 is the first LONG). Short deltas on
    either side still PACK."""
    b = Bench(dut)
    await b.start()
    await b.config(CFG_RLE)
    drives = b.edges_at(b.cyc + 30, [5, 5, 256, 300, 2, 2, 2])
    await b.run_until(drives[-1] + 400)

    got = b.stream_words()
    d0 = got[1] & 0xFF if len(got) > 1 else -1
    want = [got[0] if got else -1, pack(d0, 5, 5), word(RLE_LONG, 256),
            word(RLE_LONG, 300), pack(2, 2, 2)]
    assert got == want, (
        f"LONG stream {[hex(w) for w in got]}\n want {[hex(w) for w in want]}")
    assert kind_of(got[0]) == RLE_SYNC_LO
    check_anchor(got[0], d0, drives[0], "long")
    b.check_flags()


@cocotb.test()
@requires("EDGE-REQ-60")
async def test_rle_idle_word(dut):
    """EDGE-REQ-60: 2**G_RLE_CNT_WIDTH-1 ticks with no edge is an IDLE word (time
    advances, no toggle) and the next edge's delta counts from it: a gap of
    IDLE_TICKS + 10 is IDLE then a 10-tick delta, which PACKs."""
    b = Bench(dut)
    await b.start()
    await b.config(CFG_RLE)
    drives = b.edges_at(b.cyc + 30, [5, 5, IDLE_TICKS + 10, 2, 2])
    await b.run_until(drives[-1] + 400)

    got = b.stream_words()
    d0 = got[1] & 0xFF if len(got) > 1 else -1
    want = [got[0] if got else -1, pack(d0, 5, 5), word(RLE_IDLE, IDLE_TICKS),
            pack(10, 2, 2)]
    assert got == want, (
        f"IDLE stream {[hex(w) for w in got]}\n want {[hex(w) for w in want]}")
    assert kind_of(got[0]) == RLE_SYNC_LO
    check_anchor(got[0], d0, drives[0], "idle")
    b.check_flags()


@cocotb.test()
@requires("EDGE-REQ-61")
async def test_rle_partial_pack_flush(dut):
    """EDGE-REQ-61: a partly filled PACK is flushed, zero-padded, on the cycle its
    newest delta reaches 255 ticks without a next edge, so an edge waits at most
    255 ticks in the packer (and a later LONG/IDLE word never finds deltas held
    ahead of it). The flush happens while the capture is still running, not
    when it ends."""
    b = Bench(dut)
    await b.start()
    await b.config(CFG_RLE)
    first = b.edges_at(b.cyc + 30, [6])
    await b.run_until(first[-1] + 600)

    got = b.stream_words()
    d0 = got[1] & 0xFF if len(got) > 1 else -1
    want = [got[0] if got else -1, pack(d0, 6)]
    assert got == want, (
        f"idle-flush stream before disable {[hex(w) for w in got]}\n "
        f"want {[hex(w) for w in want]}")
    assert kind_of(got[0]) == RLE_SYNC_LO
    check_anchor(got[0], d0, first[0], "idle flush")
    # The flush leaves the packer 255 ticks after its newest edge (+1
    # reporting lag, +1 FIFO).
    flush_cycle = [c for w, s, _lc, c in b.words if s and w == pack(d0, 6)][0]
    assert flush_cycle - first[1] <= 255 + 6, (
        f"partial PACK left {flush_cycle - first[1]} ticks after its last edge (max 261)")
    await b.config(CFG_DISABLED)
    await ClockCycles(dut.CLK, 20)
    assert b.stream_words() == got, "disable after the flush wrote another word"
    b.check_flags()


@cocotb.test()
@requires("EDGE-REQ-61")
async def test_rle_disable_flush(dut):
    """EDGE-REQ-61: disabling a capture flushes the deltas still held as a
    zero-padded PACK, so the last edges of a capture are never lost."""
    b = Bench(dut)
    await b.start()
    await b.config(CFG_RLE)
    drives = b.edges_at(b.cyc + 20, [])
    await b.run_until(drives[-1] + 10)
    await b.config(CFG_DISABLED)
    await ClockCycles(dut.CLK, 20)

    got = b.stream_words()
    e0 = got[1] & 0xFF if len(got) > 1 else -1
    want = [got[0] if got else -1, pack(e0)]
    assert got == want, (
        f"disable-flush stream {[hex(w) for w in got]}\n want {[hex(w) for w in want]}")
    assert kind_of(got[0]) == RLE_SYNC_LO
    check_anchor(got[0], e0, drives[0], "disable flush")
    b.check_flags()


@cocotb.test()
@requires("EDGE-REQ-62")
async def test_rle_resync_after_lost_word(dut):
    """EDGE-REQ-62: a Stream-RLE word lost to a full FIFO pulses ED_ERRORS[0]
    once and breaks the time base, so the compressor drops what it holds,
    ignores edges until the FIFO has room, and then writes a fresh SYNC with the
    line's current level. The deltas after it reconstruct exactly.
    PROHIBITION: no PACK/LONG/IDLE word follows a lost word without a SYNC."""
    b = Bench(dut)
    await b.start(rdy=0)
    await b.config(CFG_RLE)
    # 1200 back-to-back edges: 400 PACK words, far past the 256-word FIFO.
    drives = b.edges_at(b.cyc + 30, [1] * 1199)
    await b.run_until(drives[-1] + 20)
    level_after_burst = int(dut.EDGE_IN.value)
    assert level_after_burst == 0, "1200 toggles from 0 must end low"
    # One more edge while still full (ignored: the line is high at the re-SYNC).
    last = b.edges_at(b.cyc + 5, [])
    await b.run_until(last[-1] + 10)
    # The re-SYNC is written on the first cycle the FIFO has room, i.e. as soon
    # as draining starts; the tail edges follow it by < 255 ticks so they PACK.
    dut.ED_MSG_RDY.value = 1
    tail = b.edges_at(b.cyc + 20, [4, 4])
    await b.run_until(tail[-1] + 700)

    got = b.stream_words()
    syncs = [i for i, w in enumerate(got) if kind_of(w) in (RLE_SYNC_LO, RLE_SYNC_HI)]
    assert len(syncs) == 2 and syncs[0] == 0, (
        f"want the enable SYNC and exactly one re-SYNC, SYNC indexes {syncs} in "
        f"{len(got)} words")
    assert b.loss_pulses == 1, (
        f"ED_ERRORS[0] pulsed {b.loss_pulses} times: one lost word must pulse once, "
        "and nothing may be written (or lost) until the re-SYNC")
    body = got[1:syncs[1]]
    assert body[1:] == [pack(1, 1, 1)] * (len(body) - 1), "burst PACKs are not 1,1,1"
    resync, after = got[syncs[1]], got[syncs[1] + 1:]
    assert kind_of(resync) == RLE_SYNC_HI, (
        f"re-SYNC {resync:#010x} must report the line high (the edge after the "
        "burst was ignored, so only the SYNC level carries it)")
    d0 = after[0] & 0xFF if after else -1
    assert after == [pack(d0, 4, 4)], f"after re-SYNC {[hex(w) for w in after]}"
    check_anchor(resync, d0, tail[0], "resync")
    b.check_flags()


if __name__ == "__main__":
    main()
