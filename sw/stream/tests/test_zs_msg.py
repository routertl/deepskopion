# rr unknown-llm (AI-generated)
"""Unit tests for zs_msg_header_t parse/pack + bit-slicing helpers."""

from __future__ import annotations

import struct

import pytest

from sw.stream import zs_msg


def test_header_size_matches_doc():
    """common_header.md: 20-byte (5-word) header."""
    assert zs_msg.HEADER_SIZE == 20


def test_magic_value():
    """ZS_MAGIC = 0x5A53 (LE-uint16 view; bytes [0x53, 0x5A] in memory)."""
    assert zs_msg.ZS_MAGIC == 0x5A53


def test_pack_then_parse_roundtrip():
    hdr = zs_msg.make_header(
        datalogger_id=4,
        domain=zs_msg.DOMAIN_UART,
        event_id=0x00,
        severity=zs_msg.SEV_INFO,
        msg_class=zs_msg.CLASS_DATA,
        timestamp=0x0000_1234_5678_9ABC,
        payload_len=8,
        flags=0,
    )
    raw = zs_msg.pack_header(hdr)
    assert len(raw) == 20
    back = zs_msg.parse_header(raw)
    assert back == hdr


def test_msg_type_assembly():
    hdr = zs_msg.make_header(
        datalogger_id=1, domain=zs_msg.DOMAIN_I2C,
        event_id=0x00, severity=zs_msg.SEV_ERROR,
    )
    # ERROR(0x2) << 12 | I2C(0x2) << 8 | 0 = 0x2200 = I2C_NACK
    assert hdr.msg_type == 0x2200
    assert hdr.severity == zs_msg.SEV_ERROR
    assert hdr.domain == zs_msg.DOMAIN_I2C
    assert hdr.event_id == 0
    assert hdr.is_error is True


def test_severity_bounds_check():
    with pytest.raises(ValueError, match="severity"):
        zs_msg.make_header(datalogger_id=0, domain=0, severity=0x10)


def test_domain_bounds_check():
    with pytest.raises(ValueError, match="domain"):
        zs_msg.make_header(datalogger_id=0, domain=0x10)


def test_event_id_bounds_check():
    with pytest.raises(ValueError, match="event_id"):
        zs_msg.make_header(datalogger_id=0, domain=0, event_id=0x100)


def test_parse_short_buffer_raises():
    with pytest.raises(ValueError, match="short read"):
        zs_msg.parse_header(b"\x00" * 19)


def test_parse_bad_magic_raises():
    bad = struct.pack("<HBBBBHQHH", 0xDEAD, 1, 5, 0, 0, 0, 0, 0, 0)
    with pytest.raises(ValueError, match="bad magic"):
        zs_msg.parse_header(bad)


def test_total_size_includes_payload():
    hdr = zs_msg.make_header(datalogger_id=0, domain=0, payload_len=12)
    assert hdr.total_size == 32  # 20-byte header + 12-byte payload


def test_parse_with_offset():
    """Parse from a non-zero offset inside a larger buffer."""
    hdr = zs_msg.make_header(datalogger_id=7, domain=zs_msg.DOMAIN_SPI)
    buf = b"\x00" * 4 + zs_msg.pack_header(hdr) + b"\x00" * 8
    back = zs_msg.parse_header(buf, offset=4)
    assert back.datalogger_id == 7
    assert back.domain == zs_msg.DOMAIN_SPI


def test_known_event_uart_frame_ok():
    """UART_FRAME_OK = 0x0100 per spec (INFO|UART|0x00)."""
    hdr = zs_msg.make_header(datalogger_id=0, domain=zs_msg.DOMAIN_UART,
                             event_id=0x00, severity=zs_msg.SEV_INFO)
    assert hdr.msg_type == 0x0100
    assert hdr.is_error is False


def test_known_event_i2c_nack():
    """I2C_NACK = 0x2200 per spec (ERROR|I2C|0x00)."""
    hdr = zs_msg.make_header(datalogger_id=0, domain=zs_msg.DOMAIN_I2C,
                             event_id=0x00, severity=zs_msg.SEV_ERROR)
    assert hdr.msg_type == 0x2200
    assert hdr.is_error is True


def test_known_event_uart_parity_error():
    """UART_PARITY_ERROR = 0x2100 per spec (ERROR|UART|0x00)."""
    hdr = zs_msg.make_header(datalogger_id=0, domain=zs_msg.DOMAIN_UART,
                             event_id=0x00, severity=zs_msg.SEV_ERROR)
    assert hdr.msg_type == 0x2100


# ─── EDGE_BATCH payload (DS-P2.47) ──────────────────────────────────────────

