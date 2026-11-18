"""Unit tests for RegbankDataSource against the FakeRegbank fixture.

Phase A coverage of DS-P2.31. Validates that:
  - Every tick latches the snapshot trigger.
  - Heartbeat annotations are always emitted (proves the MMIO loop is live).
  - PKT_CNT_* / ERR_CNT_* deltas turn into annotations on the right
    decoder channel (uart_byte=100 / i2c_frame=101 / qspi_xact=102 /
    protocol_stats=202).
  - FIFO overflow stickies surface as alarms on fifo_overflow=201.
  - No samples emitted in Phase A (raw-sample streaming is Phase B).
"""

from __future__ import annotations

import itertools

import pytest

from sw.stream.data_source import RegbankDataSource
from sw.stream.regbank import FakeRegbank, Regs


def _fake_clock(values):
    """Build a clock callable that yields values sequentially."""
    it = iter(values)

    def clock():
        return next(it)
    return clock


@pytest.fixture
def fake():
    return FakeRegbank()


@pytest.fixture
def source(fake):
    # Deterministic clock: tick 0 at t=0.0, then +poll_period_s on each
    # subsequent call. RegbankDataSource calls _clock() three times per
    # next_batch (duration check, pace check, timestamp); supply plenty.
    times = itertools.chain([0.0], itertools.count(start=0.05, step=0.05))
    src = RegbankDataSource(
        regbank=fake,
        sample_clk_hz=100_000_000,
        poll_period_s=0.05,
        duration_s=0.0,
        snapshot_trigger=True,
        clock=lambda: next(times),
    )
    return src


def test_sample_clk_hz_reported(source):
    assert source.sample_clk_hz() == 100_000_000


def test_each_tick_pulses_snapshot_trigger(source, fake):
    source.next_batch()
    source.next_batch()
    snapshots = [w for w in fake.writes if w[0] == Regs.TELEMETRY_SNAPSHOT_TRIGGER]
    assert snapshots == [(Regs.TELEMETRY_SNAPSHOT_TRIGGER, 1)] * 2


def test_snapshot_trigger_disabled(fake):
    times = itertools.chain([0.0], itertools.count(start=0.05, step=0.05))
    src = RegbankDataSource(
        regbank=fake, poll_period_s=0.05, snapshot_trigger=False,
        clock=lambda: next(times),
    )
    src.next_batch()
    assert fake.writes == []


def test_heartbeat_annotation_always_emitted(source, fake):
    fake.set(Regs.HEARTBEAT_CNT, 0xDEAD_BEEF)
    batch = source.next_batch()
    assert batch is not None
    hb = [a for a in batch.annotations if a.kind == "pl_heartbeat"]
    assert len(hb) == 1
    assert hb[0].channel_id == 200
    assert "deadbeef" in hb[0].text.lower()
    assert hb[0].is_error is False


def test_no_samples_phase_a(source):
    batch = source.next_batch()
    assert batch is not None
    assert batch.samples == []


def test_first_tick_no_protocol_annotations(source, fake):
    """First tick seeds prev_counters — no delta annotations on tick 1
    even if counters are nonzero (delta = current - prev = 0)."""
    fake.set(Regs.PKT_CNT_UART, 100)
    fake.set(Regs.PKT_CNT_I2C, 50)
    batch = source.next_batch()
    kinds = {a.kind for a in batch.annotations}
    assert "uart_pkt_count" not in kinds
    assert "i2c_pkt_count" not in kinds


def test_uart_pkt_delta_emits_on_uart_byte_channel(source, fake):
    source.next_batch()  # seed prev
    fake.set(Regs.PKT_CNT_UART, 7)
    batch = source.next_batch()
    matches = [a for a in batch.annotations if a.kind == "uart_pkt_count"]
    assert len(matches) == 1
    assert matches[0].channel_id == 100  # uart_byte
    assert "+7 pkts" in matches[0].text


