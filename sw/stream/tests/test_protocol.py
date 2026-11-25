# rr unknown-llm (AI-generated)
"""DS-P2.30: protocol-level round-trip tests for wave_stream_v1.

Mirrors the wire spec — frame tags, HELO shape, msgpack layout. Catches
schema drift relative to routertl/docs/protocol/wave_stream_v1.md.
"""

from __future__ import annotations

import msgpack
import pytest

from sw.stream import channels as ch
from sw.stream import protocol as proto


class TestFrameTags:
    """The numeric tag values are part of the wire — never re-number."""

    def test_tag_values_match_spec(self):
        assert proto.TAG_HELO == 0
        assert proto.TAG_HELO_OK == 1
        assert proto.TAG_SAMPLES == 2
        assert proto.TAG_RATE_HINT == 3
        assert proto.TAG_EOF == 4
        assert proto.TAG_ERR == 5
        assert proto.TAG_ANNOTATIONS == 6


class TestPackFrame:
    """pack_frame must produce a single msgpack object that round-trips."""

    def test_helo_round_trip(self):
        helo = proto.make_helo(
            sample_clk_hz=100_000,
            channels=ch.CHANNELS,
            channel_total_bits=ch.CHANNEL_TOTAL_BITS,
        )
        wire = proto.pack_frame(proto.TAG_HELO, helo)
        unp = msgpack.Unpacker(raw=False)
        unp.feed(wire)
        objs = list(unp)
        assert len(objs) == 1
        tag, payload = objs[0]
        assert tag == proto.TAG_HELO
        assert payload["ver"] == "wave_stream_v1"
        assert payload["sample_clk_hz"] == 100_000
        assert payload["channel_total_bits"] == ch.CHANNEL_TOTAL_BITS
        assert isinstance(payload["channels"], list)
        assert payload["producer"].startswith("deepskopion-daemon/")

    def test_samples_round_trip(self):
        samples_payload = {"seq": 42, "samples": [
            {"ts": 0, "value": 0xAB},
            {"ts": 10_000, "value": 0xCD},
        ]}
        wire = proto.pack_frame(proto.TAG_SAMPLES, samples_payload)
        unp = msgpack.Unpacker(raw=False)
        unp.feed(wire)
        tag, payload = next(iter(unp))
        assert tag == proto.TAG_SAMPLES
        assert payload["seq"] == 42
        assert len(payload["samples"]) == 2
        assert payload["samples"][1]["ts"] == 10_000

    def test_eof_round_trip(self):
        eof = proto.make_eof("done")
        wire = proto.pack_frame(proto.TAG_EOF, eof)
        tag, payload = msgpack.unpackb(wire, raw=False)
        assert tag == proto.TAG_EOF
        assert payload["reason"] == "done"


class TestChannels:
    """Channel topology — bit layout and slice math."""

    def test_total_bits_matches_sample_widths(self):
        sample_bits = sum(c["width"] for c in ch.CHANNELS if c["type"] == "Signal")
        # CHANNEL_TOTAL_BITS sums ALL widths; annotation channels contribute 0
        assert ch.CHANNEL_TOTAL_BITS == sample_bits

    def test_total_bits_fits_in_uint64(self):
        assert ch.CHANNEL_TOTAL_BITS <= 64, \
            "DS-P2.30 fixture must fit in slice-2 uint64 path"

    def test_slice_offset_layout(self):
        # DS-P3.124: the sample lanes sit at ids 32.. (0..31 is the edge
        # device_id space); slicing is by table order, so the offsets are
        # the same as when the ids were 0..13.
        # First level pin at offset 0
        assert ch.slice_offset(32) == 0
        # uart_rx after the 8 level pins
        assert ch.slice_offset(40) == 8
        # i2c_sda at offset 10 (8 levels + 2 uart)
        assert ch.slice_offset(42) == 10
        # qspi_cs at offset 13 (8 + 2 + 2 + 1)
        assert ch.slice_offset(45) == 13

    def test_slice_offset_unknown_id_raises(self):
        with pytest.raises(KeyError):
            ch.slice_offset(9999)

    def test_annotation_channels_are_distinct_id_range(self):
        sample_ids = {c["id"] for c in ch.CHANNELS if c["width"] > 0}
        annot_ids = {c["id"] for c in ch.CHANNELS if c["width"] == 0}
        assert sample_ids.isdisjoint(annot_ids)
        # Conventional namespace: annotation IDs >= 100 to leave room
        # for sample channels growing.
        assert all(i >= 100 for i in annot_ids)

    def test_helo_advertises_per_datalogger_i2c_lanes(self):
        """DS-P3.155: HELO advertises distinct per-datalogger lanes for multi-instance
        protocols (cam B #4 and cam A #5) so RouteWave and consumers render them
        as separate lanes instead of collapsing both onto one i2c_frame."""
        helo = proto.make_helo(
            sample_clk_hz=100_000_000,
            channels=ch.CHANNELS,
            channel_total_bits=ch.CHANNEL_TOTAL_BITS,
        )
        channel_names = {c["id"]: c["name"] for c in helo["channels"]}
        assert channel_names.get(101) == "i2c_frame:4"
        assert channel_names.get(109) == "i2c_frame:5"



class TestControlFrame:
    """DS-P3.72: consumer→producer CONTROL frame (tag 8)."""

    def test_make_decode_round_trip(self):
        op, sid, cfg = proto.decode_control(proto.make_control(2, 0x9, op="set_config"))
        assert (op, sid, cfg) == ("set_config", 2, 0x9)

    def test_control_tag_is_8(self):
        frame = proto.pack_frame(proto.TAG_CONTROL, proto.make_control(3, 0x1))
        tag, payload = msgpack.unpackb(frame, raw=False)
        assert tag == 8
        assert payload["datalogger_id"] == 3 and payload["cfg_word"] == 1

    def test_op_defaults_to_set_config(self):
        op, _, _ = proto.decode_control({"datalogger_id": 1, "cfg_word": 1})
        assert op == "set_config"

    @pytest.mark.parametrize("bad", [
        {}, {"datalogger_id": 1}, {"cfg_word": 1},
        {"datalogger_id": "x", "cfg_word": 1},
        {"datalogger_id": 1, "cfg_word": 0x1FFFF},   # out of uint16
        [1, 2],                                    # not a map
    ])
    def test_decode_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            proto.decode_control(bad)


class TestControlAck:
    """DS-P3.75: producer→consumer CONTROL_ACK frame (tag 10)."""

    def test_make_decode_ok(self):
        sid, cfg, ok, reason = proto.decode_control_ack(proto.make_control_ack(8, 0x1, ok=True))
        assert (sid, cfg, ok, reason) == (8, 0x1, True, None)

    def test_make_decode_reject_carries_reason(self):
        sid, cfg, ok, reason = proto.decode_control_ack(
            proto.make_control_ack(99, 0, ok=False, reason="bad id"))
        assert (sid, cfg, ok) == (99, 0, False) and reason == "bad id"

    def test_reason_absent_when_ok(self):
        assert "reason" not in proto.make_control_ack(8, 1, ok=True)

    def test_tag_is_10_on_wire(self):
        tag, _ = msgpack.unpackb(
            proto.pack_frame(proto.TAG_CONTROL_ACK, proto.make_control_ack(8, 1)), raw=False)
        assert tag == 10
