"""Adversarial test suite for edge_batcher_aggregator (DS-P2.47 Phase B2 step 2).

One test per EBA-REQ in
hw/rtl/datalogger/infra/edge_batcher_aggregator/requirements.yml.
The sanctioned oracle is sw/stream/zs_msg.parse_edge_batch on a wire
buffer assembled from the FR_* outputs — ROUTERTL-002 satisfied.

file-don't-fix: every failure files a T-ticket-ready diagnostic. No
muted assertions. Bug-finding is success; green-bar = yellow flag.
"""

import sys
from pathlib import Path

import cocotb
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

# Match the VHDL default generics.
NUM_CHANNELS = 8
N_MAX = 32
T_FLUSH = 10_000
PERIOD_NS = 10


# ── Helpers ────────────────────────────────────────────────────────────────

def make_ev_word(device_id, s=False, f=False, e=False, ts_us=0, ext=0):
    """32-bit edge_event word — bits[31:27]=device_id, [26]=S, [25]=F, [24]=E."""
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
    dut.FR_READY.value = 0
    await ClockCycles(dut.CLK, n_cycles)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


async def push_event(dut, device_id, ts_64, s=False, f=False, e=False):
    """Drive one (EV_VALID && EV_READY) handshake (pre-edge sampling)."""
    dut.EV_WORD.value = make_ev_word(device_id, s=s, f=f, e=e)
    dut.TIMESTAMP_64.value = ts_64
    dut.EV_VALID.value = 1
    for _ in range(64):
        await ReadOnly()
        ready = int(dut.EV_READY.value)
        await RisingEdge(dut.CLK)
        if ready == 1:
            break
    else:
        raise TimeoutError(f"event for ch{device_id} never handshaked")
    dut.EV_VALID.value = 0


async def consume_frame(dut, deadline_cycles=2 * T_FLUSH):
    """Wait for FR_VALID, sample the full FR_* tuple, ack with FR_READY=1.

    Returns dict with datalogger_id, msg_class, msg_type, flags, timestamp,
    payload_words, payload (bytes for the active portion).
    """
    for _ in range(deadline_cycles):
        await RisingEdge(dut.CLK)
        if int(dut.FR_VALID.value) == 1:
            break
    else:
        raise TimeoutError(
            f"FR_VALID never rose within {deadline_cycles} cycles")

    datalogger_id    = int(dut.FR_DATALOGGER_ID.value)
    msg_class     = int(dut.FR_MSG_CLASS.value)
    msg_type      = int(dut.FR_MSG_TYPE.value)
    flags         = int(dut.FR_FLAGS.value)
    timestamp     = int(dut.FR_TIMESTAMP.value)
    payload_words = int(dut.FR_PAYLOAD_WORDS.value)
    full          = int(dut.FR_PAYLOAD.value)

    payload_bytes = bytearray()
    for slot in range(payload_words):
        word = (full >> (slot * 32)) & 0xFFFFFFFF
        payload_bytes.extend(int(word).to_bytes(4, byteorder="little"))

    dut.FR_READY.value = 1
    await RisingEdge(dut.CLK)
    dut.FR_READY.value = 0

    return dict(
        datalogger_id=datalogger_id,
        msg_class=msg_class,
        msg_type=msg_type,
        flags=flags,
        timestamp=timestamp,
        payload_words=payload_words,
        payload=bytes(payload_bytes),
    )


def synth_frame_bytes(frame):
    """Assemble a wire buffer matching the framer's emitted bytes."""
    hdr = zsmsg.make_header(
        datalogger_id=frame["datalogger_id"],
        domain=zsmsg.DOMAIN_EDGE,
        event_id=zsmsg.EVT_EDGE_BATCH,
        severity=zsmsg.SEV_INFO,
        msg_class=zsmsg.CLASS_DATA,
        timestamp=frame["timestamp"],
        payload_len=len(frame["payload"]),
    )
    return zsmsg.pack_header(hdr) + frame["payload"]


# ── Tests ──────────────────────────────────────────────────────────────────

