"""Abstract data source + concrete implementations for the wave_stream_v1 daemon.

The daemon is intentionally decoupled from the PL-side data path: any source
that yields (timestamp, packed_sample, optional annotations) can drive the
wave_stream_v1 server. The implementations today:

  - StubDataSource: synthetic saw-tooth + protocol-shaped traffic, used for
    consumer bring-up without hardware (DS-P2.30).
  - RegbankDataSource: live PS-side reader that drains the deepskopion
    regbank's telemetry counters and surfaces them as annotations to
    RouteWave (DS-P2.31 Phase A). No raw-sample streaming yet — that
    requires DDR-ring drain via rr-ddr-stream (Phase B).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Protocol, Tuple, TYPE_CHECKING

from .ddr_mmap import DdrMmap
from .regbank import Regbank, Regs
from . import decoders, zs_msg, jtag_devices, edge_capture, datalogger_registry
from .protocol import EDGE_ANCHOR_REACH_US
from .native import edge_frames as native_edge_frames
from .jtag_reassembler import JtagReassembler
from .can_reassembler import CanReassembler
from .enable_arbiter import EnableArbiter

if TYPE_CHECKING:
    from .ring_irq import RingIrq

logger = logging.getLogger(__name__)


@dataclass
class Sample:
    ts_ns: int
    packed_value: int  # bit-packed across CHANNELS per channels.py layout


@dataclass
class Annotation:
    channel_id: int
    ts_start_ns: int
    ts_end_ns: int
    kind: str           # e.g. "uart_frame", "i2c_byte"
    text: str           # human-readable label rendered in RouteWave
    color: str = "#4a8c2a"
    is_error: bool = False
    # DS-P3.88: structured, role-tagged decoded fields (see decoders.DecodedFrame).
    # None → omitted from the wire; otherwise serialized under the ANNOTATIONS
    # "payload" key for the consumer's per-protocol columns / detail (LT-P3.168).
    fields: Optional[Dict[str, object]] = None
    # DS-P3.86: the stable handle an HWTX verdict points at (`event_refs`).
    # None until the annotation is first put on the wire, where
    # server._annotations_payload stamps it ONCE. A keyframe that resends this
    # annotation to a late joiner then carries the same id, not a new one.
    event_id: Optional[str] = None


@dataclass
class Batch:
    """One server-side iteration of work — a chunk of SAMPLES + ANNOTATIONS.

    Empty samples list means "no samples this tick"; same for annotations.
    """
    samples: List[Sample] = field(default_factory=list)
    annotations: List[Annotation] = field(default_factory=list)
    # DS-P2.74: the read head reached the commit pointer read at the start of this
    # tick. False while a ring source has a backlog. Sources with no ring: True.
    ring_drained: bool = True
    # DS-P3.69: 32-bit EDGE_EVENT words (raw Edge Detector edges) for RouteWave
    # EdgeStreams; emitted as a TAG_EDGE_EVENTS frame. Empty means none this tick.
    edge_words: List[int] = field(default_factory=list)
    # DS-P2.78: absolute ns of the FIRST edge in edge_words. An EDGE_EVENT word
    # carries only the LOW 16 BITS of its microsecond timestamp, so it wraps every
    # 65.536 ms and cannot be placed in absolute time on its own. Annotations
    # carry absolute ts_start_ns, so without this the two halves of the same
    # stream sit on incompatible timebases — measured 10 DAYS apart on a board
    # with real uptime, which is why RouteWave's edge lanes registered thousands
    # of transitions and drew nothing (LT-P2.105). 0 = not supplied.
    edge_anchor_ns: int = 0
    # DS-P3.138: edge_words cut into wire EDGE_EVENTS frames, each (anchor_ns,
    # words) with every word within protocol.EDGE_ANCHOR_REACH_US of its own
    # anchor. One anchor per batch is not enough: a consumer splices the
    # anchor's high bits onto a word's 16-bit ts_us and takes the NEAREST
    # 65.536 ms window, which is right only within +/-32.768 ms of the anchor —
    # and a DdrRingDataSource batch spans a whole 50 ms poll period, more after
    # a stall or a backlog drain. Filled by add_edge(); a Batch built with
    # edge_words alone (tests, legacy sources) goes out as one frame.
    edge_frames: List[Tuple[int, List[int]]] = field(default_factory=list)
    # DS-P1.74: the native edge path (sw/stream/native/edge_frames.py). When
    # set, every edge of this batch is held in C, add_edge included, and the
    # frames go out as the bytes C packed; edge_words and edge_frames stay
    # empty. Read edges with has_edges() / all_edge_words() / edge_wire_frames().
    native_edges: Optional[object] = None

    def add_edge(self, word: int, ts_ns: int) -> None:
        """Append one EDGE_EVENT word whose absolute time is ``ts_ns``.

        The word joins the current frame while it is within reach of that
        frame's anchor, otherwise it opens a new frame anchored on itself."""
        if self.native_edges is not None:
            self.native_edges.add(word, ts_ns)
            return
        ts_ns = int(ts_ns)
        if not self.edge_words:
            self.edge_anchor_ns = ts_ns
        self.edge_words.append(word)
        if self.edge_frames and edge_within_reach(self.edge_frames[-1][0], ts_ns):
            self.edge_frames[-1][1].append(word)
        else:
            self.edge_frames.append((ts_ns, [word]))

    def has_edges(self) -> bool:
        if self.native_edges is not None and self.native_edges.edge_count():
            return True
        return bool(self.edge_words)

    def edge_wire_frames(self) -> List["EdgeWireFrame"]:
        """The EDGE_EVENTS frames to send for this batch, in order."""
        if self.native_edges is None:
            return [EdgeWireFrame(anchor, len(words), words=words)
                    for anchor, words in self.wire_edge_frames()]
        if self.edge_words or self.edge_frames:
            raise ValueError("Batch holds native edges and Python edge_words; "
                             "build edges with Batch.add_edge")
        native = self.native_edges
        frames = []
        for i in range(native.frame_count()):
            anchor, n = native.info(i)
            frames.append(EdgeWireFrame(anchor, n, native=native, index=i))
        return frames

    def wire_edge_frames(self) -> List[Tuple[int, List[int]]]:
        """The EDGE_EVENTS frames to send for this batch, as (anchor_ns, words)."""
        if self.native_edges is not None:
            return [(f.anchor_ns, f.words()) for f in self.edge_wire_frames()]
        if not self.edge_frames:
            return [(self.edge_anchor_ns, list(self.edge_words))] if self.edge_words else []
        flat = [w for _, words in self.edge_frames for w in words]
        if flat != list(self.edge_words):
            raise ValueError("Batch.edge_frames disagree with edge_words; "
                             "build edges with Batch.add_edge")
        return [(anchor, list(words)) for anchor, words in self.edge_frames]

    def all_edge_words(self) -> List[int]:
        """Every EDGE_EVENT word of this batch, in order. Builds a Python list,
        so a native batch pays per edge here: the capture and test paths only."""
        if self.native_edges is None:
            return list(self.edge_words)
        return [w for f in self.edge_wire_frames() for w in f.words()]


class EdgeWireFrame:
    """One EDGE_EVENTS frame of a Batch: its anchor, its word count, and its
    words, either a Python list or frame ``index`` of a native batch."""

    __slots__ = ("anchor_ns", "n_words", "_words", "_native", "_index")

    def __init__(self, anchor_ns: int, n_words: int, words: Optional[List[int]] = None,
                 native: Optional[object] = None, index: int = 0):
        self.anchor_ns = int(anchor_ns)
        self.n_words = int(n_words)
        self._words = words
        self._native = native
        self._index = index

    @property
    def native(self) -> bool:
        return self._native is not None

    def words(self) -> List[int]:
        if self._native is not None:
            return self._native.words(self._index)
        return list(self._words)

    def pack(self, seq: int, clk_mhz: int) -> bytes:
        """The native frame's wire bytes, as proto.pack_frame would build them."""
        if self._native is None:
            raise ValueError("pack() is for native frames; Python frames go "
                             "through proto.make_edge_events")
        return self._native.pack(self._index, seq, clk_mhz)


