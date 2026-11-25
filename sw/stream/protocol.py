# rr unknown-llm (AI-generated)
"""wave_stream_v1 protocol primitives — encoder, frame tags, HELO helpers.

Wire spec is defined in routertl/docs/protocol/wave_stream_v1.md (D.62 + D.64).
This module is the deepskopion-side mirror of the constants and encoders that
the canonical mock_wave_stream.py producer in routertl/tools/ uses. Keep both
in sync — when the spec ticks, both files update together.

DS-P2.30: deepskopion adopts wave_stream_v1 natively as its daemon-to-host
egress format. No prior wire to migrate from; this is the only format the
daemon ever spoke.

LT-P3.86 follow-up: lifting these constants into a stand-alone pip package
(routertl-wave-stream-protocol or similar) would drop the duplication, but is
not blocking — the protocol surface is small and stable.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional

import msgpack

from . import edge_capture

PROTOCOL_VERSION = "wave_stream_v1"
_FALLBACK_DAEMON_VERSION = "0.1.0"
PRODUCER_NAME = f"deepskopion-daemon/{_FALLBACK_DAEMON_VERSION}"


@functools.lru_cache(maxsize=1)
def daemon_version() -> str:
    """The daemon binary's own version for the STATS frame (DS-P3.82).

    Resolves: ``$DS_DAEMON_VERSION`` (the deploy/init script bakes a git-describe
    here, since the board rootfs has no git) → a live ``git describe`` when run
    from a checkout (dev/host) → the hardcoded fallback. Distinct from the RTL
    git hash — the two diverge when the daemon is redeployed without re-flashing
    the SOF, which RouteWave surfaces as a mismatch. Cached (the value is fixed
    for the process)."""
    env = os.environ.get("DS_DAEMON_VERSION")
    if env:
        return env.strip()
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, errors="replace", timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return _FALLBACK_DAEMON_VERSION

# Frame tags — keep in sync with the spec and with
# routertl/tools/mock_wave_stream.py.
TAG_HELO = 0
TAG_HELO_OK = 1
TAG_SAMPLES = 2
TAG_RATE_HINT = 3
TAG_EOF = 4
TAG_ERR = 5
TAG_ANNOTATIONS = 6
TAG_EDGE_EVENTS = 7  # DS-P3.69 / LT-P3.124: raw Edge Detector edges → RouteWave EdgeStreams
TAG_CONTROL = 8      # LT-P3.142: consumer→producer datalogger enable/config (DS-P3.72)
TAG_SYNC = 9         # DS-P3.67 catch-up→live boundary marker. Renumbered 7→9 (D.14)
#                      to adopt RouteWave's frozen FrameTag enum (7=EdgeEvents,
#                      8=Control); RouteWave ignores unknown tag 9.
TAG_CONTROL_ACK = 10  # DS-P3.75: producer→consumer CONTROL apply confirmation
#                      (ground-truth applied state so the host panel can reflect it).
TAG_STATS = 11        # DS-P3.82: producer→consumer periodic telemetry (stream drops,
#                      DDR ring fill, backpressure, RTL git/build + daemon version).
TAG_DIGEST = 12       # DS-P3.89: producer→consumer rolling-window digest (per-protocol
#                      rates + error %, last-N) + standing anomaly-verdict. The streaming
#                      sibling of the sim HWTX digest/verdict tiers (RTL-P3.709) — serves
#                      RouteWave Mission Control AND an agent (an infinite live stream is
#                      not ingestible; this bounds it to a periodic summary). Consumer:
#                      RouteWave LT-P3.202. Forward-compatible (D.150): a consumer that
#                      predates tag 12 skips it and the session continues.

# DS-P3.89: schema string self-describing the DIGEST payload shape, so an agent
# (or a future consumer) can version-gate the digest body independently of the
# wire tag. Bumped if the digest body shape changes incompatibly.
DIGEST_SCHEMA = "wave_stream_digest_v1"


def pack_frame(tag: int, payload: Dict[str, Any]) -> bytes:
    """Encode a [tag, payload] tuple as a MessagePack-framed bytes blob.

    Each frame on the wire is a single msgpack object (2-element list). The
    consumer reads one msgpack object per frame using the streaming Unpacker.
    """
    return msgpack.packb([tag, payload], use_bin_type=True)


def make_helo(sample_clk_hz: int, channels: List[Dict[str, Any]],
              channel_total_bits: int) -> Dict[str, Any]:
    """Build the HELO payload that opens the session.

    Per spec, HELO carries the protocol version, producer name, sample clock
    in Hz, the channel descriptor list, and the packed sample width in bits
    (so the consumer can sanity-check slicing).
    """
    return {
        "ver": PROTOCOL_VERSION,
        "producer": PRODUCER_NAME,
        "sample_clk_hz": int(sample_clk_hz),
        "channels": channels,
        "channel_total_bits": int(channel_total_bits),
    }


def make_eof(reason: str) -> Dict[str, Any]:
    """Graceful end-of-stream payload."""
    return {"reason": str(reason)}


def make_err(code: str, detail: str = "") -> Dict[str, Any]:
    """Error frame — surfaced to the consumer before the socket closes.

    The human-readable half rides under ``message``, which is what the wire
    contract requires and what the consumer reads. It was previously emitted as
    ``detail``: RouteWave looks up ``message`` only, so the sole explanation of
    a FATAL session termination decoded to an empty string — the one frame whose
    whole purpose is to say why. Caught by WS-C0 (DS-P1.29)."""
    return {"code": str(code), "message": str(detail)}


def make_sync() -> Dict[str, Any]:
    """DS-P3.67 catch-up boundary marker. Emitted once after a new consumer's
    keyframe + backfill, before the first live frame, so the consumer can clear
    a 'catching up' indicator and reset live seq-continuity expectations."""
    return {"live": True}


def make_edge_events(seq: int, words: List[int], clk_mhz: int = 100,
                    anchor_ns: int = 0) -> Dict[str, Any]:
    """DS-P3.69 EDGE_EVENTS payload (TAG_EDGE_EVENTS) → RouteWave EdgeStreams.

    `words` are 32-bit EDGE_EVENT words, LT-frozen layout:
      [31:27]=device_id (edge datalogger instance) [26]=S [25]=F [24]=E
      [23:8]=ts_us [7:0]=ext.
    RouteWave decodes the device_id per word, so one batch may mix instances
    (e.g. EDGE0=SCL=1 + EDGE1=SDA=2).

    `anchor_ns` (DS-P2.78) is the ABSOLUTE nanosecond timestamp of the first
    word in this frame, and it is what makes the words placeable in time at all.
    ts_us is only the LOW 16 BITS of the microsecond count, so it wraps every
    65.536 ms; the old reconstruction formula `ts_us*1000 + ext*(1000/clk_mhz)`
    yields an OFFSET INSIDE A 65 ms WINDOW, not a time. ANNOTATIONS on the same
    stream carry absolute ts_start_ns, so a consumer following that formula puts
    the two halves on timebases that drift apart by the board's whole uptime —
    measured at 10 days, which is why RouteWave's edge lanes held thousands of
    transitions and drew nothing (LT-P2.105).

    Reconstruct as: take the high bits from anchor_ns, splice in ts_us, and pick
    the nearest 65.536 ms window (the batch may straddle a wrap).

    That nearest-window pick is unambiguous only while the word lies within
    half a window of the anchor, so the producer guarantees (DS-P3.138,
    wave_stream_v1 2.7.0): every word's whole-µs time is STRICTLY within
    EDGE_ANCHOR_REACH_US (32 768 µs) of anchor_ns // 1000. A batch whose edges
    span more is cut into several EDGE_EVENTS frames, each with its own anchor
    (data_source.Batch.add_edge); the cut is never left to the consumer.

    anchor_ns=0 means not supplied — a consumer must then treat the timestamps
    as window-relative and MUST NOT mix them with annotation timestamps."""
    return {
        "seq": int(seq),
        "clk_mhz": int(clk_mhz),
        "anchor_ns": int(anchor_ns),
        "words": [int(w) & 0xFFFFFFFF for w in words],
    }


def make_control(datalogger_id: int, cfg_word: int, op: str = "set_config") -> Dict[str, Any]:
    """CONTROL payload (TAG_CONTROL=8) — consumer→producer datalogger control
    (LT-P3.142 / DS-P3.72). The host requests a per-datalogger CONFIG change; the
    producer applies `cfg_word` to the datalogger addressed by `datalogger_id`. This
    builder exists for tests/symmetry — deepskopion is the CONSUMER of CONTROL."""
    # Contract revision 2.x (DS-P2.111): the instance id is `datalogger_id`.
    return {"op": str(op), "datalogger_id": int(datalogger_id),
            "cfg_word": int(cfg_word) & 0xFFFF}


OP_STIM = "stim"


def make_stim_control(protocol: str, route: Optional[str] = None) -> Dict[str, Any]:
    """CONTROL payload asking the producer to arm its on-board stimulus
    generator (DS-P2.136) for one protocol — ``uart``/``i2c``/``spi``/``edge``.
    The daemon writes the DS-P1.58 production STIM_* register sequence
    (``stimulus_armer``); the CONTROL_ACK echoes ``op``/``protocol``.
    For ``uart``/``i2c``/``spi`` an accepted ACK also carries ``ps_emio_taken``
    (bool): True when the producer infers that the armed generator now drives
    the PS EMIO legs of that protocol (a Zynq-7000 image, whose top sets
    G_STIM_DRIVES_EMIO), and then ``warning`` (str) says why and names
    ``release_stimulus_generator()`` (DS-P2.253, ``stim_emio``). Both are
    optional keys; wave_stream_v1 consumers ignore unknown keys.

    wave_stream_v1 CONTROL requires ``datalogger_id`` and ``cfg_word`` on every
    op, so a stim frame carries them too: the id is the data logger that
    observes the armed generator, and ``cfg_word`` is 0 (stim applies no
    config word). The daemon routes on ``op`` before ``decode_control``.

    ``route`` (DS-P2.343, optional key): an edge route the arm writes into
    EMIO_MUX_SEL before its burst, so the edge inputs see it (``spi`` only;
    other protocols reject a route). The ACK echoes it. A producer that does
    not know the key ignores it and arms without a route (COMPAT unknown_key)."""
    from .stimulus_contract import STIM_DATALOGGER_ID
    try:
        datalogger_id = STIM_DATALOGGER_ID[protocol]
    except KeyError:
        raise ValueError(f"no on-board stimulus generator for {protocol!r}; "
                         f"expected one of {sorted(STIM_DATALOGGER_ID)}") from None
    frame = {"op": OP_STIM, "protocol": str(protocol),
             "datalogger_id": datalogger_id, "cfg_word": 0}
    if route is not None:
        from .edge_route import ROUTES
        if route not in ROUTES:
            raise ValueError(f"no edge route {route!r}; expected one of {sorted(ROUTES)}")
        frame["route"] = str(route)
    return frame


OP_EDGE_ROUTE = "edge_route"


def make_edge_route_control(route: str) -> Dict[str, Any]:
    """CONTROL payload asking the producer to route one signal set onto the
    Edge Detector inputs (DS-P3.123): ``i2c``/``spi``/``uart``/``can``/``stim``,
    or ``pmod`` for the PMOD jb[5:2] pins. The daemon rewrites EMIO_MUX_SEL
    bits [7:3] (``edge_route.apply_edge_route``) and enables the edge data
    loggers the route feeds. The CONTROL_ACK echoes ``op``/``route`` and carries
    ``emio_mux_sel`` and the new ``edge_channels`` names, because a HELO a
    consumer already holds names the lanes as they were routed at connect.

    As for ``stim``, wave_stream_v1 CONTROL requires ``datalogger_id`` and
    ``cfg_word``: the id is EDGE0's (1) and ``cfg_word`` is 0."""
    from .edge_route import ROUTES
    if route not in ROUTES:
        raise ValueError(f"no edge route {route!r}; expected one of {sorted(ROUTES)}")
    return {"op": OP_EDGE_ROUTE, "route": str(route), "datalogger_id": 1, "cfg_word": 0}


