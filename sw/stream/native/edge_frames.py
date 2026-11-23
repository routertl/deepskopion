# SPDX-License-Identifier: MPL-2.0
"""DS-P1.74 (decision DS-D.49): the native edge path, loaded with ctypes.

``libds_edge_drain.so`` (edge_drain.c) walks the EDGE_BATCH frames of a ring
span, cuts the edges into wire EDGE_EVENTS frames (Batch.add_edge with
edge_within_reach) and packs each frame as the bytes
``proto.pack_frame(TAG_EDGE_EVENTS, proto.make_edge_events(...))`` produces.
Python handles a whole run of edge frames with one call and receives ready
wire frames, so no Python code runs per edge on the wire path.

The library is found at ``$DS_NATIVE_EDGE_LIB``, else beside this file, else
at ``SYSTEM_LIB``, where the DE25 image installs it (tools/de25_linux_image.py).
The build commands are in README.md.
"""
from __future__ import annotations

import ctypes
import logging
import os
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

LIB_NAME = "libds_edge_drain.so"
ENV_LIB = "DS_NATIVE_EDGE_LIB"
#: Where an image installs the library built for its CPU (DS-P2.210).
SYSTEM_LIB = Path("/usr/local/lib/deepskopion") / LIB_NAME


class _Cfg(ctypes.Structure):
    _fields_ = [("ns_per_tick", ctypes.c_uint64), ("clk_mhz", ctypes.c_uint32),
                ("max_frames", ctypes.c_size_t),
                # DS-P1.117 (ABI 2): the PL zero-pad rule of DdrRingDataSource._pad_run.
                ("ring_word_bytes", ctypes.c_size_t), ("ring_bytes", ctypes.c_size_t),
                ("span_ofs", ctypes.c_size_t)]


class _Result(ctypes.Structure):
    _fields_ = [("consumed", ctypes.c_size_t), ("frames", ctypes.c_size_t),
                ("edges", ctypes.c_size_t), ("resync_bytes", ctypes.c_size_t),
                ("stop", ctypes.c_int), ("has_first_ts", ctypes.c_int),
                ("first_ts_ticks", ctypes.c_uint64),
                ("pad_bytes", ctypes.c_size_t), ("after_frame", ctypes.c_int)]


#: The ds_drain_cfg / ds_drain_result layout these structures mirror (edge_drain.h DS_EDGE_DRAIN_ABI).
ABI = 2


# enum ds_drain_stop
STOP_END, STOP_PARTIAL, STOP_OTHER_FRAME, STOP_FULL, STOP_MAX_FRAMES = range(5)


@dataclass(frozen=True)
class DrainResult:
    consumed: int
    frames: int
    edges: int
    resync_bytes: int
    stop: int
    first_ts_ticks: Optional[int]
    pad_bytes: int = 0       # DS-P1.117: PL zero pad consumed (part of consumed, not of resync_bytes)
    after_frame: bool = False  # DS-P1.117: the walk ended right after a frame


class EdgeDrainLib:
    """The loaded shared library, its prototypes declared."""

    def __init__(self, path: Path):
        self.path = Path(path)
        lib = ctypes.CDLL(str(self.path))
        vp, sz, u64 = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint64
        # DS-P1.117: a library built from an older edge_drain.c has shorter structures, and would leave the
        # appended result fields unwritten; refuse it rather than read them.
        abi = getattr(lib, "ds_edge_drain_abi", None)
        if abi is None:
            raise OSError(f"{self.path}: no ds_edge_drain_abi; built before ABI {ABI}, rebuild it")
        abi.restype, abi.argtypes = ctypes.c_int, []
        if abi() != ABI:
            raise OSError(f"{self.path}: edge drain ABI {abi()}, this module needs {ABI}; rebuild it")
        lib.ds_frames_new.restype = vp
        lib.ds_frames_new.argtypes = []
        lib.ds_frames_free.restype = None
        lib.ds_frames_free.argtypes = [vp]
        lib.ds_frames_add.restype = ctypes.c_int
        lib.ds_frames_add.argtypes = [vp, ctypes.c_uint32, u64]
        lib.ds_frames_drain.restype = ctypes.c_int
        lib.ds_frames_drain.argtypes = [vp, vp, sz, ctypes.POINTER(_Cfg),
                                        ctypes.POINTER(_Result)]
        lib.ds_frames_count.restype = sz
        lib.ds_frames_count.argtypes = [vp]
        lib.ds_frames_edges.restype = sz
        lib.ds_frames_edges.argtypes = [vp]
        lib.ds_frames_info.restype = ctypes.c_int
        lib.ds_frames_info.argtypes = [vp, sz, ctypes.POINTER(u64), ctypes.POINTER(sz)]
        lib.ds_frames_words.restype = sz
        lib.ds_frames_words.argtypes = [vp, sz, ctypes.POINTER(ctypes.c_uint32), sz]
        lib.ds_frames_pack_bound.restype = sz
        lib.ds_frames_pack_bound.argtypes = [vp, sz]
        lib.ds_frames_pack.restype = sz
        lib.ds_frames_pack.argtypes = [vp, sz, u64, u64, vp, sz]
        self.lib = lib

    def new_frames(self) -> "NativeEdgeFrames":
        return NativeEdgeFrames(self)


