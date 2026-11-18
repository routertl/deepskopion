"""Adversarial test suite for edge_batcher (DS-P2.47 Phase B1).

One test per EBT-REQ in hw/rtl/datalogger/infra/edge_batcher/requirements.yml.
The sanctioned oracle is sw/stream/zs_msg.parse_edge_batch on a buffer
assembled from {BATCH_ANCHOR_TS, BATCH_CHANNEL, BATCH_N, BATCH_PAYLOAD}
— ROUTERTL-002 satisfied (hard-coded driven values, independent parse).

file-don't-fix: every failure files a T-ticket-ready diagnostic. No
muted assertions. Bug-finding is success; green-bar = yellow flag.
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: Could not import engine. Make sure the script is running in the correct environment.")
    sys.exit(1)

from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT))
from sw.stream import zs_msg as zsmsg


# Match the VHDL default generics. `rr sim --ip` does NOT apply test-time
# generic overrides; the DUT runs with G_CHANNEL_ID=0 unless overridden via
# `-g G_CHANNEL_ID=X` on the rr sim command line. Pin CHANNEL=0 here so
# the suite passes in default-build conditions; channel-non-zero coverage
# lives behind an explicit -g run (or future TbEnv generic override).
N_MAX = 32
CHANNEL = 0
T_FLUSH = 10_000   # matches VHDL G_T_FLUSH_CYCLES default
PERIOD_NS = 10


# ── Helpers ────────────────────────────────────────────────────────────────

def make_ev_word(device_id, s=False, f=False, e=False, ts_us=0, ext=0):
    """Compose the 32-bit edge_event word matching edge_datalogger.vhd.

    bits [31:27]=device_id, [26]=S, [25]=F, [24]=E, [23:8]=ts_us, [7:0]=ext.
    The batcher only consumes S/F/E + the TIMESTAMP_64 input; device_id is
    not inspected here (the upstream demux is the filter).
    """
    return ((device_id & 0x1F) << 27
            | (1 if s else 0) << 26
            | (1 if f else 0) << 25
            | (1 if e else 0) << 24
            | (ts_us & 0xFFFF) << 8
            | (ext & 0xFF))


async def reset_dut(dut, n_cycles=8):
    dut.RST.value = 1
    dut.EV_VALID.value = 0
    dut.EV_WORD.value = 0
    dut.TIMESTAMP_64.value = 0
    dut.BATCH_READY.value = 0
    await ClockCycles(dut.CLK, n_cycles)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


async def push_event(dut, ev_word, ts_64):
    """Drive one (EV_VALID && EV_READY) handshake.

    Pre-edge sampling: wait until EV_READY is high BEFORE the next rising
    edge, fire the edge to consume the handshake, then drop VALID. This
    matches the standard AXI-stream contract — both signals high in the
    same pre-edge moment latches the event on the edge that follows.
    """
    dut.EV_WORD.value = ev_word
    dut.TIMESTAMP_64.value = ts_64
    dut.EV_VALID.value = 1

    for _ in range(64):
        # Settle combinational outputs before sampling READY.
        await ReadOnly()
        ready = int(dut.EV_READY.value)
        await RisingEdge(dut.CLK)
        if ready == 1:
            break
    else:
        raise TimeoutError("event push never handshaked — DUT stuck")

    dut.EV_VALID.value = 0


async def consume_batch(dut, deadline_cycles=2 * T_FLUSH):
    """Wait for BATCH_VALID, then assert BATCH_READY for one cycle.

    Returns (anchor_ts, channel, n, payload_bytes) sampled the cycle of
    the handshake.
    """
    for _ in range(deadline_cycles):
        await RisingEdge(dut.CLK)
        if int(dut.BATCH_VALID.value) == 1:
            break
    else:
        raise TimeoutError(
            f"BATCH_VALID never rose within {deadline_cycles} cycles")

    anchor = int(dut.BATCH_ANCHOR_TS.value)
    channel = int(dut.BATCH_CHANNEL.value)
    n = int(dut.BATCH_N.value)
    # BATCH_PAYLOAD is N_MAX*32 bits; cocotb returns it as one big int.
    full = int(dut.BATCH_PAYLOAD.value)
    # Extract bytes for the n active slots, LE per entry.
    active_bytes = bytearray()
    for slot in range(n):
        word = (full >> (slot * 32)) & 0xFFFFFFFF
        active_bytes.extend(int(word).to_bytes(4, byteorder="little"))

    dut.BATCH_READY.value = 1
    await RisingEdge(dut.CLK)
    dut.BATCH_READY.value = 0
    return anchor, channel, n, bytes(active_bytes)


def synth_frame_bytes(anchor_ts, channel, payload_bytes):
    """Synthesize the equivalent zs_msg wire buffer that parse_edge_batch
    expects: 20-byte header (EDGE_BATCH msg_type) + payload."""
    hdr = zsmsg.make_header(
        datalogger_id=channel,
        domain=zsmsg.DOMAIN_EDGE,
        event_id=zsmsg.EVT_EDGE_BATCH,
        severity=zsmsg.SEV_INFO,
        msg_class=zsmsg.CLASS_DATA,
        timestamp=anchor_ts,
        payload_len=len(payload_bytes),
    )
    return zsmsg.pack_header(hdr) + payload_bytes


# ── Tests ──────────────────────────────────────────────────────────────────

@cocotb.test()
@requires("EBT-REQ-1")
async def test_reset_quiescent(dut):
    """EBT-REQ-1: under RST, BATCH_VALID='0'; post-RST, EV_READY='1'."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()

    dut.RST.value = 1
    dut.EV_VALID.value = 0
    dut.BATCH_READY.value = 0
    await ClockCycles(dut.CLK, 8)
    assert int(dut.BATCH_VALID.value) == 0, "BATCH_VALID high under RST"

    dut.RST.value = 0
    await RisingEdge(dut.CLK)
    await ClockCycles(dut.CLK, 4)
    assert int(dut.BATCH_VALID.value) == 0, "spurious BATCH_VALID after RST drop"
    assert int(dut.EV_READY.value) == 1, "EV_READY didn't rise post-RST"


