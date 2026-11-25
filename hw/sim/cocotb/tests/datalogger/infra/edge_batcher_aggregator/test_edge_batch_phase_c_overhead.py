# rr unknown-llm (AI-generated)
"""DS-P2.48 Phase C — EDGE_BATCH end-to-end overhead measurement.

Phase A (spec + Python codec, DS-P2.47), Phase B1 (per-channel batcher
FSM), Phase B2 (variable-payload framer + aggregator wiring) all landed
across deepskopion 149f7d4 → 9be6bf5.  The wire-format spec promised
"header overhead 83% → 14% at N=32, 5× per-event efficiency win".  This
test gates that claim against the actual RTL.

Three acceptance gates per ``docs/software/data_plane/message_formats/
payloads/edge_batch.md`` §6:

1. **Steady-state burst @ N_MAX=32, single channel**.  Drive back-to-back
   events on one channel; every flush should be a capacity flush
   (20 B header + 32 × 4 B payload = 148 B per 32 events = 4.625 B/ev).
   Gate: ``bytes_per_event ≤ 5`` vs the unbatched 24 B/event baseline.

2. **Mixed burst + sparse traffic**.  One channel bursting (capacity
   flushes) interleaved with another emitting sparse events (timer
   flushes — single-event batches that pay the full 24 B/event cost).
   Gate: aggregate ``bytes_per_event ≤ 12`` → ≥ 2× win over baseline.

3. **Round-trip exact-match**.  Every event driven into the aggregator
   must parse back through ``parse_edge_batch`` exactly once, with the
   right reconstructed absolute timestamp + channel + flags.  Guards
   against drops, duplicates, and ts_delta arithmetic bugs.

The DUT is ``edge_batcher_aggregator`` (same as the per-REQ adversarial
suite in ``test_edge_batcher_aggregator.py``).  Helpers are duplicated
here so each cocotb test file is self-contained — cocotb test files
aren't import-safe (decorators register on module load).

Spec reference: ``docs/software/data_plane/message_formats/payloads/edge_batch.md`` §6.
Python codec: ``sw/stream/zs_msg.py`` — ``parse_edge_batch`` is the
sanctioned oracle (ROUTERTL-002).
"""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: Could not import engine. Make sure the script is running in the correct environment.")
    sys.exit(1)

from rr_cocotb_tb.env import TbEnv

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT))
from sw.stream import zs_msg as zsmsg

# Match the VHDL default generics.
NUM_CHANNELS = 8
N_MAX = 32
T_FLUSH = 10_000
PERIOD_NS = 10

# Per-spec wire-format constants.
HEADER_BYTES = 20
EVENT_BYTES = 4
UNBATCHED_BYTES_PER_EVENT = HEADER_BYTES + EVENT_BYTES  # 24 — the baseline


# ── Helpers (duplicated from test_edge_batcher_aggregator.py) ─────────


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
    """Drive one (EV_VALID && EV_READY) handshake."""
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
    """Wait for FR_VALID, sample, ack with FR_READY=1.  Returns the frame
    fields and the byte payload."""
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


def _frame_bytes_count(frame) -> int:
    """Total wire bytes for one emitted frame: header + payload."""
    return HEADER_BYTES + len(frame["payload"])


def _events_in_frame(frame) -> int:
    """Event count for one emitted frame: payload_words == N."""
    return frame["payload_words"]


# ── Test 1: steady-state capacity flushes ─────────────────────────────