def test_i2c_pkt_delta_emits_on_i2c_frame_channel(source, fake):
    source.next_batch()
    fake.set(Regs.PKT_CNT_I2C, 3)
    batch = source.next_batch()
    matches = [a for a in batch.annotations if a.kind == "i2c_pkt_count"]
    assert len(matches) == 1
    assert matches[0].channel_id == 101  # i2c_frame


def test_qspi_pkt_delta_emits_on_qspi_xact_channel(source, fake):
    source.next_batch()
    fake.set(Regs.PKT_CNT_QSPI, 11)
    batch = source.next_batch()
    matches = [a for a in batch.annotations if a.kind == "qspi_pkt_count"]
    assert len(matches) == 1
    assert matches[0].channel_id == 102  # qspi_xact


def test_spi_and_edge_share_protocol_stats_channel(source, fake):
    source.next_batch()
    fake.set(Regs.PKT_CNT_SPI, 2)
    fake.set(Regs.PKT_CNT_EDGE, 9)
    batch = source.next_batch()
    spi = [a for a in batch.annotations if a.kind == "spi_pkt_count"]
    edge = [a for a in batch.annotations if a.kind == "edge_pkt_count"]
    assert len(spi) == 1 and spi[0].channel_id == 202
    assert len(edge) == 1 and edge[0].channel_id == 202


def test_err_delta_is_error_true_and_red(source, fake):
    source.next_batch()
    fake.set(Regs.ERR_CNT_UART, 4)
    batch = source.next_batch()
    errs = [a for a in batch.annotations if a.kind == "uart_err"]
    assert len(errs) == 1
    assert errs[0].is_error is True
    assert errs[0].color == "#cc3333"
    assert "+4 errors" in errs[0].text


def test_fifo_overflow_emits_alarm(source, fake):
    # DS-P3.94 ph3: single-tier scalar (Stream tier dead, D.4). The one real
    # FIFO_OVERFLOW_CNT drives a "fifo_overflow" alarm.
    source.next_batch()
    fake.set(Regs.FIFO_OVERFLOW_CNT, 1)
    batch = source.next_batch()
    alarms = [a for a in batch.annotations if a.kind == "fifo_overflow"]
    assert len(alarms) == 1
    assert alarms[0].channel_id == 201  # fifo_overflow
    assert alarms[0].is_error is True
    assert "FIFO overflow +1" in alarms[0].text


def test_dead_stream_rsvd_slot_emits_no_alarm(source, fake):
    # DS-P3.94 ph3: the *_RSVD (ex-"Stream") FIFO slot is dead (D.4) and must be
    # ignored entirely — a write to it produces NO annotation. Guards against the
    # dead tier ever leaking a false alarm back into the stream.
    source.next_batch()
    fake.set(Regs.FIFO_OVERFLOW_CNT_RSVD, 5)
    batch = source.next_batch()
    alarms = [a for a in batch.annotations if "fifo_overflow" in a.kind]
    assert alarms == []


def test_counter_wraps_clamped_to_uint32(source, fake):
    """A 32-bit counter that wraps past 0xFFFFFFFF still yields a sane delta."""
    source.next_batch()
    # Prev = 0; set current to 0xFFFFFFFE → delta 0xFFFFFFFE; both bounded.
    fake.set(Regs.PKT_CNT_UART, 0xFFFFFFFE)
    batch = source.next_batch()
    matches = [a for a in batch.annotations if a.kind == "uart_pkt_count"]
    assert matches[0].text == "+4294967294 pkts"


def test_duration_expiry_returns_none():
    rb = FakeRegbank()
    # First two calls land inside the budget, third call exceeds it.
    times = iter([0.0, 0.05, 0.1, 1.0, 1.0])
    src = RegbankDataSource(
        regbank=rb, poll_period_s=0.05, duration_s=0.5,
        clock=lambda: next(times),
    )
    assert src.next_batch() is not None
    assert src.next_batch() is None  # 1.0 > 0.5 budget


def test_no_protocol_traffic_emits_only_heartbeat(source):
    batch = source.next_batch()
    assert batch is not None
    kinds = [a.kind for a in batch.annotations]
    assert kinds == ["pl_heartbeat"]


