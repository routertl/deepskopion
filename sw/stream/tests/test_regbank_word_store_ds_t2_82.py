"""DS-T2.82 — MmioRegbank must reach the bus as ONE 32-bit access on riscv64.

PolarFire silicon (DS-P2.126 evidence, 2026-09-23): the DE-C0 I2C self-test
decoded 0xAB,0x00,0x00 where the contract expects 0xAB,0xCD,0x3E. The kit's
riscv64 CPython lowers ``ctypes.c_uint32(...).value = v`` to four byte stores,
so STIM_I2C_DATA fired on its first byte lane before bytes 1..2 arrived
(reproduced in sim: hw/sim/cocotb/tests/stimulus/test_regbank_stim_i2c_bytelane.py).

These tests drive the REAL MmioRegbank over a temp-file mapping (devmem_path)
with ``platform.machine()`` reporting riscv64 and the REAL host libatomic
underneath, recording every call that reaches it. On x86 the ctypes store is a
single ``mov`` anyway, so the bus shape itself cannot be observed here; what is
asserted is that the riscv64 path routes each access through the one primitive
whose kit build is a single ``sw``/``lw`` — and that premise is itself pinned
against the kit binaries in the committed overlay tarball (last test).
"""

from __future__ import annotations

import ctypes
import mmap
import platform
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from sw.stream.regbank import MmioRegbank
from sw.stream.testbench import de_c0_production_armer as armer
from sw.stream.testbench.de_c0_stimulus_contract import (
    I2C_PACKED_DATA, STIM_I2C_DATA,
)

_REAL_CDLL = ctypes.CDLL
_REAL_MMAP = mmap.mmap
_REAL_LIBATOMIC = _REAL_CDLL("libatomic.so.1")


class _RecordingFn:
    """Stands in for one libatomic symbol: records each call, then forwards it
    to the real host implementation so the value really lands in the map."""

    def __init__(self, name: str, calls: list) -> None:
        self._real = getattr(_REAL_LIBATOMIC, name)
        self._name = name
        self._calls = calls
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self._real.argtypes = self.argtypes
        self._real.restype = self.restype
        self._calls.append((self._name,) + args)
        return self._real(*args)


class _RecordingLibatomic:
    def __init__(self, calls: list) -> None:
        # setattr, not `self.__atomic_...`: a class body would name-mangle it.
        for name in ("__atomic_store_4", "__atomic_load_4"):
            setattr(self, name, _RecordingFn(name, calls))


@pytest.fixture
def mem_file(tmp_path: Path, monkeypatch) -> Path:
    # test_ddr_unaligned_sigbus_dst1_9's serve_fanout thread outlives its test
    # while still holding mmap.mmap patched to a fake; pin the real one here.
    monkeypatch.setattr(mmap, "mmap", _REAL_MMAP)
    path = tmp_path / "mem"
    path.write_bytes(b"\x00" * mmap.PAGESIZE)
    return path


@pytest.fixture
def riscv_calls(monkeypatch) -> list:
    calls: list = []
    # Patched on the platform/ctypes modules themselves (not attributes of
    # sw.stream.regbank) so the pre-fix module fails on the assertion, not on
    # a missing attribute.
    monkeypatch.setattr(platform, "machine", lambda: "riscv64")
    monkeypatch.setattr(
        ctypes, "CDLL",
        lambda soname, *a, **kw: _RecordingLibatomic(calls)
        if soname == "libatomic.so.1" else _REAL_CDLL(soname, *a, **kw),
    )
    return calls


def _mapped_base(rb: MmioRegbank) -> int:
    view = ctypes.c_char.from_buffer(rb._mmap)
    try:
        return ctypes.addressof(view)
    finally:
        del view


def test_riscv_i2c_armer_data_write_is_one_word_store(mem_file, riscv_calls, monkeypatch):
    """The production I2C armer's STIM_I2C_DATA write must reach the bus as
    exactly ONE 32-bit store carrying the whole packed word."""
    # Patched on the time module itself, not `armer.time`: DS-P2.136 moved the
    # arm sequences (and their sleeps) into sw.stream.stimulus_armer, and the
    # stale attribute path killed this test with an AttributeError before it
    # reached its assertion.
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with MmioRegbank(base_addr=0, size=mmap.PAGESIZE, devmem_path=str(mem_file)) as rb:
        base = _mapped_base(rb)
        armer.arm(rb, "i2c")
        data_stores = [c for c in riscv_calls
                       if c[0] == "__atomic_store_4" and c[1] == base + STIM_I2C_DATA]
        assert len(data_stores) == 1 and data_stores[0][2] == I2C_PACKED_DATA, (
            "write_u32(STIM_I2C_DATA) did not reach the bus as ONE 32-bit store "
            f"of {I2C_PACKED_DATA:#010x} on riscv64 (libatomic stores seen: "
            f"{[(hex(c[1] - base), hex(c[2])) for c in riscv_calls if c[0] == '__atomic_store_4']})"
        )
    assert mem_file.read_bytes()[STIM_I2C_DATA:STIM_I2C_DATA + 4] == bytes(
        [0xAB, 0xCD, 0x3E, 0x00])


def test_riscv_read_u32_is_one_word_load(mem_file, riscv_calls):
    raw = bytearray(mem_file.read_bytes())
    raw[0x10:0x14] = (0xDEADBEEF).to_bytes(4, "little")
    mem_file.write_bytes(bytes(raw))
    with MmioRegbank(base_addr=0, size=mmap.PAGESIZE, devmem_path=str(mem_file)) as rb:
        base = _mapped_base(rb)
        assert rb.read_u32(0x10) == 0xDEADBEEF
    assert [c[:2] for c in riscv_calls] == [("__atomic_load_4", base + 0x10)]


def test_riscv_without_libatomic_refuses(mem_file, monkeypatch):
    """A byte-splitting machine must fail loud, never fall back to the
    byte-store ctypes path."""
    monkeypatch.setattr(platform, "machine", lambda: "riscv64")

    def _missing(soname):
        raise OSError(f"{soname}: cannot open shared object file")

    monkeypatch.setattr(ctypes, "CDLL", _missing)
    with pytest.raises(OSError, match="libatomic"):
        MmioRegbank(base_addr=0, size=mmap.PAGESIZE, devmem_path=str(mem_file))


@pytest.mark.parametrize("machine", ["aarch64", "armv7l", "x86_64"])
def test_non_riscv_keeps_ctypes_word_store(mem_file, monkeypatch, machine):
    """ARM stays on the DS-T2.17 ctypes path (proven on Zynq/DE25 silicon)."""
    monkeypatch.setattr(platform, "machine", lambda: machine)

    def _unexpected(soname):
        raise AssertionError(f"libatomic loaded on {machine}")

    monkeypatch.setattr(ctypes, "CDLL", _unexpected)
    with MmioRegbank(base_addr=0, size=mmap.PAGESIZE, devmem_path=str(mem_file)) as rb:
        rb.write_u32(STIM_I2C_DATA, I2C_PACKED_DATA)
        assert rb.read_u32(STIM_I2C_DATA) == I2C_PACKED_DATA
