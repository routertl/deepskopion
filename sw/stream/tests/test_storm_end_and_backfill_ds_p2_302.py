"""DS-P2.302: an error storm's end on an idle bus, and what a late joiner receives of it.

1. RingDataSource.next_batch() returned before the storm flush on a tick that drained no ring data, so on an idle
   bus a storm stayed ongoing until unrelated traffic arrived (run3: an "ended" record about 100 s after the last
   error). An empty tick now ends a run after ErrorStormAggregator.IDLE_END_S of silence (20 ticks at the 50 ms poll).
   A data batch with no growth still ends a run at once, as before.
2. Every batch's error_run snapshot of a growing storm was kept in the backfill ring, so a joiner replayed all of
   them, plus the keyframe's last annotation per channel. The ring now keeps each ANNOTATIONS frame without its
   error_run records (same seq; an empty list when nothing else was in it), and the keyframe carries each run once,
   in its latest state. The live stream is unchanged byte for byte.
"""
from __future__ import annotations

import threading

import msgpack

from sw.stream import protocol as proto
from sw.stream.data_source import ERROR_RUN_KIND, Annotation, Batch, ErrorStormAggregator
from sw.stream.server import _annotations_payload, _drainer_loop, _Hub, _Subscriber
from sw.stream.tests.test_ddr_ring_data_source import _clean_uart_byte, _stop_err_frame, _storm_harness

IDLE_END_TICKS = 20   # ErrorStormAggregator.IDLE_END_S 1.0 / the harness poll period 0.05


def _runs(batch):
    return [a for a in batch.annotations if a.kind == ERROR_RUN_KIND]


def test_the_idle_end_is_twenty_ticks_at_the_default_poll():
    src, _ = _storm_harness()
    assert src._storm._idle_end_ticks == IDLE_END_TICKS


def test_a_storm_on_an_idle_bus_ends_after_twenty_empty_ticks():
    _, drain = _storm_harness()
    first = drain(b"".join(_stop_err_frame(ts=1000 + i * 10) for i in range(50)))
    assert [r.fields["ongoing"] for r in _runs(first)] == [True]
    ended_at = []
    for tick in range(1, 41):
        for r in _runs(drain(b"")):
            ended_at.append((tick, r.fields["ongoing"], r.fields["count"]))
    assert ended_at == [(20, False, 50)]


def test_a_data_batch_without_growth_still_ends_a_storm_at_once():
    _, drain = _storm_harness()
    drain(b"".join(_stop_err_frame(ts=1000 + i * 10) for i in range(50)))
    assert [(r.fields["ongoing"], r.fields["count"]) for r in _runs(drain(_clean_uart_byte(ts=9000)))] == [(False, 50)]


def test_a_slow_storm_on_an_idle_bus_stays_one_run_and_ends_once():
    """Errors every 3 ticks, 20 of them, nothing else on the bus (MC condition (d), 2026-11-14). Measured before
    the change: 8 individual, 1 run, never ended in 60 idle ticks. A one-tick idle end split it into 20 individual
    errors and no run; the 1 s end keeps one run."""
    _, drain = _storm_harness()
    individual, records, tick, last_error_tick = 0, [], 0, 0
    for i in range(20):
        for frames in (_stop_err_frame(ts=1000 + i * 150), b"", b""):
            tick += 1
            if frames:
                last_error_tick = tick
            b = drain(frames)
            individual += len([a for a in b.annotations if a.kind == "uart_stop_error"])
            records += [(tick, r) for r in _runs(b)]
    for _ in range(60):
        tick += 1
        records += [(tick, r) for r in _runs(drain(b""))]
    assert individual == ErrorStormAggregator.STORM_THRESHOLD
    assert len({r.ts_start_ns for _, r in records}) == 1
    ended = [(t, r.fields["count"]) for t, r in records if r.fields["ongoing"] is False]
    assert ended == [(last_error_tick + IDLE_END_TICKS, 20)]


class _Scripted:
    """A DataSource that yields the given batches, then ends."""

    def __init__(self, batches):
        self._batches = list(batches)

    def sample_clk_hz(self):
        return 10_000

    def next_batch(self):
        return self._batches.pop(0) if self._batches else None


def _run(i, count, ongoing):
    return Annotation(channel_id=33, ts_start_ns=1000, ts_end_ns=1000 + count, kind=ERROR_RUN_KIND,
                      text=f"! X storm x{count}", is_error=True,
                      fields={"run_of": "uart_stop_error", "count": count, "ongoing": ongoing})


