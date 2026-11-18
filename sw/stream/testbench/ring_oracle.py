"""Ring-based oracle for the PS-side EMIO self-test (DS-P3.44).

The self-test verifies Layer 1 by directly walking the DDR ring +
parsing zs_msg frames the PL just wrote, not through the daemon's
wave_events_v1 stream (DS-P2.50).

This is a stronger check than wave_events_v1 would be:
- The daemon's decoder pipeline isn't in the verification loop;
  we read the raw PL output, not the post-decode interpretation.
- A bug in the decoder can't mask a real RTL bug here.

The harness can gain a secondary check on wave_events_v1 (decoded
event matches our parsed payload) — but this primary mechanism
stays. It's the ground truth.

Workflow per scenario:

    oracle = RingOracle(regbank, ddr_mmap)
    oracle.snapshot()                  # latch RING_WRITE_PTR_before
    drive_master(...)                  # PS-side traffic
    frames = oracle.collect_new_frames(min_count=1, timeout_s=0.5)
    assert any(f.datalogger_id == ... for f, _ in frames)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from sw.stream.ddr_mmap import DdrMmap
from sw.stream.regbank import Regs
from sw.stream.testbench.emio_inject import RegbankExt
from sw.stream.zs_msg import HEADER_SIZE, ZS_MAGIC, ZsHeader, parse_header

logger = logging.getLogger(__name__)


@dataclass
class RingFrame:
    """One zs_msg frame as read from DDR."""
    header: ZsHeader
    payload: bytes
    offset: int   # byte offset inside the ring at frame start

    @property
    def datalogger_id(self) -> int:
        return self.header.datalogger_id

    @property
    def msg_type(self) -> int:
        return self.header.msg_type


class RingOracle:
    """Pre/post RING_WRITE_PTR snapshot + zs_msg ring walker.

    Uses RING_WRITE_PTR (32-bit word count, mod 2**32) as the producer
    cursor. The walk reads only the bytes between the pre and post
    snapshots, parses zs_msg frames, returns them in production order.

    The ring is in PS DDR — the PL writes via rr-ddr-stream's HP0 path,
    the PS reads via /dev/mem mapped through DdrMmap.
    """

    # The ring counter (RING_WRITE_PTR) advances by 1 per 32-bit word.
    # zs_msg with G_PAYLOAD_WORDS=1: 6 words/frame (5 hdr + 1 payload).
    # With the wider G_PAYLOAD_WORDS_MAX=32 framer (Phase B2 step 3) a
    # frame may carry up to 5 + 32 = 37 words, depending on what the
    # upstream pushed. We don't need to assume a fixed count — bytes_per
    # frame is recoverable from header_len + payload_len.
    BYTES_PER_WORD = 4

    def __init__(self, regbank: RegbankExt, ddr: DdrMmap,
                 ring_size_bytes: Optional[int] = None):
        self._rb = regbank
        self._ddr = ddr
        # Cache ring size — fall back to the regbank if not provided.
        if ring_size_bytes is None:
            ring_size_bytes = self._rb.ring_locator()["bytes"]
        self._ring_size = ring_size_bytes
        self._snap_off: Optional[int] = None   # DS-T2.20: commit-offset snapshot
        self._read_off: int = 0                # DS-P2.59: SW drain (consumed) offset

    # ── snapshot / delta ────────────────────────────────────────────────

    def commit_offset(self) -> int:
        """The DMA's committed byte offset (RING_COMMIT_PTR, DS-T2.20). The
        authoritative read boundary: everything below this HAS landed in DRAM.
        Replaces RING_WRITE_PTR*4 — an enqueue counter whose byte mapping
        DIVERGES from the DMA write address (~8 bytes/frame), so the old walk
        read drifted/older frames (Haz.59 / PS-LOOP-REQ-9)."""
        return self._rb.raw.read_u32(Regs.RING_COMMIT_PTR) % self._ring_size

    def write_idle(self) -> bool:
        """DDR_RESET_STATUS bit2 — the DMA write side is idle (all enqueued
        frames committed). A 'caught up' gate; reading up to commit_offset() is
        already safe, this just confirms there's no in-flight tail."""
        return bool((self._rb.raw.read_u32(Regs.DDR_RESET_STATUS) >> 2) & 1)

    def snapshot(self) -> int:
        """Latch the current DMA commit byte offset. Returns the byte offset."""
        self._snap_off = self.commit_offset()
        return self._snap_off

    # ── drain (DS-P2.59) ────────────────────────────────────────────────

    def advance_read(self, consumed_off: Optional[int] = None) -> int:
        """DS-P2.59: tell the PL we have CONSUMED the ring up to `consumed_off`
        (byte offset mod ring size). The PL feeds RING_READ_PTR into
        ddr_stream_axi.csr_rd_ptr_addr_i, which frees the corresponding ring
        space so the DMA write pointer can auto-wrap. This is the SPSC drain —
        without it the ring fills exactly once and stalls (silent loss). We
        already own the data (read via mmap up to commit_offset()); this only
        advances the pointer, so there is NO double-read of DRAM.

        Pass the byte offset explicitly, or omit it to re-publish the last
        offset the frame walk consumed (tracked internally)."""
        if consumed_off is None:
            consumed_off = self._read_off
        consumed_off %= self._ring_size
        self._read_off = consumed_off
        self._rb.raw.write_u32(Regs.RING_READ_PTR, consumed_off)
        return consumed_off

    # ── frame walk ──────────────────────────────────────────────────────

    def collect_new_frames(self,
                           min_count: int = 1,
                           timeout_s: float = 0.5,
                           poll_interval_s: float = 0.01,
                           max_frames: int = 256) -> List[RingFrame]:
        """Wait for ≥ min_count frames COMMITTED since snapshot, return them.

        Walks [snap_off → current commit_offset()] — the region the DMA has
        actually written — cache-coherently (RealDdrMmap re-mmaps per read so
        the cache-incoherent HP0 writes are seen), parsing zs_msg frames.
        Resyncs on bad magic by advancing 4 bytes. Times out if fewer than
        min_count arrive within timeout_s.
        """
        if self._snap_off is None:
            raise RuntimeError("RingOracle.snapshot() not called yet")

        deadline = time.monotonic() + timeout_s
        frames: List[RingFrame] = []
        walked = 0  # bytes consumed from snap_off

        while time.monotonic() < deadline:
            span = (self.commit_offset() - self._snap_off) % self._ring_size
            if span > walked:
                new_bytes = span - walked
                start_byte = (self._snap_off + walked) % self._ring_size
                blob = self._ddr.read_bytes(start_byte, new_bytes)
                parsed_bytes = self._parse_blob(blob, base_offset=start_byte, frames_out=frames)
                walked += parsed_bytes
                # DS-P2.59: free the span we just consumed so the ring drains
                # and the DMA write pointer can keep wrapping.
                self.advance_read((self._snap_off + walked) % self._ring_size)
                if len(frames) >= min_count or len(frames) >= max_frames:
                    return frames
            time.sleep(poll_interval_s)

        if len(frames) < min_count:
            raise TimeoutError(
                f"RingOracle: got {len(frames)} frames in {timeout_s}s "
                f"(wanted ≥ {min_count}); commit span="
                f"{(self.commit_offset() - self._snap_off) % self._ring_size}B, "
                f"write_idle={self.write_idle()}"
            )
        return frames

    # ── internals ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_blob(blob: bytes, base_offset: int,
                    frames_out: List[RingFrame]) -> int:
        """Walk `blob` parsing as many zs_msg frames as fit.

        Returns the number of fully-consumed bytes — caller advances
        its cursor by exactly that much. Trailing partial frame (header
        + payload not yet complete) is left for the next pass.
        """
        idx = 0
        while idx < len(blob):
            if len(blob) - idx < HEADER_SIZE:
                break  # partial header — wait for more
            try:
                hdr = parse_header(blob, idx)
            except ValueError:
                # Bad magic — advance one word and resync.
                idx += 4
                continue
            frame_total = HEADER_SIZE + hdr.payload_len
            if len(blob) - idx < frame_total:
                break  # partial payload — wait for more
            payload = blob[idx + HEADER_SIZE : idx + frame_total]
            frames_out.append(RingFrame(
                header=hdr,
                payload=payload,
                offset=base_offset + idx,
            ))
            idx += frame_total
        return idx

    # ── convenience filters ────────────────────────────────────────────

    @staticmethod
    def filter_by_datalogger(frames: List[RingFrame], datalogger_id: int) -> List[RingFrame]:
        return [f for f in frames if f.datalogger_id == datalogger_id]


def open_ring(regbank: RegbankExt, devmem_path: str = "/dev/mem") -> RingOracle:
    """Convenience: build a RingOracle reading ring base + size from the regbank."""
    from sw.stream.ddr_mmap import RealDdrMmap
    loc = regbank.ring_locator()
    base = loc["base_low"]   # Zynq-7: 32-bit physical, base_high == 0
    size = loc["bytes"]
    ddr = RealDdrMmap(base, size, devmem_path=devmem_path)
    return RingOracle(regbank, ddr, ring_size_bytes=size)