def decode_control(payload: Dict[str, Any]):
    """Decode an inbound CONTROL payload → (op, datalogger_id, cfg_word).

    Raises ValueError on a malformed payload (missing / non-int fields, or a
    cfg_word outside uint16) so the daemon can log it rather than misapply it."""
    if not isinstance(payload, dict):
        raise ValueError("CONTROL payload is not a map")
    try:
        datalogger_id = int(payload["datalogger_id"])
        cfg_word = int(payload["cfg_word"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"CONTROL missing or non-int datalogger_id/cfg_word: {exc}")
    if not 0 <= cfg_word <= 0xFFFF:
        raise ValueError(f"CONTROL cfg_word {cfg_word} out of uint16 range")
    return str(payload.get("op", "set_config")), datalogger_id, cfg_word


def make_control_ack(datalogger_id: int, applied_cfg_word: int, ok: bool = True,
                     reason: str = None) -> Dict[str, Any]:
    """CONTROL_ACK payload (TAG_CONTROL_ACK=10) — producer→consumer confirmation
    of a CONTROL apply (DS-P3.75). Reports the ACTUAL applied state so a host
    panel reflects ground truth and surfaces rejects. `datalogger_id` is
    0xFFFFFFFF when the producer couldn't decode the control; `applied_cfg_word`
    is the config actually written (0 on reject); `reason` is present only when
    ok is False. Unlike a wave_stream ERR this is non-fatal (D.15)."""
    p: Dict[str, Any] = {
        "datalogger_id": int(datalogger_id) & 0xFFFFFFFF,
        "applied_cfg_word": int(applied_cfg_word) & 0xFFFF,
        "ok": bool(ok),
    }
    if reason:
        p["reason"] = str(reason)
    return p


def decode_control_ack(payload: Dict[str, Any]):
    """Decode a CONTROL_ACK → (datalogger_id, applied_cfg_word, ok, reason). For tests/symmetry."""
    if not isinstance(payload, dict):
        raise ValueError("CONTROL_ACK payload is not a map")
    return (int(payload["datalogger_id"]), int(payload["applied_cfg_word"]),
            bool(payload.get("ok", False)), payload.get("reason"))


def make_stats(seq: int, *, daemon_ver: str, stats: Dict[str, Any]) -> Dict[str, Any]:
    """STATS payload (TAG_STATS=11) — producer→consumer periodic telemetry
    (DS-P3.82 frame, DS-P3.84 full counter set). Wraps a read_regbank_stats()
    snapshot with the frame seq + the daemon's own version (distinct from the RTL
    hash — they diverge when the daemon is redeployed without re-flashing the
    SOF). All counters are free-running uint32 totals straight from the regbank;
    the consumer differences successive STATS for rates. Idempotent — a consumer
    always takes the latest. `stats` carries: rtl_git_hash, rtl_build_date,
    ring_bytes, ring_used_bytes, ddr_backpressure_cnt, ring_alarm{excursions,
    active}, proto_pkts/proto_errs/proto_drops, fifo_overflows (scalar),
    fifo_high_watermark (scalar), arb_grants (scalar, null when RTL-undriven),
    heartbeat, clk_ddr_ticks, ddr_reset_status (see read_regbank_stats). The
    fifo/arb fields were flattened from {burst,stream} maps to scalars in
    DS-P3.94 ph3 — the Stream tier is dead (D.4). DS-P2.61: also
    proto_decode_errs{<proto>:count} — the per-protocol SW-DECODED error tally
    (annotation is_error, = the ERRORS chip count), DISTINCT from proto_errs
    which is the RTL/HW ERR_CNT_* datalogger error bus. The two legitimately diverge
    (e.g. a decoded UART STOP-ERROR may not raise the HW bus); a consumer must
    surface/label BOTH so 'uart Err 0' next to 44%-STOP-ERROR is not a contradiction."""
    return {
        "seq": int(seq),
        "daemon_ver": str(daemon_ver),
        **stats,
    }


def make_digest(seq: int, *, window_s: float, protocols: Dict[str, Any],
                recent: List[Dict[str, Any]], verdict: Dict[str, Any]) -> Dict[str, Any]:
    """DIGEST payload (TAG_DIGEST=12) — rolling-window summary + anomaly verdict
    (DS-P3.89). The streaming sibling of the sim HWTX digest/verdict (RTL-P3.709):
    a periodic, self-contained snapshot that makes an *infinite* live stream
    ingestible by RouteWave Mission Control AND an agent, without either having to
    difference successive frames or replay the whole stream.

    - ``window_s`` — the rolling window the rates/percentages are computed over.
    - ``protocols`` — ``{<proto>: {pkts, errs, pkt_rate, err_pct}}`` over the
      window (``pkt_rate`` = events/s over the observed span; ``err_pct`` =
      100·errs/pkts). Only protocols that saw traffic in the window appear.
    - ``recent`` — last-N notable (error) events ``{ts, kind, text, is_error}``,
      a compact tail so an agent sees *what* recently went wrong, not just rates.
    - ``verdict`` — the STANDING anomaly verdict: ``{status, headline,
      anomalies:[{protocol, severity, metric, value, message}]}`` where ``status``
      is ``OK``/``WARN``/``CRITICAL``. The headline is the human one-liner
      ('UART error rate 44% — likely framing/baud fault'); anomalies carry the
      per-protocol detail. Empty ``anomalies`` + ``status=OK`` = all nominal.

    Idempotent like STATS (a consumer always takes the latest); ``seq`` is
    monotonic and diagnostic-only. Carries ``schema`` (DIGEST_SCHEMA) so the body
    shape can version independently of the wire tag."""
    return {
        "seq": int(seq),
        "schema": DIGEST_SCHEMA,
        "window_s": float(window_s),
        "protocols": protocols,
        "recent": list(recent),
        "verdict": verdict,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DS-P2.50: wave_events_v1 — the agent-facing NDJSON peer of wave_stream_v1.
# Wire spec: docs/software/data_plane/wave_events_v1_spec.md.
# ─────────────────────────────────────────────────────────────────────────────

EVENTS_PROTOCOL_VERSION = "wave_events_v1"
EVENTS_DEFAULT_PORT = 7476

KIND_FRAME = "frame"
KIND_ANNOT = "annot"
KIND_EDGE = "edge"
EVENT_KINDS = (KIND_FRAME, KIND_ANNOT, KIND_EDGE)
DEFAULT_EVENT_KINDS = (KIND_FRAME, KIND_ANNOT)  # edge is opt-in (volume)

# Session lines, never subject to a subscription filter.
KIND_ERROR = "error"
KIND_WARNING = "warning"
KIND_END = "end"
KIND_PING = "ping"
SESSION_KINDS = (KIND_ERROR, KIND_WARNING, KIND_END, KIND_PING)

# wave_stream_v1 carries edges by device id, outside its channel table, so these
# ids are declared by the wave_events_v1 HELO only. Above the 200-range system
# channels; one per 5-bit EDGE_EVENT instance.
EDGE_CHANNEL_BASE = 300
EDGE_INSTANCES = 32

_ZS_FRAME_CHANNEL_ID = 203
_COUNTER_ANNOTATION = ("_pkt_count", "_err")  # regbank deltas on a protocol lane
_CHANNEL_SCHEMAS = {"uart_byte": "uart_byte_v1", "pl_heartbeat": "counter_v1"}
_EDGE_TS_PERIOD_US = 1 << 16
#: DS-P3.138: how far, in whole µs and exclusive, an EDGE_EVENTS word may lie
#: from its frame's anchor_ns. Half the 16-bit ts_us window: beyond it the
#: nearest-window reconstruction (make_edge_events) lands a whole 65.536 ms off.
#: Bound to the contract's EDGE_ANCHOR_REACH_US by the conformance test.
EDGE_ANCHOR_REACH_US = _EDGE_TS_PERIOD_US // 2


class WaveEvent(NamedTuple):
    """One serialized NDJSON line plus what a subscription matches it on."""
    ch: Optional[int]
    name: Optional[str]
    kind: str
    line: bytes


class EventFilter:
    """One consumer's subscription. ``channels``/``kinds`` None = everything."""

    def __init__(self, channels: Optional[Iterable] = None,
                 kinds: Optional[Iterable[str]] = DEFAULT_EVENT_KINDS) -> None:
        self.channels = None if channels is None else frozenset(channels)
        self.kinds = None if kinds is None else frozenset(kinds)

    def wants_kind(self, kind: str) -> bool:
        return self.kinds is None or kind in self.kinds

    def matches(self, event: WaveEvent) -> bool:
        if event.kind in SESSION_KINDS:
            return True
        if not self.wants_kind(event.kind):
            return False
        return (self.channels is None or event.ch in self.channels
                or event.name in self.channels)


def _uart_char(value: int) -> str:
    if 0x20 <= value < 0x7F or value in (0x09, 0x0A, 0x0D):
        return chr(value)
    return f"\\x{value:02x}"


class WaveEventsEncoder:
    """Serializes the daemon's decoded output as wave_events_v1 lines.

    Stateless after construction, so the drainer and every subscriber thread can
    share one. The source of truth is the same Annotation / EDGE_EVENT word the
    wave_stream_v1 frames are built from; nothing is decoded a second time."""

    def __init__(self, sample_clk_hz: int, channels: List[Dict[str, Any]],
                 clock=time.time) -> None:
        self._sample_clk_hz = int(sample_clk_hz)
        self._names = {int(c["id"]): str(c["name"]) for c in channels}
        # DS-P2.74: stacked lanes, id -> (source_channel, layer_depth).
        self._stacked = {int(c["id"]): (c["source_channel"], c["layer_depth"])
                         for c in channels if "layer_depth" in c}
        now = clock()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        self._started_at = stamp
        self._session_id = f"ds-{stamp.replace(':', '-')}-{uuid.uuid4().hex[:6]}"
        self._manifest = [self._describe(i, n) for i, n in self._names.items()
                          if self._is_annotation_channel(channels, i)]
        for entry in self._manifest:
            if entry["id"] in self._stacked:
                entry["kind"] = KIND_FRAME
                entry["source_channel"], entry["layer_depth"] = self._stacked[entry["id"]]
        self._manifest += [
            {"id": EDGE_CHANNEL_BASE + n, "name": f"edge[{n}]", "kind": KIND_EDGE,
             "schema": "edge_v1"} for n in range(EDGE_INSTANCES)]
        self.channel_ids = frozenset(c["id"] for c in self._manifest)
        self.channel_names = frozenset(c["name"] for c in self._manifest)

    @staticmethod
    def _is_annotation_channel(channels: List[Dict[str, Any]], channel_id: int) -> bool:
        # width>0 lanes are SAMPLES bits: transitions, which this stream omits.
        return any(int(c["id"]) == channel_id and not c.get("width")
                   for c in channels)

    @staticmethod
    def _describe(channel_id: int, name: str) -> Dict[str, Any]:
        kind = KIND_ANNOT if channel_id >= 200 and channel_id != _ZS_FRAME_CHANNEL_ID \
            else KIND_FRAME
        return {"id": channel_id, "name": name, "kind": kind,
                "schema": _CHANNEL_SCHEMAS.get(name, "decoded_v1")}

    @staticmethod
    def _dumps(obj: Dict[str, Any]) -> bytes:
        # ensure_ascii escapes every control char, so a payload can never split a
        # line. default=str: a decoder field JSON can't carry must not lose the event.
        return json.dumps(obj, separators=(",", ":"), default=str).encode() + b"\n"

    def helo(self) -> bytes:
        return self._dumps({
            "v": EVENTS_PROTOCOL_VERSION,
            "producer": PRODUCER_NAME,
            "started_at": self._started_at,
            "sample_clk_hz": self._sample_clk_hz,
            "session_id": self._session_id,
            "channels": self._manifest,
        })

    def annotation(self, ann: Any) -> WaveEvent:
        channel_id = int(ann.channel_id)
        name = self._names.get(channel_id, f"ch{channel_id}")
        counter = str(ann.kind).endswith(_COUNTER_ANNOTATION)
        kind = KIND_ANNOT if counter or channel_id >= 200 \
            and channel_id != _ZS_FRAME_CHANNEL_ID else KIND_FRAME
        if channel_id in self._stacked:
            kind = KIND_FRAME
        body: Dict[str, Any] = dict(ann.fields or {})
        if "byte" in body and name == "uart_byte":
            value = int(body["byte"])
            body.update(value=value, char=_uart_char(value))
        body.update(decoded_as=str(ann.kind), text=str(ann.text),
                    errors=[str(ann.kind)] if ann.is_error else [])
        return WaveEvent(channel_id, name, kind, self._dumps({
            "ts_ns": int(ann.ts_start_ns), "ts_end_ns": int(ann.ts_end_ns),
            "ch": channel_id, "kind": kind, "name": name, "body": body}))

    def edges(self, words: Iterable[int], anchor_ns: int = 0) -> List[WaveEvent]:
        """EDGE_EVENT words → edge lines, placed in absolute time from
        ``anchor_ns`` the way protocol.make_edge_events documents. Without an
        anchor the time is only an offset inside a 65.536 ms window, and the body
        says so rather than letting it pass for an annotation timestamp."""
        clk_mhz = max(1, self._sample_clk_hz // 1_000_000)
        anchor_us = int(anchor_ns) // 1000
        out = []
        for word in words:
            base_us = 0
            if anchor_ns:
                ts_us = (int(word) >> 8) & 0xFFFF
                base_us = anchor_us - (anchor_us % _EDGE_TS_PERIOD_US)
                nearest = min((base_us - _EDGE_TS_PERIOD_US, base_us,
                               base_us + _EDGE_TS_PERIOD_US),
                              key=lambda b: abs(b + ts_us - anchor_us))
                base_us = nearest
            edge = edge_capture.decode_edge_word(int(word), clk_mhz, base_us=base_us)
            channel_id = EDGE_CHANNEL_BASE + edge.instance
            name = f"edge[{edge.instance}]"
            body: Dict[str, Any] = {
                "value": edge.level,
                # S=1 is a state readback, not a transition: no previous level.
                "prev_value": None if edge.s else 1 - edge.level,
            }
            if not anchor_ns:
                body["ts_window_relative"] = True
            out.append(WaveEvent(channel_id, name, KIND_EDGE, self._dumps({
                "ts_ns": edge.timestamp_ns, "ch": channel_id, "kind": KIND_EDGE,
                "name": name, "body": body})))
        return out

    def session(self, kind: str, ts_ns: int = 0, **body: Any) -> WaveEvent:
        """An error / warning / end / ping line."""
        return WaveEvent(None, None, kind, self._dumps(
            {"ts_ns": int(ts_ns), "kind": kind, "body": body}))

    def parse_subscribe(self, line: bytes) -> EventFilter:
        """The consumer's first line → its filter. Raises ValueError on anything
        malformed or unknown: a typo'd filter that silently matched nothing would
        read as a quiet bus, the worst way for an agent to be wrong."""
        if not line.strip():
            return EventFilter()
        try:
            msg = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"first line is not JSON: {exc}") from None
        if not isinstance(msg, dict) or (msg and "subscribe" not in msg):
            raise ValueError('first line must be {} or {"subscribe": {...}}')
        sub = msg.get("subscribe", {})
        if not isinstance(sub, dict):
            raise ValueError("subscribe must be an object")
        channels = sub.get("channels", "*")
        kinds = sub.get("kinds", list(DEFAULT_EVENT_KINDS))
        if channels != "*":
            if not isinstance(channels, list):
                raise ValueError('channels must be a list or "*"')
            for c in channels:
                if c not in self.channel_ids and c not in self.channel_names:
                    raise ValueError(f"unknown channel {c!r}")
        if kinds != "*":
            if not isinstance(kinds, list):
                raise ValueError('kinds must be a list or "*"')
            for k in kinds:
                if k not in EVENT_KINDS:
                    raise ValueError(f"unknown kind {k!r}; expected {list(EVENT_KINDS)}")
        return EventFilter(None if channels == "*" else channels,
                           None if kinds == "*" else kinds)