@cocotb.test()
@requires("EBA-REQ-3")
@requires("EBA-REQ-10")
async def test_single_channel_capacity_flush(dut):
    """EBA-REQ-3 + REQ-10: 32 events on channel 3 → one capacity flush
    on batcher[3] → FR_VALID with DATALOGGER_ID=3, PAYLOAD_WORDS=32, and
    a byte-exact round-trip through sw.stream.zs_msg.parse_edge_batch."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    base_ts = 0x4000_0000
    for i in range(N_MAX):
        await push_event(dut, device_id=3, ts_64=base_ts + i * 11,
                         e=(i % 2 == 0))

    frame = await consume_frame(dut, deadline_cycles=64)
    assert frame["datalogger_id"] == 3, frame
    assert frame["payload_words"] == N_MAX, frame
    assert frame["msg_class"] == zsmsg.CLASS_DATA
    assert frame["msg_type"] == 0x0601, f"msg_type={frame['msg_type']:#06x}"
    assert frame["flags"] == 0
    assert frame["timestamp"] == base_ts, f"got {frame['timestamp']:#x}"

    wire = synth_frame_bytes(frame)
    hdr, events = zsmsg.parse_edge_batch(wire)
    assert len(events) == N_MAX
    for i, ev in enumerate(events):
        assert ev.ts_64b == base_ts + i * 11, f"event[{i}] ts mismatch"
        assert ev.channel == 3
        assert ev.e == (i % 2 == 0)


@cocotb.test()
@requires("EBA-REQ-1")
@requires("EBA-REQ-4")
async def test_concurrent_channels_priority_arb(dut):
    """EBA-REQ-1 + REQ-4: drive 32 events on ch 0 then 32 on ch 5.
    Two capacity flushes emerge; channel 0's wins the arb because it
    became valid first AND is the lowest-index batcher with a ready
    batch when channel 5's also lands."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    # Fill channel 0 to capacity first; flush sits at FR_VALID waiting.
    base0 = 0x1000
    for i in range(N_MAX):
        await push_event(dut, device_id=0, ts_64=base0 + i * 3)

    # Now fill channel 5 — its events should land in batcher[5] without
    # disturbing the already-pending channel-0 flush.
    base5 = 0x8000
    for i in range(N_MAX):
        await push_event(dut, device_id=5, ts_64=base5 + i * 7)

    # First consume: channel 0's batch (priority).
    frame0 = await consume_frame(dut, deadline_cycles=64)
    assert frame0["datalogger_id"] == 0, frame0
    assert frame0["payload_words"] == N_MAX

    # After ack, channel 5's flush becomes the visible winner.
    frame5 = await consume_frame(dut, deadline_cycles=64)
    assert frame5["datalogger_id"] == 5, frame5
    assert frame5["payload_words"] == N_MAX

    # Both round-trip cleanly.
    _, events0 = zsmsg.parse_edge_batch(synth_frame_bytes(frame0))
    _, events5 = zsmsg.parse_edge_batch(synth_frame_bytes(frame5))
    assert all(ev.channel == 0 for ev in events0)
    assert all(ev.channel == 5 for ev in events5)
    assert events0[0].ts_64b == base0
    assert events5[0].ts_64b == base5


