"""DS-P2.194 — EDGE_EVENTS labels the clock used for sub-us encoding."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import msgpack

from sw.stream import edge_capture
from sw.stream import protocol as proto
from sw.stream.data_source import Batch
from sw.stream.server import serve_fanout

from sw.stream.tests.test_server import _connect_with_retry, _read_one_frame
from sw.stream.tests.test_wave_stream_live_conformance import (
    _ReplayDataSource, _run_server_and_capture)


def test_live_edge_events_use_sample_clock_for_sub_us_decode():
    """The serve_one wire header must describe the encoding clock."""
    edge_ns = 1_240
    words = [edge_capture.edge_event_word(
        instance=1, timestamp_ns=edge_ns, s=0, f=0, e=0, clk_mhz=50)]
    source = _ReplayDataSource(
        [Batch(edge_words=words, edge_anchor_ns=edge_ns)],
        sample_clk_hz=50_000_000,
    )

    sock_path = Path("/tmp") / f"ds_p2_194_{os.getpid()}_serve.sock"
    _, captured, rc = _run_server_and_capture(sock_path, source)
    assert rc == [0]
    edge_frames = [p for t, p in captured if t == proto.TAG_EDGE_EVENTS]
    assert len(edge_frames) == 1
    payload = edge_frames[0]
    assert payload["clk_mhz"] == 50, (
        "EDGE_EVENTS must advertise the sample clock used to encode its words")
    decoded = edge_capture.decode_edge_word(payload["words"][0], payload["clk_mhz"])
    assert abs(decoded.timestamp_ns - edge_ns) <= 1, (
        f"RouteWave-style decode moved the edge to {decoded.timestamp_ns} ns; "
        f"expected {edge_ns} ns")


def test_fanout_edge_events_use_sample_clock_for_sub_us_decode():
    """The fan-out drainer must preserve a non-default sample clock."""
    class _OneEdgeSource:
        def __init__(self):
            self._batches = [Batch(
                edge_words=[edge_capture.edge_event_word(
                    1, 1_240, s=0, f=0, e=0, clk_mhz=50)],
                edge_anchor_ns=1_240)]

        def sample_clk_hz(self):
            return 50_000_000

        def next_batch(self):
            time.sleep(0.1)
            return self._batches.pop(0) if self._batches else None

        def read_stats(self):
            return None

    sock = Path("/tmp") / f"ds_p2_194_{os.getpid()}_fanout.sock"
    rc: list[int] = []
    th = threading.Thread(target=lambda: rc.append(
        serve_fanout(sock, _OneEdgeSource(), connect_timeout_s=0.3,
                     max_queue=16, max_connections=1, stats_period_s=0)), daemon=True)
    th.start()
    conn = _connect_with_retry(sock)
    try:
        unp = msgpack.Unpacker(raw=False)
        tag, _ = _read_one_frame(unp, conn)
        assert tag == proto.TAG_HELO
        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {}))
        while True:
            tag, payload = _read_one_frame(unp, conn)
            if tag == proto.TAG_EDGE_EVENTS:
                break
            assert tag == proto.TAG_SYNC, f"unexpected fan-out frame tag {tag}"
        assert payload["clk_mhz"] == 50, (
            "fan-out EDGE_EVENTS must advertise the source sample clock")
        decoded = edge_capture.decode_edge_word(payload["words"][0], payload["clk_mhz"])
        assert abs(decoded.timestamp_ns - 1_240) <= 1
        tag, _ = _read_one_frame(unp, conn)
        assert tag == proto.TAG_EOF
    finally:
        conn.close()
    th.join(3)
    assert rc == [0]
