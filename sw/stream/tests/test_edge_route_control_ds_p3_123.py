# rr unknown-llm (AI-generated)
"""DS-P3.123: a consumer routes a signal set onto the Edge Detector inputs.

The 2026-06-22 live I2C edge demo needed a host shell: ``devmem`` EMIO_MUX_SEL
(0x24) = 0x11 to put I2C SCL/SDA on EDGE0/EDGE1, and DATALOGGER_CONFIG writes
to enable edge devices 1 and 2. A consumer could enable data loggers over
CONTROL but had no path to the routing bits. CONTROL ``op="edge_route"`` is
that path. Expected register values are literals from the RTL decode
(hw/rtl/deepskopion_core.vhd: bits [2:0] source, [7:3] edge routing,
[11:8] MSS fabric), not computed from the module under test.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import msgpack
import pytest

from sw.stream import protocol as proto
from sw.stream import server
from sw.stream.data_source import DdrRingDataSource, StubDataSource
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.edge_route import apply_edge_route, route_word
from sw.stream.regbank import FakeRegbank, Regs

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "contracts" / "wave_stream_v1"))
import wave_stream_contract as contract  # noqa: E402

MUX = 0x24
CFG = 0x20


def _source(mux: int = 0):
    rb = FakeRegbank()
    rb.set(MUX, mux)
    src = DdrRingDataSource(regbank=rb, ddr=FakeDdrMmap(1 << 16),
                            sample_clk_hz=100_000_000, clock=lambda: 0.0)
    return src, rb


def _control(source, payload) -> dict:
    """Send one CONTROL through the server's apply path; return the ACK payload."""
    a, b = socket.socketpair()
    try:
        server._apply_control(payload, source, b)
        a.settimeout(2.0)
        unp = msgpack.Unpacker(raw=False)
        unp.feed(a.recv(65536))
        tag, ack = next(iter(unp))
        assert tag == proto.TAG_CONTROL_ACK
        return ack
    finally:
        a.close()
        b.close()


def test_regbank_offset_is_the_rtl_slot():
    assert Regs.EMIO_MUX_SEL == MUX


@pytest.mark.parametrize("before,route,after", [
    (0x000, "i2c", 0x010),
    (0x001, "i2c", 0x011),     # stim i2c source kept: the demo's 0x11
    (0x0F9, "spi", 0x009),     # other edge bits cleared, source bit 0 kept
    (0x302, "uart", 0x322),    # MSS bits [11:8] kept
    (0x0F8, "pmod", 0x000),    # no edge bit: PMOD jb[5:2]
    (0x000, "can", 0x040),
    (0x004, "stim", 0x084),
])
def test_route_rewrites_only_the_edge_bits(before, route, after):
    assert route_word(before, route) == after


def test_stim_then_edge_route_gives_the_demo_register_value():
    src, rb = _source()
    ack = _control(src, proto.make_stim_control("i2c"))
    assert ack["ok"] is True and rb.read_u32(MUX) == 0x001
    ack = _control(src, proto.make_edge_route_control("i2c"))
    assert ack["ok"] is True, ack
    assert rb.read_u32(MUX) == 0x011
    assert ack["op"] == "edge_route" and ack["route"] == "i2c"
    assert ack["emio_mux_sel"] == 0x011
    names = {c["id"]: c["name"] for c in ack["edge_channels"]}
    assert names == {1: "i2c_scl", 2: "i2c_sda", 3: "pmod_jb4", 6: "pmod_jb5"}


def test_the_route_enables_the_edge_data_loggers_it_feeds():
    src, rb = _source()
    rb.writes.clear()
    reject, applied = apply_edge_route(rb, "i2c")
    assert reject is None and applied == 0x010
    cfg = [v for off, v in rb.writes if off == CFG]
    # reset (enable=0) then enable (enable=1) per device, device_id in [15:11]
    assert cfg == [1 << 11, (1 << 11) | 1, 2 << 11, (2 << 11) | 1]
    rb.writes.clear()
    apply_edge_route(rb, "spi")
    enabled = sorted(v >> 11 for off, v in rb.writes if off == CFG and v & 1)
    assert enabled == [1, 2, 3, 6]


def test_an_unknown_route_is_refused_and_writes_nothing():
    src, rb = _source(0x011)
    rb.writes.clear()
    ack = _control(src, {"op": "edge_route", "route": "jtag", "datalogger_id": 1, "cfg_word": 0})
    assert ack["ok"] is False and "unknown edge route" in ack["reason"]
    assert "edge_channels" not in ack
    assert rb.writes == [] and rb.read_u32(MUX) == 0x011


def test_a_source_without_a_regbank_refuses():
    ack = _control(StubDataSource(sample_clk_hz=100_000_000),
                   proto.make_edge_route_control("i2c"))
    assert ack["ok"] is False and "unsupported" in ack["reason"]


def test_a_readback_mismatch_is_reported():
    class _Stuck(FakeRegbank):
        def write_u32(self, offset, value):
            super().write_u32(offset, 0 if offset == MUX else value)
    rb = _Stuck()
    reject, applied = apply_edge_route(rb, "i2c")
    assert reject == "EMIO_MUX_SEL reads 0x00000000 after writing 0x00000010"
    assert applied == 0


def test_the_builder_refuses_an_unknown_route():
    with pytest.raises(ValueError, match="no edge route"):
        proto.make_edge_route_control("jtag")


def test_the_control_and_its_ack_pass_the_wire_contract():
    src, _ = _source(0x001)
    control = proto.make_edge_route_control("i2c")
    ack = _control(src, control)
    helo = {"ver": "wave_stream_v1", "producer": "t", "sample_clk_hz": 100_000_000,
            "channels": [], "channel_total_bits": 0}
    frames = [("p2c", proto.TAG_HELO, helo), ("c2p", proto.TAG_HELO_OK, {}),
              ("c2p", proto.TAG_CONTROL, control), ("p2c", proto.TAG_CONTROL_ACK, ack)]
    result = contract.check_session(frames)
    assert result.accepted, [str(f) for f in result.failures]
