"""Unit tests for DdrRingDataSource — Phase B of DS-P2.31 (DS-P3.27).

Validates that the source:
  - Seeds the read pointer on its first tick (doesn't reprocess history).
  - Parses zs_msg_header_t frames out of the DDR ring on subsequent ticks
    and emits one annotation per frame on the right domain channel.
  - Advances its internal read pointer past consumed frames; leaves
    partial-tail frames for the next tick.
  - Walks past magic-mismatch bytes via 4-byte resync and surfaces a
    zs_resync annotation.
  - Drains a large commit advance instead of discarding it (DS-T1.23).
  - Bounds the DDR copy and the decode time per tick (DS-T1.23).
  - Writes the PS-side read pointer back to RING_READ_PTR when enabled.
"""

from __future__ import annotations

import itertools

import pytest

from sw.stream import zs_msg
from sw.stream.data_source import DdrRingDataSource
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.regbank import FakeRegbank, Regs


RING_BYTES = 256
_BYTES_PER_MSG = 4


def _build_frame(datalogger_id: int = 0, domain: int = zs_msg.DOMAIN_UART,
                 severity: int = zs_msg.SEV_INFO, event_id: int = 0,
                 payload: bytes = b"", timestamp: int = 0) -> bytes:
    hdr = zs_msg.make_header(
        datalogger_id=datalogger_id, domain=domain,
        event_id=event_id, severity=severity,
        timestamp=timestamp, payload_len=len(payload),
    )
    return zs_msg.pack_header(hdr) + payload


def _clock_seq():
    """Build a deterministic monotonic clock yielding 0.0, 0.05, 0.10, …"""
    return iter(itertools.chain([0.0], itertools.count(start=0.05, step=0.05)))


class _MirrorRegbank(FakeRegbank):
    """FakeRegbank that mirrors RING_COMMIT_PTR ← RING_WRITE_PTR*4.

    The decode-focused tests below position the write head by setting
    RING_WRITE_PTR (a word count). The daemon now reads the head from
    RING_COMMIT_PTR (byte offset, regbank.py: "read up to THIS, not WRITE_PTR"),
    so mirror the two here to keep those tests terse — they exercise frame
    parsing/decoding, not the pointer source. The COMMIT-vs-WRITE divergence
    contract (the real bug) is pinned by test_read_head_tracks_commit_not_write.
    """
    def read_u32(self, offset: int) -> int:
        if offset == Regs.RING_COMMIT_PTR:
            # No modulo here — the daemon mods by its own ring size (the per-test
            # ddr size, which may be smaller than the module RING_BYTES).
            return super().read_u32(Regs.RING_WRITE_PTR) * _BYTES_PER_MSG
        return super().read_u32(offset)


@pytest.fixture
def regbank():
    return _MirrorRegbank()


@pytest.fixture
def ddr():
    return FakeDdrMmap(RING_BYTES)


def _make_source(regbank, ddr, **kwargs):
    times = _clock_seq()
    defaults = dict(
        sample_clk_hz=100_000_000,
        poll_period_s=0.05,
        duration_s=0.0,
        write_back_read_ptr=True,
        clock=lambda: next(times),
    )
    defaults.update(kwargs)
    return DdrRingDataSource(regbank=regbank, ddr=ddr, **defaults)


def test_sample_clk_hz_reported(regbank, ddr):
    src = _make_source(regbank, ddr, sample_clk_hz=50_000_000)
    assert src.sample_clk_hz() == 50_000_000


