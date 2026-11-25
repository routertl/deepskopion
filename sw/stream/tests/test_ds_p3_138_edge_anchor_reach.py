# rr unknown-llm (AI-generated)
"""DS-P3.138 — every EDGE_EVENTS word lies within reach of its frame's anchor.

An EDGE_EVENT word carries only the low 16 bits of its microsecond time. The
consumer (RouteWave decodeEdgeWordAt, and this repo's own wave_events lane)
places it by splicing those bits onto the frame's ``anchor_ns`` and taking the
65.536 ms window NEAREST the anchor. That is the right window only while the
word is within +/-32.768 ms of the anchor. Until this ticket a batch had ONE
anchor, the time of its first edge, and a DdrRingDataSource batch spans a whole
50 ms poll period, more after a stall or a backlog drain. An edge 50 ms after
the first came out 65.536 ms early: 15.536 ms BEFORE the first edge.

Every test here judges what a consumer sees: wire frames decoded the way the
consumer decodes them, or the absolute ts_ns on the events lane. Times are
literals (ROUTERTL-002). The stimulus is three edges 0, 10 and 50 ms after
T0 = 5 s: the third is past the reach of T0, the second is not.
"""
from __future__ import annotations

import itertools
import os
import threading
from pathlib import Path

import msgpack

from sw.stream import protocol as proto
from sw.stream import server, zs_msg
from sw.stream.data_source import Batch, DdrRingDataSource
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.edge_capture import edge_event_word
from sw.stream.regbank import FakeRegbank, Regs
from sw.stream.server import serve_fanout

from sw.stream.tests.test_server import _connect_with_retry, _read_one_frame
from sw.stream.tests.test_wave_events import (_fanout, _free_port, _ScriptedSource,
                                              _subscribe)
from sw.stream.tests.test_wave_stream_live_conformance import (
    _ReplayDataSource, _run_server_and_capture)

INST = 1
# 100 MHz edge clock: 10 ns per tick, so ext counts 10 ns steps.
T0_NS = 5_000_000_120            # tick 500_000_012
T1_NS = 5_010_000_230            # +10 ms
T2_NS = 5_050_000_340            # +50 ms — past the 32.768 ms reach of T0
TRUTH_NS = [T0_NS, T1_NS, T2_NS]

W0 = edge_event_word(INST, T0_NS, s=0, f=0, e=1, clk_mhz=100)
W1 = edge_event_word(INST, T1_NS, s=0, f=0, e=0, clk_mhz=100)
W2 = edge_event_word(INST, T2_NS, s=0, f=0, e=1, clk_mhz=100)


