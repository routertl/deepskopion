"""DS-P3.124: the HELO names the edge lanes at their EDGE_EVENTS device_id.

RouteWave labels an Edge Scope lane with the name of the HELO channel whose id
EQUALS the lane's EDGE_EVENTS device_id (logic-trace edge_naming.hpp,
LT-P3.257). The daemon declared nothing at those ids except the level_pin
sample lanes (ids 0..7), so the I2C lanes of the 2026-06-22 injector demo
rendered as edge_1 / edge_2.

The fix has four parts, and each test below guards one of them:

* the server puts the source's edge lanes into the HELO it sends;
* the fixed channel table keeps out of ids 0..31, the 5-bit device_id space,
  because HELO channel ids must be unique (wave_stream_v1);
* the DDR source names the lanes from EMIO_MUX_SEL as it reads at HELO time;
* the names follow the RTL mux priority (deepskopion_core.vhd);
* the HELO the daemon sends, followed by edges of every device_id, passes the
  wave_stream_v1 2.8.0 edge-lane id rule (contract ``edge_lane_ids``).

Expected names are literals, taken from the RTL mux, not computed from the
table under test.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import msgpack

from sw.stream import channels as ch
from sw.stream import protocol as proto
from sw.stream import server
from sw.stream.data_source import DdrRingDataSource
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.regbank import FakeRegbank, Regs

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "contracts" / "wave_stream_v1"))
import wave_stream_contract as contract  # noqa: E402

# EMIO_MUX_SEL bits, from the deepskopion_core.vhd decode.
_SPI = 1 << 3
_I2C = 1 << 4
_UART = 1 << 5
_CAN = 1 << 6


def _ddr_source(emio_mux_sel: int, regbank=None) -> DdrRingDataSource:
    rb = regbank if regbank is not None else FakeRegbank()
    rb.set(Regs.EMIO_MUX_SEL, emio_mux_sel)
    return DdrRingDataSource(regbank=rb, ddr=FakeDdrMmap(1 << 16),
                             sample_clk_hz=100_000_000, clock=lambda: 0.0)


def _edge_lanes(source) -> list:
    """The source's edge lanes, looked up the way server._helo_channels does:
    a source without the hook declares none."""
    hook = getattr(source, "edge_channels", None)
    return list(hook() or []) if hook is not None else []


def _helo_on_the_wire(source) -> dict:
    """The HELO payload the server sends to a consumer, read off the socket."""
    a, b = socket.socketpair()
    try:
        a.sendall(proto.pack_frame(proto.TAG_HELO_OK, {}))
        assert server._handshake(b, source, timeout_s=2.0) is not None
        a.settimeout(2.0)
        unp = msgpack.Unpacker(raw=False)
        while True:
            unp.feed(a.recv(65536))
            for obj in unp:
                if obj[0] == proto.TAG_HELO:
                    return obj[1]
    finally:
        a.close()
        b.close()


def test_helo_sent_to_consumer_names_i2c_edge_lanes():
    """I2C routed onto the edge inputs (EMIO_MUX_SEL bit 4, the injector demo):
    the HELO a consumer receives declares id 1 = i2c_scl, id 2 = i2c_sda."""
    helo = _helo_on_the_wire(_ddr_source(_I2C))
    # The LAST entry at each id is the one a consumer keeps when it maps
    # id -> name. Whether the id is also unique is the next test's concern.
    declared = {d: [c["name"] for c in helo["channels"] if c["id"] == d]
                for d in (1, 2)}
    assert {d: n[-1:] for d, n in declared.items()} == {1: ["i2c_scl"], 2: ["i2c_sda"]}, \
        f"EDGE_LANE_UNNAMED: HELO ids 1/2 declare {declared}"


def test_helo_keeps_fixed_lanes_out_of_edge_device_id_space():
    """No fixed lane may sit at 0..31: a HELO channel id must be unique, so a
    fixed lane there blocks the edge lane of that device_id."""

    class _NoEdgeSource:
        def sample_clk_hz(self):
            return 100_000_000

    helo = _helo_on_the_wire(_NoEdgeSource())
    squatters = [(c["id"], c["name"]) for c in helo["channels"] if c["id"] < 32]
    assert squatters == [], f"EDGE_ID_SPACE_OCCUPIED: {squatters}"


def test_ddr_source_names_edge_lanes_from_live_mux():
    """The names follow EMIO_MUX_SEL as it reads, not a fixed guess: with the
    full SPI set routed (bit 3) the four lanes are the four SPI lines."""
    rb = FakeRegbank()
    src = _ddr_source(_SPI, rb)
    names = {c["id"]: c["name"] for c in _edge_lanes(src)}
    assert names == {1: "spi_sclk", 2: "spi_mosi", 3: "spi_miso", 6: "spi_cs"}, \
        f"EDGE_NAMES_NOT_FROM_MUX: {names}"
    rb.set(Regs.EMIO_MUX_SEL, 0)
    names = {c["id"]: c["name"] for c in _edge_lanes(src)}
    assert names == {1: "pmod_jb2", 2: "pmod_jb3", 3: "pmod_jb4", 6: "pmod_jb5"}, \
        f"EDGE_NAMES_NOT_FROM_MUX: {names}"


def test_edge_lane_names_follow_rtl_mux_priority():
    """With several bits set, the RTL's priority wins: CAN > UART > I2C > SPI
    on EDGE0, CAN > I2C > SPI on EDGE1."""
    cases = {
        _CAN | _UART | _I2C | _SPI: {1: "can_rx", 2: "can_tx", 3: "spi_miso", 6: "spi_cs"},
        _UART | _I2C: {1: "uart_tx", 2: "i2c_sda", 3: "pmod_jb4", 6: "pmod_jb5"},
        _I2C | _SPI: {1: "i2c_scl", 2: "i2c_sda", 3: "spi_miso", 6: "spi_cs"},
    }
    got = {mux: {c["id"]: c["name"] for c in (ch.edge_channels(mux) or [])}
           for mux in cases}
    assert got == cases, f"EDGE_MUX_PRIORITY: {got}"


def test_unreadable_mux_still_sends_helo_without_edge_names():
    """A failed EMIO_MUX_SEL read must not cost the consumer its HELO. No edge
    lane is declared then, so RouteWave falls back to edge_<id>."""

    class _MuxReadFails(FakeRegbank):
        def read_u32(self, offset):
            if offset == Regs.EMIO_MUX_SEL:
                raise OSError("bus error")
            return super().read_u32(offset)

    src = _ddr_source(_I2C, _MuxReadFails())
    try:
        lanes = _edge_lanes(src)
    except OSError as exc:
        lanes = f"raised {exc!r}"
    assert lanes == [], f"EDGE_MUX_READ_UNGUARDED: {lanes}"


def test_daemon_helo_with_edges_passes_the_contract_edge_lane_rule():
    """The HELO a consumer receives, then one EDGE_EVENTS frame carrying a word
    of each Edge Detector device_id (1, 2, 3, 6), is ACCEPTED by the
    wave_stream_v1 checker. Since 2.8.0 the checker rejects a width > 0 channel
    at an id the session's edge words carry, which is what the level_pin lanes
    at ids 0..7 were before this ticket."""
    helo = _helo_on_the_wire(_ddr_source(_I2C))
    words = [(dev << 27) | (100 << 8) for dev in (1, 2, 3, 6)]
    stream = (proto.pack_frame(proto.TAG_HELO, helo)
              + proto.pack_frame(proto.TAG_EDGE_EVENTS,
                                 proto.make_edge_events(0, words, anchor_ns=1_000_000)))
    result = contract.check_stream(stream, "p2c")
    assert contract.CONTRACT_REVISION == "2.8.2"
    assert result.accepted, f"EDGE_LANE_CONTRACT: {[str(f) for f in result.failures]}"


def test_a_conforming_helo_leaves_edge_words_unscanned():
    """2.8.1: the edge-lane rule reads edge words only when the HELO has a
    width > 0 channel at an id <= 31. The daemon's HELO has none, so the
    checker records no word. Measured on the Zybo-Z7-20, the 2.8.0 per-word
    scan added 824 ns per edge to the daemon's live guard (2572 -> 3643)."""
    helo = _helo_on_the_wire(_ddr_source(_I2C))
    words = [(dev << 27) | (100 << 8) for dev in (1, 2, 3, 6)] * 50
    st = contract._Session()
    out = []
    contract._h_helo("frame[0][HELO]", helo, st, out)
    st.helo_ok = True
    contract._h_edge_events("frame[1][EDGE_EVENTS]",
                            proto.make_edge_events(0, words, anchor_ns=1_000_000), st, out)
    assert st.edge_lane_watch == set() and st.edge_device_ids == {}, st.edge_device_ids
    # A width > 0 channel at id 2 is watched, and the word that carries id 2 is found.
    bad = dict(helo, channels=[{"id": 2, "name": "level_pin[2]", "width": 1}] + helo["channels"],
               channel_total_bits=helo["channel_total_bits"] + 1)
    st2 = contract._Session()
    contract._h_helo("frame[0][HELO]", bad, st2, out)
    st2.helo_ok = True
    contract._h_edge_events("frame[1][EDGE_EVENTS]",
                            proto.make_edge_events(0, words, anchor_ns=1_000_000), st2, out)
    assert st2.edge_device_ids == {2: "frame[1][EDGE_EVENTS].words[1]"}