def _plain(i):
    return Annotation(channel_id=34, ts_start_ns=5000 + i, ts_end_ns=5000 + i, kind="t", text=f"a{i}")


def _storm_batches():
    """Five batches of a growing storm (the first and third with another annotation), its end, then quiet."""
    out = []
    for i in range(5):
        anns = [_run(i, 10 + i * 10, True)] + ([_plain(i)] if i in (0, 2) else [])
        out.append(Batch(annotations=anns))
    out.append(Batch(annotations=[_run(5, 50, False)]))
    out.append(Batch(annotations=[_plain(9)]))
    return out


def _drain_all(hub, batches):
    live = _Subscriber(None, 1000, "live")
    hub.register(live)
    _drainer_loop(_Scripted(batches), hub, threading.Event())
    frames = []
    while not live.q.empty():
        frames.append(live.q.get_nowait())
    return frames


def _decode(frame):
    return msgpack.unpackb(frame, raw=False)


def test_a_joiner_gets_each_error_run_once_in_its_latest_state():
    hub = _Hub(backfill_frames=64)
    _drain_all(hub, _storm_batches())
    joiner = _Subscriber(None, 10, "late")
    backfill = hub.register(joiner)
    from_keyframe = [a for a in joiner.keyframe_anns if a.kind == ERROR_RUN_KIND]
    from_backfill = [a for f in backfill for tag, p in [_decode(f)] if tag == proto.TAG_ANNOTATIONS
                     for a in p["annotations"] if a["kind"] == ERROR_RUN_KIND]
    assert len(from_keyframe) + len(from_backfill) == 1
    assert [(a.fields["ongoing"], a.fields["count"]) for a in from_keyframe] == [(False, 50)]


def test_the_live_stream_is_byte_identical():
    """Each live ANNOTATIONS frame is exactly the bytes of the full batch at its seq, error_run records included."""
    batches = _storm_batches()
    live = _drain_all(_Hub(backfill_frames=64), [Batch(annotations=list(b.annotations)) for b in batches])
    expected = [proto.pack_frame(proto.TAG_ANNOTATIONS, _annotations_payload(seq, b.annotations))
                for seq, b in enumerate(batches)]
    assert live[:len(expected)] == expected
    assert _decode(live[len(expected)])[0] == proto.TAG_EOF


def test_the_ring_keeps_every_seq_and_an_empty_list_for_a_run_only_frame():
    hub = _Hub(backfill_frames=64)
    _drain_all(hub, _storm_batches())
    backfill = [_decode(f) for f in hub.register(_Subscriber(None, 10, "late"))]
    anns = [(p["seq"], [a["kind"] for a in p["annotations"]]) for tag, p in backfill if tag == proto.TAG_ANNOTATIONS]
    assert [s for s, _ in anns] == list(range(7))
    assert anns == [(0, ["t"]), (1, []), (2, ["t"]), (3, []), (4, []), (5, []), (6, ["t"])]


def test_the_keyframe_keeps_at_most_256_ended_runs_and_never_drops_an_ongoing_one():
    hub = _Hub()
    ongoing = [Annotation(channel_id=33, ts_start_ns=10 + k, ts_end_ns=10 + k, kind=ERROR_RUN_KIND, text="r",
                          is_error=True, fields={"run_of": "k", "count": 9, "ongoing": True}) for k in range(3)]
    hub.note_state([], ongoing)
    ended = [Annotation(channel_id=33, ts_start_ns=1000 + k, ts_end_ns=1000 + k, kind=ERROR_RUN_KIND, text="r",
                        is_error=True, fields={"run_of": "k", "count": 9, "ongoing": False}) for k in range(257)]
    hub.note_state([], ended)
    sub = _Subscriber(None, 10, "late")
    hub.register(sub)
    kept = [a for a in sub.keyframe_anns if a.kind == ERROR_RUN_KIND]
    assert len(kept) == 256
    assert {a.ts_start_ns for a in ongoing} <= {a.ts_start_ns for a in kept}
    assert 1000 not in {a.ts_start_ns for a in kept} and 1001 not in {a.ts_start_ns for a in kept}

    many = [Annotation(channel_id=33, ts_start_ns=50_000 + k, ts_end_ns=50_000 + k, kind=ERROR_RUN_KIND, text="r",
                       is_error=True, fields={"run_of": "k", "count": 9, "ongoing": True}) for k in range(300)]
    hub2 = _Hub()
    hub2.note_state([], many)
    sub2 = _Subscriber(None, 10, "late2")
    hub2.register(sub2)
    assert len([a for a in sub2.keyframe_anns if a.kind == ERROR_RUN_KIND]) == 300