def test_close_propagates_to_regbank(fake):
    src = RegbankDataSource(regbank=fake)
    src.close()
    assert fake.closed is True


# ── DS-P3.72: CONTROL apply (consumer→producer datalogger config) ──────────────

from sw.stream.data_source import apply_datalogger_config, StubDataSource  # noqa: E402


def test_apply_datalogger_config_writes_broadcast_word():
    rb = FakeRegbank()
    assert apply_datalogger_config(rb, 2, 0x9) is None
    # DATALOGGER_CONFIG = [device_id(5b) | config(11b)] = (2<<11)|9
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == (2 << 11) | 9


def test_apply_datalogger_config_rejects_and_writes_nothing():
    rb = FakeRegbank()
    assert apply_datalogger_config(rb, 99, 0x1)            # id out of 5-bit range
    assert apply_datalogger_config(rb, 2, 0x8000)          # bits ≥11 = device-id field, not config
    assert apply_datalogger_config(rb, 2, 0x1, op="arm")   # unsupported op
    assert rb.read_u32(Regs.DATALOGGER_CONFIG) == 0        # no write on any reject


def test_regbank_source_apply_config_delegates(source, fake):
    assert source.apply_config(5, 0x9) is None
    assert fake.read_u32(Regs.DATALOGGER_CONFIG) == (5 << 11) | 9


def test_stub_source_rejects_control():
    assert StubDataSource().apply_config(2, 0x9) is not None


# ── DS-P3.108b: CAN-FD data-prescaler BANKED config (one id + type bit) ─────
def test_set_can_data_prescaler_writes_banked_config(fake):
    from sw.stream.data_source import set_can_data_prescaler, CAN_DEV_ID, CAN_CFG_TYPE_DATA
    assert set_can_data_prescaler(fake, 30) is None
    # DATALOGGER_CONFIG = [device_id=20 | type_bit=1 | data_cyc=30] — same id, data bank
    assert fake.read_u32(Regs.DATALOGGER_CONFIG) == (CAN_DEV_ID << 11) | CAN_CFG_TYPE_DATA | 30


def test_set_can_data_prescaler_range_check(fake):
    from sw.stream.data_source import set_can_data_prescaler
    assert set_can_data_prescaler(fake, 0) is not None        # 0 reserved (= power-on default)
    assert set_can_data_prescaler(fake, 1024) is not None     # > 10-bit data field
    assert set_can_data_prescaler(fake, 512) is not None      # DS-T2.97: [9] selects a SP sub-word
    assert set_can_data_prescaler(fake, 511) is None          # largest data_cyc
    assert set_can_data_prescaler(fake, 20) is None           # 5 Mbit @ 100 MHz, valid


# ── DS-T2.97: bus sample points, bank-1 sub-words, data_cyc written last ────
class _Recorder:
    def __init__(self):
        self.words = []

    def write_u32(self, off, val):
        if off == Regs.DATALOGGER_CONFIG:
            self.words.append(val)

    def read_u32(self, off):
        return self.words[-1] if self.words else 0


def test_set_can_sample_points_writes_sp_then_data_cyc():
    from sw.stream.data_source import set_can_sample_points
    rec = _Recorder()
    assert set_can_sample_points(rec, 224, 189, 50) is None
    # [id 20 | bank 1 | [9]=1 [8]=0 | 224], [... [8]=1 | 189], then [... [9]=0 | 50]
    assert rec.words[-3:] == [(20 << 11) | 0x400 | 0x200 | 224,
                              (20 << 11) | 0x400 | 0x200 | 0x100 | 189,
                              (20 << 11) | 0x400 | 50]


def test_set_can_sample_points_rejects_before_any_write():
    from sw.stream.data_source import set_can_sample_points
    for args in ((0, 189, 50), (256, 189, 50), (224, 0, 50), (224, 256, 50), (224, 189, 512)):
        rec = _Recorder()
        assert set_can_sample_points(rec, *args) is not None, args
        assert rec.words == [], f"{args}: wrote {rec.words} before rejecting"