def test_first_tick_seeds_pointer_without_history(regbank, ddr):
    """Don't reprocess pre-startup frames — first batch should be empty."""
    frame = _build_frame(datalogger_id=1, domain=zs_msg.DOMAIN_UART)
    ddr.write_bytes(0, frame)
    # Pretend the PL has been writing for a while (8 frames worth).
    regbank.set(Regs.RING_WRITE_PTR, 8 * (zs_msg.HEADER_SIZE // _BYTES_PER_MSG))

    src = _make_source(regbank, ddr)
    batch = src.next_batch()
    assert batch is not None
    assert batch.annotations == []
    assert batch.samples == []


def test_single_frame_emits_one_annotation(regbank, ddr):
    """Place a single UART char frame at byte 0; advance RING_WRITE_PTR;
    expect one decoded annotation. UART is framed 1:1 (event_id = the
    message TYPE, payload[0] = the char) like every other Burst datalogger."""
    import struct as _struct
    from sw.stream import protocol_codes
    src = _make_source(regbank, ddr)
    src.next_batch()  # seed pointer

    # UART char 'U' (0x55), message type = payload.
    uart_payload = _struct.pack("<I", 0x55)
    frame = _build_frame(datalogger_id=12, domain=zs_msg.DOMAIN_UART,
                         event_id=protocol_codes.UART_TYPES["payload"],
                         severity=zs_msg.SEV_INFO,
                         payload=uart_payload, timestamp=0x123456)
    ddr.write_bytes(0, frame)
    msgs_written = (len(frame) + _BYTES_PER_MSG - 1) // _BYTES_PER_MSG
    regbank.set(Regs.RING_WRITE_PTR, msgs_written)

    batch = src.next_batch()
    assert batch is not None
    assert len(batch.annotations) == 1
    ann = batch.annotations[0]
    assert ann.channel_id == 100  # uart_byte (DOMAIN_UART → channel 100)
    assert ann.kind == "uart_byte"
    assert "0x55" in ann.text and "'U'" in ann.text
    assert ann.is_error is False


def test_error_frame_marked_red_and_is_error(regbank, ddr):
    src = _make_source(regbank, ddr)
    src.next_batch()
    frame = _build_frame(datalogger_id=4, domain=zs_msg.DOMAIN_UART,
                         severity=zs_msg.SEV_ERROR, event_id=0x00)
    ddr.write_bytes(0, frame)
    regbank.set(Regs.RING_WRITE_PTR, (len(frame) + 3) // 4)

    batch = src.next_batch()
    assert len(batch.annotations) == 1
    ann = batch.annotations[0]
    assert ann.is_error is True
    assert ann.color == "#cc3333"


def test_proto_decode_errs_reconciles_with_hw_proto_errs(regbank, ddr):
    """DS-P2.61: read_stats must surface a per-protocol DECODED-error tally
    (proto_decode_errs, = annotation is_error, what the consumer's ERRORS chip
    counts) DISTINCT from the HW proto_errs (ERR_CNT_* datalogger bus). The two
    legitimately diverge — here the HW bus is quiet (0) while the decoder finds
    3 UART + 1 I2C errors — so a consumer seeing 'uart Err 0' must not mistake
    error-heavy traffic for healthy."""
    import struct as _struct
    from sw.stream import protocol_codes
    src = _make_source(regbank, ddr)
    src.next_batch()  # seed pointer

    # 3 UART decode-errors + 1 I2C decode-error + 1 CLEAN UART byte (must NOT count).
    frames = (
        _build_frame(datalogger_id=1, domain=zs_msg.DOMAIN_UART, severity=zs_msg.SEV_ERROR)
        + _build_frame(datalogger_id=2, domain=zs_msg.DOMAIN_UART, severity=zs_msg.SEV_ERROR)
        + _build_frame(datalogger_id=3, domain=zs_msg.DOMAIN_UART, severity=zs_msg.SEV_ERROR)
        + _build_frame(datalogger_id=4, domain=zs_msg.DOMAIN_I2C,  severity=zs_msg.SEV_ERROR)
        + _build_frame(datalogger_id=5, domain=zs_msg.DOMAIN_UART,
                       event_id=protocol_codes.UART_TYPES["payload"],
                       severity=zs_msg.SEV_INFO, payload=_struct.pack("<I", 0x55))
    )
    ddr.write_bytes(0, frames)
    regbank.set(Regs.RING_WRITE_PTR, (len(frames) + 3) // 4)
    src.next_batch()

    stats = src.read_stats()
    # HW error bus quiet (FakeRegbank ERR_CNT defaults 0) — the contradiction.
    # LT-P3.250 / DS-P3.120: HW errs now live in the self-describing proto_stats map.
    assert stats["proto_stats"]["uart"]["errs"] == 0
    # Decoded tally reflects the real per-protocol error counts; the clean byte
    # is NOT counted, and a protocol with no errors stays 0.
    assert stats["proto_decode_errs"]["uart"] == 3
    assert stats["proto_decode_errs"]["i2c"] == 1
    assert stats["proto_decode_errs"]["spi"] == 0


def test_multiple_frames_parsed_in_order(regbank, ddr):
    src = _make_source(regbank, ddr)
    src.next_batch()

    f1 = _build_frame(datalogger_id=1, domain=zs_msg.DOMAIN_UART, event_id=0x00)
    f2 = _build_frame(datalogger_id=2, domain=zs_msg.DOMAIN_I2C,  event_id=0x04)
    f3 = _build_frame(datalogger_id=3, domain=zs_msg.DOMAIN_SPI,  event_id=0x02)
    blob = f1 + f2 + f3
    ddr.write_bytes(0, blob)
    regbank.set(Regs.RING_WRITE_PTR, (len(blob) + 3) // 4)

    batch = src.next_batch()
    assert len(batch.annotations) == 3
    channels = [a.channel_id for a in batch.annotations]
    assert channels == [100, 101, 102]  # uart_byte, i2c_frame, qspi_xact (all decode now)


def test_partial_tail_frame_deferred_to_next_tick(regbank, ddr):
    """A frame whose header_len declares more bytes than are in the span
    yet should be re-read on the next tick once the rest lands."""
    import struct as _struct
    src = _make_source(regbank, ddr)
    src.next_batch()

    from sw.stream import protocol_codes
    uart_payload = _struct.pack("<I", 0x41)
    full_frame = _build_frame(datalogger_id=12, domain=zs_msg.DOMAIN_UART,
                              event_id=protocol_codes.UART_TYPES["payload"],
                              payload=uart_payload)
    half = full_frame[:zs_msg.HEADER_SIZE]  # header only — payload still missing
    ddr.write_bytes(0, half)
    msgs_in_half = (len(half) + 3) // 4
    regbank.set(Regs.RING_WRITE_PTR, msgs_in_half)

    batch = src.next_batch()
    # Header without payload → partial-frame deferral, no annotation.
    assert batch.annotations == []

    # Land the rest.
    ddr.write_bytes(len(half), full_frame[zs_msg.HEADER_SIZE:])
    msgs_total = (len(full_frame) + 3) // 4
    regbank.set(Regs.RING_WRITE_PTR, msgs_total)

    batch2 = src.next_batch()
    assert len(batch2.annotations) == 1
    # Decoded text shows the byte (0x41 = 'A').
    assert "0x41" in batch2.annotations[0].text


def test_resync_walks_past_garbage_bytes(regbank, ddr):
    """A 4-byte garbage prefix should resync into the real frame and
    surface a zs_resync annotation."""
    import struct as _struct
    src = _make_source(regbank, ddr)
    src.next_batch()

    from sw.stream import protocol_codes
    uart_payload = _struct.pack("<I", 0x55)
    frame = _build_frame(datalogger_id=12, domain=zs_msg.DOMAIN_UART,
                         event_id=protocol_codes.UART_TYPES["payload"],
                         payload=uart_payload)
    junk = b"\xDE\xAD\xBE\xEF"
    blob = junk + frame
    ddr.write_bytes(0, blob)
    regbank.set(Regs.RING_WRITE_PTR, (len(blob) + 3) // 4)

    batch = src.next_batch()
    kinds = sorted(a.kind for a in batch.annotations)
    assert "uart_byte" in kinds  # decoded UART frame survives resync
    assert "zs_resync" in kinds
    resync = next(a for a in batch.annotations if a.kind == "zs_resync")
    assert resync.is_error is True
    assert "+4B" in resync.text


def _uart_char_frame(ts: int = 0) -> bytes:
    """One 24-byte UART char frame that decodes cleanly to a uart_byte
    annotation (no error, so the DS-P3.87 storm collapse never hides one)."""
    import struct as _struct
    from sw.stream import protocol_codes
    return _build_frame(datalogger_id=12, domain=zs_msg.DOMAIN_UART,
                        event_id=protocol_codes.UART_TYPES["payload"],
                        severity=zs_msg.SEV_INFO,
                        payload=_struct.pack("<I", 0x55), timestamp=ts)


def _read_ptr_writes(regbank):
    return [v for a, v in regbank.writes if a == Regs.RING_READ_PTR]


def test_large_commit_advance_is_drained_not_discarded(regbank, ddr):
    """DS-T1.23: a commit advance of >=75 % of the ring in one tick is drained.

    The PL cannot overwrite unread bytes (ddr_stream_axi gates its writes on
    FULL against RING_READ_PTR, DS-P2.59), so a big advance only means the last
    tick was slow. The old 'ring_overrun' guard snapped the read head to the
    commit head and threw the span away: on DE25 silicon it discarded 209 MB of
    valid frames with DDR_BACKPRESSURE_CNT at 0."""
    src = _make_source(regbank, ddr)
    src.next_batch()                          # seed at 0

    frame = _uart_char_frame()
    n = (RING_BYTES * 80 // 100) // len(frame)    # 8 frames = 192 B = 75 % of 256
    ddr.write_bytes(0, frame * n)
    regbank.set(Regs.RING_WRITE_PTR, n * len(frame) // _BYTES_PER_MSG)
    assert n * len(frame) >= RING_BYTES * 3 // 4

    batch = src.next_batch()
    uart = [a for a in batch.annotations if a.kind == "uart_byte"]
    assert len(uart) == n, (
        f"commit advance of {n * len(frame)}B (>=75 % of the ring) discarded "
        f"valid frames: decoded {len(uart)} of {n}, "
        f"kinds={sorted({a.kind for a in batch.annotations})}")
    assert not any(a.kind == "ring_overrun" for a in batch.annotations)
    assert _read_ptr_writes(regbank)[-1] == n * len(frame)


class _RecordingDdr(FakeDdrMmap):
    """FakeDdrMmap that records every read_bytes length (the uncached copy)."""

    def __init__(self, size: int) -> None:
        super().__init__(size)
        self.read_lengths: list[int] = []

    def read_bytes(self, offset: int, length: int) -> bytes:
        self.read_lengths.append(length)
        return super().read_bytes(offset, length)


def test_ddr_copy_per_tick_is_bounded(regbank, monkeypatch):
    """DS-T1.23: one tick copies at most _MAX_SPAN_BYTES_PER_TICK from DDR.

    The parser consumes only the whole frames inside one span (DS-P1.100: the
    tick is bounded by bytes and time, not a frame count), so copying the
    whole backlog each tick is wasted uncached-read time that grows with the
    backlog: on DE25 silicon one tick copied 53.9 MB (1.34 s) to consume
    655 KB. The drain still advances by the frames it parsed."""
    ring = 4 << 20
    ddr = _RecordingDdr(ring)
    from sw.stream import data_source as ds_mod
    monkeypatch.setattr(ds_mod, "_budget_clock", lambda: 0.0, raising=False)
    src = _make_source(regbank, ddr)
    src.next_batch()                          # seed at 0

    frame = _uart_char_frame()
    n = (2 << 20) // len(frame)               # a 2 MiB backlog (50 % of the ring)
    ddr.write_bytes(0, frame * n)
    regbank.set(Regs.RING_WRITE_PTR, n * len(frame) // _BYTES_PER_MSG)

    src.next_batch()
    cap = getattr(DdrRingDataSource, "_MAX_SPAN_BYTES_PER_TICK", 1 << 20)
    assert cap >= zs_msg.HEADER_SIZE + 0xFFFF          # a max-size frame still fits
    assert max(ddr.read_lengths) <= cap, (
        f"one tick copied {max(ddr.read_lengths)}B from the DDR window "
        f"(cap {cap}B) for a {n * len(frame)}B backlog")
    # DS-P1.100: the tick stops at the span, so it consumes every whole frame
    # inside one span: (1 MiB // 24) * 24 = 1 048 560 B.
    assert _read_ptr_writes(regbank)[-1] == (cap // len(frame)) * len(frame)


def test_read_ptr_written_back_within_decode_budget(regbank, monkeypatch):
    """DS-T1.23: a tick stops decoding when _TICK_BUDGET_S is spent and writes
    RING_READ_PTR back, so the PL sees drain progress at least that often.

    Each decoded frame advances a model clock by 2.08 ms, the per-frame cost
    measured on DE25 silicon (8.5 s for one 4096-frame tick). Without the
    budget, one tick decoded all 4096 frames and RING_READ_PTR stood still for
    the whole 8.5 s. Every frame must still arrive, over several ticks."""
    from sw.stream import data_source as ds_mod
    now = [0.0]
    real_decode = ds_mod.decoders.decode_frame

    def costly_decode(*args, **kwargs):
        now[0] += 0.00208
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(ds_mod.decoders, "decode_frame", costly_decode)
    monkeypatch.setattr(ds_mod, "_budget_clock", lambda: now[0], raising=False)
    ring = 1 << 20
    ddr = FakeDdrMmap(ring)
    src = _make_source(regbank, ddr)
    src.next_batch()                          # seed at 0

    frame = _uart_char_frame()
    n = 4096                                  # the 4096-frame tick measured on DE25 silicon
    ddr.write_bytes(0, frame * n)
    regbank.set(Regs.RING_WRITE_PTR, n * len(frame) // _BYTES_PER_MSG)

    t0 = now[0]
    first = src.next_batch()
    held_s = now[0] - t0
    budget = getattr(DdrRingDataSource, "_TICK_BUDGET_S", 0.25)
    assert held_s <= budget + 0.00208, (
        f"RING_READ_PTR held for {held_s:.2f}s of decode in one tick "
        f"(budget {budget}s)")
    decoded = len([a for a in first.annotations if a.kind == "uart_byte"])
    assert 0 < decoded < n
    assert _read_ptr_writes(regbank)[-1] == decoded * len(frame)

    for _ in range(200):
        if _read_ptr_writes(regbank)[-1] == n * len(frame):
            break
        decoded += len([a for a in src.next_batch().annotations
                        if a.kind == "uart_byte"])
    assert decoded == n
    assert _read_ptr_writes(regbank)[-1] == n * len(frame)


def test_read_pointer_written_back(regbank, ddr):
    """RING_READ_PTR is the active ring DRAIN pointer (DS-P2.59): the write-back
    is the consumed BYTE OFFSET (same units as RING_COMMIT_PTR / the PL's
    csr_rd_ptr_addr_i), NOT a message count. The PL uses it to free ring space."""
    src = _make_source(regbank, ddr, write_back_read_ptr=True)
    src.next_batch()

    frame = _build_frame(datalogger_id=4, domain=zs_msg.DOMAIN_UART)
    ddr.write_bytes(0, frame)
    msgs_written = (len(frame) + 3) // 4
    regbank.set(Regs.RING_WRITE_PTR, msgs_written)

    src.next_batch()
    writes = [w for w in regbank.writes if w[0] == Regs.RING_READ_PTR]
    assert writes, "expected RING_READ_PTR write-back"
    # Last write should be the consumed byte offset = the parsed frame's length.
    assert writes[-1][1] == len(frame)


def test_read_pointer_write_back_can_be_disabled(regbank, ddr):
    src = _make_source(regbank, ddr, write_back_read_ptr=False)
    src.next_batch()

    frame = _build_frame(datalogger_id=4, domain=zs_msg.DOMAIN_UART)
    ddr.write_bytes(0, frame)
    regbank.set(Regs.RING_WRITE_PTR, (len(frame) + 3) // 4)

    src.next_batch()
    rd_writes = [w for w in regbank.writes if w[0] == Regs.RING_READ_PTR]
    assert rd_writes == []


def test_read_head_tracks_commit_not_write_ptr(ddr):
    """REGRESSION (HW-discovered): the read head MUST be RING_COMMIT_PTR (the
    DMA's committed byte offset), NOT RING_WRITE_PTR*4 (an enqueue message count
    that diverges from the byte offset under variable-length / high-rate frames).

    Seeding the cursor from WRITE_PTR*4 lands mid-frame / past the committed data
    and the parser resyncs forever, never reaching the live frames (observed on
    silicon: 0 uart_byte, all zs_resync; read head 18.5 MB behind COMMIT). Here
    COMMIT and WRITE*4 are made to DIVERGE: a frame sits exactly in [0, COMMIT),
    while WRITE_PTR*4 points far past it. The daemon must decode the frame."""
    import struct as _struct
    from sw.stream import protocol_codes

    rb = FakeRegbank()                       # raw — NOT the mirroring fixture
    src = _make_source(rb, ddr)
    src.next_batch()                         # seed (COMMIT=0, WRITE=0)

    uart_payload = _struct.pack("<I", 0x55)
    frame = _build_frame(datalogger_id=12, domain=zs_msg.DOMAIN_UART,
                         event_id=protocol_codes.UART_TYPES["payload"],
                         payload=uart_payload)
    ddr.write_bytes(0, frame)

    # Committed head = exactly the frame's byte length …
    rb.set(Regs.RING_COMMIT_PTR, len(frame))
    # … but the enqueue count *4 points WAY past it (the divergence). If the
    # daemon used WRITE_PTR*4 it would read len(frame)*16 bytes — frame + a sea
    # of zeros → resync, no decode.
    rb.set(Regs.RING_WRITE_PTR, (len(frame) * 16) // _BYTES_PER_MSG)

    batch = src.next_batch()
    kinds = [a.kind for a in batch.annotations]
    assert kinds == ["uart_byte"], (
        f"expected exactly one decoded uart_byte at COMMIT, got {kinds} — "
        f"the read head is following RING_WRITE_PTR*4, not RING_COMMIT_PTR"
    )
    assert "0x55" in batch.annotations[0].text


def test_no_new_messages_emits_nothing(regbank, ddr):
    src = _make_source(regbank, ddr)
    src.next_batch()  # seed
    batch = src.next_batch()  # no new writes → nothing
    assert batch is not None
    assert batch.annotations == []
    assert batch.samples == []


def test_duration_expiry_returns_none(regbank, ddr):
    times = iter([0.0, 0.05, 0.1, 1.0, 1.0])
    src = DdrRingDataSource(
        regbank=regbank, ddr=ddr,
        poll_period_s=0.05, duration_s=0.5,
        clock=lambda: next(times),
    )
    assert src.next_batch() is not None
    assert src.next_batch() is None


def test_close_propagates_to_backends(regbank, ddr):
    src = _make_source(regbank, ddr)
    src.close()
    assert regbank.closed is True
    assert ddr.closed is True


def test_domain_to_channel_mapping(regbank, ddr):
    """Spot-check every domain → channel mapping in one go."""
    src = _make_source(regbank, ddr)
    src.next_batch()

    domains_and_channels = [
        (zs_msg.DOMAIN_UART, 100),
        (zs_msg.DOMAIN_I2C,  101),
        (zs_msg.DOMAIN_QSPI, 102),
        (zs_msg.DOMAIN_SPI,  102),
        # DS-P3.69: DOMAIN_EDGE no longer maps to a generic annotation channel —
        # normal edge frames become edge_words (see
        # test_edge_frame_emits_edge_word_not_annotation). Loss/error edge frames
        # still annotate (test_edge_error_frame_still_annotates_not_edge_word).
        (zs_msg.DOMAIN_SYS,  200),
    ]
    blob = b"".join(_build_frame(datalogger_id=i, domain=d)
                    for i, (d, _) in enumerate(domains_and_channels))
    ddr.write_bytes(0, blob)
    regbank.set(Regs.RING_WRITE_PTR, (len(blob) + 3) // 4)

    batch = src.next_batch()
    got = [a.channel_id for a in batch.annotations]
    expected = [ch for _, ch in domains_and_channels]
    assert got == expected


# ── JTAG reassembler integration (DS-P3.56 P2) ─────────────────────────


def _jtag_frame(event_id: int, data: int) -> bytes:
    """One JTAG zs_msg frame as datalogger_gather emits it (payload = 32-bit LE
    datalogger word; payload[0] = data byte; header.event_id = TYPE field)."""
    import struct as _struct
    word = (16 << 27) | ((event_id & 0x7) << 24) | (data & 0xFF)
    return _build_frame(
        datalogger_id=16, domain=zs_msg.DOMAIN_JTAG, event_id=event_id,
        payload=_struct.pack("<I", word),
    )


def test_jtag_chain_dual_idcode_reassembled_through_ring():
    """A post-reset dual-IDCODE DR scan through the ring drain decodes BOTH
    Zynq chain devices (DS-P3.58): PL TAP 0x23727093 + ARM DAP 0x4BA00477.
    The daemon defaults to ZYNQ7_CHAIN, so the summary is the per-device split."""
    from sw.stream import decoders

    ddr_big = FakeDdrMmap(2048)          # 19 frames * 24B = 456B
    regbank = _MirrorRegbank()
    src = _make_source(regbank, ddr_big)
    src.next_batch()                     # seed pointer

    T_EVT, T_TDI, T_TDO = (decoders.JTAG_T_EVENT,
                           decoders.JTAG_T_TDI, decoders.JTAG_T_TDO)
    # 64-bit combined IDCODE on TDO: DAP (TDO-nearest = low 32) | PL (high 32).
    DAP_ID, PL_ID = 0x4BA00477, 0x23727093
    tdo_bytes = [(DAP_ID >> (8 * k)) & 0xFF for k in range(4)] + \
                [(PL_ID >> (8 * k)) & 0xFF for k in range(4)]
    seq = [_jtag_frame(T_EVT, decoders.JTAG_EV_TAP_RESET),
           _jtag_frame(T_EVT, decoders.JTAG_EV_DR_BEGIN)]
    for b in tdo_bytes:
        seq += [_jtag_frame(T_TDI, 0x00), _jtag_frame(T_TDO, b)]
    seq.append(_jtag_frame(T_EVT, decoders.JTAG_EV_DR_END))
    blob = b"".join(seq)
    ddr_big.write_bytes(0, blob)
    regbank.set(Regs.RING_WRITE_PTR, len(blob) // 4)

    batch = src.next_batch()

    tap = [a for a in batch.annotations if a.channel_id == 103]
    reg = [a for a in batch.annotations if a.channel_id == 104]

    assert len(tap) == len(seq)          # one per-frame annotation per frame
    assert len(reg) == 1                 # exactly one register-scan summary
    assert reg[0].kind == "jtag_idcode"
    assert "PL:IDCODE 0x23727093" in reg[0].text
    assert "DAP:IDCODE 0x4BA00477" in reg[0].text


# ── DS-P3.60: ring-watermark IRQ hybrid drain pacing ────────────────────────
class _FakeIrq:
    """Stand-in for ring_irq.RingIrq: records arms; wait() returns a count to
    simulate the watermark firing, or None to simulate the poll-period timeout."""
    def __init__(self, fire: bool = True):
        self.arms = 0
        self._fire = fire

    def arm(self):
        self.arms += 1

    def wait(self, timeout=None):
        return 7 if self._fire else None


def test_drain_irq_arms_each_tick_and_counts_fires(regbank, ddr):
    """With a drain_irq, each tick arms it; a fired wait() counts as an IRQ wake
    and the batch still drains the frame (IRQ accelerates, never replaces)."""
    irq = _FakeIrq(fire=True)
    # constant clock → remaining_s > 0 each tick → the drain_irq branch runs.
    src = DdrRingDataSource(regbank=regbank, ddr=ddr, poll_period_s=0.05,
                            drain_irq=irq, clock=lambda: 0.0)
    src.next_batch()  # seed tick
    frame = _build_frame(datalogger_id=4, domain=zs_msg.DOMAIN_UART)
    ddr.write_bytes(0, frame)
    regbank.set(Regs.RING_WRITE_PTR, (len(frame) + 3) // 4)
    batch = src.next_batch()
    assert irq.arms >= 2, "IRQ must be armed every tick"
    assert src._irq_wakes >= 1, "a fired wait() must count as an IRQ wake"
    assert len(batch.annotations) == 1, "the batch must still drain the frame"


def test_drain_irq_timeout_is_not_counted_as_wake(regbank, ddr):
    """A poll-period timeout (wait()→None) must NOT count as an IRQ wake."""
    irq = _FakeIrq(fire=False)
    src = DdrRingDataSource(regbank=regbank, ddr=ddr, poll_period_s=0.05,
                            drain_irq=irq, clock=lambda: 0.0)
    src.next_batch()
    src.next_batch()
    assert irq.arms >= 2
    assert src._irq_wakes == 0, "a timeout must not count as an IRQ wake"


# ── DS-P3.69: normal edge transitions → EDGE_EVENTS words, not annotations ──────

def test_edge_batch_emits_one_word_per_entry_not_annotation(regbank, ddr):
    """A normal DOMAIN_EDGE frame is an EDGE_BATCH (DS-P2.47): one header
    (datalogger_id=channel, timestamp=clk-tick anchor) carrying N packed entries.
    The daemon must emit ONE EDGE_EVENT word PER entry — with the absolute time
    (anchor + ts_delta) and the raw S/F/E preserved — and NO text annotation.

    Regression for DS-T2.35: the historical decode took only payload[:4] (entry
    0 of up to 32) AND misread the entry layout as the old single-edge word,
    dropping 31/32 edges and mistiming the survivor. This batch has 4 entries,
    so the old code emitted 1 wrong word; the fix emits 4 correct ones."""
    from sw.stream.zs_msg import EdgeEvent, pack_edge_batch
    from sw.stream.edge_capture import words_to_transitions

    src = _make_source(regbank, ddr)
    src.next_batch()  # seed

    # Channel 1 (SCL), anchor at tick 1000. Alternating polarity (e bit);
    # for a normal edge (s=0) the decoded post-edge level is (1 - e).
    channel, anchor = 1, 1000
    raw = [(1000, 1), (1010, 0), (1025, 1), (1040, 0)]   # (tick, e)
    events = [EdgeEvent(ts_64b=t, channel=channel, s=False, f=False, e=bool(e))
              for t, e in raw]
    frame = pack_edge_batch(datalogger_id=channel, channel=channel,
                            events=events, anchor_ts=anchor)
    ddr.write_bytes(0, frame)
    regbank.set(Regs.RING_WRITE_PTR, (len(frame) + 3) // 4)

    batch = src.next_batch()
    # One word per entry — NOT one per frame (the DS-T2.35 bug).
    assert len(batch.edge_words) == len(events), batch.edge_words
    assert batch.annotations == [], "normal edge must NOT emit a text annotation"
    # Every word carries the channel in [31:27] (instance) so RouteWave can
    # split SCL/SDA lanes.
    assert all((w >> 27) & 0x1F == channel for w in batch.edge_words)
    # Round-trip: the reconstructed transitions match the entries' absolute
    # times (tick × 10 ns at 100 MHz) and levels (1 - e).
    trans = words_to_transitions(batch.edge_words, clk_mhz=100, instance=channel)
    assert trans == [(t * 10, 1 - e) for t, e in raw], trans


def test_edge_error_frame_still_annotates_not_edge_word(regbank, ddr):
    """A SEV_ERROR DOMAIN_EDGE frame (piece-3 loss report) stays on the
    annotation path (decode_frame → loss marker), NOT edge_words."""
    import struct
    src = _make_source(regbank, ddr)
    src.next_batch()
    # event_id 0xF0 = ERR_EVT_INTERNAL_FIFO_OVERFLOW; payload = lost_count delta
    frame = _build_frame(datalogger_id=0, domain=zs_msg.DOMAIN_EDGE,
                         severity=zs_msg.SEV_ERROR, event_id=0xF0,
                         payload=struct.pack("<I", 241))
    ddr.write_bytes(0, frame)
    regbank.set(Regs.RING_WRITE_PTR, (len(frame) + 3) // 4)

    batch = src.next_batch()
    assert batch.edge_words == [], "error/loss edge frame must NOT become an edge word"
    assert len(batch.annotations) == 1
    assert batch.annotations[0].kind == "loss"
    assert batch.annotations[0].is_error is True


# ── DS-P3.87: sustained error-storm run-length aggregation ──────────────────
#
# A persistent physical fault (e.g. wrong baud) makes the UART decoder emit
# thousands of identical STOP-ERROR frames. The drain loop must collapse a
# sustained run into ONE error_run event (count + t0..t1) instead of N
# identical annotations — bounding the stream for RouteWave (LT-P3.204) and any
# agent, while the run total is still preserved in proto_decode_errs.

from sw.stream.data_source import ErrorStormAggregator  # noqa: E402


def _stop_err_frame(ts: int) -> bytes:
    """One UART STOP-ERROR frame: status event (type 4), data byte 0x04 →
    decodes to kind 'uart_stop_error', is_error=True."""
    import struct as _struct
    from sw.stream import protocol_codes
    return _build_frame(datalogger_id=12, domain=zs_msg.DOMAIN_UART,
                        event_id=protocol_codes.UART_TYPES["status"],
                        severity=zs_msg.SEV_ERROR,
                        payload=_struct.pack("<I", 0x04), timestamp=ts)


def _clean_uart_byte(ts: int) -> bytes:
    import struct as _struct
    from sw.stream import protocol_codes
    return _build_frame(datalogger_id=12, domain=zs_msg.DOMAIN_UART,
                        event_id=protocol_codes.UART_TYPES["payload"],
                        severity=zs_msg.SEV_INFO,
                        payload=_struct.pack("<I", 0x55), timestamp=ts)


def _storm_harness():
    """A DdrRingDataSource on a roomy ring, with a drain(frames) helper that
    appends frames at the tracked offset, advances RING_WRITE_PTR, and returns
    the next batch. (Large ring so a 50-frame storm fits without wrap.)"""
    ddr = FakeDdrMmap(8192)
    regbank = _MirrorRegbank()
    src = _make_source(regbank, ddr)
    src.next_batch()  # seed the read pointer at 0
    state = {"ofs": 0}

    def drain(frames: bytes):
        ddr.write_bytes(state["ofs"], frames)
        state["ofs"] += len(frames)
        regbank.set(Regs.RING_WRITE_PTR, state["ofs"] // _BYTES_PER_MSG)
        return src.next_batch()

    return src, drain


def test_error_storm_collapses_to_one_run_event():
    src, drain = _storm_harness()
    N = 50
    batch = drain(b"".join(_stop_err_frame(ts=1000 + i * 10) for i in range(N)))

    indiv = [a for a in batch.annotations if a.kind == "uart_stop_error"]
    runs = [a for a in batch.annotations if a.kind == "error_run"]
    # The first STORM_THRESHOLD errors still render individually; the remaining
    # 42 collapse into a single run event → 8 + 1 instead of 50.
    assert len(indiv) == ErrorStormAggregator.STORM_THRESHOLD
    assert len(runs) == 1
    r = runs[0]
    assert r.is_error is True
    assert r.color == "#cc3333"
    assert r.fields["run_of"] == "uart_stop_error"
    assert r.fields["count"] == N            # total, incl. the few shown individually
    assert r.fields["ongoing"] is True
    assert r.ts_start_ns < r.ts_end_ns       # the span bounds the storm
    # The run total is NOT lost — proto_decode_errs still tallies every error.
    assert src.read_stats()["proto_decode_errs"]["uart"] == N


def test_short_error_burst_stays_individual_no_run():
    """A handful of errors (≤ threshold) must NOT be collapsed — a one-off
    framing glitch stays visible one-for-one."""
    src, drain = _storm_harness()
    batch = drain(b"".join(_stop_err_frame(ts=1000 + i) for i in range(3)))
    assert len([a for a in batch.annotations if a.kind == "uart_stop_error"]) == 3
    assert [a for a in batch.annotations if a.kind == "error_run"] == []


def test_error_storm_finalizes_when_it_stops():
    """A storm that ends yields a final ongoing=False run event in the first
    batch with no new errors of that kind, then nothing further."""
    src, drain = _storm_harness()
    b1 = drain(b"".join(_stop_err_frame(ts=1000 + i * 10) for i in range(50)))
    r1 = [a for a in b1.annotations if a.kind == "error_run"]
    assert len(r1) == 1 and r1[0].fields["ongoing"] is True

    b2 = drain(_clean_uart_byte(ts=9000))   # no new stop-errors this batch
    r2 = [a for a in b2.annotations if a.kind == "error_run"]
    assert len(r2) == 1
    assert r2[0].fields["ongoing"] is False
    assert r2[0].fields["count"] == 50      # final total

    b3 = drain(_clean_uart_byte(ts=9100))   # run is cleared → no more run events
    assert [a for a in b3.annotations if a.kind == "error_run"] == []


# ── ErrorStormAggregator unit (pure logic, no ring) ─────────────────────────

def test_aggregator_keys_runs_by_channel_and_kind():
    agg = ErrorStormAggregator()
    T = ErrorStormAggregator.STORM_THRESHOLD
    # Two independent storms: same kind, different channels.
    for i in range(T + 5):
        assert agg.observe(channel_id=100, kind="uart_stop_error", ts_ns=10 + i) == (i < T)
    for i in range(T + 2):
        agg.observe(channel_id=101, kind="uart_stop_error", ts_ns=500 + i)
    events = {(e.channel_id, e.fields["run_of"]): e for e in agg.flush_batch()}
    assert (100, "uart_stop_error") in events
    assert (101, "uart_stop_error") in events
    assert events[(100, "uart_stop_error")].fields["count"] == T + 5
    assert events[(101, "uart_stop_error")].fields["count"] == T + 2
    # Span reflects first..last observation on that channel.
    assert events[(100, "uart_stop_error")].ts_start_ns == 10
    assert events[(100, "uart_stop_error")].ts_end_ns == 10 + (T + 4)


def test_aggregator_below_threshold_emits_no_run():
    agg = ErrorStormAggregator()
    for i in range(ErrorStormAggregator.STORM_THRESHOLD):
        assert agg.observe(channel_id=100, kind="i2c_nack_storm", ts_ns=i) is True
    assert agg.flush_batch() == []          # never crossed threshold → nothing


def test_aggregator_error_run_text_is_ascii_renderable():
    """DS-P3.127(b): a storm summary is often the ONLY is_error annotation a
    sustained run leaves on the stream (individuals are suppressed). Its text
    MUST be ASCII — the RouteWave font atlas can't render U+26A0 (warning) or
    U+00D7 (multiply), and an unrenderable glyph hides the evidence. This guard
    would fail against the pre-fix '! ... x' text built from those code points."""
    agg = ErrorStormAggregator()
    for i in range(ErrorStormAggregator.STORM_THRESHOLD + 3):
        agg.observe(channel_id=101, kind="i2c_byte", ts_ns=i)
    runs = agg.flush_batch()
    assert len(runs) == 1
    text = runs[0].text
    assert text.isascii(), f"non-ASCII glyph in storm summary: {text!r}"
    assert "⚠" not in text and "×" not in text
    assert "storm" in text and str(ErrorStormAggregator.STORM_THRESHOLD + 3) in text


# ── RollingDigest (DS-P3.89) ────────────────────────────────────────────────

from sw.stream.data_source import RollingDigest  # noqa: E402

_S = 1_000_000_000   # 1 second in ns


def test_digest_none_until_first_observation():
    """No traffic → no DIGEST (the heartbeat emits nothing for an idle source)."""
    assert RollingDigest().snapshot() is None


def test_digest_per_protocol_rate_and_error_pct():
    d = RollingDigest()
    # 100 UART pkts over ~9 s, 44 of them errors → 44.0% err, rate ≈ 100/9 ns-span.
    for i in range(100):
        d.observe("uart", is_error=(i < 44), ts_ns=i * (9 * _S // 99))
    snap = d.snapshot()
    u = snap["protocols"]["uart"]
    assert u["pkts"] == 100
    assert u["errs"] == 44
    assert u["err_pct"] == 44.0
    # span ≈ 9 s → rate ≈ 11.1 pkt/s (not the absurd value a ~0 span would give).
    assert 9.0 < u["pkt_rate"] < 13.0


def test_digest_critical_verdict_on_sustained_error_rate():
    # DS-P3.126: when the errors carried the HW/bus flag (hw_error=True), the
    # verdict names the physical fault (framing/baud for UART).
    d = RollingDigest()
    for i in range(100):
        d.observe("uart", is_error=(i < 44), ts_ns=i * (_S // 10),
                  kind="uart_stop_error", text="STOP ERR", hw_error=(i < 44))
    v = d.snapshot()["verdict"]
    assert v["status"] == "CRITICAL"
    assert "UART" in v["headline"] and "44%" in v["headline"]
    assert "framing/baud" in v["headline"]            # HW-dominated → physical hint
    a = v["anomalies"]
    assert len(a) == 1 and a[0]["protocol"] == "uart"
    assert a[0]["severity"] == "critical" and a[0]["value"] == 44.0
    assert a[0]["hw_errs"] == 44 and a[0]["decode_errs"] == 0


# ── DS-P3.126: HW-vs-decode verdict hint split ──────────────────────────────

def test_digest_hint_decode_dominated_when_hw_errors_zero():
    """The motivating bug: a 33% I2C error rate where the HW/bus error path is
    SILENT (hw_error=False on every error) must read as a DECODE/sampling issue,
    NOT 'addressing/ACK fault' (the old static hint). Fails against the pre-fix
    static _FAULT_HINT map which always said 'likely addressing/ACK fault'."""
    d = RollingDigest()
    for i in range(100):
        d.observe("i2c", is_error=(i < 33), ts_ns=i * (_S // 10),
                  kind="i2c_byte", text="0xNN NACK", hw_error=False)
    v = d.snapshot()["verdict"]
    assert v["status"] == "CRITICAL"
    assert "I2C" in v["headline"] and "33%" in v["headline"]
    assert "decode/sampling" in v["headline"]          # decode-dominated
    assert "addressing/ACK" not in v["headline"]        # NOT the HW hint
    a = v["anomalies"][0]
    assert a["hw_errs"] == 0 and a["decode_errs"] == 33


def test_digest_hint_hw_dominated_reads_as_ack_fault():
    """When the HW/bus error path raised most of the I2C errors, the verdict must
    name the physical fault — 'addressing/ACK fault' — not decode/sampling."""
    d = RollingDigest()
    for i in range(100):
        d.observe("i2c", is_error=(i < 33), ts_ns=i * (_S // 10),
                  kind="i2c_ack_error", text="ACK ERR", hw_error=(i < 33))
    v = d.snapshot()["verdict"]
    assert "addressing/ACK fault" in v["headline"]      # HW-dominated → physical
    assert "decode/sampling" not in v["headline"]
    a = v["anomalies"][0]
    assert a["hw_errs"] == 33 and a["decode_errs"] == 0


def test_digest_protocols_carry_hw_decode_split():
    """The per-protocol body surfaces the hw_errs / decode_errs breakdown so a
    consumer can SEE why the verdict picked its hint (mixed case here)."""
    d = RollingDigest()
    for i in range(100):
        is_err = i < 20
        d.observe("i2c", is_error=is_err, ts_ns=i * (_S // 10),
                  hw_error=(i < 8))   # 8 of the 20 errors are HW
    p = d.snapshot()["protocols"]["i2c"]
    assert p["errs"] == 20 and p["hw_errs"] == 8 and p["decode_errs"] == 12


def test_digest_warning_band_between_thresholds():
    d = RollingDigest()
    # 15% error rate → above WARN (10), below CRIT (25) → 'warning', status WARN.
    for i in range(100):
        d.observe("i2c", is_error=(i < 15), ts_ns=i * (_S // 10))
    v = d.snapshot()["verdict"]
    assert v["status"] == "WARN"
    assert v["anomalies"][0]["severity"] == "warning"


def test_digest_clean_traffic_is_ok_no_anomalies():
    d = RollingDigest()
    for i in range(50):
        d.observe("spi", is_error=False, ts_ns=i * (_S // 10))
    v = d.snapshot()["verdict"]
    assert v["status"] == "OK"
    assert v["anomalies"] == []
    assert "nominal" in v["headline"]


def test_digest_below_min_samples_emits_no_verdict():
    """A couple of errors out of a couple of packets must NOT trip a verdict —
    too little evidence (the MIN_SAMPLES gate)."""
    d = RollingDigest()
    for i in range(RollingDigest.MIN_SAMPLES - 1):
        d.observe("uart", is_error=True, ts_ns=i)   # 100% err but < MIN_SAMPLES
    v = d.snapshot()["verdict"]
    assert v["status"] == "OK"
    assert v["anomalies"] == []


def test_digest_window_trims_old_events():
    d = RollingDigest(window_s=10.0)
    # Old burst at t=0..1s, then fresh traffic 100s later → old events fall out.
    for i in range(40):
        d.observe("uart", is_error=True, ts_ns=i * (_S // 40))
    for i in range(10):
        d.observe("uart", is_error=False, ts_ns=100 * _S + i * (_S // 10))
    u = d.snapshot()["protocols"]["uart"]
    assert u["pkts"] == 10            # the 40 old ones aged out of the 10 s window
    assert u["errs"] == 0
    assert d.snapshot()["verdict"]["status"] == "OK"


def test_digest_recent_tail_holds_last_n_errors():
    d = RollingDigest()
    for i in range(20):
        d.observe("uart", is_error=True, ts_ns=i * (_S // 100),
                  kind="uart_stop_error", text=f"err{i}")
    recent = d.snapshot()["recent"]
    assert len(recent) == RollingDigest.RECENT_N      # capped tail
    assert recent[-1]["text"] == "err19"              # newest last
    assert all(e["is_error"] for e in recent)


def test_digest_integration_through_ddr_source():
    """End-to-end: drive a UART STOP-ERROR storm through the ring source and read
    the digest off read_digest() — the storm-suppressed individuals still count
    toward the digest's per-protocol rate/error% (digest is fed before the storm
    gate)."""
    src, drain = _storm_harness()
    N = 50
    # ts is a PL tick count (10 ns/tick @ 100 MHz) — keep the whole storm well
    # inside the 10 s digest window: 50 frames × 100k ticks = 50 ms span.
    drain(b"".join(_stop_err_frame(ts=1000 + i * 100_000) for i in range(N)))
    snap = src.read_digest()
    assert snap is not None
    u = snap["protocols"]["uart"]
    assert u["pkts"] == N and u["errs"] == N
    assert u["err_pct"] == 100.0
    assert snap["verdict"]["status"] == "CRITICAL"
    # read_digest() returns the BODY only; seq + schema are added by make_digest
    # on the server's heartbeat. make_digest(**body) must accept it cleanly.
    from sw.stream import protocol as proto
    framed = proto.make_digest(7, **snap)
    assert framed["seq"] == 7 and framed["schema"] == "wave_stream_digest_v1"
    assert framed["verdict"]["status"] == "CRITICAL"


# ── DS-P3.127: register-less I2C read decode-path reproduction ───────────────
# The motivating field evidence: a continuous `i2cget -y 0 0x48` (register-less
# single-byte read) to the ACKing i2c_injector at 0x48 produced 826 START + 826
# STOP but only 46 i2c_byte annotations (~94% of bytes missing) AND a climbing
# proto_decode_errs.i2c with NO is_error i2c annotation reaching the stream.
#
# These tests PROVE the SW decode path is NOT the byte-loss culprit: when the
# byte FRAMES are present in the ring, the decoder turns every one into a visible
# i2c_byte annotation. (The byte loss is a HW datalogger back-pressure defect — the
# i2c_datalogger payload-valid is not held until I2C_MSG_RDY, see the DS RTL
# sub-ticket. Nothing the SW daemon can do recovers a frame the HW never wrote.)

from sw.stream import protocol_codes as _PC  # noqa: E402


def _i2c_event_frame(event_name: str, *, datalogger_id: int = 4, ts: int = 0,
                     severity: int = zs_msg.SEV_INFO) -> bytes:
    """An I2C lifecycle/boundary event frame (START / STOP / *_error)."""
    code = next(c for c, n in _PC.I2C_EVENTS.items() if n == event_name)
    return _build_frame(datalogger_id=datalogger_id, domain=zs_msg.DOMAIN_I2C,
                        event_id=_PC.I2C_TYPES["event"], severity=severity,
                        payload=bytes([code, 0, 0, 0]), timestamp=ts)


def _i2c_byte_frame(data: int, *, ack: bool, datalogger_id: int = 4,
                    ts: int = 0) -> bytes:
    """An I2C payload byte frame; event_id low bit = ack_nack (0=ACK, 1=NACK)."""
    et = (_PC.I2C_TYPES["payload"] << 1) | (0 if ack else 1)
    return _build_frame(datalogger_id=datalogger_id, domain=zs_msg.DOMAIN_I2C,
                        event_id=et, severity=zs_msg.SEV_INFO,
                        payload=bytes([data, 0, 0, 0]), timestamp=ts)


def test_i2c_registerless_read_byte_decodes_through_source(regbank, ddr):
    """A register-less read = START, address byte (ACK), data byte (master NACK),
    STOP. When all four frames are present, the decode path MUST surface the two
    byte annotations (address + data) byte-exact — i.e. the SW decoder does not
    drop bytes. This is the offline reproduction asked for by DS-P3.127(a)."""
    src = _make_source(regbank, ddr)
    src.next_batch()  # seed pointer
    blob = (
        _i2c_event_frame("start", ts=1)
        + _i2c_byte_frame(0x91, ack=True, ts=2)   # 0x48<<1 | R, slave ACKs
        + _i2c_byte_frame(0xAB, ack=False, ts=3)  # data byte, master NACKs (read end)
        + _i2c_event_frame("stop", ts=4)
    )
    ddr.write_bytes(0, blob)
    regbank.set(Regs.RING_WRITE_PTR, (len(blob) + 3) // 4)

    anns = src.next_batch().annotations
    kinds = [a.kind for a in anns]
    # Both bytes decoded — NO byte loss in SW.
    bytes_seen = [a for a in anns if a.kind == "i2c_byte"]
    assert len(bytes_seen) == 2, f"expected 2 byte annotations, got {kinds}"
    assert "0x91" in bytes_seen[0].text and "ACK" in bytes_seen[0].text
    assert "0xAB" in bytes_seen[1].text and "NACK" in bytes_seen[1].text
    # START + STOP also present (proving the byte sits BETWEEN them).
    assert any("start" in k for k in kinds) and any("stop" in k for k in kinds)


def test_i2c_annotations_distinguish_cam_a_and_cam_b_by_originating_id(regbank, ddr):
    """DS-P3.155: When concurrent I2C frames from datalogger #4 (cam B) and #5 (cam A)
    land on channel 101, their annotation fields MUST carry datalogger_id
    so RouteWave / consumers can filter or separate cam A vs cam B config dialogues.

    Without this, both collapse onto channel 101 with identical fields={data, ack},
    making the stereo-vision dialogues indistinguishable."""
    src = _make_source(regbank, ddr)
    src.next_batch()
    blob = (
        _i2c_event_frame("start", datalogger_id=4, ts=1)
        + _i2c_byte_frame(0x3C, ack=True, datalogger_id=4, ts=2)   # Cam B config
        + _i2c_event_frame("start", datalogger_id=5, ts=3)
        + _i2c_byte_frame(0x3C, ack=True, datalogger_id=5, ts=4)   # Cam A config
    )
    ddr.write_bytes(0, blob)
    regbank.set(Regs.RING_WRITE_PTR, (len(blob) + 3) // 4)

    anns = src.next_batch().annotations
    assert len(anns) == 4

    # Cam B frames carry datalogger_id=4
    assert anns[0].fields == {"event": "start", "datalogger_id": 4}
    assert anns[1].fields == {"data": 0x3C, "ack": True, "datalogger_id": 4}

    # Cam A frames carry datalogger_id=5
    assert anns[2].fields == {"event": "start", "datalogger_id": 5}
    assert anns[3].fields == {"data": 0x3C, "ack": True, "datalogger_id": 5}

    # The two byte annotations have different originating IDs despite same byte payload
    assert anns[1].fields["datalogger_id"] == 4
    assert anns[3].fields["datalogger_id"] == 5


def test_dual_datalogger_i2c_renders_distinct_channel_lanes(regbank, ddr):
    """DS-P3.155: When concurrent I2C frames from datalogger #4 (cam B) and #5 (cam A)
    are processed, they MUST map to distinct per-datalogger channel lanes (101 and 109)
    so RouteWave / consumers render them as separate lanes instead of collapsing.
    A headless assertion on the channel table verifies both lanes render distinctly.
    """
    src = _make_source(regbank, ddr)
    src.next_batch()

    blob = (
        _i2c_event_frame("start", datalogger_id=4, ts=1)
        + _i2c_byte_frame(0x3C, ack=True, datalogger_id=4, ts=2)   # Cam B config
        + _i2c_event_frame("start", datalogger_id=5, ts=3)
        + _i2c_byte_frame(0x3C, ack=True, datalogger_id=5, ts=4)   # Cam A config
    )
    ddr.write_bytes(0, blob)
    regbank.set(Regs.RING_WRITE_PTR, (len(blob) + 3) // 4)

    batch = src.next_batch()
    assert len(batch.annotations) == 4

    # Distinct channel lanes: datalogger #4 lands on 101, datalogger #5 lands on 109
    assert batch.annotations[0].channel_id == 101
    assert batch.annotations[1].channel_id == 101
    assert batch.annotations[2].channel_id == 109
    assert batch.annotations[3].channel_id == 109

    # Headless assertion on the channel table:
    # Resolving channel_id through the HELO channel table renders two distinct I2C lanes
    from sw.stream import channels as ch
    from sw.stream import protocol as proto
    helo = proto.make_helo(sample_clk_hz=100_000_000, channels=ch.CHANNELS,
                           channel_total_bits=ch.CHANNEL_TOTAL_BITS)
    channel_table = {c["id"]: c["name"] for c in helo["channels"]}
    rendered_lanes = [channel_table[a.channel_id] for a in batch.annotations]
    assert rendered_lanes == ["i2c_frame:4", "i2c_frame:4", "i2c_frame:5", "i2c_frame:5"]
    assert set(rendered_lanes) == {"i2c_frame:4", "i2c_frame:5"}


def test_single_datalogger_i2c_capture_unchanged(regbank, ddr):
    """DS-P3.155: Single-id captures (only datalogger #4) remain on channel 101,
    rendering exactly one I2C lane in the channel table."""
    src = _make_source(regbank, ddr)
    src.next_batch()

    blob = (
        _i2c_event_frame("start", datalogger_id=4, ts=1)
        + _i2c_byte_frame(0x3C, ack=True, datalogger_id=4, ts=2)
    )
    ddr.write_bytes(0, blob)
    regbank.set(Regs.RING_WRITE_PTR, (len(blob) + 3) // 4)

    batch = src.next_batch()
    assert len(batch.annotations) == 2
    assert all(a.channel_id == 101 for a in batch.annotations)

    from sw.stream import channels as ch
    from sw.stream import protocol as proto
    helo = proto.make_helo(sample_clk_hz=100_000_000, channels=ch.CHANNELS,
                           channel_total_bits=ch.CHANNEL_TOTAL_BITS)
    channel_table = {c["id"]: c["name"] for c in helo["channels"]}
    rendered_lanes = {channel_table[a.channel_id] for a in batch.annotations}
    assert len(rendered_lanes) == 1
    assert "i2c_frame:4" in rendered_lanes




# ── DS-P3.127(b): decode failures must be OBSERVABLE as is_error annotations ──

def test_i2c_decode_failure_emits_is_error_annotation(regbank, ddr):
    """A frame the decoder flags as an error (here a short/truncated I2C payload)
    must produce an is_error annotation on the stream — not just bump the
    proto_decode_errs counter. The field bug was a climbing decode-error count
    with NO visible error annotation, leaving RouteWave/agents blind to WHAT
    failed. Guards the observability contract."""
    src = _make_source(regbank, ddr)
    src.next_batch()
    # Truncated I2C payload (0 bytes) → decode_i2c returns i2c_short, is_error.
    frame = _build_frame(datalogger_id=4, domain=zs_msg.DOMAIN_I2C,
                         event_id=_PC.I2C_TYPES["payload"] << 1,
                         severity=zs_msg.SEV_INFO, payload=b"")
    ddr.write_bytes(0, frame)
    regbank.set(Regs.RING_WRITE_PTR, (len(frame) + 3) // 4)

    anns = src.next_batch().annotations
    errs = [a for a in anns if a.is_error]
    assert errs, "a decode failure must surface at least one is_error annotation"
    assert any("i2c" in a.kind for a in errs)
    # And the decode-error tally moved in lock-step with the visible annotation.
    assert src.read_stats()["proto_decode_errs"]["i2c"] == 1


def test_i2c_decode_error_count_matches_visible_or_summarized(regbank, ddr):
    """No silent decode errors: every increment of proto_decode_errs.i2c must be
    accompanied by an observable is_error annotation OR a storm error_run summary
    (so the increment is never invisible). Sparse errors (below the storm
    threshold) render individually; this asserts the count == visible errors."""
    src = _make_source(regbank, ddr)
    src.next_batch()
    # 3 NACK data bytes (each is_error per the decoder) — well under the storm
    # threshold, so each must render as its own is_error annotation.
    blob = b"".join(_i2c_byte_frame(0x10 + i, ack=False, ts=i) for i in range(3))
    ddr.write_bytes(0, blob)
    regbank.set(Regs.RING_WRITE_PTR, (len(blob) + 3) // 4)

    anns = src.next_batch().annotations
    visible_errs = [a for a in anns if a.is_error]
    summary_runs = [a for a in anns if a.kind == "error_run"]
    count = src.read_stats()["proto_decode_errs"]["i2c"]
    assert count == 3
    # Every counted error is observable: individual annotations + any storm
    # summary together cover the count.
    summarized = sum(r.fields.get("count", 0) for r in summary_runs)
    assert len(visible_errs) + (summarized - len(summary_runs)) >= count
    assert len(visible_errs) == 3   # sparse → all individual


# ── LossProvenanceWatcher (DS-P3.70) ─────────────────────────────────────────

from sw.stream.data_source import LossProvenanceWatcher  # noqa: E402


def test_loss_watcher_first_poll_seeds_no_emit():
    rb = _MirrorRegbank()
    rb.set(Regs.ERR_CNT_EDGE, 100)        # a non-zero counter at startup
    assert LossProvenanceWatcher().poll(rb, ts_ns=1000, window_ns=50) == []


def test_loss_watcher_emits_on_rising_edge_internal_overflow():
    rb = _MirrorRegbank()
    w = LossProvenanceWatcher()
    rb.set(Regs.ERR_CNT_EDGE, 100)
    w.poll(rb, 1000, 50)                   # seed at 100
    rb.set(Regs.ERR_CNT_EDGE, 137)         # +37 edges lost in the internal FIFO
    anns = w.poll(rb, 2000, 50)
    assert len(anns) == 1
    a = anns[0]
    assert a.kind == "loss" and a.is_error is True
    assert a.ts_start_ns == 2000
    assert a.fields["loss_stage"] == "internal_fifo_overflow"
    assert a.fields["provenance_code"] == 0xF0
    assert a.fields["lost_count"] == 37
    assert a.fields["domain"] == "edge"
    assert a.fields["in_band"] is False    # out-of-band (polled) path


def test_loss_watcher_distinguishes_stages_and_domains():
    rb = _MirrorRegbank()
    w = LossProvenanceWatcher()
    w.poll(rb, 0, 50)                      # seed all at 0
    rb.set(Regs.DROP_CNT_UART, 3)          # arbiter dropped 3 UART frames
    rb.set(Regs.DDR_BACKPRESSURE_CNT, 7)   # ring-full backpressure
    by_stage = {a.fields["loss_stage"]: a for a in w.poll(rb, 100, 50)}
    assert by_stage["arbiter_drop"].fields["domain"] == "uart"
    assert by_stage["arbiter_drop"].fields["lost_count"] == 3
    assert by_stage["ring_full"].fields["lost_count"] == 7
    assert by_stage["ring_full"].fields["provenance_code"] == 0xF2
    # ring-full is the out-of-band-ONLY class (can't ride a full ring, by construction)
    assert by_stage["ring_full"].fields["in_band"] is False


def test_loss_watcher_ignores_counter_reset_no_phantom_loss():
    """A counter reset (bitstream reload → value drops to ~0) masks to a huge
    uint32 delta — must NOT report billions of phantom losses."""
    rb = _MirrorRegbank()
    w = LossProvenanceWatcher()
    rb.set(Regs.DROP_CNT_UART, 5000)
    w.poll(rb, 0, 50)                      # seed at 5000
    rb.set(Regs.DROP_CNT_UART, 0)          # reset
    assert w.poll(rb, 100, 50) == []       # no phantom loss
    rb.set(Regs.DROP_CNT_UART, 4)          # re-seeded at 0 → a real +4 now emits
    anns = w.poll(rb, 200, 50)
    assert len(anns) == 1 and anns[0].fields["lost_count"] == 4


def test_loss_watcher_integrated_through_next_batch():
    ddr = FakeDdrMmap(8192)
    regbank = _MirrorRegbank()
    src = _make_source(regbank, ddr)
    src.next_batch()                       # tick 1: seeds ring ptrs + loss baseline
    regbank.set(Regs.ERR_CNT_EDGE, 9)      # 9 edges lost upstream of the gather
    batch = src.next_batch()               # tick 2: idle ring, but the loss surfaces
    losses = [a for a in batch.annotations if a.kind == "loss"]
    assert len(losses) == 1
    assert losses[0].fields["loss_stage"] == "internal_fifo_overflow"
    assert losses[0].fields["lost_count"] == 9
    assert losses[0].fields["in_band"] is False


# ── In-band SEV_ERROR provenance decode (DS-P3.70) ───────────────────────────

def test_in_band_loss_frame_decodes_with_provenance_fields():
    """A SEV_ERROR | DOMAIN_EDGE | ERR_EVT_INTERNAL_FIFO_OVERFLOW frame
    (payload = lost_count LE) decodes to the SAME 'loss' shape as the watcher,
    tagged in_band=True (the precise PL-timestamped path)."""
    import struct
    from sw.stream import decoders
    hdr = zs_msg.make_header(
        datalogger_id=12, domain=zs_msg.DOMAIN_EDGE,
        event_id=zs_msg.ERR_EVT_INTERNAL_FIFO_OVERFLOW,
        severity=zs_msg.SEV_ERROR, timestamp=0, payload_len=4)
    df = decoders.decode_frame(hdr, struct.pack("<I", 37), 100_000_000)
    assert df is not None
    assert df.kind == "loss" and df.is_error is True
    assert df.fields["loss_stage"] == "internal_fifo_overflow"
    assert df.fields["provenance_code"] == 0xF0
    assert df.fields["lost_count"] == 37
    assert df.fields["domain"] == "edge"
    assert df.fields["in_band"] is True
