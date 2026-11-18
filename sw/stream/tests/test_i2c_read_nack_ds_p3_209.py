"""DS-P3.209: the master's NACK that ends an I2C read is not an error.

Measured 2026-11-06 on the Zybo (canfd_demo from 97303610): 200 i2cget reads,
200/200 answered, and the digest reported "errs 200, 14%, WARN: likely
decode/sampling issue (HW bus clean)": every read's final "0xFF NACK" byte was
decoded as an error. In a read the ACK bit after a data byte is the master's,
and the master NACKs the last byte it wants. The decoder now tracks each
datalogger's phase (START / REP-START, then the address byte's R/W bit).
"""
from __future__ import annotations

import pytest

from sw.stream import decoders, zs_msg
from sw.stream import protocol_codes as _PC

_EVT = {v: k for k, v in _PC.I2C_EVENTS.items()}


@pytest.fixture(autouse=True)
def _fresh_i2c_phase():
    decoders.reset_i2c_state()
    yield
    decoders.reset_i2c_state()


def _event(name: str, dl: int = 4):
    hdr = zs_msg.make_header(datalogger_id=dl, domain=zs_msg.DOMAIN_I2C,
                             event_id=_PC.I2C_TYPES["event"], severity=zs_msg.SEV_INFO,
                             payload_len=4)
    return decoders.decode_i2c(hdr, bytes([_EVT[name], 0, 0, 0]), 100_000_000)


def _byte(data: int, ack: bool, dl: int = 4):
    et = (_PC.I2C_TYPES["payload"] << 1) | (0 if ack else 1)
    hdr = zs_msg.make_header(datalogger_id=dl, domain=zs_msg.DOMAIN_I2C, event_id=et,
                             severity=zs_msg.SEV_INFO, payload_len=4)
    return decoders.decode_i2c(hdr, bytes([data, 0, 0, 0]), 100_000_000)


def test_an_i2cget_carries_no_error():
    """i2cget -y 0 0x50 0x00: write the register, repeated start, read one byte."""
    seq = [_event("start"), _byte(0xA0, True), _byte(0x00, True),
           _event("repeated_start"), _byte(0xA1, True), _byte(0xFF, False), _event("stop")]
    assert [f.is_error for f in seq] == [False] * 7
    assert seq[5].text == "0xFF NACK" and seq[5].fields == {"data": 0xFF, "ack": False,
                                                            "datalogger_id": 4}


def test_a_multi_byte_read_ends_on_the_master_nack():
    seq = [_event("start"), _byte(0xA1, True), _byte(0x11, True), _byte(0x22, True),
           _byte(0x33, False), _event("stop")]
    assert not any(f.is_error for f in seq)


@pytest.mark.parametrize("seq_builder,bad", [
    (lambda: [_event("start"), _byte(0xA1, False)], 1),          # address NACK: no device
    (lambda: [_event("start"), _byte(0xA0, True), _byte(0x55, False)], 2),  # write refused
    (lambda: [_byte(0xFF, False)], 0),                            # phase unknown
    (lambda: [_event("start"), _byte(0xA1, True), _event("stop"), _byte(0xFF, False)], 3),
])
def test_a_slave_nack_or_an_unknown_phase_stays_an_error(seq_builder, bad):
    seq = seq_builder()
    assert seq[bad].is_error is True
    assert not any(f.is_error for i, f in enumerate(seq) if i != bad)


def test_the_phase_is_per_datalogger():
    """Multi-instance I2C: a read on #4 does not excuse a refused write on #5."""
    _event("start", dl=4); _byte(0xA1, True, dl=4)
    _event("start", dl=5); _byte(0xA0, True, dl=5)
    assert _byte(0xFF, False, dl=4).is_error is False
    assert _byte(0x55, False, dl=5).is_error is True