def candidates(path: Optional[os.PathLike] = None) -> List[Path]:
    """Where load() looks, in order: *path*, else ``$DS_NATIVE_EDGE_LIB``, else
    beside this file and then ``SYSTEM_LIB``."""
    explicit = path or os.environ.get(ENV_LIB)
    if explicit:
        return [Path(explicit)]
    return [Path(__file__).with_name(LIB_NAME), SYSTEM_LIB]


def load(path: Optional[os.PathLike] = None, required: bool = False) -> Optional[EdgeDrainLib]:
    """The first library found among ``candidates(path)``.

    Returns None when none is there and not *required*; raises when *required*
    and none is there, or when a file is there and does not load."""
    places = candidates(path)
    for candidate in places:
        if candidate.is_file():
            return EdgeDrainLib(candidate)
    if required:
        raise FileNotFoundError("native edge drain required, not found at "
                                + ", ".join(str(p) for p in places))
    return None


class NativeEdgeFrames:
    """One batch's edges, held in C: the frame cut and the packed wire frames."""

    def __init__(self, lib: EdgeDrainLib):
        self._lib = lib.lib
        handle = self._lib.ds_frames_new()
        if not handle:
            raise MemoryError("ds_frames_new")
        self._h = ctypes.c_void_p(handle)
        weakref.finalize(self, self._lib.ds_frames_free, self._h)
        self._cfg = _Cfg()
        self._res = _Result()

    def add(self, word: int, ts_ns: int) -> None:
        if self._lib.ds_frames_add(self._h, int(word) & 0xFFFFFFFF, int(ts_ns)):
            raise MemoryError("ds_frames_add")

    def drain(self, span: bytes, cursor: int, max_frames: int, ns_per_tick: int,
              clk_mhz: int, ring_word_bytes: int = 0, ring_bytes: int = 0,
              ring_ofs: int = 0) -> DrainResult:
        """Walk ``span[cursor:]`` for at most *max_frames* frames.

        DS-P1.117: with *ring_word_bytes* set, a zero run right after a walked frame up to the next
        DDR-word boundary is consumed as PL pad, by DdrRingDataSource._pad_run's rule, where *ring_ofs*
        is the ring byte offset of ``span[0]`` and *ring_bytes* the ring size. 0 keeps the DS-P2.281
        walk, which stops at such a zero word."""
        if not isinstance(span, bytes):
            span = bytes(span)
        cfg = self._cfg
        cfg.ns_per_tick, cfg.clk_mhz, cfg.max_frames = ns_per_tick, clk_mhz, max_frames
        cfg.ring_word_bytes, cfg.ring_bytes = int(ring_word_bytes or 0), int(ring_bytes or 0)
        cfg.span_ofs = int(ring_ofs) + int(cursor)
        base = ctypes.cast(ctypes.c_char_p(span), ctypes.c_void_p).value
        if self._lib.ds_frames_drain(self._h, base + cursor, len(span) - cursor,
                                     ctypes.byref(cfg), ctypes.byref(self._res)):
            raise MemoryError("ds_frames_drain")
        r = self._res
        return DrainResult(r.consumed, r.frames, r.edges, r.resync_bytes, r.stop,
                           r.first_ts_ticks if r.has_first_ts else None,
                           r.pad_bytes, bool(r.after_frame))

    def frame_count(self) -> int:
        return self._lib.ds_frames_count(self._h)

    def edge_count(self) -> int:
        return self._lib.ds_frames_edges(self._h)

    def info(self, i: int) -> tuple[int, int]:
        """(anchor_ns, word count) of frame *i*."""
        anchor, n = ctypes.c_uint64(), ctypes.c_size_t()
        if self._lib.ds_frames_info(self._h, i, ctypes.byref(anchor), ctypes.byref(n)):
            raise IndexError(i)
        return anchor.value, n.value

    def words(self, i: int) -> List[int]:
        _, n = self.info(i)
        buf = (ctypes.c_uint32 * n)()
        if n and self._lib.ds_frames_words(self._h, i, buf, n) != n:
            raise IndexError(i)
        return list(buf)

    def pack(self, i: int, seq: int, clk_mhz: int) -> bytes:
        """Frame *i* as wire bytes: [TAG_EDGE_EVENTS, make_edge_events(seq, ...)]."""
        bound = self._lib.ds_frames_pack_bound(self._h, i)
        if not bound:
            raise IndexError(i)
        buf = ctypes.create_string_buffer(bound)
        n = self._lib.ds_frames_pack(self._h, i, seq, clk_mhz, buf, bound)
        if not n:
            raise ValueError(f"ds_frames_pack failed for frame {i}")
        return ctypes.string_at(buf, n)