def test_edge_event_struct_is_4_bytes():
    """payloads/edge_batch.md §2: per-event entry is exactly 4 bytes."""
    assert zs_msg.EDGE_EVENT_SIZE == 4


def test_edge_batch_event_id_is_0x01_under_domain_edge():
    """event_and_datalogger_id_spec.md §3: EVT_EDGE_BATCH = 0x01 under DOMAIN_EDGE
    → msg_type = 0x0601 (SEV_INFO|DOMAIN_EDGE|0x01)."""
    assert zs_msg.EVT_EDGE_BATCH == 0x01
    hdr = zs_msg.make_header(datalogger_id=12, domain=zs_msg.DOMAIN_EDGE,
                             event_id=zs_msg.EVT_EDGE_BATCH,
                             severity=zs_msg.SEV_INFO)
    assert hdr.msg_type == 0x0601


def test_edge_batch_roundtrip_n1():
    """Degenerate N=1 batch — packs + parses cleanly without penalty."""
    ev = zs_msg.EdgeEvent(ts_64b=0x1000, channel=12, e=True)
    wire = zs_msg.pack_edge_batch(datalogger_id=12, channel=12, events=[ev])

    # 20-byte header + 1*4-byte entry = 24 bytes total
    assert len(wire) == zs_msg.HEADER_SIZE + zs_msg.EDGE_EVENT_SIZE

    hdr, events = zs_msg.parse_edge_batch(wire)
    assert hdr.datalogger_id == 12
    assert hdr.event_id == zs_msg.EVT_EDGE_BATCH
    assert hdr.timestamp == 0x1000           # anchor = first event ts
    assert hdr.payload_len == 4
    assert len(events) == 1
    assert events[0] == ev


def test_edge_batch_roundtrip_n32():
    """Full PoC batch — N=32 monotonically-increasing events.

    Verifies absolute timestamps come back intact after the anchor-delta
    encoding round-trip.
    """
    base = 0x12345678_DEAD_0000  # arbitrary 64-bit anchor
    events = [
        zs_msg.EdgeEvent(
            ts_64b=base + i * 13,    # 13-cycle spacing
            channel=7,
            e=(i % 2 == 0),
            f=(i == 31),
        )
        for i in range(32)
    ]
    wire = zs_msg.pack_edge_batch(datalogger_id=7, channel=7, events=events)
    assert len(wire) == zs_msg.HEADER_SIZE + 32 * zs_msg.EDGE_EVENT_SIZE

    hdr, parsed = zs_msg.parse_edge_batch(wire)
    assert hdr.payload_len == 32 * 4
    assert len(parsed) == 32
    for i, (orig, back) in enumerate(zip(events, parsed)):
        assert back.ts_64b == orig.ts_64b, f"event[{i}] ts mismatch"
        assert back.channel == orig.channel
        assert back.e == orig.e
        assert back.f == orig.f
        assert back.s is False  # never set in this fixture


def test_edge_batch_per_entry_efficiency_at_n32():
    """edge_batch.md §6 worked numbers — at N=32 the per-event wire cost
    drops to ~4.6 bytes (148B / 32 ≈ 4.625)."""
    events = [zs_msg.EdgeEvent(ts_64b=i, channel=0) for i in range(32)]
    wire = zs_msg.pack_edge_batch(datalogger_id=0, channel=0, events=events)
    per_event_cost = len(wire) / len(events)
    assert len(wire) == 148
    assert per_event_cost == pytest.approx(4.625)


def test_edge_batch_anchor_defaults_to_first_event():
    """If anchor_ts is None, the packer uses events[0].ts_64b — and the
    first entry's ts_delta should therefore be 0."""
    events = [
        zs_msg.EdgeEvent(ts_64b=0x5000, channel=4),
        zs_msg.EdgeEvent(ts_64b=0x5010, channel=4),
    ]
    wire = zs_msg.pack_edge_batch(datalogger_id=4, channel=4, events=events)
    # First entry's ts_delta is 16 cycles into the payload; unpack directly.
    first_delta = struct.unpack_from("<H", wire, zs_msg.HEADER_SIZE)[0]
    second_delta = struct.unpack_from("<H", wire,
                                      zs_msg.HEADER_SIZE + 4)[0]
    assert first_delta == 0
    assert second_delta == 0x10


def test_edge_batch_anchor_can_be_explicit():
    """Caller can override anchor_ts (e.g. when the framer uses a wall-clock
    snapshot taken before the first event arrives at the FSM)."""
    ev = zs_msg.EdgeEvent(ts_64b=0x5005, channel=2)
    wire = zs_msg.pack_edge_batch(datalogger_id=2, channel=2, events=[ev],
                                  anchor_ts=0x5000)
    hdr, events = zs_msg.parse_edge_batch(wire)
    assert hdr.timestamp == 0x5000
    assert events[0].ts_64b == 0x5005