def _consumer_place_ns(anchor_ns: int, word: int, clk_mhz: int = 100) -> int:
    """The consumer's rule, written out independently of the producer: splice
    ts_us onto the anchor's high bits, take the window nearest the anchor."""
    ts_us = (word >> 8) & 0xFFFF
    ext = word & 0xFF
    anchor_us = anchor_ns // 1000
    base = anchor_us - anchor_us % 65536
    us = min((base - 65536 + ts_us, base + ts_us, base + 65536 + ts_us),
             key=lambda c: abs(c - anchor_us))
    return us * 1000 + ext * (1000 // clk_mhz)


def _placed(frames) -> list:
    """Absolute ns of every word of every EDGE_EVENTS payload, consumer-decoded."""
    return [_consumer_place_ns(p["anchor_ns"], w, p["clk_mhz"])
            for p in frames for w in p["words"]]


def _split_batch() -> Batch:
    """The batch the producer builds for this stimulus, written out literally so
    the server tests do not depend on the producer's cut."""
    batch = Batch(edge_words=[W0, W1, W2], edge_anchor_ns=T0_NS)
    batch.edge_frames = [(T0_NS, [W0, W1]), (T2_NS, [W2])]
    return batch


def _wire(batch) -> list:
    """The EDGE_EVENTS payloads a server sends for ``batch``: one per anchored
    frame. A batch that carries no anchored frames goes out as ONE frame on its
    first-edge anchor, which is what every send site did before DS-P3.138."""
    frames = getattr(batch, "wire_edge_frames", None)
    frames = frames() if frames else [(batch.edge_anchor_ns, batch.edge_words)]
    return [proto.make_edge_events(seq, words, anchor_ns=anchor)
            for seq, (anchor, words) in enumerate(frames)]


# ---------------------------------------------------------------------------
# Producer: a ring drain spanning a poll period is cut into anchored frames
# ---------------------------------------------------------------------------

class _CommitMirrorRegbank(FakeRegbank):
    def read_u32(self, offset: int) -> int:
        if offset == Regs.RING_COMMIT_PTR:
            return super().read_u32(Regs.RING_WRITE_PTR) * 4
        return super().read_u32(offset)


def _edge_batch_frame(tick: int, e: bool) -> bytes:
    ev = zs_msg.EdgeEvent(ts_64b=tick, channel=INST, s=False, f=False, e=e)
    return zs_msg.pack_edge_batch(datalogger_id=INST, channel=INST, events=[ev])


def test_ring_drain_spanning_a_poll_period_reaches_the_consumer_in_place():
    """Three EDGE_BATCH frames in one 50 ms poll, through the real
    DdrRingDataSource decode, then put on the wire the way serve_one does."""
    frames = b"".join([_edge_batch_frame(500_000_012, True),
                       _edge_batch_frame(501_000_023, False),
                       _edge_batch_frame(505_000_034, True)])
    regbank, ddr = _CommitMirrorRegbank(), FakeDdrMmap(1024)
    times = iter(itertools.chain([0.0], itertools.count(start=0.05, step=0.05)))
    src = DdrRingDataSource(regbank=regbank, ddr=ddr, sample_clk_hz=100_000_000,
                            poll_period_s=0.05, duration_s=0.0,
                            write_back_read_ptr=True, clock=lambda: next(times))
    src.next_batch()
    ddr.write_bytes(0, frames)
    regbank.set(Regs.RING_WRITE_PTR, (len(frames) + 3) // 4)
    wire = _wire(src.next_batch())
    assert _placed(wire) == TRUTH_NS, (
        "a consumer placing these EDGE_EVENTS words from their frame anchors "
        f"got {_placed(wire)}, the edges were at {TRUTH_NS}: an edge more than "
        "32.768 ms from its anchor lands a whole 65.536 ms window off")
    assert [p["anchor_ns"] for p in wire] == [T0_NS, T2_NS]


def test_rle_drain_spanning_a_wrap_reaches_the_consumer_in_place():
    """The Stream-RLE path (DS-P2.16): the capture of test_edge_rle_ds_p2_16,
    whose quiet IDLE stretch and re-anchor put its last two edges 167.8 ms and
    335.2 ms after its first, decoded by the real DdrRingDataSource."""
    from sw.stream.tests.test_edge_rle_ds_p2_16 import FRAME_TICKS, WORDS, _drain
    from sw.stream.tests.test_edge_rle_ds_p2_16 import _frame as _rle_frame
    wire = _wire(_drain(b"".join(_rle_frame(w, t) for w, t in zip(WORDS, FRAME_TICKS))))
    want = [214_748_367_360, 214_748_367_460, 214_748_367_660, 214_748_367_960,
            214_748_377_960, 214_916_150_150, 215_083_914_240]
    assert _placed(wire) == want, (
        f"a consumer placed the RLE edges at {_placed(wire)}, they were at {want}")
    assert [p["anchor_ns"] for p in wire] == [214_748_367_360, 214_916_150_150,
                                              215_083_914_240]


# ---------------------------------------------------------------------------
# Server: every send site puts out one frame per anchor
# ---------------------------------------------------------------------------

def test_serve_one_sends_one_edge_events_frame_per_anchor():
    sock = Path("/tmp") / f"ds_p3_138_{os.getpid()}_serve.sock"
    _, captured, rc = _run_server_and_capture(sock, _ReplayDataSource([_split_batch()]))
    assert rc == [0]
    edge = [p for t, p in captured if t == proto.TAG_EDGE_EVENTS]
    assert [p["anchor_ns"] for p in edge] == [T0_NS, T2_NS], (
        "serve_one must send each of the batch's anchored edge frames")
    assert _placed(edge) == TRUTH_NS
    assert edge[1]["seq"] > edge[0]["seq"]


def test_fanout_wave_lane_sends_one_edge_events_frame_per_anchor():
    class _OneBatch:
        def __init__(self):
            self._batches = [_split_batch()]

        def sample_clk_hz(self):
            return 100_000_000

        def next_batch(self):
            threading.Event().wait(0.1)
            return self._batches.pop(0) if self._batches else None

        def read_stats(self):
            return None

    sock = Path("/tmp") / f"ds_p3_138_{os.getpid()}_fanout.sock"
    rc: list = []
    th = threading.Thread(target=lambda: rc.append(
        serve_fanout(sock, _OneBatch(), connect_timeout_s=0.3, max_queue=16,
                     max_connections=1, stats_period_s=0)), daemon=True)
    th.start()
    conn = _connect_with_retry(sock)
    edge = []
    try:
        unp = msgpack.Unpacker(raw=False)
        tag, _ = _read_one_frame(unp, conn)
        assert tag == proto.TAG_HELO
        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {}))
        while True:
            tag, payload = _read_one_frame(unp, conn)
            if tag == proto.TAG_EOF:
                break
            if tag == proto.TAG_EDGE_EVENTS:
                edge.append(payload)
    finally:
        conn.close()
    th.join(3)
    assert rc == [0]
    assert [p["anchor_ns"] for p in edge] == [T0_NS, T2_NS], (
        "the fan-out wave_stream lane must send each anchored edge frame")
    assert _placed(edge) == TRUTH_NS


