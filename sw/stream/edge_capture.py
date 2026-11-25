# rr unknown-llm (AI-generated)
"""Edge-capture codec + file format (DS-P2.60).

Turns the wave_stream EDGE_EVENTS word stream (raw Edge Detector edges produced
by ``DdrRingDataSource``) into a portable on-disk capture, and back into an
absolute (timestamp_ns, level) transition list that a cocotb testbench can
replay as a real DUT input — "dogfooding a captured real signal".

EDGE_EVENT word layout (LT-frozen, mirrors logic-trace/src/edge_decode.cpp and
``protocol.make_edge_events``)::

    [31:27] instance (edge datalogger device id, 5b)
    [26]    S  — readback flag: when 1, E is the DIRECT current level
    [25]    F  — (reserved/format flag, preserved verbatim)
    [24]    E  — edge polarity: post-edge level = !E  (E=0 → rising → 1,
                 E=1 → falling → 0); when S=1, level = E directly
    [23:8]  ts_us — low 16 bits of the 1 µs timestamp (wraps every 65.536 ms)
    [7:0]   ext   — sub-µs remainder; ns = ts_us*1000 + ext*(1000/clk_mhz)

The capture file is JSON Lines: one header line then one record per batch::

    {"format": "deepskopion-edge-capture/v1", "clk_mhz": 100, "meta": {...}}
    {"seq": 0, "words": [123, 456, ...]}
    {"seq": 1, "words": [...]}

JSONL append makes live capture from the daemon cheap (one line per tick) and
keeps the reader streaming-friendly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

FORMAT_TAG = "deepskopion-edge-capture/v1"

# Bit-field shifts/masks (single source — used by encode and decode).
_INSTANCE_SHIFT = 27
_INSTANCE_MASK = 0x1F
_S_SHIFT, _F_SHIFT, _E_SHIFT = 26, 25, 24
_TS_US_SHIFT, _TS_US_MASK = 8, 0xFFFF
_EXT_MASK = 0xFF
_TS_US_PERIOD = 1 << 16  # ts_us wraps every 65536 µs
#: A backwards ts_us step is a WRAP only when it exceeds this SKEW tolerance, in
#: µs (DS-P1.44). The word stream is not strictly ordered in time: the PL stamps
#: each event when it is sampled, and the S=1 state-readback in the committed
#: DE-C0 retirement capture is stamped 320 ns AFTER the first edge yet arrives
#: BEFORE it — across a µs boundary, so ts_us goes 40894 -> 40893. Reading that
#: 1 µs step-back as a 16-bit wrap added a phantom 65.536 ms to every following
#: transition on the capture that retires ``edge_scl``.
#:
#: Sizing: a genuine wrap steps BACK by ``_TS_US_PERIOD - forward_gap``, so a
#: tolerance T resolves every wrap whose forward gap is under
#: ``_TS_US_PERIOD - T`` (64.512 ms here) and rejects any reorder skew under T.
#: 1024 µs is ~1000x the skew this stream actually exhibits while giving up only
#: 1.024 ms of the unambiguous forward range. Half the period would also reject
#: the skew, but would silently stop unrolling wraps for any line quiet longer
#: than 32.768 ms — a worse trade for a datalogger whose whole job is idle lines.
_TS_US_SKEW_TOLERANCE = 1024



@dataclass(frozen=True)
class EdgeSample:
    """One decoded edge event."""
    instance: int
    timestamp_ns: int      # absolute (wrap-unrolled) ns
    level: int             # post-edge line level (0/1)
    s: int
    f: int
    e: int


def _ns_per_cycle(clk_mhz: int) -> float:
    if clk_mhz <= 0:
        raise ValueError(f"clk_mhz must be positive, got {clk_mhz}")
    return 1000.0 / clk_mhz


def _wrap_base_us(wrap_state: dict, inst: int, ts_us: int) -> int:
    """Advance the per-instance ts_us wrap bookkeeping for one word and return the
    unrolled µs base that word's ts_us should be added to.

    ``wrap_state`` maps instance -> ``[base_us, prev_ts_us]``. Shared by
    :func:`words_to_transitions` and :func:`words_to_initial_level` so the two
    cannot drift apart in how they unroll the same stream — they DID drift in
    effect before DS-P1.44, reporting an initial level and a first transition
    65.536 ms apart on the same capture.

    Only a backwards step LARGER than :data:`_TS_US_SKEW_TOLERANCE` counts as a
    wrap; see that constant for why a small step back is skew, not a wrap."""
    st = wrap_state.setdefault(inst, [0, None])
    prev = st[1]
    if prev is not None and prev - ts_us > _TS_US_SKEW_TOLERANCE:
        st[0] += _TS_US_PERIOD            # this instance's ts_us wrapped
    st[1] = ts_us
    return st[0]


def decode_edge_word(word: int, clk_mhz: int = 100, *, base_us: int = 0) -> EdgeSample:
    """Decode one 32-bit EDGE_EVENT word. ``base_us`` is the unrolled µs base for
    this word's 16-bit ts_us field (the caller tracks wraps; see
    :func:`words_to_transitions`)."""
    word &= 0xFFFFFFFF
    instance = (word >> _INSTANCE_SHIFT) & _INSTANCE_MASK
    s = (word >> _S_SHIFT) & 1
    f = (word >> _F_SHIFT) & 1
    e = (word >> _E_SHIFT) & 1
    ts_us = (word >> _TS_US_SHIFT) & _TS_US_MASK
    ext = word & _EXT_MASK
    abs_us = base_us + ts_us
    t_ns = int(abs_us * 1000 + round(ext * _ns_per_cycle(clk_mhz)))
    # post-edge level: S=1 → E is the direct level; else !E.
    level = e if s else (1 - e)
    return EdgeSample(instance=instance, timestamp_ns=t_ns, level=level, s=s, f=f, e=e)


def encode_edge_word(instance: int, timestamp_ns: int, level: int,
                     clk_mhz: int = 100, *, readback: bool = False) -> int:
    """Encode one edge event into a 32-bit EDGE_EVENT word (inverse of
    :func:`decode_edge_word`). ts_us is the low 16 bits of the µs timestamp."""
    ns_per_cycle = _ns_per_cycle(clk_mhz)
    ts_us_full = timestamp_ns // 1000
    ext = int(round((timestamp_ns - ts_us_full * 1000) / ns_per_cycle)) & _EXT_MASK
    ts_us = ts_us_full & _TS_US_MASK
    s = 1 if readback else 0
    # level→E: readback stores level in E directly; edge stores !level in E.
    e = (level & 1) if readback else (1 - (level & 1))
    f = 0
    return (((instance & _INSTANCE_MASK) << _INSTANCE_SHIFT)
            | (s << _S_SHIFT) | (f << _F_SHIFT) | ((e & 1) << _E_SHIFT)
            | (ts_us << _TS_US_SHIFT) | ext) & 0xFFFFFFFF


def edge_event_word(instance: int, timestamp_ns: int,
                    s: int, f: int, e: int, clk_mhz: int = 100) -> int:
    """Pack a wire EDGE_BATCH entry (raw S/F/E flags + an absolute timestamp)
    into the 32-bit EDGE_EVENT stream word :func:`decode_edge_word` consumes.

    Unlike :func:`encode_edge_word` — which assumes a single edge (S=0, F=0)
    and *derives* E from a desired post-edge level — this preserves S/F/E
    verbatim. That matters for EDGE_BATCH events (DS-P2.47): each entry already
    carries the raw S (read-event), F (FIFO pressure) and E (polarity) bits, and
    the daemon must round-trip them losslessly so RouteWave / sigrok see the same
    flags the PL emitted. ``timestamp_ns`` is the absolute (anchor + ts_delta)
    time; it is folded into the 16-bit ts_us + 8-bit ext fields the same way
    :func:`encode_edge_word` does, so the ts_us-wrap unroll in
    :func:`words_to_transitions` still recovers the absolute timeline."""
    ns_per_cycle = _ns_per_cycle(clk_mhz)
    ts_us_full = timestamp_ns // 1000
    ext = int(round((timestamp_ns - ts_us_full * 1000) / ns_per_cycle)) & _EXT_MASK
    ts_us = ts_us_full & _TS_US_MASK
    return (((instance & _INSTANCE_MASK) << _INSTANCE_SHIFT)
            | ((s & 1) << _S_SHIFT) | ((f & 1) << _F_SHIFT) | ((e & 1) << _E_SHIFT)
            | (ts_us << _TS_US_SHIFT) | ext) & 0xFFFFFFFF


def words_to_transitions(words: List[int], clk_mhz: int = 100,
                         instance: Optional[int] = None) -> List[Tuple[int, int]]:
    """Decode a word stream into absolute (timestamp_ns, level) transitions for
    one instance (or all if ``instance`` is None), unrolling the 16-bit ts_us
    wraps. Output is monotonic in time and deduplicated against no-op repeats.

    The ts_us-wrap base is tracked PER INSTANCE: the live daemon emits SCL and
    SDA (and any other channels) in SEPARATE EDGE_BATCH frames that interleave in
    the word stream, so each channel's ts_us marches independently. A single
    shared wrap-counter would see channel B's ts_us "drop below" channel A's and
    spuriously add a 65.536 ms wrap, corrupting the timeline (DS-T2.35).

    Within ONE instance, only a backwards ts_us step larger than half the period
    is a wrap (DS-P1.44). The stream is not strictly ordered — the S=1 readback in
    the committed DE-C0 retirement capture is stamped 320 ns after the first edge
    but arrives before it — and reading that 1 µs step-back as a wrap put every
    following transition 65.536 ms late, 65.536 ms after the readback that
    :func:`words_to_initial_level` reports for the same capture.

    **S=1 (state-readback) words are NOT transitions** and are excluded (DS-P1.44).
    A readback word carries the datalogger's CURRENT LEVEL at the moment it was
    sampled — the "initial state-readback record" the RTL emits on enable
    (``hw/sim/cocotb/tests/datalogger/infra/full_data_path/test_datalogger_to_ddr.py``),
    which the gather-level contract already separates from edge events
    ("s_bit must be 0 for every edge event (enable-capturing, not readback)",
    ``test_datalogger_gather_edge.py``). Emitting it as a transition broke this
    function's own documented monotonicity: in the committed DE-C0 silicon
    capture the readback is stamped 300 ns AFTER the first real edge but arrives
    BEFORE it in the word stream, so the output ran backwards
    (``(54139340, 0), (54139040, 1), ...``). It also injected a phantom edge into
    :class:`~hw.sim.vip.drivers.edge_replay_source` DUT replay. Readback words
    still participate in wrap tracking (they carry a valid ts_us) and are
    available through :func:`words_to_initial_level`.
    """
    out: List[Tuple[int, int]] = []
    wrap_state: dict = {}   # instance -> [base_us, prev_ts_us]
    for w in words:
        inst = (w >> _INSTANCE_SHIFT) & _INSTANCE_MASK
        base_us = _wrap_base_us(wrap_state, inst, (w >> _TS_US_SHIFT) & _TS_US_MASK)
        if instance is not None and inst != instance:
            continue
        s = decode_edge_word(w, clk_mhz, base_us=base_us)
        if s.s:                              # state readback, not a transition
            continue
        if out and out[-1][1] == s.level and out[-1][0] == s.timestamp_ns:
            continue
        out.append((s.timestamp_ns, s.level))
    return out


def words_to_initial_level(words: List[int], clk_mhz: int = 100,
                           instance: Optional[int] = None
                           ) -> Optional[Tuple[int, int]]:
    """Return ``(timestamp_ns, level)`` of the FIRST S=1 state-readback word for
    ``instance``, or ``None`` if the stream carries no readback.

    This is the information :func:`words_to_transitions` deliberately no longer
    reports as a transition: the line's level at datalogger-enable, which a replay
    consumer needs to initialise its driven signal before the first real edge."""
    wrap_state: dict = {}
    for w in words:
        inst = (w >> _INSTANCE_SHIFT) & _INSTANCE_MASK
        base_us = _wrap_base_us(wrap_state, inst, (w >> _TS_US_SHIFT) & _TS_US_MASK)
        if instance is not None and inst != instance:
            continue
        s = decode_edge_word(w, clk_mhz, base_us=base_us)
        if s.s:
            return (s.timestamp_ns, s.level)
    return None


@dataclass
class EdgeCapture:
    """In-memory capture: a flat word stream + clock + metadata."""
    clk_mhz: int = 100
    words: List[int] = field(default_factory=list)
    meta: Dict = field(default_factory=dict)

    def extend(self, words: List[int]) -> None:
        self.words.extend(int(w) & 0xFFFFFFFF for w in words)

    def transitions(self, instance: Optional[int] = None) -> List[Tuple[int, int]]:
        return words_to_transitions(self.words, self.clk_mhz, instance)

    def instances(self) -> List[int]:
        return sorted({(w >> _INSTANCE_SHIFT) & _INSTANCE_MASK for w in self.words})

    def write(self, path: str) -> None:
        """Write JSONL: header line + one record (all words in one batch)."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"format": FORMAT_TAG, "clk_mhz": self.clk_mhz,
                                 "meta": self.meta}) + "\n")
            fh.write(json.dumps({"seq": 0, "words": self.words}) + "\n")

    @classmethod
    def read(cls, path: str) -> "EdgeCapture":
        cap = cls()
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            header = json.loads(fh.readline())
            if header.get("format") != FORMAT_TAG:
                raise ValueError(f"not a {FORMAT_TAG} file: {header.get('format')!r}")
            cap.clk_mhz = int(header.get("clk_mhz", 100))
            cap.meta = dict(header.get("meta", {}))
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                cap.words.extend(int(w) & 0xFFFFFFFF for w in rec.get("words", []))
        return cap