@cocotb.test()
@requires("EBT-REQ-4")
@requires("EBT-REQ-8")
async def test_capacity_flush_n32(dut):
    """EBT-REQ-4 + REQ-8: N_MAX events → one flush; payload parses round-trip
    via sw.stream.zs_msg.parse_edge_batch with byte-exact entries."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    base_ts = 0x1234_5678_DEAD_0000
    spacing = 7  # 7 cycles per event — well inside u16 range over 32 events

    for i in range(N_MAX):
        ev_word = make_ev_word(device_id=CHANNEL, e=(i % 2 == 0), f=(i == N_MAX - 1))
        await push_event(dut, ev_word, base_ts + i * spacing)

    anchor, channel, n, payload = await consume_batch(dut)
    assert n == N_MAX, f"BATCH_N={n}, want {N_MAX}"
    assert channel == CHANNEL, f"BATCH_CHANNEL={channel}, want {CHANNEL}"
    assert anchor == base_ts, f"BATCH_ANCHOR_TS={anchor:#x}, want {base_ts:#x}"

    # Round-trip through the Python oracle.
    wire = synth_frame_bytes(anchor, channel, payload)
    hdr, events = zsmsg.parse_edge_batch(wire)
    assert hdr.payload_len == N_MAX * 4
    assert len(events) == N_MAX
    for i, ev in enumerate(events):
        assert ev.ts_64b == base_ts + i * spacing, (
            f"event[{i}] ts: got {ev.ts_64b:#x}, want {base_ts + i * spacing:#x}")
        assert ev.channel == CHANNEL
        assert ev.e == (i % 2 == 0)
        assert ev.f == (i == N_MAX - 1)
        assert ev.s is False


@cocotb.test()
@requires("EBT-REQ-5")
async def test_delta_overflow_flush(dut):
    """EBT-REQ-5: two events spaced > 0xFFFF cycles → first one flushes alone,
    second becomes the anchor of a fresh batch."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    base_ts = 0x4000
    overflow_ts = base_ts + 0x10000   # delta = 0x10000 → overflows u16

    # push_event uses pre-edge sampling: handshake fires the cycle EV_READY
    # is observed high, then VALID drops. For the overflow event the FSM
    # raises READY (n<N_MAX), accepts into the pending stash on that edge,
    # and transitions to FLUSH — the producer correctly sees a single ack.
    await push_event(dut, make_ev_word(device_id=CHANNEL, e=True), base_ts)
    await push_event(dut, make_ev_word(device_id=CHANNEL, e=False), overflow_ts)

    # First batch: the original event alone (anchor=base_ts, n=1).
    anchor1, ch1, n1, payload1 = await consume_batch(dut, deadline_cycles=32)
    assert n1 == 1, f"first batch should hold only the first event, got n={n1}"
    assert anchor1 == base_ts, f"first batch anchor mismatch"

    # FSM transitions FLUSH → ACCUM inline (consuming pending). The parked
    # event becomes the anchor of the second batch with n=1; the idle timer
    # then expires after T_FLUSH cycles and flushes it.
    anchor2, ch2, n2, payload2 = await consume_batch(dut, deadline_cycles=T_FLUSH * 2)
    assert n2 == 1, f"second batch should hold the parked event, got n={n2}"
    assert anchor2 == overflow_ts, (
        f"second batch anchor: got {anchor2:#x}, want {overflow_ts:#x}")

    # First entry of the second batch must have ts_delta == 0.
    wire = synth_frame_bytes(anchor2, ch2, payload2)
    _, events = zsmsg.parse_edge_batch(wire)
    assert events[0].ts_64b == overflow_ts


