# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""DS-T1.9 regression: the production stream daemon died SILENTLY the moment the
stimulus generator wrote fresh events into the DDR ring — a SIGBUS, not a
catchable exception, so no traceback and no consumer needed.

ROOT CAUSE, proven on DE25 (Agilex 5 HPS) silicon: the PS-DDR ring is
``/dev/mem``-mapped as UNCACHED device memory, and ``RealDdrMmap._read_coherent``
did ``bytes(m[skew:skew + n])`` — which makes CPython's buffer copy START at
``base + skew``. Real zs_msg frames are 4-byte aligned, so the drain cursor's
sub-page ``skew`` is 4/8/12 mod 16 almost always; the wide (NEON) load the memcpy
uses on that unaligned *device-memory* address raises SIGBUS and the whole daemon
process dies. Measured: skew 0x538 -> "Fatal Python error: Bus error"; skew 0x480
(16-aligned) -> fine. The stop-daemon path survived only by hitting aligned
offsets in its short run. The fix copies from the page-aligned (thus 16-aligned)
mmap base — ``bytes(m)[skew:skew + n]`` — then slices normal RAM.

WHY THIS IS BOARD-FREE YET FALSIFIABLE: a SIGBUS on uncached device memory cannot
be produced from ordinary test RAM (ordinary RAM allows unaligned access), and a
SIGBUS cannot be caught to assert on. So ``_DeviceMemFake`` models the *exact
fault signature*: a buffer whose UNALIGNED-START slice raises (standing in for the
hardware SIGBUS), while a full ``bytes(obj)`` copy from the aligned base always
works. The tests drive the REAL ``RealDdrMmap._read_coherent`` and the REAL
``serve_fanout`` drainer over this fake (via a patched ``mmap.mmap``), so they
exercise the shipped code path, not a re-implementation.