class EdgeCaptureWriter:
    """Streaming JSONL writer for live daemon capture (one record per tick)."""

    def __init__(self, path: str, clk_mhz: int = 100, meta: Optional[Dict] = None):
        self._fh = open(path, "w", encoding="utf-8")
        self._fh.write(json.dumps({"format": FORMAT_TAG, "clk_mhz": int(clk_mhz),
                                   "meta": dict(meta or {})}) + "\n")
        self._fh.flush()
        self._seq = 0
        self.total_words = 0

    def append(self, words: List[int]) -> None:
        if not words:
            return
        ws = [int(w) & 0xFFFFFFFF for w in words]
        self._fh.write(json.dumps({"seq": self._seq, "words": ws}) + "\n")
        self._fh.flush()
        self._seq += 1
        self.total_words += len(ws)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self) -> "EdgeCaptureWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ─── Max-frequency (min edge-to-edge interval) decode (DS-P3.117) ───────────
#
# edge_datalogger.vhd exposes the FASTEST validated activity seen on the captured
# line as ED_MAX_INTERVAL: the SMALLEST edge-to-edge interval (half-period) in
# CLK ticks since the last re-arm. The host converts that to a frequency. The
# all-ones value is the "no edge measured yet / just re-armed" sentinel.
#
# This is single-sourced here so the value's carrier — the IN-BAND
# EVT_EDGE_MAXFREQ frame on the edge stream (DOMAIN_EDGE|0x02; see zs_msg.py
# parse_edge_maxfreq + decoders.decode_edge, DS-P3.117) — uses the SAME math.
# freq = clk / (2 * interval): an interval is one half-period, so a full
# square-wave period spans two of them.

