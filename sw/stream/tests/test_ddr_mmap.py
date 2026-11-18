"""Unit tests for FakeDdrMmap (the test-friendly half of ddr_mmap.py)."""

from __future__ import annotations

import pytest

from sw.stream.ddr_mmap import FakeDdrMmap


def test_initial_buffer_is_zero():
    mm = FakeDdrMmap(64)
    assert mm.read_bytes(0, 64) == b"\x00" * 64


def test_initial_buffer_seeded():
    mm = FakeDdrMmap(64, initial=b"\xAB" * 32)
    assert mm.read_bytes(0, 32) == b"\xAB" * 32
    assert mm.read_bytes(32, 32) == b"\x00" * 32


def test_write_then_read_roundtrip():
    mm = FakeDdrMmap(64)
    mm.write_bytes(0, b"hello, world")
    assert mm.read_bytes(0, 12) == b"hello, world"


def test_read_wraps_at_ring_boundary():
    mm = FakeDdrMmap(16, initial=b"0123456789ABCDEF")
    # offset 12, length 8 → wraps: tail 'CDEF' + head '0123'
    assert mm.read_bytes(12, 8) == b"CDEF0123"


def test_write_wraps_at_ring_boundary():
    mm = FakeDdrMmap(16)
    mm.write_bytes(14, b"ABCDEF")
    # 'AB' at offsets 14-15; 'CDEF' wraps to offsets 0-3.
    assert mm.read_bytes(0, 16) == b"CDEF" + b"\x00" * 10 + b"AB"


def test_read_zero_length_returns_empty():
    mm = FakeDdrMmap(16)
    assert mm.read_bytes(5, 0) == b""


def test_read_larger_than_ring_raises():
    mm = FakeDdrMmap(16)
    with pytest.raises(ValueError, match="ring size"):
        mm.read_bytes(0, 32)


def test_size_returns_ring_size():
    mm = FakeDdrMmap(256)
    assert mm.size() == 256


def test_close_sets_flag():
    mm = FakeDdrMmap(16)
    assert mm.closed is False
    mm.close()
    assert mm.closed is True


def test_zero_size_rejected():
    with pytest.raises(ValueError, match=">"):
        FakeDdrMmap(0)


def test_offset_modulo_wraps():
    """offset >= ring size is silently wrapped — convenient for callers
    that maintain a free-running counter."""
    mm = FakeDdrMmap(16, initial=b"0123456789ABCDEF")
    assert mm.read_bytes(16, 4) == b"0123"  # offset 16 wraps to 0
    assert mm.read_bytes(20, 4) == b"4567"  # offset 20 wraps to 4