@cocotb.test()
@requires("EBA-REQ-2")
async def test_unmapped_device_id_drop_and_consume(dut):
    """EBA-REQ-2 (DS-P3.50 hardened): an unmapped device_id is DROPPED-AND-
    CONSUMED — EV_READY='1' (one clean handshake, producer moves on), no batcher
    sees the pulse, FR_VALID stays low. The pre-DS-P3.50 behaviour was
    EV_READY='0' = a permanent producer stall (deadlock); a malformed id would
    wedge the whole edge pipe. This test would FAIL on that old stall."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    # device_id 15 is out of range (identity map, G_NUM_CHANNELS=8) → unmapped.
    dut.EV_WORD.value = make_ev_word(device_id=15, e=True)
    dut.TIMESTAMP_64.value = 0x500
    dut.EV_VALID.value = 1
    # The unmapped event must be CONSUMED (EV_READY='1') so the pipe never
    # deadlocks — and no batcher must latch it.
    await ReadOnly()
    assert int(dut.EV_READY.value) == 1, (
        "EBA-REQ-2: unmapped device_id must drop-and-consume (EV_READY='1'), "
        "not stall — pre-DS-P3.50 deadlock regression")
    await RisingEdge(dut.CLK)
    dut.EV_VALID.value = 0

    # FR_VALID must stay low — nothing got into a batcher.
    await ClockCycles(dut.CLK, 4)
    assert int(dut.FR_VALID.value) == 0, "FR_VALID rose from a dropped event"

    # Now drive a valid in-range event; capacity flush should still work.
    base = 0x2000
    for i in range(N_MAX):
        await push_event(dut, device_id=2, ts_64=base + i)
    frame = await consume_frame(dut, deadline_cycles=64)
    assert frame["datalogger_id"] == 2
    assert frame["payload_words"] == N_MAX


@cocotb.test()
@requires("EBA-REQ-3")
async def test_delta_overflow_parks_event_and_reanchors(dut):
    """Adversarial: exercise the batcher's delta-overflow flush + pending-stash
    re-anchor PATH through the aggregator (uncovered by the prior suite). A
    first event anchors a batch; a second event whose TIMESTAMP_64 - anchor
    exceeds the 16-bit ts_delta field (> 0xFFFF) must force a flush of the
    1-event batch AND re-emerge as the anchor of the NEXT batch — no event lost,
    no duplicate, correct per-batch anchors."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    anchor0 = 0x10_0000
    await push_event(dut, device_id=1, ts_64=anchor0)          # opens batch, n=1
    # Second event 0x1_0000 ticks later → delta 65536 > 0xFFFF → overflow flush.
    anchor1 = anchor0 + 0x1_0000
    await push_event(dut, device_id=1, ts_64=anchor1)          # parks, closes batch

    # First flush: the single-event batch anchored at anchor0.
    f0 = await consume_frame(dut, deadline_cycles=64)
    assert f0["datalogger_id"] == 1, f0
    assert f0["payload_words"] == 1, f"overflow flush should carry exactly 1 event: {f0}"
    assert f0["timestamp"] == anchor0, f"anchor0 wrong: {f0['timestamp']:#x}"

    # The parked event must re-anchor the next batch. Add more events, then a
    # timer flush delivers the second batch anchored at anchor1.
    for i in range(3):
        await push_event(dut, device_id=1, ts_64=anchor1 + 1 + i)
    f1 = await consume_frame(dut, deadline_cycles=2 * T_FLUSH + 64)
    assert f1["datalogger_id"] == 1, f1
    assert f1["payload_words"] == 4, f"parked re-anchor + 3 = 4 events: {f1}"
    assert f1["timestamp"] == anchor1, f"re-anchor ts wrong: {f1['timestamp']:#x}"
    _, evs = zsmsg.parse_edge_batch(synth_frame_bytes(f1))
    assert evs[0].ts_64b == anchor1, "parked event lost its timestamp"
    assert all(ev.channel == 1 for ev in evs)


@cocotb.test()
@requires("EBA-REQ-20")
async def test_reset_clears_all_state(dut):
    """EBA-REQ-20: mid-burst RST clears every batcher to n_r=0, drops
    FR_VALID, and after RST releases the aggregator accepts a fresh
    burst on a different channel that flushes cleanly."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    # Partial burst on channel 1.
    for i in range(5):
        await push_event(dut, device_id=1, ts_64=0x100 + i)

    # Mid-burst reset.
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 4)
    assert int(dut.FR_VALID.value) == 0, "FR_VALID high under RST"
    dut.RST.value = 0
    await RisingEdge(dut.CLK)

    # Fresh capacity burst on channel 4 — should work cleanly post-RST.
    base = 0xABCD_0000
    for i in range(N_MAX):
        await push_event(dut, device_id=4, ts_64=base + i)
    frame = await consume_frame(dut, deadline_cycles=64)
    assert frame["datalogger_id"] == 4
    assert frame["payload_words"] == N_MAX
    assert frame["timestamp"] == base


# ── Sanctioned simulation entry point ────────────────────────────────────────

RTL = DEEPSKOPION_ROOT / "hw" / "rtl"


def main():
    # Self-contained compile set (the aggregator only needs edge_batcher); avoids
    # pulling the whole deepskopion_top project + its OLO/ddr_stream_axi libs.
    run_simulation(
        top_level="edge_batcher_aggregator",
        module=Path(__file__).stem,
        custom_libraries={"tich": [
            f"{RTL}/datalogger/infra/edge_batcher/edge_batcher.vhd",
            f"{RTL}/datalogger/infra/edge_batcher_aggregator/edge_batcher_aggregator.vhd",
        ]},
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
