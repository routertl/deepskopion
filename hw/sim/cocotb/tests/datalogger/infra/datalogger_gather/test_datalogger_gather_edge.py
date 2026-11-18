"""DS-P3.32 (final arm) — protocol-level multi-channel EDGE traffic through the
full datalogger_gather.

REAL edge pulse trains are driven onto ALL THREE of the gather's EDGE datalogger
pins at once (EDGE0..2 = lines 42..44, device ids 1/2/3, the DS-P1.104 default mix). This is the
multi-source SGA-REQ-3 stress the single-instance I2C/UART/SPI harnesses could
NOT exert: three live sources contending for the per-protocol EDGE arbiter and
the top burst arbiter simultaneously. Every non-EDGE pin idles high so only the
CONFIG-enabled EDGE instances emit.

Each channel is driven a DISTINCT, hard-coded pulse train (different edge count
AND polarity sequence per channel, ROUTERTL-002) so any cross-route, merge, or
dropped/duplicated edge is unambiguous.

EDGE message (nonstd, edge_datalogger_protocol.yml): device_id[31:27], s[26],
f[25], e[24], ts[23:8], ext[7:0]. For an edge event (s=0) e = NOT(level-after):
e=0 ⇒ rising (→1), e=1 ⇒ falling (→0). DS-T2.43 ENABLED lifecycle frames carry
[26:24]==0b100 and are filtered out.

DEFECT FOUND + FIXED (DS-T2.45): the harness found that enabling an EDGE channel
on a statically-LOW line injected TWO phantom falling-edge frames at the head of
the stream. Root cause (proven by hierarchy probing in the triggering cadence):
the per-line `glitch_filter` reset its output to a hard '1' (an idle-HIGH
assumption that matches I2C/UART/SPI but NOT an EDGE line idling LOW), so its
output started high and the spurious startup '1'->'0' convergence — sampled while
`configEnable` was asserted during the settling window — was a real detected
falling edge. Fixed by making glitch_filter come out of reset already tracking
the line (filt_r <= sync_r), so Q reaches the actual idle level for any polarity.
Both tests below now assert the strict, phantom-free contract:
  * test_edge_multichannel_routing — each channel's captured edge-polarity
    sequence EQUALS the driven one exactly (no phantoms), correctly routed, no
    foreign-id leak, no tag corruption, no silent channel.
  * test_edge_enable_emits_no_phantom_edges — enabling on a static line yields
    ZERO edge events (only the lifecycle frame). This is the DS-T2.45 regression
    guard; it failed before the fix (was expect_fail) and is a real pass now.

bug-finding-is-success: the wolf hunt found a real defect AND slayed it; the
strict assertions were never weakened (ROUTERTL-002).
"""
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import RisingEdge, ClockCycles, Timer

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT / "hw" / "sim" / "cocotb"))   # ds_sim_sources
from ds_sim_sources import scoped_libraries   # noqa: E402

PERIOD_NS = 10
ENABLE_CAPTURING = 0b01          # edge_datalogger CONFIG control code (capture both edges)

# Per-channel: (device_id, the cocotb edge-line port name, the driven level
# sequence starting from idle-low). Distinct lengths + endings make any
# cross-route / merge / drop obvious. Hard-coded (ROUTERTL-002).
#   level sequence → expected edge events: e_bit = NOT(level), so
#     level 1 (rising)  → e=0,  level 0 (falling) → e=1
CHANNELS = [
    {"id": 1, "line": "EDGE0_IN", "levels": [1, 0, 1, 0]},          # RFRF — 4 edges
    {"id": 2, "line": "EDGE1_IN", "levels": [1, 0, 1]},             # RFR  — 3 edges, ends high
    {"id": 3, "line": "EDGE2_IN", "levels": [1, 0, 1, 0, 1, 0]},    # RFRFRF — 6 edges
]

EDGE_SPACING_NS = 500           # gap between successive edges on one channel
CHANNEL_STAGGER_NS = 130        # start offset between channels → interleave at the arbiter


def expected_polarity(levels):
    """Driven level sequence → expected (e_bit) sequence. e = NOT(level)."""
    return [0 if lvl else 1 for lvl in levels]


async def reset(dut, n=10):
    dut.RST.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.DOUT_REQ.value = 0
    dut.EDGE0_IN.value = 0      # edge lines idle LOW
    dut.EDGE1_IN.value = 0
    dut.EDGE2_IN.value = 0
    await ClockCycles(dut.CLK, n)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


async def _ramp_timestamp(dut):
    t = 0
    while True:
        await RisingEdge(dut.CLK)
        t = (t + 1) & 0xFFFFFFFF
        dut.TIMESTAMP.value = t


class DoutCollector:
    def __init__(self, dut):
        self.dut = dut
        self.frames = []   # (datalogger_id, payload)
        self._run = True

    async def run(self):
        self.dut.DOUT_REQ.value = 1
        while self._run:
            await RisingEdge(self.dut.CLK)
            if int(self.dut.DOUT_VALID.value) == 1:
                self.frames.append((int(self.dut.DOUT_DATALOGGER_ID.value),
                                    int(self.dut.DOUT_PAYLOAD.value)))

    def stop(self):
        self._run = False


async def _drive_channel(dut, line_handle, levels, stagger_ns):
    """Play one channel's level sequence onto its edge line, staggered start."""
    if stagger_ns:
        await Timer(stagger_ns, units="ns")
    for lvl in levels:
        line_handle.value = lvl
        await Timer(EDGE_SPACING_NS, units="ns")


def is_lifecycle(payload):
    """DS-T2.43 EDGE ENABLED/DISABLED lifecycle marker: [26:24] == 0b100."""
    return ((payload >> 24) & 0x7) == 0b100