def test_edge_batch_flag_bits():
    """flags byte: bit[0]=E, bit[1]=F, bit[2]=S (edge_batch.md §3.3)."""
    ev = zs_msg.EdgeEvent(ts_64b=0, channel=0, s=True, f=False, e=True)
    wire = zs_msg.pack_edge_batch(datalogger_id=0, channel=0, events=[ev])
    flags_byte = wire[zs_msg.HEADER_SIZE + 3]
    assert flags_byte == (zs_msg.EDGE_FLAG_S | zs_msg.EDGE_FLAG_E)
    _, parsed = zs_msg.parse_edge_batch(wire)
    assert parsed[0].s is True
    assert parsed[0].f is False
    assert parsed[0].e is True


def test_edge_batch_empty_rejected():
    """A zero-event batch is illegal — the framer never emits payload_len=0."""
    with pytest.raises(ValueError, match="at least 1 event"):
        zs_msg.pack_edge_batch(datalogger_id=0, channel=0, events=[])


def test_edge_batch_delta_overflow_rejected():
    """Pack-time overflow of ts_delta is a framer-side close condition —
    the codec catches and rejects with a clear error so the FSM bug is
    surfaced rather than silently truncating timestamps."""
    events = [
        zs_msg.EdgeEvent(ts_64b=0,          channel=0),
        zs_msg.EdgeEvent(ts_64b=0x10000,    channel=0),  # delta = 65536, overflows u16
    ]
    with pytest.raises(ValueError, match="ts_delta=65536 out of uint16 range"):
        zs_msg.pack_edge_batch(datalogger_id=0, channel=0, events=events)


def test_edge_batch_channel_mismatch_rejected_at_pack():
    """v1 requires single-channel batches — packer rejects mixed channels."""
    events = [
        zs_msg.EdgeEvent(ts_64b=0, channel=4),
        zs_msg.EdgeEvent(ts_64b=1, channel=5),
    ]
    with pytest.raises(ValueError, match="mismatches batch"):
        zs_msg.pack_edge_batch(datalogger_id=4, channel=4, events=events)


def test_edge_batch_channel_mismatch_rejected_at_parse():
    """If a malformed frame slips through with mismatched per-entry channels,
    the parser must reject — not silently surface bogus annotations."""
    hdr = zs_msg.make_header(datalogger_id=4, domain=zs_msg.DOMAIN_EDGE,
                             event_id=zs_msg.EVT_EDGE_BATCH,
                             timestamp=0, payload_len=4)
    payload = zs_msg.EDGE_EVENT_STRUCT.pack(0, 9, 0)  # channel=9 != datalogger_id=4
    wire = zs_msg.pack_header(hdr) + payload
    with pytest.raises(ValueError, match="channel=9 mismatches"):
        zs_msg.parse_edge_batch(wire)


def test_edge_batch_not_edge_msg_rejected():
    """Parser rejects frames whose msg_type isn't EDGE_BATCH."""
    hdr = zs_msg.make_header(datalogger_id=0, domain=zs_msg.DOMAIN_UART,
                             event_id=0, payload_len=4)
    wire = zs_msg.pack_header(hdr) + b"\x00" * 4
    with pytest.raises(ValueError, match="not an EDGE_BATCH frame"):
        zs_msg.parse_edge_batch(wire)


def test_edge_batch_misaligned_payload_rejected():
    """payload_len must be a multiple of 4 (EDGE_EVENT_SIZE)."""
    hdr = zs_msg.make_header(datalogger_id=0, domain=zs_msg.DOMAIN_EDGE,
                             event_id=zs_msg.EVT_EDGE_BATCH,
                             payload_len=5)
    wire = zs_msg.pack_header(hdr) + b"\x00" * 5
    with pytest.raises(ValueError, match="not a multiple of 4"):
        zs_msg.parse_edge_batch(wire)


def test_edge_batch_short_read_rejected():
    """Parser rejects when fewer payload bytes are available than payload_len
    says — corrupted wire / partial mmap recovery."""
    hdr = zs_msg.make_header(datalogger_id=0, domain=zs_msg.DOMAIN_EDGE,
                             event_id=zs_msg.EVT_EDGE_BATCH,
                             payload_len=128)
    wire = zs_msg.pack_header(hdr) + b"\x00" * 16  # only 16 of 128 bytes
    with pytest.raises(ValueError, match="short read"):
        zs_msg.parse_edge_batch(wire)


