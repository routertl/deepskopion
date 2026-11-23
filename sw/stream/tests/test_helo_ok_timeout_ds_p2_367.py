# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""The stream server waits a named, overridable time for the consumer's HELO_OK.

wave_stream_v1 sets no HELO_OK deadline. The server gave a consumer 2.0 s after HELO; a RouteWave Debug build under
load answers in 1.1-2.5 s, so ds-python-suite saw the connection dropped ("HELO_OK timeout; aborting") and RouteWave
attached with 0 annotations. The default is now DEFAULT_HELO_OK_TIMEOUT_S, overridden by DS_HELO_OK_TIMEOUT_S, and
the timeout message names the value and the peer. A consumer that never answers is still dropped.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

import msgpack
import pytest

from sw.stream import protocol as proto
from sw.stream import server as S
from sw.stream.data_source import StubDataSource


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _connect(host: str, port: int, attempts: int = 40) -> socket.socket:
    for _ in range(attempts):
        try:
            return socket.create_connection((host, port), timeout=2.0)
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)
    raise RuntimeError(f"could not connect to {host}:{port}")


def _frame(unp: msgpack.Unpacker, conn: socket.socket, deadline_s: float = 3.0):
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        for obj in unp:
            return obj[0], obj[1]
        conn.settimeout(max(0.01, end - time.monotonic()))
        try:
            data = conn.recv(8192)
        except socket.timeout:
            continue
        if not data:
            return None, None              # the server closed the connection
        unp.feed(data)
    raise TimeoutError("no frame")


def _serve(host: str, port: int, rc: list, duration_s: float = 0.3) -> threading.Thread:
    # The stub's duration runs on wall-clock time from its start, so a consumer that answers late needs a longer one.
    source = StubDataSource(sample_clk_hz=10_000, samples_per_batch=1000, emit_annotations=True, duration_s=duration_s)
    t = threading.Thread(target=lambda: rc.append(S.serve_one((host, port), source, connect_timeout_s=5.0)),
                         daemon=True)
    t.start()
    return t


def test_the_default_is_named_and_longer_than_a_loaded_debug_consumer_needs(monkeypatch):
    monkeypatch.delenv(S.HELO_OK_TIMEOUT_ENV, raising=False)
    assert S.HELO_OK_TIMEOUT_ENV == "DS_HELO_OK_TIMEOUT_S"
    assert S.DEFAULT_HELO_OK_TIMEOUT_S == 10.0
    assert S.helo_ok_timeout_s() == 10.0
    monkeypatch.setenv(S.HELO_OK_TIMEOUT_ENV, "4.5")
    assert S.helo_ok_timeout_s() == 4.5


@pytest.mark.parametrize("bad", ["0", "-1", "soon"])
def test_an_unusable_override_falls_back_to_the_default(monkeypatch, caplog, bad):
    monkeypatch.setenv(S.HELO_OK_TIMEOUT_ENV, bad)
    with caplog.at_level(logging.WARNING, logger=S.logger.name):
        assert S.helo_ok_timeout_s() == S.DEFAULT_HELO_OK_TIMEOUT_S
    assert S.HELO_OK_TIMEOUT_ENV in caplog.text and repr(bad) in caplog.text


def test_a_consumer_that_answers_helo_after_3_s_is_served(monkeypatch):
    """Red before the change: the 2.0 s wait dropped this consumer."""
    monkeypatch.delenv("DS_HELO_OK_TIMEOUT_S", raising=False)
    host, rc = "127.0.0.1", []
    port = _free_port(host)
    t = _serve(host, port, rc, duration_s=4.0)
    conn = _connect(host, port)
    try:
        unp = msgpack.Unpacker(raw=False)
        tag, _ = _frame(unp, conn)
        assert tag == proto.TAG_HELO
        time.sleep(3.0)
        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"consumer": "slow"}))
        tags = []
        while True:
            tag, _ = _frame(unp, conn)
            if tag is None:
                break
            tags.append(tag)
            if tag == proto.TAG_EOF:
                break
    finally:
        conn.close()
    t.join(timeout=5.0)
    assert proto.TAG_SAMPLES in tags and proto.TAG_EOF in tags, f"slow consumer was not served; frames {tags}"
    assert rc == [0]


def test_a_silent_consumer_is_dropped_and_the_message_names_the_wait_and_the_peer(monkeypatch, caplog):
    monkeypatch.setenv(S.HELO_OK_TIMEOUT_ENV, "0.5")
    host, rc = "127.0.0.1", []
    port = _free_port(host)
    with caplog.at_level(logging.ERROR, logger=S.logger.name):
        t = _serve(host, port, rc)
        conn = _connect(host, port)
        try:
            unp = msgpack.Unpacker(raw=False)
            tag, _ = _frame(unp, conn)
            assert tag == proto.TAG_HELO
            t0 = time.monotonic()
            tag, _ = _frame(unp, conn, deadline_s=5.0)   # never answer HELO
            waited = time.monotonic() - t0
        finally:
            conn.close()
        t.join(timeout=5.0)
    assert tag is None, "the server kept a consumer that never sent HELO_OK"
    assert 0.4 <= waited < 3.0, f"dropped after {waited:.2f} s, override was 0.5 s"
    assert rc == [1]
    msg =[r.getMessage() for r in caplog.records if "HELO_OK" in r.getMessage()]
    assert msg and "0.5 s" in msg[-1] and "127.0.0.1:" in msg[-1] and S.HELO_OK_TIMEOUT_ENV in msg[-1], msg