async def _enable_channel(dut, dev_id):
    """Enable one EDGE datalogger (config_control hardened, DS-P3.96 — plain CONFIG
    steps latch). config_control filters by device id, so each write enables
    exactly one channel and latches; later writes don't disturb it."""
    dut.CONFIG.value = (dev_id << 11) | ENABLE_CAPTURING
    await ClockCycles(dut.CLK, 16)


@cocotb.test()
@requires("SGA-REQ-3")
async def test_edge_multichannel_routing(dut):
    """SGA-REQ-3: three concurrent EDGE pulse trains driven through the whole
    gather each emerge routed to their own device id (1/2/3) with the exact
    rising/falling polarity sequence recovered per channel.

    With DS-T2.45 fixed, each channel's captured edge-polarity sequence EQUALS
    the driven sequence exactly — no phantom prefix, no foreign-id leak, no tag
    corruption, no silent channel."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset(dut)
    cocotb.start_soon(_ramp_timestamp(dut))

    coll = DoutCollector(dut)
    cocotb.start_soon(coll.run())

    for ch in CHANNELS:
        await _enable_channel(dut, ch["id"])
    await ClockCycles(dut.CLK, 32)

    # Drive all three pulse trains CONCURRENTLY (staggered starts so their edges
    # interleave at the arbiter — the real multi-source contention).
    drivers = [
        cocotb.start_soon(_drive_channel(dut, getattr(dut, ch["line"]),
                                         ch["levels"], i * CHANNEL_STAGGER_NS))
        for i, ch in enumerate(CHANNELS)
    ]
    for d in drivers:
        await d

    # Settle: let the last edges propagate through the FIFOs + arbiters to DOUT.
    await ClockCycles(dut.CLK, 256)
    coll.stop()

    assert coll.frames, "gather emitted NO DOUT frames for three concurrent EDGE trains"

    events = [(sid, pw) for sid, pw in coll.frames if not is_lifecycle(pw)]

    # No leak to a foreign device id.
    valid_ids = {ch["id"] for ch in CHANNELS}
    misrouted = sorted({sid for sid, _ in events if sid not in valid_ids})
    assert not misrouted, (
        f"EDGE events mis-routed to foreign device_ids {misrouted} "
        f"(expected only {sorted(valid_ids)})")

    # s_bit must be 0 for every edge event (enable-capturing, not readback).
    bad_s = [(sid, pw) for sid, pw in events if (pw >> 26) & 0x1]
    assert not bad_s, (
        f"{len(bad_s)} edge events have s_bit=1 (readback) under enable-capturing "
        f"— unexpected: first {[(s, hex(p)) for s, p in bad_s[:3]]}")

    for ch in CHANNELS:
        got = [(pw >> 24) & 0x1 for sid, pw in events if sid == ch["id"]]
        want = expected_polarity(ch["levels"])
        # Byte-exact (DS-T2.44 standard, now reachable post-DS-T2.45): the
        # captured edge-polarity sequence must EQUAL the driven one — exact
        # count, order, polarity; no phantom prefix, no drops, no duplicates.
        assert got == want, (
            f"EDGE channel id={ch['id']} polarity sequence not byte-exact through "
            f"the gather: got {got}, expected {want} (driven levels {ch['levels']})")
        # The in-payload device_id must match the routed datalogger id (no tag corruption).
        bad_dev = [hex(pw) for sid, pw in events
                   if sid == ch["id"] and ((pw >> 27) & 0x1F) != ch["id"]]
        assert not bad_dev, (
            f"EDGE channel id={ch['id']}: payload device_id field disagrees with "
            f"DOUT_DATALOGGER_ID for {len(bad_dev)} frames: {bad_dev[:3]}")

    emitted = {sid for sid, _ in events}
    silent = sorted(valid_ids - emitted)
    assert not silent, f"EDGE channels {silent} emitted NO edge events (driven but silent)"

    dut._log.info(
        f"SGA-REQ-3 EDGE multi-channel routing PASS — {len(events)} edge events "
        f"across {sorted(emitted)}; each channel's polarity sequence byte-exact, "
        f"no phantoms (DS-T2.45 fixed)")


@cocotb.test()
@requires("SGA-REQ-3")
async def test_edge_enable_emits_no_phantom_edges(dut):
    """DS-T2.45 regression guard: enabling an EDGE datalogger on a line held at a
    constant level must produce ZERO edge events — only the DS-T2.43 ENABLED
    lifecycle frame. Before the glitch_filter reset fix this emitted 2 phantom
    falling-edge frames (the idle-high reset value mismatching an idle-low line);
    it is a real pass now. If this ever regresses, the glitch_filter reset
    tracking (filt_r <= sync_r) has been broken again."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset(dut)
    cocotb.start_soon(_ramp_timestamp(dut))

    coll = DoutCollector(dut)
    cocotb.start_soon(coll.run())

    # Enable ONE channel; the line is held statically low — no real transitions.
    await _enable_channel(dut, CHANNELS[0]["id"])
    await ClockCycles(dut.CLK, 256)
    coll.stop()

    phantom = [(sid, hex(pw)) for sid, pw in coll.frames
               if sid == CHANNELS[0]["id"] and not is_lifecycle(pw)]
    assert not phantom, (
        f"DS-T2.45: enabling EDGE id={CHANNELS[0]['id']} on a static line emitted "
        f"{len(phantom)} phantom edge frame(s) (only the lifecycle marker is "
        f"correct): {phantom}")


def main():
    run_simulation(
        top_level="tb_datalogger_gather_edge",
        module=Path(__file__).stem,
        custom_libraries=scoped_libraries("tb_datalogger_gather_edge"),
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
