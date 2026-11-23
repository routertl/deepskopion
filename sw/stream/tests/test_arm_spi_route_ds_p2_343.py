# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.343: arm_spi arms the on-chip SPI responder and, with a route, owns the whole EMIO_MUX_SEL.

The board's live_capture_client sends op=stim before op=edge_route, and arm_spi waits after each byte, so an edge
route applied after the arm switched the edge inputs only after the last byte (coverage sim with the armer's sleeps
replayed, 2026-11-17). With ``route`` the arm writes the stimulus bit OR the route bit, and enables the route's edge
loggers, before the first byte. These host tests pin that order with hard-coded offsets and values.
"""
from __future__ import annotations

import pytest

from sw.stream import protocol as proto
from sw.stream import stimulus_armer as A
from sw.stream.data_source import apply_stimulus

EMIO_MUX_SEL, DATALOGGER_CONFIG = 0x24, 0x20
SPI_INJ_CONTROL, SPI_INJ_DATAIN = 0x40, 0x44
STIM_SPI_CONTROL, STIM_SPI_CLK_DIV, STIM_SPI_DATA = 0x238, 0x23C, 0x240
MUX_SPI, MUX_EDGE_SPI = 0x4, 0x8
MOSI = [0xDE, 0xAD, 0xBE, 0xEF]
MISO = [0xA5, 0x5A, 0xC3, 0x3C]


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
def _no_sleep_legacy_mux(monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda _s: None)
    monkeypatch.setenv("DS_MSS_FABRIC", "0")


def _arm(route=None):
    rb = _Rb()
    A.arm_spi(rb, route=route) if route else A.arm_spi(rb)
    return rb.writes


def test_responder_is_reset_enabled_and_preloaded_before_each_byte():
    w = _arm()
    assert w.index((SPI_INJ_CONTROL, 0x200)) < w.index((SPI_INJ_CONTROL, 0x1)) < w.index((STIM_SPI_CONTROL, 0x1))
    burst = [(o, v) for o, v in w if o in (SPI_INJ_DATAIN, STIM_SPI_DATA)]
    expected = []
    for mosi, miso in zip(MOSI, MISO):
        expected += [(SPI_INJ_DATAIN, miso), (SPI_INJ_DATAIN, 0x00), (STIM_SPI_DATA, mosi)]
    assert burst == expected


def test_without_route_the_mux_is_the_spi_stimulus_bit_alone():
    w = _arm()
    assert [v for o, v in w if o == EMIO_MUX_SEL] == [MUX_SPI]
    assert [v >> 11 for o, v in w if o == DATALOGGER_CONFIG] == [8, 8]


def test_with_route_the_final_mux_and_edge_enables_precede_the_first_byte():
    w = _arm("spi")
    assert [v for o, v in w if o == EMIO_MUX_SEL] == [MUX_SPI | MUX_EDGE_SPI]
    first_byte = w.index((STIM_SPI_DATA, MOSI[0]))
    enables = [i for i, (o, v) in enumerate(w) if o == DATALOGGER_CONFIG and (v >> 11) in (1, 2, 3, 6)]
    assert len(enables) == 8                    # reset + enable for each of the four SPI-route edge loggers
    assert w.index((EMIO_MUX_SEL, MUX_SPI | MUX_EDGE_SPI)) < min(enables)
    assert max(enables) < first_byte


def test_unknown_route_raises_before_any_write():
    rb = _Rb()
    with pytest.raises(ValueError):
        A.arm_spi(rb, route="nope")
    assert rb.writes == []


@pytest.mark.parametrize("protocol", ["uart", "edge"])
def test_daemon_rejects_a_route_on_an_unrouted_arm_and_writes_nothing(protocol):
    rb = _Rb()
    reason = apply_stimulus(rb, protocol, route="spi")
    assert reason and protocol in reason
    assert rb.writes == []


def test_daemon_arms_spi_with_its_route():
    rb = _Rb()
    assert apply_stimulus(rb, "spi", route="spi") is None
    assert (EMIO_MUX_SEL, MUX_SPI | MUX_EDGE_SPI) in rb.writes


def test_stim_control_carries_the_route_only_when_given():
    assert "route" not in proto.make_stim_control("spi")
    assert proto.make_stim_control("spi", route="spi")["route"] == "spi"
    with pytest.raises(ValueError):
        proto.make_stim_control("spi", route="nope")


def test_daemon_stim_frame_with_route_is_armed_routed_and_acked():
    import socket

    import msgpack

    from sw.stream.data_source import RegbankDataSource
    from sw.stream.regbank import FakeRegbank
    from sw.stream.server import _poll_inbound

    rb = FakeRegbank()
    daemon_end, consumer_end = socket.socketpair()
    try:
        consumer_end.sendall(proto.pack_frame(proto.TAG_CONTROL, proto.make_stim_control("spi", route="spi")))
        assert _poll_inbound(daemon_end, RegbankDataSource(rb), msgpack.Unpacker(raw=False)) is False
        consumer_end.settimeout(2.0)
        tag, ack = msgpack.unpackb(consumer_end.recv(65536), raw=False)
    finally:
        daemon_end.close()
        consumer_end.close()
    assert tag == proto.TAG_CONTROL_ACK
    assert (ack["ok"], ack["op"], ack["protocol"], ack["route"]) == (True, "stim", "spi", "spi"), ack
    assert (EMIO_MUX_SEL, MUX_SPI | MUX_EDGE_SPI) in rb.writes
