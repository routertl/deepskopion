# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.359: with an edge route, arm_i2c routes SCL/SDA onto the edge inputs before its one transaction.

The I2C self-test is one transaction of about 0.2 ms, started by the register sequence. live_capture_client sent
op=stim and then op=edge_route, so the daemon enabled the edge loggers 23-29 ms after the transaction's STOP: on the
PolarFire SoC (2026-11-18) and in both Zybo DS-P2.344 captures the edge loggers saw none of its edges. With ``route``
the arm writes the stimulus bit OR the I2C edge bit, and enables edge loggers 1 (SCL) and 2 (SDA), before the first
register of the sequence. These host tests pin that order with hard-coded offsets and values.
"""
from __future__ import annotations

import socket

import msgpack
import pytest

from sw.stream import protocol as proto
from sw.stream import stimulus_armer as A
from sw.stream.data_source import RegbankDataSource, apply_stimulus
from sw.stream.regbank import FakeRegbank
from sw.stream.server import _poll_inbound

EMIO_MUX_SEL, DATALOGGER_CONFIG = 0x24, 0x20
I2C_INJ_CONTROL, STIM_I2C_CONTROL, STIM_I2C_CLK_DIV, STIM_I2C_DATA = 0x30, 0x22C, 0x230, 0x234
MUX_I2C, MUX_EDGE_I2C = 0x1, 0x10
SEQUENCE = [(I2C_INJ_CONTROL, (0x50 << 1) | 0x1), (STIM_I2C_CLK_DIV, 125),
            (STIM_I2C_CONTROL, (2 << 8) | (0x50 << 1) | 0x1), (STIM_I2C_DATA, 0x003ECDAB)]


class _Rb:
    def __init__(self):
        self.writes = []

    def write_u32(self, off, val):
        self.writes.append((int(off), int(val)))

    def read_u32(self, off):
        for o, v in reversed(self.writes):
            if o == off:
                return v
        return 0


@pytest.fixture(autouse=True)
def _legacy_mux(monkeypatch):
    monkeypatch.setenv("DS_MSS_FABRIC", "0")


def _arm(route=None):
    rb = _Rb()
    A.arm_i2c(rb, route=route) if route else A.arm_i2c(rb)
    return rb.writes


def _enabled_ids(writes):
    return [v >> 11 for o, v in writes if o == DATALOGGER_CONFIG]


def test_without_route_the_mux_is_the_i2c_stimulus_bit_and_only_the_i2c_logger_is_enabled():
    w = _arm()
    assert [v for o, v in w if o == EMIO_MUX_SEL] == [MUX_I2C]
    assert set(_enabled_ids(w)) == {4}
    assert [x for x in w if x[0] in dict(SEQUENCE)] == SEQUENCE


def test_with_route_the_mux_and_both_edge_enables_precede_the_first_register_of_the_sequence():
    w = _arm("i2c")
    assert [v for o, v in w if o == EMIO_MUX_SEL] == [MUX_I2C | MUX_EDGE_I2C]
    start = w.index(SEQUENCE[0])
    edge = [i for i, (o, v) in enumerate(w) if o == DATALOGGER_CONFIG and (v >> 11) in (1, 2)]
    assert sorted({w[i][1] >> 11 for i in edge}) == [1, 2]
    assert len(edge) == 4                       # reset + enable for edge logger 1 (SCL) and 2 (SDA)
    assert w.index((EMIO_MUX_SEL, MUX_I2C | MUX_EDGE_I2C)) < min(edge)
    assert max(edge) < start
    assert [x for x in w if x[0] in dict(SEQUENCE)] == SEQUENCE


def test_unknown_route_raises_before_any_write():
    rb = _Rb()
    with pytest.raises(ValueError):
        A.arm_i2c(rb, route="nope")
    assert rb.writes == []


@pytest.mark.parametrize("route", ["spi", "uart", "can", "stim", "pmod"])
def test_an_i2c_arm_with_another_route_is_refused_and_writes_nothing(route):
    # The shape the DS-P2.343 test covered before i2c became a routed arm: i2c given route="spi" (and every other
    # route that is not i2c) is refused before any regbank write.
    rb = _Rb()
    with pytest.raises(ValueError):
        A.arm_i2c(rb, route=route)
    assert rb.writes == []


def test_daemon_acks_a_reason_for_i2c_with_route_spi_and_writes_nothing():
    rb = FakeRegbank()
    daemon_end, consumer_end = socket.socketpair()
    try:
        consumer_end.sendall(proto.pack_frame(proto.TAG_CONTROL, proto.make_stim_control("i2c", route="spi")))
        assert _poll_inbound(daemon_end, RegbankDataSource(rb), msgpack.Unpacker(raw=False)) is False
        consumer_end.settimeout(2.0)
        tag, ack = msgpack.unpackb(consumer_end.recv(65536), raw=False)
    finally:
        daemon_end.close()
        consumer_end.close()
    assert tag == proto.TAG_CONTROL_ACK
    assert ack["ok"] is False and ack["op"] == "stim" and ack["protocol"] == "i2c" and ack["route"] == "spi"
    assert "i2c" in ack["reason"] and "spi" in ack["reason"]
    assert rb.writes == []


def test_i2c_is_a_routed_arm():
    assert "i2c" in A.ROUTED_ARMERS


def test_daemon_arms_i2c_with_its_route():
    rb = _Rb()
    assert apply_stimulus(rb, "i2c", route="i2c") is None
    assert (EMIO_MUX_SEL, MUX_I2C | MUX_EDGE_I2C) in rb.writes


def test_client_sends_one_stim_frame_carrying_the_i2c_route_and_no_edge_route_frame(monkeypatch):
    from sw.stream.testbench import live_capture_client as C

    sent = []
    daemon_end, consumer_end = socket.socketpair()

    def fake_connect(_addr, timeout=None):
        return consumer_end

    monkeypatch.setattr(C.socket, "create_connection", fake_connect)
    daemon_end.sendall(proto.pack_frame(proto.TAG_HELO, {"ver": "wave_stream_v1", "channels": []}))
    daemon_end.sendall(proto.pack_frame(proto.TAG_SYNC, {"seq": 0}))
    daemon_end.sendall(proto.pack_frame(proto.TAG_EOF, {}))
    try:
        C.capture("host", 0, 1.0, stim="i2c", edge_route="i2c")
        daemon_end.settimeout(1.0)
        unp = msgpack.Unpacker(raw=False)
        while True:
            try:
                data = daemon_end.recv(65536)
            except (socket.timeout, OSError):
                break
            if not data:
                break
            unp.feed(data)
        sent = [obj for obj in unp]
    finally:
        daemon_end.close()
    controls = [p for t, p in sent if t == proto.TAG_CONTROL]
    assert [c.get("op") for c in controls] == ["stim"]
    assert controls[0]["protocol"] == "i2c" and controls[0]["route"] == "i2c"


def test_daemon_stim_frame_with_i2c_route_is_armed_routed_and_acked():
    rb = FakeRegbank()
    daemon_end, consumer_end = socket.socketpair()
    try:
        consumer_end.sendall(proto.pack_frame(proto.TAG_CONTROL, proto.make_stim_control("i2c", route="i2c")))
        assert _poll_inbound(daemon_end, RegbankDataSource(rb), msgpack.Unpacker(raw=False)) is False
        consumer_end.settimeout(2.0)
        tag, ack = msgpack.unpackb(consumer_end.recv(65536), raw=False)
    finally:
        daemon_end.close()
        consumer_end.close()
    assert tag == proto.TAG_CONTROL_ACK
    assert ack["ok"] is True and ack["op"] == "stim" and ack["protocol"] == "i2c" and ack["route"] == "i2c"
    assert rb.read_u32(EMIO_MUX_SEL) == MUX_I2C | MUX_EDGE_I2C