# Sentinel: ED_MAX_INTERVAL reads all-ones until the first validated interval is
# latched (and again immediately after a host re-arm). The RTL default is a
# 24-bit register; callers pass the actual width.
def maxfreq_is_sentinel(interval: int, width: int = 24) -> bool:
    """True when ED_MAX_INTERVAL is the all-ones 'no max yet / re-armed' value."""
    return (int(interval) & ((1 << width) - 1)) == ((1 << width) - 1)


def maxfreq_interval_to_hz(interval: int, clk_hz: int, width: int = 24) -> Optional[float]:
    """Convert an ED_MAX_INTERVAL (min half-period in CLK ticks) to Hz.

    Returns None for the all-ones sentinel (no measurement yet / re-armed) and
    for a zero/negative interval (impossible — guards against a corrupt read).
    freq_max = clk_hz / (2 * interval).
    """
    iv = int(interval) & ((1 << width) - 1)
    if maxfreq_is_sentinel(iv, width) or iv <= 0:
        return None
    return clk_hz / (2.0 * iv)


# ─── Stream-RLE word decode (DS-P2.16) ───────────────────────────────────────
#
# edge_datalogger in Stream-RLE mode (CONFIG[4:3] = "10") emits one word per
# DOMAIN_EDGE/EVT_EDGE_RLE (0x0609) frame instead of one EDGE_EVENT per edge.
# Word layout (edge_datalogger_protocol.yml rle_* messages):
#
#     [31:27] instance       [26:24] word type       [23:0] payload
#
#     000 / 001  SYNC_LO / SYNC_HI  payload = TIMESTAMP[23:0]; sets the level
#     101        PACK   delta_2[23:16] delta_1[15:8] delta_0[7:0], oldest first;
#                       each 1..255 ticks and toggles the line; 0x00 = empty slot
#     110        LONG   one edge, payload = its delta (256+ ticks)
#     111        IDLE   payload ticks with no edge; time advances, no toggle
#
# An edge's time is the SYNC timestamp plus every delta since, which is the
# TIMESTAMP an event-mode word stamps on the same edge. A SYNC after the first
# re-anchors: the FPGA writes one after it lost a word to a full FIFO.