@cocotb.test()
async def test_steady_burst_meets_5b_per_event(dut):
    """DS-P2.48 acceptance gate 1: at PoC default N_MAX=32, a back-to-
    back burst on a single channel must measure ≤ 5 B/event.

    Spec §6 promises 4.625 B/event at N=32 (148 B / 32 events).  We
    drive 5 capacity-sized bursts = 160 events to confirm steady-state
    (not just a single first-flush).  Round-trip via parse_edge_batch
    confirms no decode-side surprises.
    """
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    n_bursts = 5
    n_events_per_burst = N_MAX  # 32
    base_ts = 0x1000

    # Interleave push + drain so EV_READY stays high (FR_VALID would
    # otherwise hold and back-pressure the producer side).  Each
    # n_events_per_burst push cycle exactly fills one capacity batch;
    # the consume_frame call right after drains it before the next
    # batch starts accumulating.
    driven = []
    frames = []
    for burst in range(n_bursts):
        for i in range(n_events_per_burst):
            idx = burst * n_events_per_burst + i
            ts = base_ts + idx * 7   # 7-cycle ts step keeps ts_delta < 0xFFFF
            await push_event(dut, device_id=3, ts_64=ts, e=(idx % 2 == 0))
            driven.append((3, ts, (idx % 2 == 0)))
        frames.append(await consume_frame(dut, deadline_cycles=128))
    total_driven = n_bursts * n_events_per_burst  # 160

    # Tally + assert.
    total_bytes = sum(_frame_bytes_count(f) for f in frames)
    total_events = sum(_events_in_frame(f) for f in frames)
    bytes_per_event = total_bytes / total_events

    dut._log.info(
        "Phase C steady-burst: %d events, %d frames, %d bytes, "
        "%.3f B/event (baseline=%d, target ≤ 5)",
        total_events, len(frames), total_bytes,
        bytes_per_event, UNBATCHED_BYTES_PER_EVENT,
    )

    assert total_events == total_driven, (
        f"drop/dup: drove {total_driven} events, parsed {total_events}")
    assert all(_events_in_frame(f) == N_MAX for f in frames), (
        f"expected capacity flushes (N={N_MAX}); got "
        f"{[_events_in_frame(f) for f in frames]}")
    assert bytes_per_event <= 5.0, (
        f"steady-burst overhead {bytes_per_event:.3f} B/event exceeds the "
        f"≤5 B/event spec gate (baseline {UNBATCHED_BYTES_PER_EVENT}); "
        f"capacity flush should hit 4.625 B/event")

    # Round-trip: every driven event must reappear, exactly once, with
    # the right reconstructed timestamp.
    reconstructed = []
    for frame in frames:
        wire = synth_frame_bytes(frame)
        _, events = zsmsg.parse_edge_batch(wire)
        for ev in events:
            reconstructed.append((ev.channel, ev.ts_64b, ev.e))
    assert reconstructed == driven, (
        "round-trip mismatch — driven events != reconstructed; "
        "first divergence at index "
        f"{next((i for i, (a, b) in enumerate(zip(driven, reconstructed)) if a != b), 'tail')}")


# ── Test 2: mixed burst + sparse aggregate beats baseline 2× ──────────


@cocotb.test()
async def test_mixed_traffic_beats_baseline_twofold(dut):
    """DS-P2.48 acceptance gate 2: intermixed burst + sparse traffic
    must still measure ≤ 12 B/event in aggregate (≥ 2× win over the
    24 B/event unbatched baseline).

    Recipe: K capacity bursts on channel 0 + M sparse single-event
    timer-flushes on channel 1.  Per-event aggregate:
        (K * 148 + M * 24) / (K * 32 + M)
    K=5, M=10: (740 + 240) / (160 + 10) = 5.76 B/ev → 4.2× win.
    """
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    n_bursts = 5
    n_sparse = 10
    burst_events = n_bursts * N_MAX     # 160
    base_burst_ts = 0x10_0000
    base_sparse_ts = 0x80_0000

    driven_burst = []
    driven_sparse = []

    # Drive K capacity-sized bursts on channel 0 (all capacity flushes).
    # Interleave consume_frame so FR_VALID doesn't back-pressure EV_READY.
    burst_frames = []
    for burst in range(n_bursts):
        for i in range(N_MAX):
            idx = burst * N_MAX + i
            ts = base_burst_ts + idx * 5
            await push_event(dut, device_id=0, ts_64=ts, e=(idx % 3 == 0))
            driven_burst.append((0, ts, (idx % 3 == 0)))
        burst_frames.append(await consume_frame(dut, deadline_cycles=256))

    # Drive M sparse single events on channel 1, each separated by
    # > T_FLUSH cycles of idle so each one flushes via timer alone.
    sparse_frames = []
    for j in range(n_sparse):
        ts = base_sparse_ts + j * (T_FLUSH * 2)
        await push_event(dut, device_id=1, ts_64=ts, e=True)
        driven_sparse.append((1, ts, True))
        # Wait long enough for the timer flush to fire (T_FLUSH cycles).
        sparse_frames.append(await consume_frame(dut, deadline_cycles=2 * T_FLUSH))

    # Aggregate metric across BOTH traffic classes.
    all_frames = burst_frames + sparse_frames
    total_bytes = sum(_frame_bytes_count(f) for f in all_frames)
    total_events = sum(_events_in_frame(f) for f in all_frames)
    bytes_per_event = total_bytes / total_events
    win_ratio = UNBATCHED_BYTES_PER_EVENT / bytes_per_event

    dut._log.info(
        "Phase C mixed-traffic: %d burst-events (%d frames, %d B) + "
        "%d sparse-events (%d frames, %d B) → aggregate %.3f B/event "
        "(%.2f× win over baseline)",
        sum(_events_in_frame(f) for f in burst_frames), len(burst_frames),
        sum(_frame_bytes_count(f) for f in burst_frames),
        sum(_events_in_frame(f) for f in sparse_frames), len(sparse_frames),
        sum(_frame_bytes_count(f) for f in sparse_frames),
        bytes_per_event, win_ratio,
    )

    assert total_events == burst_events + n_sparse, (
        f"drop/dup: drove {burst_events + n_sparse} events, parsed {total_events}")
    assert all(_events_in_frame(f) == N_MAX for f in burst_frames), (
        f"expected pure capacity flushes on the burst channel; got "
        f"{[_events_in_frame(f) for f in burst_frames]}")
    assert all(_events_in_frame(f) == 1 for f in sparse_frames), (
        f"expected sparse channel to timer-flush single-event batches; got "
        f"{[_events_in_frame(f) for f in sparse_frames]}")
    assert bytes_per_event <= 12.0, (
        f"mixed-traffic overhead {bytes_per_event:.3f} B/event exceeds the "
        f"≤12 B/event spec gate (must beat baseline {UNBATCHED_BYTES_PER_EVENT} "
        f"by ≥ 2×)")

    # Per-channel round-trip exact-match.
    burst_recon = []
    for frame in burst_frames:
        _, events = zsmsg.parse_edge_batch(synth_frame_bytes(frame))
        for ev in events:
            burst_recon.append((ev.channel, ev.ts_64b, ev.e))
    assert burst_recon == driven_burst, "burst-channel round-trip mismatch"

    sparse_recon = []
    for frame in sparse_frames:
        _, events = zsmsg.parse_edge_batch(synth_frame_bytes(frame))
        for ev in events:
            sparse_recon.append((ev.channel, ev.ts_64b, ev.e))
    assert sparse_recon == driven_sparse, "sparse-channel round-trip mismatch"