def test_edge_batch_payload_len_zero_rejected_at_parse():
    """A zero-payload EDGE_BATCH frame is illegal at parse time too."""
    hdr = zs_msg.make_header(datalogger_id=0, domain=zs_msg.DOMAIN_EDGE,
                             event_id=zs_msg.EVT_EDGE_BATCH,
                             payload_len=0)
    wire = zs_msg.pack_header(hdr)
    with pytest.raises(ValueError, match="payload_len=0 is invalid"):
        zs_msg.parse_edge_batch(wire)


# ── EVT_EDGE_MAXFREQ (DS-P3.117) ───────────────────────────────────────────

def test_edge_maxfreq_event_id_is_0x02_under_domain_edge():
    """EVT_EDGE_MAXFREQ = 0x02 under DOMAIN_EDGE → msg_type 0x0602 (SEV_INFO).
    Hard-coded (ROUTERTL-002), distinct from EVENT(0x00)/BATCH(0x01)."""
    assert zs_msg.EVT_EDGE_MAXFREQ == 0x02
    mf = zs_msg.EdgeMaxFreq(channel=6, min_interval=8, ival_width=24)
    hdr, _ = zs_msg.parse_edge_maxfreq(zs_msg.pack_edge_maxfreq(mf))
    assert hdr.domain == zs_msg.DOMAIN_EDGE
    assert hdr.event_id == zs_msg.EVT_EDGE_MAXFREQ
    assert hdr.msg_type == 0x0602


def test_edge_maxfreq_roundtrip():
    """pack → parse is identity for the measured-value case. Header datalogger_id
    defaults to the payload channel so the two agree by construction."""
    mf = zs_msg.EdgeMaxFreq(channel=3, min_interval=50, ival_width=24)
    hdr, got = zs_msg.parse_edge_maxfreq(
        zs_msg.pack_edge_maxfreq(mf, timestamp=0xABCD))
    assert hdr.datalogger_id == 3
    assert hdr.timestamp == 0xABCD
    assert got == mf
    assert got.is_sentinel is False


def test_edge_maxfreq_sentinel_roundtrip():
    """All-ones (24-bit) min_interval round-trips and flags is_sentinel."""
    mf = zs_msg.EdgeMaxFreq(channel=1, min_interval=0x00FFFFFF, ival_width=24)
    _hdr, got = zs_msg.parse_edge_maxfreq(zs_msg.pack_edge_maxfreq(mf))
    assert got.min_interval == 0x00FFFFFF
    assert got.is_sentinel is True


def test_edge_maxfreq_payload_is_8_bytes():
    """Tiny fixed payload: 8 bytes (u32 interval, u8 channel, u8 width, u16
    flags) — guards against silent struct drift."""
    assert zs_msg.EDGE_MAXFREQ_SIZE == 8
    wire = zs_msg.pack_edge_maxfreq(
        zs_msg.EdgeMaxFreq(channel=2, min_interval=100, ival_width=24))
    assert len(wire) == zs_msg.HEADER_SIZE + 8


def test_edge_maxfreq_wrong_event_id_rejected():
    """parse_edge_maxfreq rejects a frame that is not EVT_EDGE_MAXFREQ."""
    hdr = zs_msg.make_header(datalogger_id=0, domain=zs_msg.DOMAIN_EDGE,
                             event_id=zs_msg.EVT_EDGE_BATCH, payload_len=8)
    wire = zs_msg.pack_header(hdr) + b"\x00" * 8
    with pytest.raises(ValueError, match="not an EDGE_MAXFREQ frame"):
        zs_msg.parse_edge_maxfreq(wire)


def test_edge_maxfreq_channel_header_mismatch_rejected():
    """A payload whose channel disagrees with header.datalogger_id is corrupt."""
    hdr = zs_msg.make_header(datalogger_id=2, domain=zs_msg.DOMAIN_EDGE,
                             event_id=zs_msg.EVT_EDGE_MAXFREQ, payload_len=8)
    bad = zs_msg.EDGE_MAXFREQ_STRUCT.pack(100, 7, 24, 0)  # channel 7 != 2
    with pytest.raises(ValueError, match="mismatches header.datalogger_id"):
        zs_msg.parse_edge_maxfreq(zs_msg.pack_header(hdr) + bad)


def test_edge_maxfreq_bad_payload_len_rejected():
    """A non-8-byte EDGE_MAXFREQ payload is rejected."""
    hdr = zs_msg.make_header(datalogger_id=2, domain=zs_msg.DOMAIN_EDGE,
                             event_id=zs_msg.EVT_EDGE_MAXFREQ, payload_len=4)
    wire = zs_msg.pack_header(hdr) + b"\x00" * 4
    with pytest.raises(ValueError, match="must be 8"):
        zs_msg.parse_edge_maxfreq(wire)