def edge_within_reach(anchor_ns: int, ts_ns: int) -> bool:
    """True when a word at ``ts_ns`` decodes back to ``ts_ns`` from an
    EDGE_EVENTS frame anchored at ``anchor_ns`` (DS-P3.138).

    The consumer picks the 65.536 ms window nearest the anchor in whole µs, so
    the word's µs count must lie STRICTLY inside +/-EDGE_ANCHOR_REACH_US of the
    anchor's. At exactly half a window two candidate windows tie."""
    return abs(int(ts_ns) // 1000 - int(anchor_ns) // 1000) < EDGE_ANCHOR_REACH_US


# The new run-length annotation KIND emitted by the storm aggregator. Carried on
# the existing ANNOTATIONS frame — forward-compatible (D.150): a consumer that
# doesn't know it renders the `text` label; RouteWave LT-P3.204 renders it as one
# distinct link-failure row using the `count`/`run_of`/`ongoing` fields below.
ERROR_RUN_KIND = "error_run"


@dataclass
class _ErrorRun:
    """In-flight run of consecutive identical error annotations (one channel,
    one kind). `t0_ns`..`t1_ns` is the span; `count` is the total seen."""
    count: int = 0
    t0_ns: int = 0
    t1_ns: int = 0
    grew_this_batch: bool = False
    storm: bool = False   # crossed the threshold → suppressing individuals
    idle_ticks: int = 0   # DS-P2.302: consecutive empty ticks (no ring data) since the last error


class ErrorStormAggregator:
    """DS-P3.87: collapse a sustained run of identical error annotations into a
    single run-length / link-failure event, instead of N identical per-frame
    annotations (the 4809-UART-STOP-ERROR storm).

    A *run* is consecutive error frames of the SAME ``kind`` on the SAME
    ``channel_id``. The first ``STORM_THRESHOLD`` errors of a run still render
    individually (a one-off / short burst must stay visible); once the run
    exceeds the threshold the per-frame annotations are SUPPRESSED at source and
    the run is summarized as one ``error_run`` event per batch — ``ongoing=True``
    while it grows, then a final ``ongoing=False`` event when the storm ends: a
    batch that carries data but no new errors of that kind ends it at once
    (``flush_batch``), and on an idle bus ``IDLE_END_S`` of empty ticks ends it
    (``flush_idle``, DS-P2.302). ``count`` is always the run
    total (including the few shown individually at the start); the span
    ``t0..t1`` bounds the storm on the RouteWave timeline.

    This bounds the stream for both consumers (RouteWave render cost; agent
    tokens) without losing the signal — the total still flows via
    ``proto_decode_errs`` and the storm's count/span/identity via ``error_run``.
    """

    # A run must exceed this many consecutive same-kind errors before it is
    # treated as a storm (and individuals start being suppressed). Small enough
    # that a real link failure collapses fast; large enough that a couple of
    # stray framing errors still render one-for-one.
    STORM_THRESHOLD = 8

    # DS-P2.302: on an idle bus (a tick that drains no ring data) a run ends only after this much silence. A data
    # batch with no growth still ends it at once, as before. Converted to ticks by the source from its poll period.
    IDLE_END_S = 1.0

    def __init__(self, idle_end_ticks: int = 1) -> None:
        self._runs: Dict[tuple, _ErrorRun] = {}
        self._idle_end_ticks = max(1, int(idle_end_ticks))

    def observe(self, channel_id: int, kind: str, ts_ns: int) -> bool:
        """Record one error frame. Returns True if the caller should EMIT the
        individual annotation, False if it should be SUPPRESSED (storm mode)."""
        key = (channel_id, kind)
        run = self._runs.get(key)
        if run is None:
            run = _ErrorRun(count=0, t0_ns=ts_ns, t1_ns=ts_ns)
            self._runs[key] = run
        run.count += 1
        run.t1_ns = ts_ns
        run.grew_this_batch = True
        run.idle_ticks = 0
        if run.count <= self.STORM_THRESHOLD:
            return True
        run.storm = True
        return False

    def flush_batch(self) -> List[Annotation]:
        """End-of-batch: emit one ``error_run`` annotation per active storm run
        (ongoing while still growing; a final ``ongoing=False`` when a batch
        brought no new errors → the storm has ended). Drops finalized/transient
        runs. Resets the per-batch growth flags."""
        events: List[Annotation] = []
        for key, run in list(self._runs.items()):
            channel_id, kind = key
            if run.storm:
                ongoing = run.grew_this_batch
                label = kind.replace("_", " ").upper()
                state = "ongoing" if ongoing else "ended"
                events.append(Annotation(
                    channel_id=channel_id,
                    ts_start_ns=run.t0_ns,
                    ts_end_ns=run.t1_ns,
                    kind=ERROR_RUN_KIND,
                    # DS-P3.127(b): ASCII-only text — the RouteWave font atlas
                    # can't render U+26A0 (warning sign) or U+00D7 (multiply), so
                    # a storm summary built from them rendered as a missing glyph
                    # and the consumer SAW NO error evidence even though the run
                    # was real. Use "!" and "x" so the only is_error annotation a
                    # storm leaves on the stream is actually visible.
                    text=f"! {label} storm x{run.count} ({state})",
                    color="#cc3333",
                    is_error=True,
                    fields={"run_of": kind, "count": run.count, "ongoing": ongoing},
                ))
                if not ongoing:
                    del self._runs[key]
            elif not run.grew_this_batch:
                # Never reached storm and brought no new errors → a short burst
                # already shown individually; forget it.
                del self._runs[key]
            run.grew_this_batch = False
        return events

    def flush_idle(self) -> List[Annotation]:
        """DS-P2.302: a tick that drained no ring data. Without this, the end of a run was decided only by the next
        batch that carried data, so on an idle bus a storm stayed ongoing until unrelated traffic arrived. A run ends
        here after ``idle_end_ticks`` consecutive empty ticks: a storm emits its final ``ongoing=False`` record, a
        short burst is forgotten. Nothing is emitted for a run that is still within its idle allowance."""
        events: List[Annotation] = []
        for key, run in list(self._runs.items()):
            run.idle_ticks += 1
            if run.idle_ticks < self._idle_end_ticks:
                continue
            if run.storm:
                run.grew_this_batch = False
                events.extend(self.flush_batch_for(key))
            else:
                del self._runs[key]
        return events

    def flush_batch_for(self, key: tuple) -> List[Annotation]:
        """The final record of one storm run (the flush_batch logic for a run that did not grow), removing it."""
        run = self._runs.pop(key)
        channel_id, kind = key
        label = kind.replace("_", " ").upper()
        return [Annotation(
            channel_id=channel_id, ts_start_ns=run.t0_ns, ts_end_ns=run.t1_ns, kind=ERROR_RUN_KIND,
            text=f"! {label} storm x{run.count} (ended)", color="#cc3333", is_error=True,
            fields={"run_of": kind, "count": run.count, "ongoing": False},
        )]


class RollingDigest:
    """DS-P3.89: a rolling-window per-protocol digest + a standing anomaly
    verdict — the streaming sibling of the sim HWTX digest/verdict (RTL-P3.709).

    A live datalogger is an *infinite* stream: neither RouteWave Mission Control nor
    an agent can ingest every frame. This aggregator collapses the last
    ``window_s`` of decoded traffic into a small, self-contained summary —
    per-protocol packet rate + error %, a last-N tail of error events, and a
    standing verdict ('UART error rate 44% — likely framing/baud fault') — that
    the daemon emits periodically as a DIGEST frame.

    Thread model mirrors the existing ``_decode_errs`` split: ``observe()`` is
    called from the single drainer thread (per decoded frame), ``snapshot()`` is
    called from the heartbeat thread. Unlike the GIL-atomic int increments of
    ``_decode_errs``, the rolling deques need a real lock (append + trim + read
    are multi-step), so both methods take ``self._lock``. The lock is held only
    for cheap deque ops — never across IO.
    """

    WINDOW_S = 10.0          # rolling window the rates / percentages cover
    MIN_SAMPLES = 8          # a protocol needs ≥ this many pkts before a verdict
    WARN_ERR_PCT = 10.0      # err% ≥ this → a 'warning' anomaly
    CRIT_ERR_PCT = 25.0      # err% ≥ this → a 'critical' anomaly
    RECENT_N = 8             # last-N error events carried in the digest tail
    _MIN_RATE_SPAN_S = 1.0   # floor for the rate denominator (avoids early-stream
    #                          single-batch spans dividing by ~0 → absurd rates)

    # Per-protocol fault hint appended to a verdict message — the human read of a
    # sustained error rate. DS-P3.126: the hint must reflect the HW-vs-decode
    # split. A sustained error rate has two very different causes:
    #
    #   - HW/bus errors (proto_errs / hdr.is_error) dominate → the physical bus is
    #     faulting: addressing/ACK on I2C, framing/baud on UART, mode/CPHA on SPI.
    #   - SW-decoded errors dominate while HW errors are ~0 → the bus is healthy
    #     but the SW decoder is mis-reading it (sampling/decode issue) — e.g. a
    #     register-less I2C read whose final master-NACK is counted as a "decode
    #     error" even though the bus ACKed (DS-P3.127).
    #
    # The static map (pre-DS-P3.126) alway read "addressing/ACK fault" for I2C,
    # which is wrong when HW errors are 0 and only decode errors are climbing.
    _FAULT_HINT_HW = {
        "uart": "likely framing/baud fault",
        "i2c":  "likely addressing/ACK fault",
        "spi":  "likely mode/CPHA fault",
        "qspi": "likely mode/CPHA fault",
    }
    _FAULT_HINT_HW_DEFAULT = "sustained protocol errors"
    # Decode-dominated (HW errors ~0): the SW decoder, not the bus, is the suspect.
    _FAULT_HINT_DECODE = {
        "uart": "likely decode/sampling issue (HW bus clean)",
        "i2c":  "likely decode/sampling issue (HW bus clean)",
        "spi":  "likely decode/sampling issue (HW bus clean)",
        "qspi": "likely decode/sampling issue (HW bus clean)",
    }
    _FAULT_HINT_DECODE_DEFAULT = "likely decode/sampling issue (HW bus clean)"
    # A run is treated as "HW dominated" when at least this fraction of the
    # window's errors carried the HW/bus error flag. Below it, the errors are
    # SW-decode dominated and the decode hint applies.
    _HW_DOMINANCE_FRAC = 0.5

    def __init__(self, window_s: float = WINDOW_S) -> None:
        self._window_s = float(window_s)
        self._window_ns = int(window_s * 1_000_000_000)
        self._lock = threading.Lock()
        # proto -> deque[(ts_ns, is_error, hw_error)] within the rolling window.
        # DS-P3.126: hw_error distinguishes a HW/bus fault (hdr.is_error / the
        # ERR_CNT_* bus) from a pure SW-decode error so the verdict hint can name
        # the right suspect (addressing/ACK fault vs decode/sampling issue).
        self._events: Dict[str, Deque[Tuple[int, bool, bool]]] = {}
        # Last-N *error* events (the interesting tail for an agent).
        self._recent: Deque[Dict[str, object]] = deque(maxlen=self.RECENT_N)
        self._newest_ts = 0   # max ts observed → the window's "now" (timeline-based,
        #                       not wallclock, so replay and live behave identically)

    def observe(self, proto: str, is_error: bool, ts_ns: int,
                kind: Optional[str] = None, text: str = "",
                hw_error: bool = False) -> None:
        """Record one decoded protocol frame. ``proto`` is the domain name
        (uart/i2c/spi/qspi/edge/i2s); ``is_error`` marks a decoded error; ``kind``
        / ``text`` enrich the last-N error tail. ``hw_error`` (DS-P3.126) marks an
        error that the HW/bus error path raised (hdr.is_error / ERR_CNT_*), as
        opposed to a pure SW-decode error — the verdict uses the HW-vs-decode split
        to pick the right fault hint. Cheap: append + trim under lock."""
        with self._lock:
            if ts_ns > self._newest_ts:
                self._newest_ts = ts_ns
            dq = self._events.get(proto)
            if dq is None:
                dq = deque()
                self._events[proto] = dq
            dq.append((int(ts_ns), bool(is_error), bool(hw_error)))
            if is_error:
                self._recent.append({
                    "ts": int(ts_ns), "kind": kind or proto,
                    "text": text, "is_error": True, "hw_error": bool(hw_error),
                })
            self._trim(self._newest_ts)

    def _trim(self, now_ns: int) -> None:
        """Drop events older than the window from the left of every deque."""
        floor = now_ns - self._window_ns
        for dq in self._events.values():
            while dq and dq[0][0] < floor:
                dq.popleft()

    def snapshot(self) -> Optional[dict]:
        """Build the DIGEST payload body (protocols / recent / verdict), or None
        if nothing has been observed yet (→ the heartbeat emits no DIGEST). The
        rate denominator is the observed span within the window (floored at
        ``_MIN_RATE_SPAN_S``), so it converges to the true windowed rate as the
        window fills rather than understating against the nominal 10 s early on."""
        with self._lock:
            now = self._newest_ts
            self._trim(now)
            protocols: Dict[str, dict] = {}
            anomalies: List[dict] = []
            for proto, dq in self._events.items():
                if not dq:
                    continue
                pkts = len(dq)
                errs = sum(1 for _, e, _ in dq if e)
                hw_errs = sum(1 for _, e, hw in dq if e and hw)
                span_s = max((dq[-1][0] - dq[0][0]) / 1_000_000_000.0,
                             self._MIN_RATE_SPAN_S)
                pkt_rate = pkts / span_s
                err_pct = (100.0 * errs / pkts) if pkts else 0.0
                protocols[proto] = {
                    "pkts": pkts,
                    "errs": errs,
                    # DS-P3.126: surface the HW-vs-decode split so a consumer can
                    # see WHY the verdict picked its hint. hw_errs are errors that
                    # carried the HW/bus flag; the remainder are SW-decode errors.
                    "hw_errs": hw_errs,
                    "decode_errs": errs - hw_errs,
                    "pkt_rate": round(pkt_rate, 2),
                    "err_pct": round(err_pct, 1),
                }
                # Standing anomaly verdict: a sustained error rate over enough
                # samples. Below MIN_SAMPLES we stay silent (too little evidence).
                if pkts >= self.MIN_SAMPLES and err_pct >= self.WARN_ERR_PCT:
                    severity = "critical" if err_pct >= self.CRIT_ERR_PCT else "warning"
                    # DS-P3.126: pick the hint from the HW-vs-decode split. When
                    # the HW/bus error path raised most of these, name the physical
                    # fault (addressing/ACK …); when HW errors are ~0 and only the
                    # SW decoder is flagging them, name a decode/sampling issue.
                    hw_dominated = (errs > 0 and
                                    (hw_errs / errs) >= self._HW_DOMINANCE_FRAC)
                    if hw_dominated:
                        hint = self._FAULT_HINT_HW.get(
                            proto, self._FAULT_HINT_HW_DEFAULT)
                    else:
                        hint = self._FAULT_HINT_DECODE.get(
                            proto, self._FAULT_HINT_DECODE_DEFAULT)
                    anomalies.append({
                        "protocol": proto,
                        "severity": severity,
                        "metric": "err_pct",
                        "value": round(err_pct, 1),
                        "hw_errs": hw_errs,
                        "decode_errs": errs - hw_errs,
                        # ASCII '-' (not em-dash): the RouteWave font atlas can't
                        # render U+2014, so a runtime headline must stay in-range.
                        "message": f"{proto.upper()} error rate "
                                   f"{err_pct:.0f}% - {hint}",
                    })
                recent = list(self._recent)
            if not protocols:
                return None
        # Lock released — derive the verdict status/headline (pure, no shared state).
        return {
            "window_s": self._window_s,
            "protocols": protocols,
            "recent": recent,
            "verdict": self._verdict(anomalies, protocols),
        }

    @classmethod
    def _verdict(cls, anomalies: List[dict], protocols: Dict[str, dict]) -> dict:
        """Roll the per-protocol anomalies into a standing verdict: an overall
        status (CRITICAL > WARN > OK) + a single human headline. The headline is
        the worst anomaly's message, or an 'all nominal' line when clean."""
        has_crit = any(a["severity"] == "critical" for a in anomalies)
        has_warn = any(a["severity"] == "warning" for a in anomalies)
        status = "CRITICAL" if has_crit else ("WARN" if has_warn else "OK")
        if anomalies:
            # Worst first: critical before warning, then by error %.
            worst = sorted(anomalies, key=lambda a: (a["severity"] != "critical",
                                                     -a["value"]))[0]
            headline = worst["message"]
        else:
            total = sum(p["pkts"] for p in protocols.values())
            headline = (f"all nominal - {len(protocols)} protocol(s), "
                        f"{total} pkts in window, 0 anomalies")
        return {"status": status, "headline": headline, "anomalies": anomalies}


# DS-P3.72: consumer→producer CONTROL apply. The 16-bit DATALOGGER_CONFIG word is
# ADDRESSED, not flat: [device_id(15:11) | payload(10:0)] — config_control routes
# by the top 5 bits and hands the matched datalogger only payload[10:0] as CONFIG_OUT
# (datalogger_gather_contract §"Config word layout", ratified v1.0 DS-P3.76). So the
# per-datalogger config space is 11 bits; cfg_word bits ≥ 11 would land in the
# routing id, not config. Live host knobs today: enable[0] (all); QSPI CPHA[1] +
# half_duplex[2] + no_cs[3]. There is NO cfg_valid bit (config_control commits on
# word-change). QSPI CPOL is auto-detected; UART format bits are decoded but
# currently dangling (always auto-calibrates, DS-T3.9); I2C is enable-only.
_DATALOGGER_CFG_DELIVERABLE = 0x07FF   # payload = CONFIG bits [10:0]


def _control_reject_reason(datalogger_id: int, cfg_word: int,
                           op: str = "set_config") -> Optional[str]:
    """Validate a CONTROL request without writing anything (None = valid)."""
    if op != "set_config":
        return f"unsupported op {op!r} (v0.1 only 'set_config')"
    if not 0 <= datalogger_id <= 0x1F:
        return f"datalogger_id {datalogger_id} out of 5-bit range [0,31]"
    if cfg_word & ~_DATALOGGER_CFG_DELIVERABLE:
        return (f"cfg_word 0x{cfg_word:04x} sets bits [15:11] = the device-id routing "
                f"field, not config (per-datalogger payload is CONFIG[10:0])")
    return None


def apply_datalogger_config(regbank, datalogger_id: int, cfg_word: int,
                         op: str = "set_config") -> Optional[str]:
    """Apply a CONTROL frame to the DATALOGGER_CONFIG broadcast register (0x0020).

    Returns None on success, or a human reason string on reject (the daemon
    logs it; v0.1 is fire-and-forget — no fatal ERR, see DS-P3.72/D-record).
    The config_control of the matching device_id latches the word; an
    unconfigured id is a harmless no-op (structured reject feedback is the
    DS-P3.75 CONTROL_ACK job)."""
    reason = _control_reject_reason(datalogger_id, cfg_word, op)
    if reason is not None:
        return reason
    word = ((datalogger_id & 0x1F) << 11) | (cfg_word & _DATALOGGER_CFG_DELIVERABLE)
    regbank.write_u32(Regs.DATALOGGER_CONFIG, word)
    return None


def _make_enable_arbiter(regbank) -> EnableArbiter:
    """DS-P3.74: one enable arbiter per regbank — every CONFIG write (host
    CONTROL or trigger enable-mask) is composed there before it lands."""
    return EnableArbiter(
        lambda dl_id, word: apply_datalogger_config(regbank, dl_id, word))


def apply_stimulus(regbank, protocol: str, route: Optional[str] = None) -> Optional[str]:
    """DS-P2.136: arm the on-board stimulus generator (ds_stimulus_gen) for
    ``protocol`` from the DAEMON, via the same DS-P1.58 production register
    sequence the witness armer uses — one arming code path, so the daemon and
    the side-script cannot drift. Regbank WRITES only; the ring reader is not
    touched. Returns None on success or a reject reason.

    The armer is production code (``stimulus_armer``), never testbench: the DE25
    rootfs ships sw/stream without sw/stream/testbench."""
    from .stimulus_armer import ARMERS, PROTOCOLS, ROUTED_ARMERS
    armer = ARMERS.get(protocol)
    if armer is None:
        return f"unknown stimulus protocol {protocol!r}; expected one of {PROTOCOLS}"
    if route is None:
        armer(regbank)
        return None
    # DS-P2.343: the arm writes the edge route into EMIO_MUX_SEL before its burst.
    from .edge_route import ROUTES
    if protocol not in ROUTED_ARMERS:
        return f"stimulus {protocol!r} takes no edge route; routed arms: {sorted(ROUTED_ARMERS)}"
    if route not in ROUTES:
        return f"unknown edge route {route!r}; expected one of {sorted(ROUTES)}"
    armer(regbank, route=route)
    return None


# DS-P3.108b: the CAN datalogger's CAN-FD data-phase bit period is a BANKED config —
# the SAME device_id (20) with a type bit selects which config word it is:
# payload[10]=0 ⇒ {bit_prescaler,enable}; payload[10]=1 ⇒ bank 1. One id, no
# pollution. (Preferred over a pseudo-id; preserves one-id-one-datalogger.)
# DS-T2.97: bank 1 carries three sub-words, each latched on its own:
#   [9]=0        data_cyc[8:0]            (1..511 clk_sys cycles per data bit)
#   [9]=1 [8]=0  nominal sample point [7:0] / 256   (0 ⇒ 192 = 75 %)
#   [9]=1 [8]=1  data sample point    [7:0] / 256   (0 ⇒ 192 = 75 %)
# Single-sourced in enable_arbiter (the DS-P3.74 arbiter must know the data bank
# is not an enable word); re-exported here for existing callers.
from .enable_arbiter import CAN_DEV_ID, CAN_CFG_TYPE_DATA  # noqa: E402,F401


CAN_CFG_SP = 0x200           # bank-1 sub-word: a sample point (DS-T2.97)
CAN_CFG_SP_DATA = 0x100      # with CAN_CFG_SP: the data sample point (else the nominal)
CAN_DATA_CYC_MAX = 0x1FF     # data_cyc is [8:0] since DS-T2.97


def set_can_data_prescaler(regbank, data_cyc_per_bit: int) -> Optional[str]:
    """Set the CAN datalogger's CAN-FD data-phase bit period (clk_sys cycles per data
    bit) at runtime (DS-P3.108b). The data sample point is its own register
    (set_can_sample_points). 0 is reserved (= the power-on default, 2 Mbit @ 100 MHz =
    50). E.g. 5 Mbit @ 100 MHz → 20. Writes the CAN datalogger's id with the type bit
    set (bank 1, [9]=0). Values above 511 are rejected, not clamped: [9] selects a
    sample-point sub-word (DS-T2.97). Returns None / reason."""
    if not 1 <= data_cyc_per_bit <= CAN_DATA_CYC_MAX:
        return f"data_cyc_per_bit {data_cyc_per_bit} out of the 9-bit range [1,{CAN_DATA_CYC_MAX}]"
    return apply_datalogger_config(regbank, CAN_DEV_ID, CAN_CFG_TYPE_DATA | data_cyc_per_bit)


def set_can_sample_points(regbank, nominal_256: int, data_256: int,
                          data_cyc_per_bit: int) -> Optional[str]:
    """Program the CAN datalogger with the bus's sample points (DS-T2.97), each a
    fraction of its own bit in 1/256 (224 = 87.5 %, 189 ≈ 0.740), then data_cyc.

    A CAN FD transmitter switches to the data rate at ITS nominal sample point, so the
    datalogger must switch at the same one: at 250k/2M Linux sets 87.5 % (measured on
    the Zybo CTU cores), and a datalogger at its 75 % reset value reads every field
    after BRS one bit late. The sub-words are written first and data_cyc last, so the
    last bank-1 word holds the data rate. All three are range-checked before any
    write. Returns None / reason."""
    for name, v in (("nominal_256", nominal_256), ("data_256", data_256)):
        if not 1 <= v <= 0xFF:
            return f"{name} {v} out of range [1,255] (fraction of a bit in 1/256)"
    if not 1 <= data_cyc_per_bit <= CAN_DATA_CYC_MAX:
        return f"data_cyc_per_bit {data_cyc_per_bit} out of the 9-bit range [1,{CAN_DATA_CYC_MAX}]"
    for word in (CAN_CFG_TYPE_DATA | CAN_CFG_SP | nominal_256,
                 CAN_CFG_TYPE_DATA | CAN_CFG_SP | CAN_CFG_SP_DATA | data_256):
        err = apply_datalogger_config(regbank, CAN_DEV_ID, word)
        if err:
            return err
    return set_can_data_prescaler(regbank, data_cyc_per_bit)


def build_proto_stats(r) -> dict:
    """LT-P3.250 / DS-P3.120: a SELF-DESCRIBING per-protocol STATS map keyed by
    EVERY active datalogger protocol — the identity set single-sourced from
    ``datalogger_registry`` (generated from datalogger_config.yml, the same source
    datalogger_gather_gen.py uses; no second hardcoded list here).

    For a protocol WITH a regbank counter slot the real counter value is read;
    for one WITHOUT (CAN/JTAG today — their proto_counters are computed in
    deepskopion_top.vhd but never fanned into a regbank slot, DS-T2.56) the
    field is ``None`` (msgpack nil) so RouteWave renders "n/a" (truthful, like
    arb_grants) instead of a fabricated/stale number.

    ``r`` is regbank.read_u32. Returns an ORDERED dict keyed by
    datalogger_registry.ACTIVE_PROTOCOLS, each value ``{pkts, errs, drops}``.
    """
    stats: dict = {}
    for proto in datalogger_registry.ACTIVE_PROTOCOLS:
        suffix = datalogger_registry.REGBANK_SUFFIX[proto]
        slots = datalogger_registry.SLOTS[proto]
        stats[proto] = {
            "pkts": r(getattr(Regs, f"PKT_CNT_{suffix}")) if slots["pkts"] else None,
            "errs": r(getattr(Regs, f"ERR_CNT_{suffix}")) if slots["errs"] else None,
            "drops": r(getattr(Regs, f"DROP_CNT_{suffix}")) if slots["drops"] else None,
        }
    return stats


def read_regbank_stats(regbank: Regbank) -> dict:
    """DS-P3.82: snapshot the regbank telemetry for a wave_stream STATS frame.

    Reads the free-running counters + rom_info identity straight from the
    regbank and returns a dict of the STATS payload fields (minus seq/daemon_ver,
    which the heartbeat adds). Best-effort and lock-free: each read is an aligned
    32-bit access, and occupancy is derived from the commit/read pointers — a
    torn pointer read at worst skews the fill fraction by one batch, harmless for
    telemetry. ring_used = (commit - read) mod ring_bytes (the drainer reads up
    to RING_COMMIT_PTR, DS-T2.20; RING_READ_PTR is how far the PS has consumed)."""
    r = regbank.read_u32
    ring_bytes = r(Regs.RING_BYTES)
    ring_used = (r(Regs.RING_COMMIT_PTR) - r(Regs.RING_READ_PTR)) % ring_bytes if ring_bytes else 0
    alarm = r(Regs.RING_ALARM_CNT)   # bits[30:0]=#excursions≥75%, bit31=live alarm
    return {
        # identity
        "rtl_git_hash": f"{r(Regs.GIT_HASH):08x}",
        "rtl_build_date": f"0x{r(Regs.BUILD_DATE):08x}",
        # ring / DMA
        "ring_bytes": ring_bytes,
        "ring_used_bytes": ring_used,
        "ddr_backpressure_cnt": r(Regs.DDR_BACKPRESSURE_CNT),
        "ring_alarm": {"excursions": alarm & 0x7FFFFFFF, "active": bool(alarm >> 31)},
        # per-protocol — SELF-DESCRIBING map keyed by EVERY active datalogger
        # protocol (LT-P3.250 / DS-P3.120). The identity set is single-sourced
        # from datalogger_registry (generated from datalogger_config.yml) — NOT a
        # second hardcoded list here, which is exactly the drift that showed a
        # stale `i2s` row and dropped `can`/`jtag`. Each value is
        # {pkts, errs, drops}; null where the protocol has no regbank counter
        # slot (CAN/JTAG today, DS-T2.56) so the consumer renders "n/a".
        "proto_stats": build_proto_stats(r),
        # FIFO health + arbiter fairness — single-tier scalars (DS-P3.94 ph3).
        # The "Stream" tier was deprecated (D.4): the arbiter collapsed to ONE
        # tier, so the *_RSVD regbank slots (0xC8/0x134) read 0 forever. The old
        # {burst,stream} nesting is gone — these are now plain scalars.
        "fifo_overflows": r(Regs.FIFO_OVERFLOW_CNT),
        "fifo_high_watermark": r(Regs.FIFO_HIGH_WATERMARK),
        # DS-T2.33: the ARB_GRANT_CNT regbank slot (0x110) is declared but NEVER
        # driven in RTL — deepskopion_top.vhd fans PKT/ERR/DROP/RING into
        # telemetry_in but has no telemetry_in(C_INDEX_ARB_GRANT_CNT/4) driver,
        # and datalogger_gather.vhd has no grant counter signal at all. The slot
        # therefore always reads 0 even when capture is healthy. Reporting that
        # raw 0 is MISLEADING — a reader sees grants=0 and wrongly concludes "no
        # traffic". Until the RTL drives it, emit null so the consumer renders
        # "n/a" instead of a false zero.
        "arb_grants": None,   # n/a — undriven in RTL (DS-T2.33)
        # liveness / clock-reset
        "heartbeat": r(Regs.HEARTBEAT_CNT),
        "clk_ddr_ticks": r(Regs.CLK_DDR_TICKS),
        "ddr_reset_status": r(Regs.DDR_RESET_STATUS),
    }


class DataSource(Protocol):
    """Anything that can be drained into a wave_stream_v1 batch.

    Implementations decide their own batching cadence — the server simply
    calls next_batch() in a loop and emits whatever is returned. Returning
    Batch with empty lists is fine (the server skips emitting that tick);
    returning None signals end-of-stream and the server sends EOF.
    """

    def sample_clk_hz(self) -> int:
        """Effective sample rate the consumer should use as its base clock."""
        ...

    def next_batch(self) -> Optional[Batch]:
        """Return the next chunk of work, or None to signal clean EOF."""
        ...

    def apply_config(self, datalogger_id: int, cfg_word: int,
                     op: str = "set_config") -> Optional[str]:
        """DS-P3.72: apply an inbound CONTROL frame. None=ok, str=reject reason."""
        ...

    def read_stats(self) -> Optional[dict]:
        """DS-P3.82: telemetry snapshot for a STATS frame (see
        read_regbank_stats), or None if this source has no regbank — the
        heartbeat then emits no STATS for it."""
        ...

    def read_digest(self) -> Optional[dict]:
        """DS-P3.89: rolling-window digest body (per-protocol rates + error %,
        last-N, anomaly verdict) for a DIGEST frame, or None if this source does
        not maintain one (the heartbeat then emits no DIGEST for it). Optional —
        the server calls it via getattr so sources predating it are unaffected."""
        ...


# ---------------------------------------------------------------------------
# Stub source — synthetic data for protocol bring-up without hardware
# ---------------------------------------------------------------------------


class StubDataSource:
    """Synthetic source that emits saw-tooth + protocol-shaped traffic.

    Mirrors mock_wave_stream.py's pattern so the consumer's eyes-on test is
    identical when pointed at deepskopion or at the routertl mock. Useful
    during deepskopion's pre-ship phase to validate the daemon's wire layer
    without needing the FPGA bitstream loaded.
    """

    def __init__(self, sample_clk_hz: int = 100_000,
                 samples_per_batch: int = 1000,
                 emit_annotations: bool = True,
                 duration_s: float = 0.0) -> None:
        self._sample_clk_hz = int(sample_clk_hz)
        self._samples_per_batch = int(samples_per_batch)
        self._emit_annotations = bool(emit_annotations)
        self._duration_s = float(duration_s)

        self._dt_ns = int(round(1e9 / self._sample_clk_hz))
        self._sample_index = 0
        self._t0_ns = 0
        self._wall_start = time.monotonic()
        self._last_annotation_ts_ns = 0
        # Annotations once per ms of sample time, mirroring mock cadence.
        self._annotation_period_ns = 1_000_000

    def sample_clk_hz(self) -> int:
        return self._sample_clk_hz

    def apply_config(self, datalogger_id: int, cfg_word: int,
                     op: str = "set_config") -> Optional[str]:
        # No regbank in stub mode — controls can't be applied.
        return "control unsupported by the stub source (no regbank)"

    def read_stats(self) -> Optional[dict]:
        return None   # stub has no regbank — no telemetry to stream

    def next_batch(self) -> Optional[Batch]:
        if self._duration_s > 0 and (time.monotonic() - self._wall_start) >= self._duration_s:
            return None

        batch = Batch()
        for k in range(self._samples_per_batch):
            i = self._sample_index + k
            ts_ns = self._t0_ns + i * self._dt_ns
            packed = self._synth_pack(i)
            batch.samples.append(Sample(ts_ns=ts_ns, packed_value=packed))

        if self._emit_annotations:
            # Emit one annotation per ms of sample time elapsed.
            batch_end_ts_ns = self._t0_ns + (self._sample_index + self._samples_per_batch) * self._dt_ns
            while self._last_annotation_ts_ns + self._annotation_period_ns <= batch_end_ts_ns:
                self._last_annotation_ts_ns += self._annotation_period_ns
                ts = self._last_annotation_ts_ns
                # Demo UART byte annotation on uart_byte (channel 100)
                byte_val = (ts // self._annotation_period_ns) & 0xFF
                batch.annotations.append(Annotation(
                    channel_id=100,
                    ts_start_ns=ts,
                    ts_end_ns=ts + 80_000,
                    kind="uart_frame",
                    text=f"0x{byte_val:02X}",
                    color="#4a8c2a",
                    is_error=False,
                    fields={"byte": byte_val},
                ))

        self._sample_index += self._samples_per_batch

        # Pace ourselves to roughly real-time so a consumer streaming at
        # sample_clk_hz isn't drowned by the producer.
        target_wall_s = (self._sample_index * self._dt_ns) / 1e9
        actual_wall_s = time.monotonic() - self._wall_start
        if target_wall_s > actual_wall_s:
            time.sleep(target_wall_s - actual_wall_s)

        return batch

    def _synth_pack(self, i: int) -> int:
        """Pack 14 bits of synthetic data per channels.py layout.

        Pattern designed to exercise both fast-changing and quiet channels:
          - level_pin[0..7]: rotating bit pattern (counter & 0xFF lower 8)
          - uart_rx/tx:      square waves at rate/8 and rate/16
          - i2c_sda/scl:     square waves at rate/32 and rate/64
          - qspi_clk:        toggles every sample
          - qspi_cs:         long quiet (high) with periodic low pulses
        """
        level = i & 0xFF
        uart_rx = (i // 4) & 0x1
        uart_tx = (i // 8) & 0x1
        i2c_sda = (i // 16) & 0x1
        i2c_scl = (i // 32) & 0x1
        qspi_clk = i & 0x1
        qspi_cs = 0 if (i % 256 < 16) else 1
        return ((level & 0xFF) << 0) \
             | ((uart_rx & 0x1) << 8) \
             | ((uart_tx & 0x1) << 9) \
             | ((i2c_sda & 0x1) << 10) \
             | ((i2c_scl & 0x1) << 11) \
             | ((qspi_clk & 0x1) << 12) \
             | ((qspi_cs & 0x1) << 13)


# ---------------------------------------------------------------------------
# Live PL data source — DS-P2.31 Phase A (regbank telemetry counters)
# ---------------------------------------------------------------------------


# Per-protocol stat channel id (drives the `protocol_stats` annotation lane,
# id=202 in channels.py). Each tick's nonzero PKT_CNT_* / ERR_CNT_* delta
# becomes one annotation on this channel.
_PROTO_CHANNEL = 202
_HEARTBEAT_CHANNEL = 200
_FIFO_OVERFLOW_CHANNEL = 201

# Protocol → (pkt_cnt_offset, err_cnt_offset) pairs.
_PROTO_COUNTERS = {
    "i2c":  (Regs.PKT_CNT_I2C,  Regs.ERR_CNT_I2C),
    "spi":  (Regs.PKT_CNT_SPI,  Regs.ERR_CNT_SPI),
    "uart": (Regs.PKT_CNT_UART, Regs.ERR_CNT_UART),
    "qspi": (Regs.PKT_CNT_QSPI, Regs.ERR_CNT_QSPI),
    "edge": (Regs.PKT_CNT_EDGE, Regs.ERR_CNT_EDGE),
    "i2s":  (Regs.PKT_CNT_I2S,  None),
}


class RegbankDataSource:
    """Drain regbank telemetry counters and emit them as annotations.

    Phase A of DS-P2.31 — no raw-sample streaming yet (no SAMPLE_DATA slot
    exists in the regbank; Phase B will pump DDR-resident samples via the
    rr-ddr-stream ring already wired in deepskopion_top.vhd's u_ddr_burst).

    Per poll tick:
      - Optionally pulse TELEMETRY_SNAPSHOT_TRIGGER so all counters latch
        atomically before the read sweep.
      - Read HEARTBEAT_CNT → emit one pl_heartbeat annotation echoing the
        counter (proves the PS↔PL MMIO loop is live, regardless of
        datalogger activity).
      - For each protocol with a nonzero PKT_CNT delta, emit one
        protocol_stats annotation describing the delta on its decoder
        channel (e.g. PKT_CNT_UART delta → ts annotation on uart_byte=100).
      - Same for ERR_CNT_* (is_error=True, red).
      - BURST/STREAM FIFO overflow increments → sticky alarm annotations
        on fifo_overflow=201.

    samples list stays empty until Phase B.

    Real-time pacing: next_batch sleeps up to poll_period_s so the daemon
    doesn't burn CPU. duration_s>0 stops the source after the wall-clock
    budget; 0 = stream forever until consumer disconnects.
    """

    # Channel-id targets for per-protocol stat annotations. Decoder lanes
    # already exist for the ones with native channels (uart_byte=100,
    # i2c_frame=101, qspi_xact=102); the rest go on protocol_stats=202.
    _PROTO_CHANNEL_MAP = {
        "uart": 100,  # uart_byte
        "i2c":  101,  # i2c_frame
        "qspi": 102,  # qspi_xact
        "spi":  _PROTO_CHANNEL,
        "edge": _PROTO_CHANNEL,
        "i2s":  _PROTO_CHANNEL,
    }

    def __init__(self,
                 regbank: Regbank,
                 sample_clk_hz: int = 100_000_000,
                 poll_period_s: float = 0.05,
                 duration_s: float = 0.0,
                 snapshot_trigger: bool = True,
                 clock: Optional[callable] = None) -> None:
        self._regbank = regbank
        self._enable = _make_enable_arbiter(regbank)
        self._sample_clk_hz = int(sample_clk_hz)
        self._poll_period_s = float(poll_period_s)
        self._duration_s = float(duration_s)
        self._snapshot_trigger = bool(snapshot_trigger)
        # Injectable monotonic clock for deterministic tests.
        self._clock = clock or time.monotonic

        self._wall_start = self._clock()
        self._prev_counters: dict[int, int] = {}
        self._tick_index = 0

    def sample_clk_hz(self) -> int:
        return self._sample_clk_hz

    def apply_config(self, datalogger_id: int, cfg_word: int,
                     op: str = "set_config") -> Optional[str]:
        # DS-P3.74: host CONFIG is the enable baseline; it goes through the
        # arbiter so an active trigger enable-mask is honoured, never clobbered.
        reason = _control_reject_reason(datalogger_id, cfg_word, op)
        if reason is not None:
            return reason
        return self._enable.apply_host(datalogger_id, cfg_word)

    def apply_trigger_enable_mask(self, allow_mask: int) -> Optional[str]:
        """DS-P3.39 trigger enable-mask path (DS-P3.74 rule): a trigger may only
        gate host-enabled dataloggers off (effective = host AND allow)."""
        return self._enable.apply_trigger_mask(allow_mask)

    def release_trigger_enable_mask(self) -> Optional[str]:
        """Trigger revert: the latest host baseline stands again."""
        return self._enable.release_trigger_mask()

    def apply_stimulus(self, protocol: str, route: Optional[str] = None) -> Optional[str]:
        return apply_stimulus(self._regbank, protocol, route)

    def apply_edge_route(self, route: str):
        """DS-P3.123: EMIO_MUX_SEL edge routing; ``(reject, applied word)``."""
        from .edge_route import apply_edge_route
        return apply_edge_route(self._regbank, route)

    def read_stats(self) -> Optional[dict]:
        return read_regbank_stats(self._regbank)

    def next_batch(self) -> Optional[Batch]:
        # Honour the duration budget if set.
        if self._duration_s > 0:
            elapsed = self._clock() - self._wall_start
            if elapsed >= self._duration_s:
                return None

        # Pace ourselves to poll_period_s wall-clock.
        target_wall_s = (self._tick_index + 1) * self._poll_period_s
        actual_wall_s = self._clock() - self._wall_start
        if target_wall_s > actual_wall_s:
            time.sleep(target_wall_s - actual_wall_s)

        # Latch all counters atomically (self-clearing bit[0] per regbank spec).
        if self._snapshot_trigger:
            self._regbank.write_u32(Regs.TELEMETRY_SNAPSHOT_TRIGGER, 1)

        # Snapshot every counter we care about.
        snap: dict[int, int] = {}
        for offset in (Regs.HEARTBEAT_CNT,
                       Regs.FIFO_OVERFLOW_CNT,
                       Regs.FIFO_OVERFLOW_CNT_RSVD):
            snap[offset] = self._regbank.read_u32(offset)
        for proto, (pkt_off, err_off) in _PROTO_COUNTERS.items():
            snap[pkt_off] = self._regbank.read_u32(pkt_off)
            if err_off is not None:
                snap[err_off] = self._regbank.read_u32(err_off)

        ts_ns = int((self._clock() - self._wall_start) * 1e9)
        window_ns = int(self._poll_period_s * 1e9)
        batch = Batch()

        # 1) PL liveness — heartbeat annotation, always emitted.
        hb = snap[Regs.HEARTBEAT_CNT]
        batch.annotations.append(Annotation(
            channel_id=_HEARTBEAT_CHANNEL,
            ts_start_ns=ts_ns,
            ts_end_ns=ts_ns + window_ns,
            kind="pl_heartbeat",
            text=f"hb={hb:#010x}",
            color="#4a8c2a",
            is_error=False,
            fields={"counter": hb},
        ))

        # 2) Per-protocol pkt / err deltas.
        for proto, (pkt_off, err_off) in _PROTO_COUNTERS.items():
            ch_id = self._PROTO_CHANNEL_MAP.get(proto, _PROTO_CHANNEL)
            pkt = snap[pkt_off]
            pkt_prev = self._prev_counters.get(pkt_off, pkt)
            dp = (pkt - pkt_prev) & 0xFFFFFFFF
            if dp:
                batch.annotations.append(Annotation(
                    channel_id=ch_id,
                    ts_start_ns=ts_ns,
                    ts_end_ns=ts_ns + window_ns,
                    kind=f"{proto}_pkt_count",
                    text=f"+{dp} pkts",
                    color="#4a8c2a",
                    is_error=False,
                ))
            if err_off is not None:
                err = snap[err_off]
                err_prev = self._prev_counters.get(err_off, err)
                de = (err - err_prev) & 0xFFFFFFFF
                if de:
                    batch.annotations.append(Annotation(
                        channel_id=ch_id,
                        ts_start_ns=ts_ns,
                        ts_end_ns=ts_ns + window_ns,
                        kind=f"{proto}_err",
                        text=f"+{de} errors",
                        color="#cc3333",
                        is_error=True,
                    ))

        # 3) FIFO overflow sticky — any positive delta is an alarm. Single-tier
        #    (DS-P3.94 ph3): the Stream FIFO (FIFO_OVERFLOW_CNT_RSVD) is dead
        #    (D.4) and reads 0 forever, so only the one real counter is watched.
        cur = snap[Regs.FIFO_OVERFLOW_CNT]
        prev = self._prev_counters.get(Regs.FIFO_OVERFLOW_CNT, cur)
        do = (cur - prev) & 0xFFFFFFFF
        if do:
            batch.annotations.append(Annotation(
                channel_id=_FIFO_OVERFLOW_CHANNEL,
                ts_start_ns=ts_ns,
                ts_end_ns=ts_ns + window_ns,
                kind="fifo_overflow",
                text=f"FIFO overflow +{do} (total={cur})",
                color="#cc3333",
                is_error=True,
            ))

        self._prev_counters = snap
        self._tick_index += 1
        return batch

    def close(self) -> None:
        try:
            self._regbank.close()
        except Exception as exc:
            logger.warning("regbank close failed: %s", exc)


# ---------------------------------------------------------------------------
# Live PL data source — DS-P3.27 Phase B (DDR-ring drain via rr-ddr-stream)
# ---------------------------------------------------------------------------


# Annotation channel for unparsed-payload zs_msg frames + ring-resync events.
_ZS_FRAME_CHANNEL  = 203
_ZS_RESYNC_CHANNEL = 204

# DS-P3.56 P2: reassembled JTAG register scans (IDCODE / IR / DR summaries).
_JTAG_REG_CHANNEL  = 104
_CAN_FRAME_CHANNEL = 106   # DS-P3.104: reassembled CAN frame summary lane

# Each rr-ddr-stream entry is one 32-bit datalogger message → 4 bytes in DDR.
# Used to translate the regbank's RING_WRITE_PTR (message count) into a
# byte offset within the mmaped ring.
_BYTES_PER_MSG = 4

# Domain → existing annotation channel ID. Frames whose domain matches one
# of the protocol-decoder lanes ride on that lane; unrecognized domains
# fall back to _ZS_FRAME_CHANNEL.
# DS-P3.155: multi-instance dataloggers (I2C x2) map (domain, datalogger_id) to
# distinct per-datalogger channel lanes so cam B (#4) and cam A (#5) do not collapse
# onto one lane in RouteWave.
_DOMAIN_CHANNEL_MAP = {
    zs_msg.DOMAIN_UART:     100,  # uart_byte
    zs_msg.DOMAIN_I2C:      101,  # i2c_frame / i2c_frame:4 fallback
    (zs_msg.DOMAIN_I2C, 4): 101,  # i2c_frame:4 (cam B #4)
    (zs_msg.DOMAIN_I2C, 5): 109,  # i2c_frame:5 (cam A #5)
    zs_msg.DOMAIN_QSPI:     102,  # qspi_xact
    zs_msg.DOMAIN_JTAG:     103,  # jtag_tap
    zs_msg.DOMAIN_CAN:      105,  # can_msg (DS-P3.104)
    zs_msg.DOMAIN_SPI:      102,  # qspi_xact
    zs_msg.DOMAIN_EDGE:     _ZS_FRAME_CHANNEL,
    zs_msg.DOMAIN_I2S:      _ZS_FRAME_CHANNEL,
    zs_msg.DOMAIN_SYS:      200,  # pl_heartbeat (system events)
    zs_msg.DOMAIN_TRIG:     _ZS_FRAME_CHANNEL,
}


# DS-P3.70: the OUT-OF-BAND loss-provenance counters — the always-readable
# backstop. Each entry maps a regbank counter to (provenance event_id, domain,
# channel). The watcher diffs these every tick and, on a rising delta, emits the
# SAME "loss" annotation the in-band SEV_ERROR path emits (decoders._decode_error_event)
# — but tagged in_band=False (polled, approximate ts). Only these counters are
# LOSS: ERR_CNT_EDGE is the edge internal-FIFO overflow (DS-P3.24); DROP_CNT_*
# are arbiter drops; DDR_BACKPRESSURE_CNT is ring-full. The other ERR_CNT_* are
# decode/quality (proto_errs), NOT loss — they are deliberately excluded. The
# ring-full case MUST be out-of-band by construction (an in-band frame can't
# traverse a full ring), so it ONLY ever appears via this path.
_LOSS_COUNTERS = [
    # (reg, provenance_event_id, domain, channel)
    (Regs.ERR_CNT_EDGE,         zs_msg.ERR_EVT_INTERNAL_FIFO_OVERFLOW, "edge", _ZS_FRAME_CHANNEL),
    (Regs.DROP_CNT_I2C,         zs_msg.ERR_EVT_ARBITER_DROP,           "i2c",  101),
    (Regs.DROP_CNT_SPI,         zs_msg.ERR_EVT_ARBITER_DROP,           "spi",  102),
    (Regs.DROP_CNT_UART,        zs_msg.ERR_EVT_ARBITER_DROP,           "uart", 100),
    (Regs.DDR_BACKPRESSURE_CNT, zs_msg.ERR_EVT_RING_FULL,              "ring", 200),
]


class LossProvenanceWatcher:
    """DS-P3.70: turn the always-readable OUT-OF-BAND loss counters into inline
    timeline markers. Each poll diffs the provenance counters (edge internal-FIFO
    overflow / arbiter drop / ring-full backpressure) and, on a rising delta,
    emits one ``kind="loss"`` annotation tagging WHICH pipeline stage lost and HOW
    MANY — so RouteWave can draw '⚠ N lost here' at the poll time, instead of only
    showing a number in STATS. The emitted shape is identical to the in-band path
    (decoders._decode_error_event) so the consumer renders both uniformly; this
    path tags ``in_band=False`` (polled, approximate ts) vs the in-band path's
    precise PL timestamp.

    First poll seeds the baseline (no emit). Counters are free-running uint32, so
    the delta is taken mod 2^32 (correct across a true wrap); a counter RESET
    (e.g. bitstream reload → value drops to ~0) would mask to a huge bogus delta,
    so a sanity cap drops + re-seeds anything above ``_MAX_SANE_DELTA`` rather than
    reporting billions of phantom losses."""

    _MAX_SANE_DELTA = 1 << 24   # >16M losses in one poll = a reset/glitch, not real

    def __init__(self) -> None:
        self._last: Dict[int, int] = {}   # reg addr -> last value

    def poll(self, regbank, ts_ns: int, window_ns: int) -> List[Annotation]:
        out: List[Annotation] = []
        for reg, event_id, domain, channel in _LOSS_COUNTERS:
            try:
                val = regbank.read_u32(reg) & 0xFFFFFFFF
            except Exception:  # noqa: BLE001 - a flaky read must not kill the drain
                continue
            prev = self._last.get(reg)
            self._last[reg] = val
            if prev is None:
                continue                      # seed — no baseline yet
            delta = (val - prev) & 0xFFFFFFFF
            if delta == 0 or delta > self._MAX_SANE_DELTA:
                continue                      # no change, or reset/wrap glitch
            stage = decoders.error_stage_code(event_id)
            out.append(Annotation(
                channel_id=channel,
                ts_start_ns=ts_ns,
                ts_end_ns=ts_ns + window_ns,
                kind="loss",
                # DS-P3.127(b): ASCII-only — U+26A0 doesn't render in RouteWave's
                # font atlas, hiding the loss marker the consumer needs to see.
                text=f"! {delta} {domain} event(s) lost "
                     f"({decoders.error_stage_human(event_id)})",
                color="#cc3333",
                is_error=True,
                fields={
                    "loss_stage": stage,
                    "provenance_code": event_id,
                    "lost_count": delta,
                    "domain": domain,
                    "in_band": False,
                },
            ))
        return out


# DS-T1.23: the clock that times DdrRingDataSource's per-tick decode budget
# (_TICK_BUDGET_S). Kept apart from the injectable pacing clock: that one models
# poll ticks, this one measures decode work.
_budget_clock = time.monotonic


def _check_ring_word_bytes(w: Optional[int]) -> Optional[int]:
    """DS-P2.280: the declared DDR word width in bytes (G_DDR_DATA_WIDTH / 8): a power of two, at least two
    32-bit messages. None keeps the pre-DS-P2.280 walk and logs once that PL pads will read as zs_resync."""
    if w is None:
        logger.warning("ring word width not declared (--ring-word-bytes / ds_ring_word_bytes): PL zero-pad "
                       "words will be reported as zs_resync (DS-P2.280)")
        return None
    w = int(w)
    if w < 2 * _BYTES_PER_MSG or w & (w - 1):
        raise ValueError(f"ring_word_bytes must be a power of two >= {2 * _BYTES_PER_MSG}, got {w}")
    return w


class DdrRingDataSource:
    """Drain the rr-ddr-stream PS-DDR ring buffer of zs_msg_header_t frames.

    Phase B of DS-P2.31 — the throughput-mode counterpart to
    RegbankDataSource. Where RegbankDataSource summarised activity by
    polling counters, this source delivers per-event annotations carrying
    each frame's datalogger_id / domain / event / timestamp.

    Per tick:
      1. Read RING_COMMIT_PTR (byte offset) — bytes unread
         = (commit - read) mod RING_BYTES.
      2. Pull that many bytes from the DDR mmap (handles ring wrap), at most
         _MAX_SPAN_BYTES_PER_TICK.
      3. Walk frames: parse header → emit Annotation → advance by
         total_size; magic mismatch triggers a 4-byte resync. The walk stops
         at _MAX_FRAMES_PER_TICK frames or when _TICK_BUDGET_S is spent.
      4. Advance internal read pointer and write the consumed BYTE OFFSET
         back to RING_READ_PTR — the active ring DRAIN pointer (DS-P2.59).
         The PL feeds it into ddr_stream_axi.csr_rd_ptr_addr_i to free ring
         space so the DMA write side keeps wrapping (lossless SPSC drain).

    Samples list stays empty — Phase B emits annotations only. Per-protocol
    payload decoders (UART byte → text, I2C frame → "WR 0xA0 ACK 0x55", etc.)
    are deliberately out of scope; they land per-protocol in follow-on
    tickets where the payload format spec stabilises.
    """

    # DS-T1.23: most bytes copied out of the uncached DDR window per tick.
    # Copying the whole backlog is wasted work: measured on DE25 silicon, one
    # tick copied 53.9 MB (1.34 s) to consume 655 KB. Must stay above the
    # largest frame (HEADER_SIZE + 0xFFFF), or a frame could never be completed.
    #
    # DS-P1.100: the span is the per-tick byte budget, so span / poll_period is
    # the drain's nominal ceiling. It is sized for at least 2x the DS-DE25-TPUT-001
    # ring write rate (20.0e6 B/s): 2 MiB x 20 ticks/s = 41.9e6 B/s. At 1 MiB the
    # ceiling was 20.97e6 B/s, 4.9 % above the write rate, so a busy ring barely
    # emptied. The real ceiling is the uncached copy rate (about 40e6 B/s from the
    # 53.9 MB / 1.34 s measurement above) plus decode time: a larger span copies
    # longer than one 50 ms poll period and adds latency, not rate. The time
    # budget (_TICK_BUDGET_S) still bounds every tick.
    _MAX_SPAN_BYTES_PER_TICK = 2 << 20

    # DS-P1.100: a tick is bounded by bytes (_MAX_SPAN_BYTES_PER_TICK) and by
    # time (_TICK_BUDGET_S), not by a frame count. This cap is the most frames
    # one span can hold (the smallest frame is a bare header), so it never stops
    # a tick before either budget does; it remains only as the walk's loop
    # bound. The former fixed 4096 bound the drain on DE25 silicon at
    # 4096 x 160 B x 20 ticks/s = 13.1e6 B/s against a 20.0e6 B/s ring write
    # (DS-P1.74 run 3): the ring filled at the difference.
    _MAX_FRAMES_PER_TICK = _MAX_SPAN_BYTES_PER_TICK // zs_msg.HEADER_SIZE

    # DS-T1.23: decode-time budget per tick. When it is spent the tick stops at
    # a frame boundary and writes RING_READ_PTR back, so the PL sees the drain
    # progress at least this often. Measured on DE25 silicon without it: one
    # 4096-frame EDGE_BATCH tick took 8.5 s, and RING_READ_PTR did not move for
    # that whole time.
    _TICK_BUDGET_S = 0.25

    def __init__(self,
                 regbank: Regbank,
                 ddr: DdrMmap,
                 sample_clk_hz: int = 100_000_000,
                 poll_period_s: float = 0.05,
                 duration_s: float = 0.0,
                 write_back_read_ptr: bool = True,
                 jtag_device: Optional[jtag_devices.JtagDevice] =
                 jtag_devices.DEFAULT_DEVICE,
                 jtag_chain: Optional[jtag_devices.JtagChain] =
                 jtag_devices.ZYNQ7_CHAIN,
                 drain_irq: "Optional[RingIrq]" = None,
                 clock: Optional[callable] = None,
                 native_edge_drain: Optional[object] = None,
                 ring_word_bytes: Optional[int] = None) -> None:
        self._regbank = regbank
        self._enable = _make_enable_arbiter(regbank)
        # DS-P2.280: the PL writes the ring in DDR words of ring_word_bytes (the top's G_DDR_DATA_WIDTH / 8,
        # declared per target as the ds_ring_word_bytes kernel-cmdline token). On producer idle with a partial
        # word, rr_ddr_stream ddr_fifo_intf completes the word with zero 32-bit messages so it commits. Those
        # zeros are pad, not lost sync: a zero run from the read cursor to the next word boundary is consumed
        # silently. None (undeclared) keeps the old behaviour, a pad reads as zs_resync, and says so once.
        # The native EDGE walk applies the same rule in C (DS-P2.281 stop, DS-P1.117 consumption).
        self._ring_word_bytes = _check_ring_word_bytes(ring_word_bytes)
        self._pad_total = 0
        # A pad only ever follows the PL's last real message, so it is taken only right after a parsed frame;
        # carried across ticks for a pad held back at the end of a span.
        self._after_frame = False
        # DS-P1.74 (DS-D.49): a loaded sw.stream.native.edge_frames.EdgeDrainLib.
        # When set, runs of EDGE_BATCH frames are walked, cut into EDGE_EVENTS
        # frames and packed in C; every other frame takes the Python walk below.
        self._native_edges = native_edge_drain
        self._ddr = ddr
        self._sample_clk_hz = int(sample_clk_hz)
        self._poll_period_s = float(poll_period_s)
        self._duration_s = float(duration_s)
        self._write_back_read_ptr = bool(write_back_read_ptr)
        self._clock = clock or time.monotonic
        # DS-P3.60: optional ring-watermark IRQ. When set, each tick waits up to
        # poll_period on the IRQ instead of a blind sleep — so a producer burst
        # that crosses the 75% watermark wakes the drain in interrupt latency
        # (µs) instead of up to poll_period (steady-state draining below the
        # watermark still happens on the periodic timeout). None = legacy poll.
        self._drain_irq = drain_irq
        self._irq_wakes = 0

        self._wall_start = self._clock()
        self._tick_index = 0

        # Last-observed RING_WRITE_PTR (message count) — telemetry only.
        self._last_write_msg_cnt: Optional[int] = None
        # Last-observed RING_COMMIT_PTR (byte offset) — the AUTHORITATIVE,
        # beat-aligned committed write head. The read head MUST track this, not
        # RING_WRITE_PTR*4: the enqueue message count *4 diverges from the DMA's
        # committed byte offset (variable-length frames + beat alignment), so
        # seeding the read cursor from WRITE_PTR*4 lands mid-frame and the parser
        # resyncs forever, never reaching the live frames (hint DS-T2.18).
        self._last_commit_ofs: Optional[int] = None

        # PS-side byte offset within the ring. Advances as frames are
        # parsed; wraps at ring_bytes.
        self._read_byte_ofs = 0

        # Cached ring window size — must match the regbank's RING_BYTES.
        self._ring_bytes = self._ddr.size()

        # Sticky resync counter — surfaces as the zs_resync annotation.
        self._resync_total = 0

        # DS-P2.61: per-protocol DECODED-error tally (annotation is_error=True),
        # distinct from proto_errs (the RTL/HW ERR_CNT_* parity/framing/overrun
        # bus). The two legitimately diverge — a UART STOP-ERROR is found by the SW
        # decoder yet may not raise the HW error bus — so a consumer seeing
        # 'uart Err 0' must NOT read it as healthy when 44% of frames decode as
        # errors. Keys pre-seeded from DOMAIN_NAMES so the heartbeat thread's
        # read_stats() snapshot never races a size change on next_batch()'s update.
        self._decode_errs: dict[str, int] = {
            name: 0 for name in zs_msg.DOMAIN_NAMES.values()
        }

        # DS-P3.87: collapse sustained error storms (e.g. a persistent UART
        # baud/framing fault → thousands of identical STOP-ERROR annotations)
        # into one run-length `error_run` event per batch. Per-frame error
        # annotations above the threshold are suppressed at source; the run
        # total still flows via _decode_errs above. Consumer: RouteWave LT-P3.204.
        self._storm = ErrorStormAggregator(
            idle_end_ticks=round(ErrorStormAggregator.IDLE_END_S / self._poll_period_s)
            if self._poll_period_s > 0 else 1)

        # DS-P3.89: rolling-window digest — every decoded protocol frame is fed
        # here (packet + error, with its timeline ts), and the heartbeat thread
        # snapshots it into a periodic DIGEST frame (per-protocol rates + error %,
        # last-N error tail, standing anomaly verdict). Bounds the infinite live
        # stream into something RouteWave Mission Control AND an agent can ingest.
        # Consumer: RouteWave LT-P3.202.
        self._digest = RollingDigest()

        # DS-P3.70: out-of-band loss-provenance watcher. Diffs the always-readable
        # loss counters (edge internal-FIFO overflow / arbiter drops / ring-full
        # backpressure) each tick and emits inline "loss" markers naming WHICH
        # stage lost and HOW MANY — the polled backstop that complements the
        # precise in-band SEV_ERROR path. Consumer: RouteWave LT-P3.203.
        self._loss = LossProvenanceWatcher()

        # DS-P3.56 P2/P3: stateful JTAG IR/DR reassembler. Fed every JTAG
        # frame in shift order; emits a per-scan summary annotation (IDCODE /
        # IR / DR / BSCAN) on the jtag_reg lane, alongside the 1:1 per-frame
        # jtag_tap annotation. The chain profile (default: the board's Zynq-7
        # PL-TAP + ARM-DAP chain) makes the decode chain-aware — splits the
        # combined IR per device + names USERn/BSCAN/IDCODE (DS-P3.58). chain
        # takes priority over device in the reassembler.
        self._jtag_reassembler = JtagReassembler(device=jtag_device,
                                                 chain=jtag_chain)

        # DS-P2.16: one Stream-RLE decoder per edge datalogger id. The word
        # stream is stateful (each delta counts from the previous edge), so the
        # decoder lives across ticks like the reassemblers.
        self._rle_decoders: Dict[int, edge_capture.EdgeRleDecoder] = {}

        # DS-P3.104: stateful CAN frame reassembler. Fed every CAN frame in
        # capture order; emits one per-frame summary (id/IDE/RTR/DLC/data/CRC/
        # ACK) on the can_frame lane on frame_end (or an aborted summary on a
        # stuff error / dropped frame_end), alongside the 1:1 can_msg annotation.
        self._can_reassembler = CanReassembler()

        # RTL-DS-P1.12: anchor PL ``hdr.timestamp`` (clk_sys-tick counter,
        # free-running from FPGA reset) to wallclock at the first
        # observed frame.  Per-frame ``ts_start_ns`` then preserves the
        # PL-side cycle-accurate timing on the RouteWave timeline.  PL
        # clock vs daemon wallclock drift over long runs is acceptable
        # for the PoC; once we have a deterministic PL-side epoch
        # source (e.g. trigger broadcast) we can replace this anchor.
        self._pl_ts_anchor_ticks: Optional[int] = None
        self._pl_ts_anchor_wallclock_ns: Optional[int] = None
        # Pre-computed ns/tick for the configured PL sample rate.
        if self._sample_clk_hz > 0:
            self._pl_ns_per_tick = 1_000_000_000 // self._sample_clk_hz
        else:
            self._pl_ns_per_tick = 0

    def sample_clk_hz(self) -> int:
        return self._sample_clk_hz

    def edge_channels(self) -> List[dict]:
        """DS-P3.124: HELO descriptors naming the edge lanes this source streams.

        This is the only source that emits EDGE_EVENTS (the DOMAIN_EDGE
        EVT_EDGE_BATCH decode in next_batch), so it is the one that declares
        them. The name of each lane comes from EMIO_MUX_SEL as it reads NOW,
        because the mux decides which signal an Edge Detector sees. The server
        calls this once per HELO, so a consumer that reconnects after a
        stimulus re-routes the mux gets the new names. A failed read declares
        no edge lanes: RouteWave then shows edge_<id>, which is honest, where a
        guessed name could be wrong."""
        from . import channels
        try:
            mux = self._regbank.read_u32(Regs.EMIO_MUX_SEL)
        except Exception as exc:  # noqa: BLE001 - HELO must still go out
            logger.warning("EMIO_MUX_SEL unreadable (%s): edge lanes not named", exc)
            return []
        return channels.edge_channels(mux)

    def apply_config(self, datalogger_id: int, cfg_word: int,
                     op: str = "set_config") -> Optional[str]:
        # DS-P3.74: host CONFIG is the enable baseline; it goes through the
        # arbiter so an active trigger enable-mask is honoured, never clobbered.
        reason = _control_reject_reason(datalogger_id, cfg_word, op)
        if reason is not None:
            return reason
        return self._enable.apply_host(datalogger_id, cfg_word)

    def apply_trigger_enable_mask(self, allow_mask: int) -> Optional[str]:
        """DS-P3.39 trigger enable-mask path (DS-P3.74 rule): a trigger may only
        gate host-enabled dataloggers off (effective = host AND allow)."""
        return self._enable.apply_trigger_mask(allow_mask)

    def release_trigger_enable_mask(self) -> Optional[str]:
        """Trigger revert: the latest host baseline stands again."""
        return self._enable.release_trigger_mask()

    def apply_stimulus(self, protocol: str, route: Optional[str] = None) -> Optional[str]:
        return apply_stimulus(self._regbank, protocol, route)

    def apply_edge_route(self, route: str):
        """DS-P3.123: EMIO_MUX_SEL edge routing; ``(reject, applied word)``."""
        from .edge_route import apply_edge_route
        return apply_edge_route(self._regbank, route)

    def read_stats(self) -> Optional[dict]:
        # DS-P2.61: reconcile the two error notions. proto_errs = HW/RTL datalogger
        # error bus (ERR_CNT_*); proto_decode_errs = SW-decoded protocol errors
        # (annotation is_error — what the consumer's ERRORS chip counts). Emitting
        # + naming both means 'uart Err 0' next to '44% STOP-ERROR' is no longer a
        # contradiction. Snapshot iterates fixed (pre-seeded) keys → no size-change
        # race with next_batch()'s decode-thread value updates.
        s = read_regbank_stats(self._regbank)
        if s is not None:
            s["proto_decode_errs"] = {k: self._decode_errs[k] for k in self._decode_errs}
        return s

    def read_digest(self) -> Optional[dict]:
        # DS-P3.89: rolling-window digest snapshot for a DIGEST frame. None until
        # the first protocol frame is observed (heartbeat emits no DIGEST then).
        return self._digest.snapshot()

    def next_batch(self) -> Optional[Batch]:
        if self._duration_s > 0:
            elapsed = self._clock() - self._wall_start
            if elapsed >= self._duration_s:
                return None

        target_wall_s = (self._tick_index + 1) * self._poll_period_s
        actual_wall_s = self._clock() - self._wall_start
        remaining_s = target_wall_s - actual_wall_s
        if remaining_s > 0:
            if self._drain_irq is not None:
                # Hybrid: wake at the next poll tick OR immediately when the ring
                # crosses 75% (watermark IRQ), whichever comes first. arm() each
                # tick (re-arm is harmless if it didn't fire); a non-None wait()
                # return means the watermark woke us early — count it.
                self._drain_irq.arm()
                if self._drain_irq.wait(timeout=remaining_s) is not None:
                    self._irq_wakes += 1
            else:
                time.sleep(remaining_s)

        # The read head is the DMA's COMMITTED byte offset (beat/frame-aligned),
        # NOT RING_WRITE_PTR*4 (an enqueue message count that diverges from the
        # committed byte offset and lands mid-frame → perpetual resync). See
        # _last_commit_ofs note + hint DS-T2.18. RING_WRITE_PTR is read only for
        # telemetry.
        write_byte_ofs = self._regbank.read_u32(Regs.RING_COMMIT_PTR) % self._ring_bytes
        write_msg_cnt = self._regbank.read_u32(Regs.RING_WRITE_PTR)
        ts_ns = int((self._clock() - self._wall_start) * 1e9)
        window_ns = int(self._poll_period_s * 1e9)
        batch = Batch()
        if self._native_edges is not None:
            batch.native_edges = self._native_edges.new_frames()

        # DS-P3.70: poll the out-of-band loss counters every tick — BEFORE the
        # early returns below — so a loss is surfaced even on an idle bus (e.g.
        # ring-full backpressure with no new frames to drain). First poll seeds.
        batch.annotations.extend(self._loss.poll(self._regbank, ts_ns, window_ns))

        if self._last_commit_ofs is None:
            # First tick — seed without draining the back-history.
            self._last_commit_ofs = write_byte_ofs
            self._last_write_msg_cnt = write_msg_cnt
            self._read_byte_ofs = write_byte_ofs
            if self._write_back_read_ptr:
                self._write_seed_read_ptr(write_byte_ofs)
            self._tick_index += 1
            return batch

        # Bytes-available = (commit - read) mod ring_bytes. This re-reads
        # the un-consumed tail each tick (so a partial-frame deferral on
        # tick N gets re-seen on tick N+1 with the additional bytes now
        # present), avoiding the delta-only blind spot.
        bytes_unread = (write_byte_ofs - self._read_byte_ofs) % self._ring_bytes
        self._last_commit_ofs = write_byte_ofs
        self._last_write_msg_cnt = write_msg_cnt

        if bytes_unread == 0:
            # DS-P2.302: an idle tick still decides whether a storm has ended.
            batch.annotations.extend(self._storm.flush_idle())
            self._tick_index += 1
            return batch

        # DS-T1.23: no "overrun" skip here. The PL cannot overwrite unread
        # bytes: ddr_stream_axi gates its writes on FULL against the
        # RING_READ_PTR it is fed (DS-P2.59), so a large commit advance in one
        # tick means this tick was slow, not that the data was lost. The old
        # guard snapped the read head to the commit head on a >=75 % advance
        # and so discarded valid data itself (209 MB on DE25 silicon, with
        # DDR_BACKPRESSURE_CNT 0). Ring fill is reported by STATS
        # ring_used_bytes / ring_alarm and producer loss by
        # DDR_BACKPRESSURE_CNT (LossProvenanceWatcher).

        # Pull the unread span, bounded per tick — read_bytes wraps for us.
        span = self._ddr.read_bytes(self._read_byte_ofs,
                                    min(bytes_unread, self._MAX_SPAN_BYTES_PER_TICK))
        cursor = 0
        frames_parsed = 0
        resync_bytes = 0
        pad_bytes = 0
        after_frame = self._after_frame
        decode_deadline = _budget_clock() + self._TICK_BUDGET_S

        while (cursor < len(span)
               and frames_parsed < self._MAX_FRAMES_PER_TICK
               and _budget_clock() < decode_deadline):
            # DS-P2.280: a PL zero-pad run to the next DDR-word boundary is consumed before the header-size
            # guard, so an idle ring (frame, then a pad shorter than one header) drains to the commit offset.
            pad = self._pad_run(span, cursor, after_frame)
            if pad < 0:
                break                       # zeros up to the span end, boundary beyond it: next tick decides
            if pad > 0:
                cursor += pad
                pad_bytes += pad
                after_frame = False
                continue
            if cursor + zs_msg.HEADER_SIZE > len(span):
                break
            if batch.native_edges is not None:
                # DS-P1.74: C walks the run of EDGE_BATCH frames from here (the
                # resync, partial-frame and malformed-batch rules of the branch
                # below) and stops before the first frame of another kind.
                # DS-P1.117: with the word width, C also consumes the PL pad after each frame, so a run
                # of padded frames is one call, not one call per frame.
                walked = batch.native_edges.drain(
                    span, cursor, self._MAX_FRAMES_PER_TICK - frames_parsed,
                    self._pl_ns_per_tick, self._sample_clk_hz // 1_000_000,
                    ring_word_bytes=self._ring_word_bytes or 0, ring_bytes=self._ring_bytes,
                    ring_ofs=self._read_byte_ofs)
                if walked.first_ts_ticks is not None and self._pl_ts_anchor_ticks is None:
                    self._pl_ts_anchor_ticks = walked.first_ts_ticks
                    self._pl_ts_anchor_wallclock_ns = ts_ns
                cursor += walked.consumed
                frames_parsed += walked.frames
                resync_bytes += walked.resync_bytes
                pad_bytes += walked.pad_bytes
                if walked.consumed:
                    # DS-P1.117: the walk's own state (it ends after a frame, a pad or a resync), as this
                    # loop would have it.
                    after_frame = walked.after_frame
                if (walked.stop == native_edge_frames.STOP_OTHER_FRAME
                        and span[cursor:cursor + _BYTES_PER_MSG] == bytes(_BYTES_PER_MSG)):
                    # DS-P2.281: the C walk stops at a zero word only right after a frame it walked, so the
                    # pad rule at the loop top judges it (pad, or resync as a whole zero word) exactly as on
                    # this walk.
                    after_frame = True
                    if frames_parsed >= self._MAX_FRAMES_PER_TICK:
                        break
                    continue
                if (walked.stop != native_edge_frames.STOP_OTHER_FRAME
                        or frames_parsed >= self._MAX_FRAMES_PER_TICK):
                    break
            try:
                hdr = zs_msg.parse_header(span, cursor)
            except ValueError:
                # Magic mismatch — slide the cursor forward 4 bytes (one
                # 32-bit word) and try again. Real PL output is
                # word-aligned, so this resyncs within a few words.
                cursor += _BYTES_PER_MSG
                resync_bytes += _BYTES_PER_MSG
                after_frame = False
                continue

            frame_bytes = hdr.total_size
            if cursor + frame_bytes > len(span):
                # Partial frame at end of span — leave it for the next tick.
                break

            # DS-P3.155: look up (domain, datalogger_id) first so multi-instance
            # dataloggers (e.g. cam B #4 vs cam A #5) land on distinct channel lanes;
            # fall back to domain-level mapping, then _ZS_FRAME_CHANNEL.
            channel_id = _DOMAIN_CHANNEL_MAP.get(
                (hdr.domain, hdr.datalogger_id),
                _DOMAIN_CHANNEL_MAP.get(hdr.domain, _ZS_FRAME_CHANNEL))
            domain_name = zs_msg.DOMAIN_NAMES.get(hdr.domain, f"dom{hdr.domain:x}")

            # DS-P1.12: per-frame timestamp derived from hdr.timestamp
            # (PL clk_sys-tick anchor) rather than the poll-tick
            # wallclock — preserves sub-µs accuracy on the RouteWave
            # timeline.  First frame anchors to wallclock; subsequent
            # frames advance by PL delta ticks.
            hdr_ts_ticks = hdr.timestamp & ((1 << 48) - 1)
            if self._pl_ts_anchor_ticks is None:
                self._pl_ts_anchor_ticks = hdr_ts_ticks
                self._pl_ts_anchor_wallclock_ns = ts_ns
                frame_ts_ns = ts_ns
            else:
                delta_ticks = (hdr_ts_ticks - self._pl_ts_anchor_ticks) \
                    & ((1 << 48) - 1)
                frame_ts_ns = self._pl_ts_anchor_wallclock_ns \
                    + delta_ticks * self._pl_ns_per_tick

            # DS-P1.12: per-protocol payload decoder dispatch.  Fall back
            # to the generic envelope text when no decoder is registered.
            payload_bytes = bytes(span[cursor + zs_msg.HEADER_SIZE
                                       : cursor + frame_bytes])

            # DS-P3.69/DS-T2.35: a DOMAIN_EDGE frame is an EDGE_BATCH (DS-P2.47):
            # one header (datalogger_id=channel, timestamp=64-bit clk_sys-tick anchor)
            # carrying N packed entries of (ts_delta:u16, channel:u8, flags:u8).
            # Decode every entry via the sanctioned zs_msg.parse_edge_batch oracle
            # and emit one LT-frozen EDGE_EVENT stream word per entry, with the
            # absolute time (anchor + ts_delta) and the raw S/F/E preserved, so
            # RouteWave EdgeStreams (TAG_EDGE_EVENTS) see the full transition set.
            #
            # The historical decode took only payload[:4] (entry 0 of up to 32)
            # AND misread the batch-entry layout as the old single-edge word —
            # dropping 31/32 edges and mistiming the survivor (DS-T2.35). The
            # SEV_ERROR loss frames (DS-P3.70 piece 3) still fall through to
            # decode→annotation below.
            if (hdr.domain == zs_msg.DOMAIN_EDGE and not hdr.is_error
                    and hdr.event_id == zs_msg.EVT_EDGE_BATCH):
                try:
                    _, edge_events = zs_msg.parse_edge_batch(span, cursor)
                except ValueError:
                    # Malformed EVT_EDGE_BATCH frame — skip rather than emit a
                    # mistimed word (no_silent_failures: surfaced as a resync gap).
                    edge_events = []
                    resync_bytes += frame_bytes
                for ev in edge_events:
                    ev_ts_ns = (ev.ts_64b & ((1 << 48) - 1)) * self._pl_ns_per_tick
                    # DS-P2.78 / DS-P3.138: the packed word discards everything
                    # above 16 bits of µs, so add_edge keeps the FULL absolute ns
                    # of every edge to anchor each wire frame it is cut into.
                    batch.add_edge(edge_capture.edge_event_word(
                        instance=ev.channel, timestamp_ns=ev_ts_ns,
                        s=ev.s, f=ev.f, e=ev.e, clk_mhz=self._sample_clk_hz // 1_000_000),
                        ev_ts_ns)
                cursor += frame_bytes
                frames_parsed += 1
                after_frame = True
                continue

            # DS-P2.16: a Stream-RLE word (EVT_EDGE_RLE) is decoded by the
            # instance's EdgeRleDecoder into the same EDGE_EVENT stream words an
            # event-mode capture produces: the SYNC anchor as an S=1 level word,
            # each edge as an S=0 word, so RouteWave draws both modes alike.
            if (hdr.domain == zs_msg.DOMAIN_EDGE and not hdr.is_error
                    and hdr.event_id == zs_msg.EVT_EDGE_RLE and len(payload_bytes) >= 4):
                word = int.from_bytes(payload_bytes[:4], "little")
                dec = self._rle_decoders.setdefault(
                    hdr.datalogger_id, edge_capture.EdgeRleDecoder())
                is_sync = ((word >> 24) & 0x7) in (edge_capture.RLE_SYNC_LO,
                                                   edge_capture.RLE_SYNC_HI)
                try:
                    transitions = dec.feed(word, frame_tick=hdr_ts_ticks)
                except ValueError:
                    # Reserved word type: a corrupt word, possibly one that
                    # carried edges, so the level and time base after it are
                    # unknown. Drop it and wait for the next SYNC rather than
                    # emit mistimed edges (surfaced as a resync gap).
                    dec.lose_sync()
                    transitions = []
                    resync_bytes += frame_bytes
                for tick, level in transitions:
                    ev_ts_ns = tick * self._pl_ns_per_tick
                    batch.add_edge(edge_capture.edge_event_word(
                        instance=hdr.datalogger_id, timestamp_ns=ev_ts_ns,
                        s=1 if is_sync else 0, f=0,
                        e=level if is_sync else 1 - level,
                        clk_mhz=self._sample_clk_hz // 1_000_000), ev_ts_ns)
                cursor += frame_bytes
                frames_parsed += 1
                after_frame = True
                continue

            decoded = decoders.decode_frame(hdr, payload_bytes,
                                            self._sample_clk_hz)
            if decoded is not None:
                ann_kind = decoded.kind
                ann_text = decoded.text
                ann_color = decoded.color if decoded.color is not None else (
                    "#cc3333" if hdr.is_error else "#4a8c2a")
                ann_is_error = decoded.is_error if decoded.is_error is not None \
                    else hdr.is_error
                ann_fields = decoded.fields            # DS-P3.88: structured payload
            else:
                ann_kind = f"{domain_name}_zs_frame"
                ann_text = (f"id={hdr.datalogger_id} "
                            f"evt={hdr.event_id:#04x} "
                            f"cls={hdr.msg_class} "
                            f"payload={hdr.payload_len}B "
                            f"ts={hdr_ts_ticks:#x}")
                ann_color = "#cc3333" if hdr.is_error else "#4a8c2a"
                ann_is_error = hdr.is_error
                ann_fields = None

            # DS-P2.61: tally the decoded error per protocol (only known domains;
            # value-update on a pre-seeded key → safe vs the read_stats snapshot).
            # Counts EVERY error, including storm-suppressed ones — the total is
            # preserved here even when the individual annotation is collapsed.
            if ann_is_error and domain_name in self._decode_errs:
                self._decode_errs[domain_name] += 1

            # DS-P3.89: feed every decoded protocol frame (packet + error flag,
            # with its timeline ts + kind/text) to the rolling-window digest. Only
            # real protocol domains (those in _decode_errs) count toward the
            # per-protocol rates — system/heartbeat frames are excluded.
            if domain_name in self._decode_errs:
                # DS-P3.126: hw_error = the HW/bus error path raised this frame
                # (hdr.is_error / ERR_CNT_*), distinct from a pure SW-decode error
                # (e.g. an I2C master-NACK). Lets the digest verdict pick the right
                # fault hint (addressing/ACK vs decode/sampling).
                self._digest.observe(domain_name, ann_is_error, frame_ts_ns,
                                     kind=ann_kind, text=ann_text,
                                     hw_error=bool(hdr.is_error))

            # DS-P3.87: feed errors to the storm aggregator; once a run of the
            # same kind on this channel exceeds the threshold, suppress the
            # individual annotation (it is summarized as one error_run event in
            # the end-of-batch flush below). Non-error frames always emit.
            emit_individual = True
            if ann_is_error:
                emit_individual = self._storm.observe(channel_id, ann_kind, frame_ts_ns)

            if emit_individual:
                batch.annotations.append(Annotation(
                    channel_id=channel_id,
                    ts_start_ns=frame_ts_ns,
                    ts_end_ns=frame_ts_ns + window_ns,
                    kind=ann_kind,
                    text=ann_text,
                    color=ann_color,
                    is_error=ann_is_error,
                    fields=ann_fields,
                ))

            # DS-P3.56 P2: feed JTAG frames to the cross-frame reassembler;
            # a completed IR/DR scan yields one summary on the jtag_reg lane.
            if hdr.domain == zs_msg.DOMAIN_JTAG:
                summary = self._jtag_reassembler.feed(hdr, payload_bytes)
                if summary is not None:
                    batch.annotations.append(Annotation(
                        channel_id=_JTAG_REG_CHANNEL,
                        ts_start_ns=frame_ts_ns,
                        ts_end_ns=frame_ts_ns + window_ns,
                        kind=summary.kind,
                        text=summary.text,
                        color=summary.color if summary.color is not None
                        else "#5c8cc8",
                        is_error=bool(summary.is_error),
                        fields=summary.fields,   # DS-P3.88: IR/DR scan summary fields
                    ))

            # DS-P3.104: feed CAN frames to the cross-frame reassembler; a
            # completed (or aborted) frame yields one summary on the can_frame lane.
            if hdr.domain == zs_msg.DOMAIN_CAN:
                summary = self._can_reassembler.feed(hdr, payload_bytes)
                if summary is not None:
                    batch.annotations.append(Annotation(
                        channel_id=_CAN_FRAME_CHANNEL,
                        ts_start_ns=frame_ts_ns,
                        ts_end_ns=frame_ts_ns + window_ns,
                        kind=summary.kind,
                        text=summary.text,
                        color=summary.color if summary.color is not None
                        else "#3a7ca8",
                        is_error=bool(summary.is_error),
                        fields=summary.fields,
                    ))

            cursor += frame_bytes
            frames_parsed += 1
            after_frame = True

        # DS-P3.87: emit any run-length error_run summaries for storms that were
        # active this batch (one per channel+kind), instead of the thousands of
        # individual error annotations they replaced. Appended after the per-frame
        # annotations so the storm summary trails its (suppressed) members.
        batch.annotations.extend(self._storm.flush_batch())

        # Advance the PS-side read pointer past the bytes we actually
        # consumed (parsed frames + resync slides). Leftover bytes in the
        # tail (partial frame) stay un-advanced — next tick re-reads them.
        consumed_bytes = cursor
        self._pad_total += pad_bytes
        self._after_frame = after_frame
        self._read_byte_ofs = (self._read_byte_ofs + consumed_bytes) % self._ring_bytes
        batch.ring_drained = self._read_byte_ofs == write_byte_ofs

        if resync_bytes > 0:
            self._resync_total += resync_bytes
            batch.annotations.append(Annotation(
                channel_id=_ZS_RESYNC_CHANNEL,
                ts_start_ns=ts_ns,
                ts_end_ns=ts_ns + window_ns,
                kind="zs_resync",
                text=f"resync +{resync_bytes}B (total={self._resync_total}B)",
                color="#cc3333",
                is_error=True,
            ))

        if self._write_back_read_ptr:
            # DS-P2.59: RING_READ_PTR is now the active ring DRAIN pointer — the
            # PL feeds it into ddr_stream_axi.csr_rd_ptr_addr_i to free ring
            # space. Write the consumed BYTE OFFSET directly (same units as
            # RING_COMMIT_PTR), NOT a message count. Without this advance the
            # ring fills once and the DMA write side stalls (silent loss).
            try:
                self._regbank.write_u32(Regs.RING_READ_PTR,
                                        self._read_byte_ofs & 0xFFFFFFFF)
            except Exception as exc:
                logger.warning("RING_READ_PTR write-back failed: %s", exc)

        self._tick_index += 1
        return batch

    def _write_seed_read_ptr(self, seed: int) -> None:
        """DS-P2.284: write RING_READ_PTR at the seed tick, not only after the first drain.

        The seed skips the ring's back-history (the read head starts at the commit). Until this write, the PL's
        drain pointer still holds whatever the previous reader (or reset) left there; on a ring that was full at
        start the PL sees no free space, writes nothing new, the commit never moves, no drain ever runs and so
        the pointer was never written: the ring stayed full for the life of the daemon. Writing the seed frees
        the skipped bytes at once. One line names the skip so the window-start evidence of a run can be read.
        """
        try:
            old = self._regbank.read_u32(Regs.RING_READ_PTR) % self._ring_bytes
        except Exception as exc:
            old = None
            logger.warning("RING_READ_PTR read at seed failed: %s", exc)
        try:
            self._regbank.write_u32(Regs.RING_READ_PTR, seed & 0xFFFFFFFF)
        except Exception as exc:
            logger.warning("RING_READ_PTR write at seed failed: %s", exc)
            return
        if old is None:
            logger.info("ring seed: RING_READ_PTR set to %d (previous value unread) (DS-P2.284)", seed)
        else:
            logger.info("ring seed: RING_READ_PTR %d -> %d, %d bytes of back-history skipped (DS-P2.284)",
                        old, seed, (seed - old) % self._ring_bytes)

    def _pad_run(self, span, cursor: int, after_frame: bool = True) -> int:
        """DS-P2.280: bytes of PL zero pad at span[cursor:], 0 when there is none, -1 when the bytes are zero up
        to the span end and the word boundary lies beyond it. A pad runs from a position that is not on a
        DDR-word boundary up to the next boundary, all zero (ddr_fifo_intf adds 1..ratio-1 zero messages to
        complete a partial word). Zero bytes that start on a boundary, or that stop before it, are not pad: the
        caller resyncs on them as before. A pad is taken only right after a parsed frame (after_frame), since the
        PL completes the word after its last real message; after a resync slide the zeros are resynced too.
        A whole zero DDR word that starts on a boundary is therefore reported as zs_resync. Its known source is
        DDR-T2.24: a producer write on the idle-flush trigger cycle with ratio-1 messages pending makes
        ddr_fifo_intf commit a whole extra zero word (shown in sim at ratio 4 and 8)."""
        w = self._ring_word_bytes
        if not w or not after_frame:
            return 0
        rem = ((self._read_byte_ofs + cursor) % self._ring_bytes) % w
        if rem == 0:
            return 0
        n = w - rem
        run = span[cursor:cursor + n]
        if any(run):
            return 0
        return n if len(run) == n else -1

    def close(self) -> None:
        for closer, name in ((self._ddr, "ddr"), (self._regbank, "regbank")):
            try:
                closer.close()
            except Exception as exc:
                logger.warning("%s close failed: %s", name, exc)


class EyeSweepDataSource:
    """DS-P2.66 Phase 5 — ACTIVE eye-scan producer (live bathtub → RouteWave).

    The eye instrument is ACTIVE, not passive: the PS drives the eye_engine
    point-by-point and streams each measured (freq, phase) cell to RouteWave as
    the DOMAIN_EYE ``eye_point`` / ``eye_closed`` annotation its BathtubView
    (LT-P3.218) consumes — so starting this daemon fills the bathtub LIVE, cell
    by cell. Unlike the datalogger sources (which DRAIN PL-produced frames from the
    DDR ring), eye points are PRODUCED here on the PS: one
    ``EyeEngineDriver.measure()`` per ``next_batch()``.

    Implements the DataSource protocol, so it plugs into the same serve_fanout
    loop as the datalogger sources (``python3 -m sw.stream --source eye``). A cell
    that never locks — or a wedged engine that never asserts DONE (e.g. no PMOD
    loopback jumper) — is reported as an ``eye_closed`` cell, never a fabricated
    BER (bug-finding-is-success). The sweep loops by default so the eye updates
    live; ``apply_config`` re-triggers it and toggles the crosstalk aggressor.
    """

    EYE_CHANNEL = 107          # channels.py "eye_point" annotation channel
    EYE_SUMMARY_CHANNEL = 108  # channels.py "eye_summary" (DS-P3.119 max-rate)
    _CELL_DT_NS = 1_000_000    # 1 ms per cell on the RouteWave timeline
    # mirror decoders.decode_eye colours (the canonical DOMAIN_EYE annotation)
    _OK_COLOR = "#33aa55"
    _CLOSED_COLOR = "#cc3333"

    def __init__(self, regbank: Regbank, freqs, phases, *,
                 aggressor: bool = False, bit_target: int = 1_000_000,
                 poll_timeout_s: float = 0.5, sample_clk_hz: int = 100_000_000,
                 cell_period_s: float = 0.0, loop: bool = True,
                 max_freq: bool = False, ber_threshold: float = 1e-6,
                 eye_margin_ui: float = 0.30):
        from .eye_sweep import EyeEngineDriver  # local: avoid load-order coupling
        self._regbank = regbank
        self._driver = EyeEngineDriver(regbank, bit_target=bit_target,
                                       poll_timeout_s=poll_timeout_s)
        self._freqs = list(freqs)
        self._phases = list(phases)
        self._aggressor = aggressor
        self._sample_clk_hz = sample_clk_hz
        self._cell_period_s = cell_period_s
        self._loop = loop
        self._grid: List[Tuple[object, int]] = [
            (f, p) for f in self._freqs for p in self._phases
        ]
        self._pos = 0
        self._pass = 0
        self._ts_ns = 0
        # DS-P3.119 max-frequency mode: per next_batch, scan ONE frequency's full
        # sample phase (MMCM fine-PS) and stream its phase_ps-stamped points; after
        # the last frequency, emit the authoritative DOMAIN_EYE_SUMMARY verdict.
        self._max_freq = max_freq
        self._ber_threshold = ber_threshold
        self._eye_margin_ui = eye_margin_ui
        self._mf_pos = 0
        self._mf_results: list = []

    # ── DataSource protocol ──────────────────────────────────────────────
    def sample_clk_hz(self) -> int:
        return self._sample_clk_hz

    def next_batch(self) -> Optional["Batch"]:
        if self._max_freq:
            return self._next_batch_maxfreq()
        if self._pos >= len(self._grid):
            if not self._loop:
                return None                       # clean EOF after one sweep
            self._pos = 0
            self._pass += 1
            logger.info("eye-scan: sweep pass %d done; restarting (aggressor=%s)",
                        self._pass, self._aggressor)
        freq, phase = self._grid[self._pos]
        self._pos += 1

        if self._cell_period_s:
            time.sleep(self._cell_period_s)       # pace the live fill

        try:
            point = self._driver.measure(freq, phase, aggressor=self._aggressor)
        except TimeoutError:
            # Engine never reached DONE (e.g. no PMOD jumper → never locks):
            # surface as a CLOSED cell, never crash the drain loop.
            point = zs_msg.EyePoint(freq_sel=freq.sel, phase_sel=phase,
                                    err_count=0, bit_count=0, locked=False,
                                    freq_hz=freq.hz)
        except Exception as exc:                  # a transient bus glitch is non-fatal
            logger.warning("eye-scan: measure(f=%d phi=%d) failed: %s",
                           freq.sel, phase, exc)
            point = zs_msg.EyePoint(freq_sel=freq.sel, phase_sel=phase,
                                    err_count=0, bit_count=0, locked=False,
                                    freq_hz=freq.hz)

        batch = Batch()
        batch.annotations.append(self._annotate(point))
        self._ts_ns += self._CELL_DT_NS
        return batch

    def apply_config(self, datalogger_id: int, cfg_word: int,
                     op: str = "set_config") -> Optional[str]:
        """Live trigger: bit1 = aggressor enable; any write re-arms the sweep
        from cell 0, so a CONTROL frame re-triggers the experiment."""
        self._aggressor = bool(cfg_word & 0x2)
        self._pos = 0
        self._mf_pos = 0
        self._mf_results = []
        logger.info("eye-scan: re-triggered via CONTROL (aggressor=%s)",
                    self._aggressor)
        return None

    def read_stats(self) -> Optional[dict]:
        return None

    def close(self) -> None:
        try:
            self._regbank.close()
        except Exception:
            pass

    # ── internal ─────────────────────────────────────────────────────────
    def _annotate(self, pt) -> "Annotation":
        """EyePoint -> Annotation, mirroring decoders.decode_eye (the canonical
        DOMAIN_EYE eye_point / eye_closed shape RouteWave's BathtubView keys on:
        kind + fields{freq_sel, phase_sel, freq_hz, locked, err_count, bit_count,
        ber}). Inline so the PS-side producer needs no PL-ring frame."""
        t0, t1 = self._ts_ns, self._ts_ns + self._CELL_DT_NS
        if not pt.locked or pt.bit_count == 0:
            return Annotation(
                channel_id=self.EYE_CHANNEL, ts_start_ns=t0, ts_end_ns=t1,
                kind="eye_closed",
                text=f"eye[f{pt.freq_sel} phi{pt.phase_sel}] CLOSED (no lock)",
                color=self._CLOSED_COLOR, is_error=False,
                fields={"freq_sel": pt.freq_sel, "phase_sel": pt.phase_sel,
                        "phase_ps": pt.phase_ps, "phase_ui": pt.phase_ui,
                        "freq_hz": pt.freq_hz, "locked": False,
                        "err_count": pt.err_count, "bit_count": pt.bit_count,
                        "ber": None})
        return Annotation(
            channel_id=self.EYE_CHANNEL, ts_start_ns=t0, ts_end_ns=t1,
            kind="eye_point",
            text=f"eye[f{pt.freq_sel} phi{pt.phase_sel}] BER={pt.ber:.2e} "
                 f"({pt.err_count}/{pt.bit_count})",
            color=self._OK_COLOR, is_error=False,
            fields={"freq_sel": pt.freq_sel, "phase_sel": pt.phase_sel,
                    "phase_ps": pt.phase_ps, "phase_ui": pt.phase_ui,
                    "freq_hz": pt.freq_hz, "locked": True,
                    "err_count": pt.err_count, "bit_count": pt.bit_count,
                    "ber": pt.ber})

    # ── DS-P3.119 max-frequency mode ──────────────────────────────────────
    def _next_batch_maxfreq(self) -> Optional["Batch"]:
        """One frequency's full-UI MMCM-PS phase scan per call; after the last
        frequency, append the authoritative DOMAIN_EYE_SUMMARY verdict that
        RouteWave's link-margin view (LT-P3.243) consumes."""
        from .eye_sweep import eye_width, FreqResult, MaxFreqResult, summary_from_result
        if self._mf_pos >= len(self._freqs):
            if not self._loop:
                return None
            self._mf_pos = 0
            self._mf_results = []
            self._pass += 1
            logger.info("eye max-freq: sweep pass %d done; restarting", self._pass)

        freq = self._freqs[self._mf_pos]
        self._mf_pos += 1
        if self._cell_period_s:
            time.sleep(self._cell_period_s)

        batch = Batch()
        try:
            scan = self._driver.scan_phase(freq, aggressor=self._aggressor)
        except Exception as exc:                  # a wedged PS/engine is non-fatal
            logger.warning("eye max-freq: scan(f=%d) failed: %s", freq.sel, exc)
            scan = None

        if scan is not None:
            for s in scan.samples:
                batch.annotations.append(self._annotate(s.point))
                self._ts_ns += self._CELL_DT_NS
            width = eye_width(scan, self._ber_threshold)
            passed = width.width_ui >= self._eye_margin_ui
            self._mf_results.append(FreqResult(freq=freq, width=width, passed=passed))

        # After the final frequency, emit the summary verdict.
        if self._mf_pos >= len(self._freqs) and self._mf_results:
            passing = [r.freq.hz for r in self._mf_results if r.passed]
            result = MaxFreqResult(
                max_freq_hz=max(passing) if passing else 0,
                results=list(self._mf_results),
                ber_threshold=self._ber_threshold,
                eye_margin_ui=self._eye_margin_ui)
            batch.annotations.append(self._annotate_summary(summary_from_result(result)))
            logger.info("eye max-freq: MAX RATE %.1f MHz (margin %.2f UI, BER<=%.0e)",
                        result.max_freq_hz / 1e6, self._eye_margin_ui, self._ber_threshold)
        return batch

    def _annotate_summary(self, summary) -> "Annotation":
        """EyeSummary -> Annotation on the eye_summary channel (kind=eye_summary),
        mirroring decoders._decode_eye_summary so RouteWave keys on the same shape."""
        t0, t1 = self._ts_ns, self._ts_ns + self._CELL_DT_NS
        if summary.max_freq_hz > 0:
            text = (f"MAX RATE {summary.max_freq_hz/1e6:.1f} MHz "
                    f"@ BER<={summary.ber_threshold:.0e} "
                    f"(margin {summary.eye_margin_ui:.2f} UI)")
            color, is_err = self._OK_COLOR, False
        else:
            text = (f"LINK FAILS — no rate holds {summary.eye_margin_ui:.2f} UI "
                    f"@ BER<={summary.ber_threshold:.0e}")
            color, is_err = self._CLOSED_COLOR, True
        return Annotation(
            channel_id=self.EYE_SUMMARY_CHANNEL, ts_start_ns=t0, ts_end_ns=t1,
            kind="eye_summary", text=text, color=color, is_error=is_err,
            fields={
                "max_freq_hz": summary.max_freq_hz,
                "ber_threshold": summary.ber_threshold,
                "eye_margin_ui": summary.eye_margin_ui,
                "curve": [
                    {"freq_hz": f.freq_hz, "eye_width_ps": f.eye_width_ps,
                     "eye_width_ui": f.eye_width_ui, "passed": f.passed,
                     "partial": f.partial}
                    for f in summary.freqs
                ],
            })