BASELINE (against the pre-fix ``bytes(m[skew:skew + n])``): both tests are RED —
``test_read_coherent_*`` raises ``_DeviceBus`` on the unaligned read, and
``test_serve_fanout_drainer_survives_*`` sees the drainer collapse the daemon.
MUTATION: revert the fix line and they go RED again; that is exactly how the
pre-fix silicon crash reproduces here. A GREEN run against the fix earns nothing
unless you have first seen it RED against the old copy (do it: it takes 10s).
"""
from __future__ import annotations

import socket
import threading
import time
from unittest import mock

import msgpack
import pytest

from sw.stream import protocol as proto
from sw.stream import protocol_codes, zs_msg
from sw.stream import ddr_mmap as ddr_mod
from sw.stream.ddr_mmap import RealDdrMmap
from sw.stream.data_source import DdrRingDataSource
from sw.stream.regbank import FakeRegbank, Regs
from sw.stream.server import serve_fanout

_ALIGN = 16
_BYTES_PER_MSG = 4


class _DeviceBus(Exception):
    """Stands in for the ARM SIGBUS raised on an unaligned wide load from the
    uncached /dev/mem device mapping of the PS-DDR ring (DS-T1.9)."""


class _DeviceMemFake:
    """A /dev/mem-style mapping over ``buf``: byte-accessible, EXCEPT a slice whose
    START is not 16-byte aligned raises ``_DeviceBus`` — mirroring the silicon
    SIGBUS. ``bytes(self)`` copies the whole buffer from the (16-aligned) base and
    is ALWAYS safe, which is exactly the property the fix relies on."""

    def __init__(self, buf: bytes):
        self._buf = bytes(buf)

    def __len__(self):
        return len(self._buf)

    def __getitem__(self, key):
        if isinstance(key, slice):
            start = key.start or 0
            if start % _ALIGN != 0:
                raise _DeviceBus(
                    f"unaligned device-memory read at offset {start:#x} "
                    f"(skew % {_ALIGN} = {start % _ALIGN}) — SIGBUS on silicon")
            return self._buf[key]
        if key % _ALIGN != 0:
            raise _DeviceBus(f"unaligned device-memory read at {key:#x}")
        return self._buf[key]

    def __bytes__(self):
        # Whole-buffer copy from the page-aligned base — the alignment-safe path.
        return self._buf

    def close(self):
        pass


def _patch_devmem(ring: bytes):
    """Patch ddr_mmap.mmap.mmap so RealDdrMmap._read_coherent maps a
    _DeviceMemFake over ``ring[offset .. offset+length]`` (base_addr assumed 0)."""
    def _fake_mmap(fd, length, flags=0, prot=0, offset=0):
        return _DeviceMemFake(ring[offset:offset + length])
    return mock.patch.object(ddr_mod.mmap, "mmap", _fake_mmap)


def _real_ddr_over(ring: bytes) -> RealDdrMmap:
    d = RealDdrMmap.__new__(RealDdrMmap)     # skip the /dev/mem open
    d._fd = -1
    d._base_addr = 0
    d._size = len(ring)
    return d


# ── Test 1: the exact fixed read, at an unaligned offset ───────────────────────
@pytest.mark.parametrize("offset", [0x08, 0x538, 0x4, 0xC, 0x14])
def test_read_coherent_unaligned_offset_returns_data_not_bus_error(offset):
    """RealDdrMmap.read_bytes at a NON-16-aligned offset must return the correct
    bytes — never fault. RED against the pre-fix ``bytes(m[skew:skew+n])`` (raises
    _DeviceBus at the unaligned slice); GREEN with ``bytes(m)[skew:skew+n]``."""
    ring = bytes((i * 7 + 3) & 0xFF for i in range(0x2000))
    n = 0x88
    ddr = _real_ddr_over(ring)
    with _patch_devmem(ring):
        got = ddr.read_bytes(offset, n)
    assert got == ring[offset:offset + n], (
        "unaligned device-memory read did not return the ring bytes — the "
        "pre-fix code SIGBUSes here on silicon (DS-T1.9)")


def test_read_coherent_aligned_offset_is_the_control():
    """POSITIVE CONTROL: a 16-aligned offset works on BOTH old and new code, so a
    green here does NOT prove the fix — it proves the harness reads real data.
    The unaligned test above is the one that discriminates."""
    ring = bytes((i * 5 + 1) & 0xFF for i in range(0x2000))
    ddr = _real_ddr_over(ring)
    with _patch_devmem(ring):
        got = ddr.read_bytes(0x480, 0xC0)
    assert got == ring[0x480:0x480 + 0xC0]


# ── Test 2: the daemon loop — drainer survives a ring that gains events ─────────
def _uart_frame(byte: int, ts: int) -> bytes:
    hdr = zs_msg.make_header(
        datalogger_id=12, domain=zs_msg.DOMAIN_UART,
        event_id=protocol_codes.UART_TYPES["payload"], severity=zs_msg.SEV_INFO,
        timestamp=ts, payload_len=4)
    import struct
    return zs_msg.pack_header(hdr) + struct.pack("<I", byte & 0xFF)


def _drain_one_subscriber(sock_path, out, ready):
    """Connect, HELO/HELO_OK, then record ANNOTATIONS byte payloads until EOF/idle."""
    c = None
    for _ in range(50):
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock_path))
            break
        except OSError:
            time.sleep(0.05)
    if c is None:
        return
    ready.set()
    unp = msgpack.Unpacker(raw=False)
    c.settimeout(3.0)
    deadline = time.monotonic() + 3.0
    helo_ok = False
    while time.monotonic() < deadline:
        try:
            data = c.recv(65536)
        except socket.timeout:
            break
        if not data:
            break
        unp.feed(data)
        for obj in unp:
            if not (isinstance(obj, list) and len(obj) == 2):
                continue
            tag, payload = obj
            if tag == proto.TAG_HELO and not helo_ok:
                c.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"consumer": "t"}))
                helo_ok = True
            elif tag == proto.TAG_ANNOTATIONS:
                for a in payload.get("annotations", []):
                    p = a.get("payload") or {}
                    if a.get("kind") == "uart_byte" and "byte" in p:
                        out.append(p["byte"])
    c.close()


def test_serve_fanout_drainer_survives_unaligned_ring_and_delivers(sock_dir):
    """The DS-P1.44 clause-2 surface: a subscriber attaches to serve_fanout, THEN
    the ring gains events at an unaligned drain offset. The daemon MUST stay alive
    (drainer not collapsed) AND the subscriber MUST receive the decoded bytes.

    RED against the pre-fix read: the drainer's DDR read raises _DeviceBus, the
    broad ``except Exception`` collapses the drainer -> serve_fanout returns and no
    annotation is delivered (this is the silicon crash's board-free image — on
    silicon the same read is an uncatchable SIGBUS that kills the whole process).
    GREEN with the fix: daemon alive, bytes delivered.
    """
    payload = [0xA3, 0x5C, 0x71, 0x0E, 0xFF, 0x42]
    blob = b"".join(_uart_frame(b, ts=100 * (i + 1)) for i, b in enumerate(payload))
    # Place frames at a NON-16-aligned ring offset so the drain read starts unaligned.
    start = 0x08
    assert start % _ALIGN != 0
    ring_size = 0x4000
    ring = bytearray(ring_size)
    ring[start:start + len(blob)] = blob

    rb = FakeRegbank()
    rb.set(Regs.RING_COMMIT_PTR, start)      # seed the drain cursor at the unaligned start
    rb.set(Regs.RING_WRITE_PTR, start // _BYTES_PER_MSG)

    ddr = _real_ddr_over(bytes(ring))

    src = DdrRingDataSource(regbank=rb, ddr=ddr, sample_clk_hz=50_000_000,
                            poll_period_s=0.005, write_back_read_ptr=False)

    sock_path = sock_dir / "d.sock"
    got: list = []
    ready = threading.Event()
    server_rc: list = []

    def run_server():
        with _patch_devmem(bytes(ring)):
            server_rc.append(serve_fanout(
                sock_path, src, connect_timeout_s=1.0,
                backfill_bytes=1 << 20, backfill_frames=256,
                stats_period_s=0.05, max_connections=1))

    th = threading.Thread(target=run_server, daemon=True)
    th.start()

    sub = threading.Thread(target=_drain_one_subscriber,
                           args=(sock_path, got, ready), daemon=True)
    sub.start()
    assert ready.wait(3.0), "subscriber never connected"
    time.sleep(0.2)

    # Ring GAINS events after the subscriber is attached (the DS-T1.9 scenario).
    rb.set(Regs.RING_COMMIT_PTR, start + len(blob))
    rb.set(Regs.RING_WRITE_PTR, (start + len(blob)) // _BYTES_PER_MSG)

    sub.join(timeout=4.0)

    # (1) the daemon delivered the exact decoded bytes over the wire ...
    n = len(payload)
    delivered = any(got[i:i + n] == payload for i in range(0, max(1, len(got) - n + 1)))
    assert delivered, (
        f"subscriber never received the armed UART bytes {payload}; got {got!r} — "
        "the drainer collapsed on the unaligned DDR read (DS-T1.9 crash image)")

    # (2) ... and the drainer is still ALIVE draining (never set stop / returned).
    #     A collapsed drainer makes serve_fanout(max_connections=1) return via its
    #     drainer.join; assert it did NOT finish early.
    time.sleep(0.1)
    assert not server_rc or th.is_alive(), (
        "serve_fanout returned — the drainer collapsed (DS-T1.9). A live daemon "
        "keeps serving after the single subscriber leaves.")