# ── Test 3: dense round-trip across all flush conditions ──────────────


@cocotb.test()
async def test_round_trip_no_drops_or_duplicates(dut):
    """DS-P2.48 acceptance gate 3: every driven event must parse back
    exactly once with the right ts_64b.  Stresses capacity + timer +
    channel-switch flushes in one run."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    # Plan: 32 capacity events on ch 2, then 1 sparse event on ch 5
    # (timer-flush), then a partial 7-event batch on ch 2 (will flush
    # via timer once we stop driving).
    driven = []

    base_a = 0x2_0000
    for i in range(N_MAX):
        ts = base_a + i * 3
        await push_event(dut, device_id=2, ts_64=ts, e=(i % 4 == 0))
        driven.append((2, ts, (i % 4 == 0)))

    frame_a = await consume_frame(dut, deadline_cycles=128)

    ts_sparse = 0x9_0000
    await push_event(dut, device_id=5, ts_64=ts_sparse, e=False)
    driven.append((5, ts_sparse, False))
    frame_b = await consume_frame(dut, deadline_cycles=2 * T_FLUSH)

    base_c = 0xA_0000
    for i in range(7):
        ts = base_c + i * 11
        await push_event(dut, device_id=2, ts_64=ts, e=True)
        driven.append((2, ts, True))
    frame_c = await consume_frame(dut, deadline_cycles=2 * T_FLUSH)

    frames = [frame_a, frame_b, frame_c]
    total_events = sum(_events_in_frame(f) for f in frames)
    total_bytes = sum(_frame_bytes_count(f) for f in frames)
    dut._log.info(
        "Phase C round-trip: %d events, %d frames, %d bytes, %.3f B/event",
        total_events, len(frames), total_bytes, total_bytes / total_events,
    )

    assert total_events == len(driven), (
        f"drop/dup: drove {len(driven)} events, parsed {total_events}")

    reconstructed = []
    for frame in frames:
        _, events = zsmsg.parse_edge_batch(synth_frame_bytes(frame))
        for ev in events:
            reconstructed.append((ev.channel, ev.ts_64b, ev.e))
    assert reconstructed == driven, (
        f"round-trip mismatch — len(driven)={len(driven)}, "
        f"len(reconstructed)={len(reconstructed)}")


# ── Sanctioned simulation entry point ────────────────────────────────

RTL = DEEPSKOPION_ROOT / "hw" / "rtl"


if __name__ == "__main__":
    # Same self-contained compile set as the other aggregator suites. The
    # aggregator lives in library `tich` in the project closure
    # (deepskopion_core instantiates `entity tich.edge_batcher_aggregator`),
    # so a bare top_level elaboration against the default work lib only ever
    # worked against a stale library image.
    run_simulation(
        top_level="edge_batcher_aggregator",
        module=Path(__file__).stem,
        custom_libraries={"tich": [
            f"{RTL}/datalogger/infra/edge_batcher/edge_batcher.vhd",
            f"{RTL}/datalogger/infra/edge_batcher_aggregator/edge_batcher_aggregator.vhd",
        ]},
        work_lib="tich",
    )
