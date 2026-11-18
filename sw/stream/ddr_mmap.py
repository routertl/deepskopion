"""PS-side DDR window access for the rr-ddr-stream ring buffer.

DdrRingDataSource (data_source.py) mmaps a contiguous slab of PS DDR
where the PL has DMA'd zs_msg_header_t frames; this module provides the
abstract DdrMmap Protocol plus a /dev/mem-backed implementation for
PetaLinux and a bytearray-backed fake for tests.

Layout-of-record: docs/software/data_plane/message_formats/common_header.md
(20-byte little-endian header per frame). The ring base address and size
come from the regbank's RING_BASE_LOW / RING_BASE_HIGH / RING_BYTES slots
(DS-P3.27).
"""

from __future__ import annotations

import logging
import mmap
import os
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


class DdrMmap(Protocol):
    """Read-only window over a slab of PS DDR.

    The Phase B reader only ever reads; the PL is the sole writer of
    the ring. read_bytes() should return bytes whose length matches the
    requested count; underlying implementations honour ring wrap by
    returning the splice across the base.
    """

    def read_bytes(self, offset: int, length: int) -> bytes: ...
    def size(self) -> int: ...
    def close(self) -> None: ...


class RealDdrMmap:
    """/dev/mem-backed DDR window for a PS PetaLinux runtime.

    Maps `size` bytes at `base_addr`. Caller is responsible for close().
    Both base_addr and size must be page-aligned (`mmap.PAGESIZE`). On
    Zynq-7 the PS PLL clock is 33.33 MHz and the DDR window is typically
    256 MB at 0x10000000; both satisfy page alignment naturally.

    Reads wrap at the ring boundary so a caller asking for N bytes from
    offset O always gets N bytes back (length-preserving — the consumer
    doesn't need to do splice math).
    """

    def __init__(self,
                 base_addr: int,
                 size: int,
                 devmem_path: str = "/dev/mem") -> None:
        if size <= 0:
            raise ValueError(f"DDR window size {size} must be > 0")
        if size % mmap.PAGESIZE != 0:
            raise ValueError(f"DDR window size {size} must be multiple of {mmap.PAGESIZE}")
        if base_addr % mmap.PAGESIZE != 0:
            raise ValueError(f"base_addr {base_addr:#x} must be page-aligned")

        self._base_addr = base_addr
        self._size = size
        self._fd: Optional[int] = None
        self._mmap: Optional[mmap.mmap] = None

        fd = os.open(devmem_path, os.O_RDONLY | os.O_SYNC)
        try:
            self._mmap = mmap.mmap(
                fd, size,
                flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ,
                offset=base_addr,
            )
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        logger.info("RealDdrMmap mapped %#x..%#x via %s", base_addr,
                    base_addr + size - 1, devmem_path)

    def size(self) -> int:
        return self._size

    def read_bytes(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        if length > self._size:
            raise ValueError(f"requested {length}B > ring size {self._size}B")
        start = offset % self._size
        end = start + length
        if end <= self._size:
            return self._read_coherent(start, length)
        # Wrap — splice (two coherent reads)
        return (self._read_coherent(start, self._size - start)
                + self._read_coherent(0, end - self._size))

    def _read_coherent(self, off: int, n: int) -> bytes:
        """Cache-coherent read of `n` bytes at ring offset `off`.

        DS-T2.18 / Haz.59: the HP0 DMA is cache-INCOHERENT with the APU, so the
        persistent mmap returns STALE cache (the PL writes bypass the CPU cache
        straight to DRAM). A FRESH, page-aligned mmap per read lands on a new
        virtual address whose cache lines are cold, so the read pulls from DRAM
        — proven in the DS-T2.20 diagnostics. Slower than a cached read, but
        correct; the self-test isn't throughput-bound.
        """
        assert self._fd is not None
        page = mmap.PAGESIZE
        a_off = off & ~(page - 1)                      # page-align down
        skew = off - a_off
        a_len = ((skew + n + page - 1) // page) * page  # page-multiple span
        m = mmap.mmap(self._fd, a_len, flags=mmap.MAP_SHARED,
                      prot=mmap.PROT_READ, offset=self._base_addr + a_off)
        try:
            # DS-T1.9: copy from the PAGE-ALIGNED mmap base, THEN slice — never
            # ``bytes(m[skew:skew + n])``. On DE25 (Agilex 5 HPS) the PS-DDR ring
            # is /dev/mem-mapped as UNCACHED device memory, and the ``m[skew:...]``
            # slice makes CPython's buffer copy START at ``base + skew``. When
            # ``skew`` is not 16-byte aligned (real zs_msg frames are 4-aligned,
            # so the drain cursor lands on skew % 16 == 4/8/12 constantly) the
            # wide/NEON load the memcpy uses on that unaligned device-memory
            # address raises SIGBUS — a FATAL signal no ``try/except`` can catch,
            # so the whole daemon process dies silently (measured on silicon: skew
            # 0x538 -> "Fatal Python error: Bus error", skew 0x480 -> fine). This
            # is exactly the DS-P1.44 clause-2 daemon-live blocker. ``bytes(m)``
            # copies from the page-aligned (thus 16-aligned) base — always safe —
            # and slicing the resulting NORMAL bytes is a plain in-RAM copy. Same
            # byte count copied as before; no data dropped, no batch skipped.
            return bytes(m)[skew:skew + n]
        finally:
            m.close()

    def close(self) -> None:
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "RealDdrMmap":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class FakeDdrMmap:
    """In-memory ring for tests + dev — bytearray-backed, wraps on reads.

    write_bytes() is a test helper for fixture setup; production code
    treats the ring as PL-write, PS-read.
    """

    def __init__(self, size: int, initial: Optional[bytes] = None) -> None:
        if size <= 0:
            raise ValueError(f"size {size} must be > 0")
        self._buf = bytearray(size)
        if initial is not None:
            n = min(len(initial), size)
            self._buf[:n] = initial[:n]
        self.closed = False

    def size(self) -> int:
        return len(self._buf)

    def read_bytes(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        size = len(self._buf)
        if length > size:
            raise ValueError(f"requested {length}B > ring size {size}B")
        start = offset % size
        end = start + length
        if end <= size:
            return bytes(self._buf[start:end])
        return bytes(self._buf[start:size]) + bytes(self._buf[0:end - size])

    def write_bytes(self, offset: int, data: bytes) -> None:
        """Test helper — install bytes at offset (wraps on overflow)."""
        size = len(self._buf)
        start = offset % size
        end = start + len(data)
        if end <= size:
            self._buf[start:end] = data
        else:
            head = size - start
            self._buf[start:size] = data[:head]
            self._buf[0:end - size] = data[head:]

    def close(self) -> None:
        self.closed = True