def test_fanout_events_lane_places_every_edge_from_its_own_anchor():
    gate = threading.Event()
    _, _, events_port = _fanout(_ScriptedSource([_split_batch()], gate=gate))
    events = _subscribe(events_port, {"subscribe": {"kinds": ["edge"]}})
    gate.set()
    placed = [l["ts_ns"] for l in events.until_closed() if l["kind"] == "edge"]
    assert placed == TRUTH_NS, (
        f"fan-out events lane placed the edges at {placed}, they were at {TRUTH_NS}")


def test_serve_one_events_places_every_edge_from_its_own_anchor():
    port = _free_port()
    rc: list = []
    th = threading.Thread(target=lambda: rc.append(server.serve_one_events(
        ("127.0.0.1", port), _ReplayDataSource([_split_batch()]),
        connect_timeout_s=3.0)), daemon=True)
    th.start()
    events = _subscribe(port, {"subscribe": {"kinds": ["edge"]}})
    placed = [l["ts_ns"] for l in events.until_closed() if l["kind"] == "edge"]
    th.join(3)
    assert placed == TRUTH_NS, (
        f"serve_one_events placed the edges at {placed}, they were at {TRUTH_NS}")
    assert rc == [0]


# ---------------------------------------------------------------------------
# The bound itself, and the contract that states it
# ---------------------------------------------------------------------------

def test_reach_is_exclusive_at_half_a_window():
    """At exactly 32 768 µs two windows tie, so the word must open a new frame;
    one µs less stays in the current frame."""
    batch = Batch()
    batch.add_edge(W0, 5_000_000_120)
    batch.add_edge(W1, 5_032_767_999)      # 32 767 whole µs after: in reach
    batch.add_edge(W2, 5_032_768_000)      # 32 768 whole µs after: a tie
    assert batch.wire_edge_frames() == [(5_000_000_120, [W0, W1]),
                                        (5_032_768_000, [W2])], (
        "an edge exactly 32 768 µs from the anchor ties two windows and must "
        "open its own frame")


def test_producer_reach_is_the_contract_reach():
    from sw.stream.wave_stream_guard import _C
    assert _C.contract_json().get("edge_anchor_reach_us") == 32768, (
        "contract.json must state the EDGE_EVENTS anchor reach")
    assert proto.EDGE_ANCHOR_REACH_US == _C.EDGE_ANCHOR_REACH_US == 32768