RLE_SYNC_LO, RLE_SYNC_HI = 0b000, 0b001
RLE_PACK, RLE_LONG, RLE_IDLE = 0b101, 0b110, 0b111
_RLE_TYPE_SHIFT, _RLE_TYPE_MASK, _RLE_PAYLOAD_MASK = 24, 0x7, 0xFFFFFF


class EdgeRleDecoder:
    """Stream-RLE words of ONE edge datalogger → (tick, level) transitions.

    ``tick`` is in TIMESTAMP units (sample-clock ticks), starting from the low
    24 bits a SYNC carries. Words before the first SYNC cannot be placed in time
    and are counted in ``orphans``; ``resyncs`` counts the SYNCs after the first
    (each one marks a gap where the FPGA lost words)."""

    def __init__(self) -> None:
        self.tick: Optional[int] = None
        self.level = 0
        self.resyncs = 0
        self.orphans = 0

    def feed(self, word: int, frame_tick: Optional[int] = None) -> List[Tuple[int, int]]:
        """Decode one word. ``frame_tick`` is the 64-bit tick its zs_msg header
        carries; given it, a SYNC is placed on that wide timeline (the latest
        tick at or before the frame whose low 24 bits match) instead of at its
        bare 24-bit TIMESTAMP."""
        kind = (word >> _RLE_TYPE_SHIFT) & _RLE_TYPE_MASK
        payload = word & _RLE_PAYLOAD_MASK
        if kind in (RLE_SYNC_LO, RLE_SYNC_HI):
            if self.tick is not None:
                self.resyncs += 1
            self.tick = payload if frame_tick is None else (
                frame_tick - ((frame_tick - payload) & _RLE_PAYLOAD_MASK))
            self.level = 1 if kind == RLE_SYNC_HI else 0
            return [(self.tick, self.level)]
        if kind not in (RLE_PACK, RLE_LONG, RLE_IDLE):
            raise ValueError(f"Stream-RLE word {word:#010x}: reserved type {kind:#05b}")
        if self.tick is None:
            self.orphans += 1
            return []
        if kind == RLE_IDLE:
            self.tick += payload
            return []
        deltas = [payload] if kind == RLE_LONG else [
            d for d in (payload & 0xFF, (payload >> 8) & 0xFF, payload >> 16) if d]
        out = []
        for d in deltas:
            self.tick += d
            self.level ^= 1
            out.append((self.tick, self.level))
        return out

    def lose_sync(self) -> None:
        """Forget the time base: words until the next SYNC are orphans. For a
        word the caller could not decode, after which the level and time are
        unknown."""
        self.tick = None


def rle_words_to_transitions(words: List[int]) -> List[Tuple[int, int]]:
    """Decode a whole Stream-RLE word list (one instance) to transitions; the
    first entry is the SYNC anchor's (tick, level)."""
    dec = EdgeRleDecoder()
    out: List[Tuple[int, int]] = []
    for w in words:
        out.extend(dec.feed(w))
    return out