@cocotb.test()
@requires("EBT-REQ-6")
async def test_idle_timer_flush(dut):
    """EBT-REQ-6: 1 event then idle T_FLUSH cycles → partial-batch flush."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    await push_event(dut, make_ev_word(device_id=CHANNEL, e=True), 0x9000)

    # Wait somewhat longer than T_FLUSH for the partial-batch flush.
    anchor, channel, n, payload = await consume_batch(dut,
                                                       deadline_cycles=T_FLUSH + 32)
    assert n == 1, f"timer flush should emit n=1, got {n}"
    assert anchor == 0x9000


@cocotb.test()
@requires("EBT-REQ-7")
async def test_backpressure_holds_batch(dut):
    """EBT-REQ-7: BATCH_VALID stays high until BATCH_READY; EV_READY drops
    while waiting for the downstream handshake."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    # Fill to capacity → forces a flush.
    for i in range(N_MAX):
        await push_event(dut, make_ev_word(device_id=CHANNEL), 0x1000 + i)

    # Wait for BATCH_VALID without taking it.
    for _ in range(32):
        await RisingEdge(dut.CLK)
        if int(dut.BATCH_VALID.value) == 1:
            break
    else:
        raise AssertionError("capacity flush never raised BATCH_VALID")

    # Hold BATCH_READY low for a stretch — BATCH_VALID must stay up,
    # EV_READY must stay low.
    for _ in range(64):
        await RisingEdge(dut.CLK)
        assert int(dut.BATCH_VALID.value) == 1, "BATCH_VALID dropped without BATCH_READY"
        assert int(dut.EV_READY.value) == 0, "EV_READY high during pending flush"

    # Now drain.
    dut.BATCH_READY.value = 1
    await RisingEdge(dut.CLK)
    dut.BATCH_READY.value = 0
    await RisingEdge(dut.CLK)
    assert int(dut.BATCH_VALID.value) == 0, "BATCH_VALID lingered after handshake"
    assert int(dut.EV_READY.value) == 1, "EV_READY didn't recover after flush"


# ── Sanctioned simulation entry point ────────────────────────────────────────

if __name__ == "__main__":
    run_simulation(top_level="edge_batcher", module=Path(__file__).stem)
