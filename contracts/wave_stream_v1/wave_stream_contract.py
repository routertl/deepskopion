#!/usr/bin/env python3
"""WS-C0 (DS-P1.29) — the executable `wave_stream_v1` producer/consumer contract.

This directory is the SEAM between a wave_stream producer (the deepskopion
daemon, the routertl REA bridge, the mock) and a wave_stream consumer
(RouteWave). Its job is to make "both sides speak the same protocol" a
checkable claim instead of a shared belief.

WHY it exists at all
--------------------
Before it, the seam was implemented twice and pinned nowhere: deepskopion
encodes the protocol in ``sw/stream/protocol.py``, RouteWave decodes it in
``logic-trace/include/logic/wave_stream/protocol.hpp``, and the only thing
holding the two together was a prose spec plus a coherence test that lives in
neither product (the super-repo ``tests/test_contract_alignment.py``). That
shape has already produced a wire defect: deepskopion shipped ``SYNC`` on tag 7
while RouteWave had frozen tag 7 as ``EDGE_EVENTS``, so one side's boundary
marker decoded as the other side's edge batch (D.14 — ``SYNC`` was renumbered to
9 afterwards). A contract that only exists as prose cannot fail; this one can.

WHO OWNS IT
-----------
Neither product. The normative prose spec is routertl's
(``routertl/docs/protocol/wave_stream_v1.md``); this directory is its executable
form and is *vendored* — copied verbatim into every repo that implements the
protocol. It is identified by ``CONTRACT_ID`` + the digest of its own files
(``manifest.json``), so "are we on the same contract?" is answered by comparing
a digest across repos, not by reading two headers side by side. A repo that
needs a change makes it in the spec and re-vendors; a repo MUST NOT edit its
local copy to make its own implementation pass.

WHAT A CONSUMER NEEDS FROM IT
-----------------------------
Nothing Python. The consumable artifacts are language-neutral:

  fixtures/*.p2c.mp   the exact bytes a consumer reads off the socket
  fixtures/*.c2p.mp   the exact bytes a producer reads off the socket
  fixtures/*.json     the same sessions in readable form (the source of truth
                      the .mp files are generated from, byte-for-byte)
  contract.json       tag registry, per-tag payload shapes, the compatibility
                      policy, and the role-key tables — as data
  manifest.json       every fixture, its digest, whether the checker must ACCEPT
                      or REJECT it, and (for rejections) the field the checker
                      must name

A C++ consumer feeds ``*.p2c.mp`` to its parser and asserts the accept/reject
verdict in the manifest. A Python producer can import this module directly.

THE COMPATIBILITY POLICY (the thing the ticket asks to be stated)
-----------------------------------------------------------------
See ``COMPAT`` below. In one line: an unknown *tag* is skipped, an unknown *key*
is ignored, an unknown *enum member* falls back to the documented default, and
the ONLY fatal incompatibility is a ``HELO.ver`` this side does not implement.

CLI
---
  wave_stream_contract.py check <fixture.json|stream.mp> [...]
        judge a fixture (full session) or a raw byte stream (one direction).
        exit 0 iff every argument got the verdict the manifest expects (for a
        fixture) or was ACCEPTED (for a bare stream).
  wave_stream_contract.py emit [--check]
        regenerate fixtures/*.mp + manifest.json from fixtures/*.json.
        --check regenerates into memory and fails if the committed bytes differ.
  wave_stream_contract.py verify
        run every fixture in manifest.json and require its recorded verdict AND
        (for rejections) its recorded failure field. This is the gate.
  wave_stream_contract.py --emit-contract-json
        print contract.json (the language-neutral projection of this module).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import msgpack

HERE = Path(__file__).resolve().parent
FIXTURE_DIR = HERE / "fixtures"
MANIFEST = HERE / "manifest.json"
CONTRACT_JSON = HERE / "contract.json"

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

#: The wire version string a producer MUST put in ``HELO.ver``.
PROTOCOL_VERSION = "wave_stream_v1"

#: Identity of THIS executable contract, independent of the wire version. The
#: wire version changes only on a breaking protocol change; this revision ticks
#: whenever the checker or the fixtures change, so two repos can tell whether
#: they vendored the same rules.
CONTRACT_ID = "wave_stream_v1"
CONTRACT_REVISION = "2.8.2"

# ---------------------------------------------------------------------------
# Compatibility policy — stated ONCE, here, and enforced below
# ---------------------------------------------------------------------------
#
# The prose spec says two contradictory things about extension. Its
# "Forward compatibility (v0.3+)" note says an unrecognised tag MUST be skipped
# and the session kept alive; its "Versioning" section still says adding a tag
# is breaking and an old consumer MUST answer ERR{FRAME_DECODE} and close. Both
# sentences are in the same document. That ambiguity is not academic — it is the
# soil the D.14 tag collision grew in, because "adding a tag is breaking" makes
# renumbering look as safe as appending. The contract resolves it, and the
# fixtures make the resolution testable.

COMPAT = {
    "unknown_tag": (
        "SKIP the frame and keep the session alive. Never ERR, never close. A "
        "tag this build does not know is a producer speaking a newer revision, "
        "not a corrupt stream."),
    "tag_registry": (
        "APPEND-ONLY. A tag number, once published in TAGS, is frozen to its "
        "meaning forever; it may never be reused or renumbered. Adding a tag is "
        "NOT a breaking change (see unknown_tag) — renumbering one is, and it is "
        "the change no consumer can detect. This is the D.14 rule."),
    "unknown_key": (
        "IGNORE it. Adding an optional map key to an existing frame is "
        "additive and non-breaking. A consumer MUST NOT fail on a key it does "
        "not know, so the required-key set below is the whole of what a "
        "consumer may rely on."),
    "unknown_enum_member": (
        "FALL BACK to the documented default for that field (channel `type` -> "
        "Signal, `role` -> Wire, `radix` -> Hexadecimal). This is what RouteWave "
        "already does, so a producer that invents a member gets SILENT fallback "
        "-- which is why unregistered members are a contract violation on the "
        "PRODUCER side even though they are survivable on the consumer side."),
    "unknown_kind": (
        "RENDER it generically. Annotation `kind` is a free-form producer "
        "string; a consumer groups and filters on it but never interprets it. "
        "A kind REGISTERED in ANNOTATION_PAYLOAD below must carry its registered "
        "role keys — that key set is the cross-repo contract."),
    "body_schema": (
        "SKIP the frame. A frame that carries its own body-schema string "
        "(DIGEST) versions its body independently of the wire tag; a consumer "
        "that does not implement the body revision skips that frame only."),
    "version_mismatch": (
        "FATAL, and the only fatal incompatibility. A HELO.ver whose value this "
        "side does not implement ends the session: answer ERR "
        "{code: VERSION_MISMATCH} and close. Guessing at an unknown wire "
        "version is how a green session stops meaning what it says."),
}


# ---------------------------------------------------------------------------
# The frozen tag registry
# ---------------------------------------------------------------------------
# tag -> (NAME, direction). Direction: "p2c" producer->consumer, "c2p"
# consumer->producer, "both" either side may send.
#
# APPEND-ONLY (COMPAT["tag_registry"]). The numbers are the contract.

TAGS = {
    0:  ("HELO", "p2c"),
    1:  ("HELO_OK", "c2p"),
    2:  ("SAMPLES", "p2c"),
    3:  ("RATE_HINT", "c2p"),
    4:  ("EOF", "both"),
    5:  ("ERR", "both"),
    6:  ("ANNOTATIONS", "p2c"),
    7:  ("EDGE_EVENTS", "p2c"),
    8:  ("CONTROL", "c2p"),
    9:  ("SYNC", "p2c"),
    10: ("CONTROL_ACK", "p2c"),
    11: ("STATS", "p2c"),
    12: ("DIGEST", "p2c"),
}

TAG_BY_NAME = {name: tag for tag, (name, _dir) in TAGS.items()}

#: ERR codes the spec defines. Extensible — an unknown code is not a failure,
#: it is a producer reporting something this revision has no name for.
ERR_CODES = ("FRAME_DECODE", "VERSION_MISMATCH", "CHANNEL_OVERFLOW",
             "ANNOTATION_REF", "TRANSPORT_LOST", "INTERNAL")

# ---------------------------------------------------------------------------
# Per-tag payload shapes
# ---------------------------------------------------------------------------
# Types: uint int float num str bool bin map list any; a trailing "?" means the
# value may also be null (msgpack nil).
#
# `req` is the whole of what a consumer may rely on (COMPAT["unknown_key"]).
# `opt` is documented-but-optional: type-checked when present, never demanded.
# Keys in neither set are ignored, by policy — so what makes a body belong to
# its tag is its REQUIRED keys, and that is precisely the check that catches a
# tag collision: a SYNC body carrying an EDGE_EVENTS payload has no `live`.

FRAME_SPEC = {
    "HELO": {
        "req": {"ver": "str", "producer": "str", "sample_clk_hz": "uint",
                "channels": "list", "channel_total_bits": "uint"},
        "opt": {"sample_width": "uint"},
    },
    "HELO_OK": {
        # A bare [1, {}] is a valid acknowledgement — the spec says so, and the
        # reference producer only waits for the frame, not for its contents.
        "req": {},
        "opt": {"consumer": "str", "max_rate_hint_hz": "uint"},
    },
    "SAMPLES": {"req": {"seq": "uint", "samples": "list"}, "opt": {}},
    "RATE_HINT": {"req": {"max_rate_hz": "uint"}, "opt": {}},
    "EOF": {"req": {"reason": "str"}, "opt": {}},
    "ERR": {"req": {"code": "str", "message": "str"}, "opt": {}},
    "ANNOTATIONS": {"req": {"seq": "uint", "annotations": "list"}, "opt": {}},
    "EDGE_EVENTS": {
        "req": {"seq": "uint"},
        "opt": {"clk_mhz": "uint", "anchor_ticks": "uint", "anchor_ns": "uint",
                "datalogger_id": "uint", "batch": "bin", "words": "list"},
    },
    "CONTROL": {
        "req": {"op": "str", "datalogger_id": "uint", "cfg_word": "uint"},
        "opt": {},
    },
    "SYNC": {"req": {"live": "bool"}, "opt": {}},
    "CONTROL_ACK": {
        "req": {"datalogger_id": "uint", "applied_cfg_word": "uint", "ok": "bool"},
        "opt": {"reason": "str"},
    },
    "STATS": {
        "req": {"seq": "uint", "daemon_ver": "str", "proto_stats": "map"},
        "opt": {"rtl_git_hash": "str", "rtl_build_date": "str",
                "ring_bytes": "uint", "ring_used_bytes": "uint",
                "ddr_backpressure_cnt": "uint", "ring_alarm": "map",
                "proto_decode_errs": "map", "fifo_overflows": "uint",
                "fifo_high_watermark": "uint", "arb_grants": "uint?",
                "heartbeat": "uint", "clk_ddr_ticks": "uint",
                "ddr_reset_status": "uint"},
    },
    "DIGEST": {
        "req": {"seq": "uint", "schema": "str", "window_s": "num",
                "protocols": "map", "recent": "list", "verdict": "map"},
        "opt": {},
    },
}

#: One entry of DIGEST.recent (the rolling error tail). Lifted out of _h_digest so that
#: the time-unit check below walks it like every other shape (2.4.0, LT-P2.251).
DIGEST_RECENT_SPEC = {"req": {"ts": "uint", "kind": "str", "text": "str", "is_error": "bool"},
                      "opt": {}}

#: Map-typed fields' shapes, lifted out of the validators so that the time-unit
#: walk sees their keys (2.4.2). Same keys as the literals they replace.
#: DIGEST.protocols.<name>: one per protocol in the digest window.
DIGEST_PROTOCOL_STAT_SPEC = {"req": {"pkts": "uint", "errs": "uint",
                                     "pkt_rate": "num", "err_pct": "num"},
                             "opt": {}}
#: DIGEST.verdict: the machine-readable "is the bus healthy?" answer.
DIGEST_VERDICT_SPEC = {"req": {"status": "str", "headline": "str",
                               "anomalies": "list"}, "opt": {}}
#: One entry of DIGEST.verdict.anomalies.
DIGEST_ANOMALY_SPEC = {"req": {"protocol": "str", "severity": "str",
                               "metric": "str", "value": "num",
                               "message": "str"}, "opt": {}}
#: STATS.proto_stats.<name>: one per data-logger protocol.
STATS_PROTO_COUNTERS_SPEC = {"req": {"pkts": "uint?", "errs": "uint?",
                                     "drops": "uint?"},
                             "opt": {"decode_errs": "uint?"}}
#: STATS.ring_alarm.
STATS_RING_ALARM_SPEC = {"req": {"excursions": "uint", "active": "bool"},
                         "opt": {}}

#: The DIGEST body-shape version this contract revision implements. Versioned
#: separately from the wire tag on purpose (COMPAT["body_schema"]).
DIGEST_SCHEMA = "wave_stream_digest_v1"

# ---------------------------------------------------------------------------
# Channel descriptors
# ---------------------------------------------------------------------------
# A descriptor field whose value is outside these sets gets SILENTLY replaced by
# the default in the consumer (COMPAT["unknown_enum_member"]), so the producer
# never learns its intent was dropped. That silence is why the contract checks
# the members here rather than trusting the fallback.

CHANNEL_TYPES = ("Signal", "Analog", "HandshakeCounter", "HandshakeHeader")
CHANNEL_ROLES = ("Wire", "Register", "Parameter", "String")
CHANNEL_RADIXES = ("Hexadecimal", "DecimalUnsigned", "DecimalSigned", "Binary",
                   "Ascii", "Enum")
CHANNEL_DEFAULTS = {"type": "Signal", "role": "Wire", "radix": "Hexadecimal"}

CHANNEL_SPEC = {
    "req": {"id": "uint", "name": "str", "width": "uint"},
    "opt": {"type": "str", "role": "str", "radix": "str", "enum_map": "map",
            "synthetic": "bool"},
}

SAMPLE_SPEC = {"req": {"ts": "uint", "value": "any"}, "opt": {}}

ANNOTATION_SPEC = {
    "req": {"ts_start": "uint", "ts_end": "uint", "channel_id": "uint",
            "kind": "str", "text": "str"},
    "opt": {"payload": "map", "color": "str", "is_error": "bool",
            "event_id": "str", "text_res": "map",
            "value": "str", "mask": "str", "op": "uint"},
}

#: ANNOTATIONS.annotations[].text_res (2.5.0): the same label at two
#: resolutions, so a reader picks the verbosity it can afford. `short` is the
#: inline waveform label; `long` reads on its own, out of the timeline (an HWTX
#: transcript line). `text` stays the REQUIRED string it always was, so a
#: consumer that does not know `text_res` is unaffected.
ANNOTATION_TEXT_RES_SPEC = {"req": {"long": "str", "short": "str"}, "opt": {}}

#: The longest `text_res.short` a producer may send, in characters. The prose
#: spec already gives the inline label this bound; `text` predates the check and
#: is not held to it, `text_res.short` is.
TEXT_SHORT_MAX = 32

#: EDGE_EVENTS (2.7.0, DS-P3.138): how far, in whole µs and EXCLUSIVE, any word
#: of a frame may lie from that frame's `anchor_ns // 1000`. A word's ts_us is
#: only the low 16 bits of a µs count, and the consumer places it in the
#: 65.536 ms window nearest the anchor, which is the right window only inside
#: half a window either way. A producer whose edges span more cuts them into
#: several EDGE_EVENTS frames, each with its own anchor. The checker cannot
#: verify this from the wire (a word too far away decodes to a wrong time, not
#: an invalid one), so the bound is stated here rather than inferred.
EDGE_ANCHOR_REACH_US = 32768

#: EDGE_EVENTS (2.8.0, DS-P3.124): the HELO entry that names an edge lane. Each
#: EDGE_EVENT word carries a 5-bit device_id in bits [31:27], and a consumer
#: names the lane of that device_id after the HELO channel whose id EQUALS it
#: (RouteWave edge_naming.hpp, LT-P3.257); with no such entry the lane reads
#: edge_<id>. That entry describes a lane whose data arrives as EDGE_EVENTS, so
#: it has width 0. A channel with width > 0 at an id the session's EDGE_EVENTS
#: words use is a SAMPLES lane squatting on an edge lane's name: the edge lane
#: either takes the sample lane's name or cannot be named at all. That is the
#: DS-P3.124 defect (level_pin[1]/[2] at ids 1/2, edges shown as edge_1/edge_2).
#: Producers keep SAMPLES and annotation channels at ids >= EDGE_LANE_IDS["max"]
#: + 1 when they stream edges; a session with no EDGE_EVENTS words is not
#: constrained.
EDGE_LANE_IDS = {
    "min": 0,
    "max": 31,
    "keyed_by": "EDGE_EVENTS words[] bits [31:27] (device_id)",
    "width": 0,
}

# ---------------------------------------------------------------------------
# The data-logger protocols a STATS.proto_stats map may be keyed by
# ---------------------------------------------------------------------------
# WHICH data loggers a build carries is a property of that build, not of the
# wire: it is single-sourced from the producer's tools/datalogger-builder/config/
# datalogger_config.yml, and it changes with a legitimate producer config change
# (DS-P2.121 / DS-D.43 made the CAN data logger opt-in, so a build from before
# it streams `can` and one from after does not). Revisions <= 2.2.0 pinned that
# per-build selection here as an exact key set, which made every such change a
# silent contract fork: deepskopion edited its copy to drop `can` without a
# revision bump, RouteWave kept the old set, both copies still claimed 2.2.0,
# and each side then rejected the other's honest stream (DS-P1.61).
#
# So the contract pins the VOCABULARY, not the selection. A key MUST be a
# protocol this revision registers — the names a consumer knows how to label,
# the same set PROTOCOL_EVENTS (plus `edge`, which carries raw transitions and
# has no event vocabulary) describes. A retired or unknown name is still a
# rejection: that is the DS-P3.120 phantom `i2s` row. Adding a protocol to this
# tuple IS a contract change (revision bump, re-vendor); opting a registered
# data logger in or out of a build is NOT.
#
# The half of DS-P3.120 this can no longer see — "a live data logger is missing
# from the map" — is a producer fact, so it is gated on the producer: every
# producer asserts its emitted key set equals its own build's active set
# (deepskopion: sw/stream/tests/test_proto_stats_registry.py). On the wire the
# contract still rejects the observable form of it: a key set that CHANGES
# within one session, which is a row appearing or vanishing under the viewer.

REGISTERED_PROTOCOLS = ("can", "edge", "i2c", "jtag", "spi", "uart")

#: Per-protocol event vocabulary, single-sourced upstream from the RTL datalogger
#: contracts (hw/rtl/datalogger/*/*_datalogger_protocol.yml -> the generated
#: sw/stream/protocol_codes.py). Pinned here so a consumer knows the closed set
#: of event names it can receive; the producer-side coherence test asserts these
#: lists still equal the generated tables.
PROTOCOL_EVENTS = {
    "uart": ("calibration_done", "calibration_timeout", "disabled", "enabled",
             "parity_error", "start_error", "stop_error"),
    "i2c": ("disabled", "enabled", "glitch_error", "repeated_start",
            "repeated_start_error", "start", "stop", "stop_error",
            "stretch_error"),
    "spi": ("cs_down", "cs_down_new_pol_h", "cs_down_new_pol_l", "cs_up",
            "cs_up_incomplete", "start", "stop", "timeout"),
    "jtag": ("disabled", "dr_shift_begin", "dr_shift_end", "enabled",
             "ir_shift_begin", "ir_shift_end", "overflow_error", "tap_reset"),
    "can": ("ack_error", "ack_observed", "crc_error", "disabled", "enabled",
            "form_error", "frame_begin", "frame_end", "overflow_error",
            "overload_marker", "stuff_count_error", "stuff_error"),
}

# ---------------------------------------------------------------------------
# Annotation payload role keys — the cross-repo field contract
# ---------------------------------------------------------------------------
# An annotation's `payload` map carries the decoder's role-tagged fields. The
# KEYS are the contract: the consumer builds per-protocol packet-table columns
# from them, so renaming one silently empties a column rather than failing.
# Registered here for every protocol the golden target decodes. A kind that is
# NOT registered is legal and unconstrained (COMPAT["unknown_kind"]).

def _payload(req=None, opt=None):
    return {"req": dict(req or {}), "opt": dict(opt or {})}


ANNOTATION_PAYLOAD = {
    # UART
    "uart_byte": _payload({"byte": "uint"}, {"ascii": "str", "nine_bit": "bool"}),
    "uart_config": _payload({"config": "uint"}),
    # I2C
    "i2c_byte": _payload({"data": "uint", "ack": "bool"}),
    # SPI
    "spi_mosi": _payload({"mosi": "uint"}),
    "spi_miso": _payload({"miso": "uint"}),
    "spi_event": _payload({"event": "str"}),
    # JTAG
    "jtag_tdi": _payload({"tdi": "uint"}),
    "jtag_tdo": _payload({"tdo": "uint"}),
    "jtag_tap_state": _payload({"tap_state": "uint", "state": "str"}),
    "jtag_ir_begin": _payload({"event": "str"}),
    "jtag_ir_end": _payload({"event": "str"}, {"bits": "uint"}),
    "jtag_dr_begin": _payload({"event": "str"}),
    "jtag_dr_end": _payload({"event": "str"}, {"bits": "uint"}),
    "jtag_tap_reset": _payload({"event": "str"}),
    "jtag_enabled": _payload({"event": "str"}),
    "jtag_disabled": _payload({"event": "str"}),
    # CAN — the golden target's headline bus. Not in the prose spec's payload
    # table; pinned here from the reference producer (sw/stream/decoders.py).
    "can_id": _payload({"id_byte": "uint"}),
    "can_data": _payload({"data": "uint"}),
    "can_crc": _payload({"crc_byte": "uint"}),
    "can_control": _payload({"ide": "bool", "rtr": "bool", "dlc": "uint"}),
    # Well-known cross-protocol kinds
    "error_run": _payload({"run_of": "str", "count": "uint", "ongoing": "bool"}),
    "loss": _payload({"loss_stage": "str", "lost_count": "uint", "domain": "str",
                      "in_band": "bool"},
                     {"provenance_code": "uint", "datalogger_id": "uint"}),
    "eye_point": _payload({"freq_sel": "uint", "phase_sel": "uint",
                           "freq_hz": "uint", "err_count": "uint",
                           "bit_count": "uint", "locked": "bool"},
                          {"ber": "num", "phase_ps": "int", "phase_ui": "num"}),
    "eye_closed": _payload({"freq_sel": "uint", "phase_sel": "uint",
                            "freq_hz": "uint", "bit_count": "uint"},
                           {"err_count": "uint", "locked": "bool",
                            "phase_ps": "int", "phase_ui": "num"}),
    "eye_summary": _payload({"max_freq_hz": "uint", "ber_threshold": "num",
                             "eye_margin_ui": "num", "curve": "list"}),
    # EDGE (2.6.0, DS-P3.117): the FPGA-authoritative max frequency of one captured
    # line, from the in-band EVT_EDGE_MAXFREQ frame. min_interval_ticks is the
    # smallest validated edge-to-edge interval (a half-period) in edge-clock
    # ticks; freq_hz = clk / (2 * interval). freq_hz is null for the all-ones
    # "no edge yet / re-armed" sentinel: the host re-armed the latch, and a
    # consumer must clear the value it shows rather than keep a stale maximum.
    "edge_maxfreq": _payload({"channel": "uint", "min_interval_ticks": "uint",
                              "freq_hz": "num?"}),
}

#: One entry of eye_summary.curve, one per scanned frequency. Declared so the
#: time-unit walk sees its keys (2.4.1). Not yet enforced on received payloads.
EYE_CURVE_POINT_SPEC = {"req": {"freq_hz": "uint", "eye_width_ps": "uint",
                                "eye_width_ui": "num", "passed": "bool",
                                "partial": "bool"},
                        "opt": {}}

# Event-bearing kinds are generated, so the kind space and the event vocabulary
# cannot drift apart: uart status kinds carry {"status"}, i2c/can event kinds
# carry {"event"}. JTAG and SPI name their event kinds differently and are
# registered by hand above.
for _name in PROTOCOL_EVENTS["uart"]:
    ANNOTATION_PAYLOAD.setdefault(f"uart_{_name}", _payload({"status": "str"}))
for _proto in ("i2c", "can"):
    for _name in PROTOCOL_EVENTS[_proto]:
        ANNOTATION_PAYLOAD.setdefault(f"{_proto}_{_name}",
                                      _payload({"event": "str"}))

#: Loss-provenance stages — the `system_ids.yml` error_events keys. A stage
#: outside this set means the consumer cannot bucket the loss, so the loss is
#: reported as an unattributable number, which is the state the taxonomy exists
#: to end.
LOSS_STAGES = ("internal_fifo_overflow", "arbiter_drop", "ring_full",
               "decode_error")

#: DIGEST verdict statuses.
VERDICT_STATUSES = ("OK", "WARN", "CRITICAL")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

class Failure:
    """One named reason a session does not conform."""

    __slots__ = ("field", "reason")

    def __init__(self, field, reason):
        self.field = field
        self.reason = reason

    def __repr__(self):
        return f"Failure({self.field!r}, {self.reason!r})"

    def __str__(self):
        return f"{self.field}: {self.reason}"


class Result:
    """The judgement on one session or stream."""

    def __init__(self, failures, skipped_tags=()):
        self.failures = list(failures)
        #: Tags that were skipped as unknown. Not failures — evidence that the
        #: forward-compatibility rule was exercised rather than merely stated.
        self.skipped_tags = list(skipped_tags)

    @property
    def verdict(self):
        return "REJECT" if self.failures else "ACCEPT"

    @property
    def accepted(self):
        return not self.failures

    @property
    def fields(self):
        return [f.field for f in self.failures]

    def __str__(self):
        if self.accepted:
            return "ACCEPT"
        return "REJECT\n" + "\n".join(f"  - {f}" for f in self.failures)


# ---------------------------------------------------------------------------
# Type checking
# ---------------------------------------------------------------------------

def _is_uint(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _type_ok(value, kind):
    if kind.endswith("?"):
        if value is None:
            return True
        kind = kind[:-1]
    if kind == "any":
        return True
    if kind == "uint":
        return _is_uint(value)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "float":
        return isinstance(value, float)
    if kind == "num":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "str":
        return isinstance(value, str)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "bin":
        return isinstance(value, (bytes, bytearray))
    if kind == "map":
        return isinstance(value, dict)
    if kind == "list":
        return isinstance(value, list)
    raise KeyError(f"unknown type in spec: {kind}")


def _check_map(path, value, spec, out):
    """Required keys must be present and typed; optional keys typed if present;
    every other key is ignored, by policy (COMPAT["unknown_key"])."""
    if not isinstance(value, dict):
        out.append(Failure(path, f"expected map, got {type(value).__name__}"))
        return False
    ok = True
    for key, kind in spec["req"].items():
        if key not in value:
            out.append(Failure(f"{path}.{key}", "is missing (required)"))
            ok = False
        elif not _type_ok(value[key], kind):
            out.append(Failure(f"{path}.{key}",
                               f"expected {kind}, got "
                               f"{type(value[key]).__name__}"))
            ok = False
    for key, kind in spec["opt"].items():
        if key in value and not _type_ok(value[key], kind):
            out.append(Failure(f"{path}.{key}",
                               f"expected {kind}, got "
                               f"{type(value[key]).__name__}"))
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Byte layer
# ---------------------------------------------------------------------------

def decode_stream(data):
    """Decode one direction's bytes into ``[(tag, payload), ...]``.

    Returns ``(frames, failures)``. A stream that does not decode is a
    FRAME_DECODE failure naming the byte offset, never an exception: a consumer
    facing a corrupt producer has to report, not crash.
    """
    out = []
    frames = []
    unpacker = msgpack.Unpacker(raw=False, strict_map_key=False,
                                use_list=True)
    unpacker.feed(bytes(data))
    index = 0
    while True:
        try:
            obj = unpacker.unpack()
        except msgpack.OutOfData:
            break
        except (msgpack.UnpackException, ValueError) as exc:
            out.append(Failure(f"frame[{index}]",
                               f"FRAME_DECODE: {exc}"))
            return frames, out
        if not isinstance(obj, list) or len(obj) != 2:
            out.append(Failure(f"frame[{index}]",
                               "FRAME_DECODE: a frame is a 2-element array "
                               f"[tag, payload], got "
                               f"{type(obj).__name__} of "
                               f"{len(obj) if isinstance(obj, list) else 'n/a'}"))
            return frames, out
        tag, payload = obj
        if not _is_uint(tag):
            out.append(Failure(f"frame[{index}].tag",
                               f"FRAME_DECODE: tag must be a uint, got "
                               f"{type(tag).__name__}"))
            return frames, out
        frames.append((tag, payload))
        index += 1
    tail = unpacker.tell()
    if tail != len(data):
        out.append(Failure(f"frame[{index}]",
                           f"FRAME_DECODE: {len(data) - tail} trailing byte(s) "
                           f"do not form a frame (truncated stream)"))
    return frames, out


# ---------------------------------------------------------------------------
# Session semantics — the checks that make the seam real
# ---------------------------------------------------------------------------

class _Session:
    """Running state while walking a session's frames in wire order."""

    def __init__(self):
        self.helo = None
        self.channel_ids = set()
        self.helo_ok = False
        self.closed_by = None
        self.sync_count = 0
        self.seq = {}
        self.last_sample_ts = -1
        self.annotation_count = 0
        self.edge_words_without_anchor = []
        #: HELO path and its channels as (index, id, width), for the edge-lane
        #: id check at session end (2.8.0).
        self.helo_path = None
        self.helo_channel_widths = []
        #: device_ids the session's EDGE_EVENTS words carry, first path each.
        #: Only ids in edge_lane_watch are recorded (2.8.1).
        self.edge_device_ids = {}
        #: HELO channel ids <= EDGE_LANE_IDS["max"] with a non-zero width: the
        #: only ids the edge-lane rule can fire on. Empty for a conforming
        #: producer, so its edge words are not scanned word by word (2.8.1:
        #: the per-word scan of 2.8.0 cost 824 ns per edge in the daemon's
        #: live guard on a Cortex-A9).
        self.edge_lane_watch = set()
        self.proto_keys = None
        #: event_id -> the annotation it names, as (channel_id, ts_start,
        #: ts_end, kind). An id may be sent again (a late joiner's keyframe
        #: repeats the last annotation per lane) but only for the same event.
        self.event_ids = {}


def _check_frame(path, tag, payload, direction, st, out):
    entry = TAGS.get(tag)
    if entry is None:
        # COMPAT["unknown_tag"] — not a failure. The caller records it.
        return False
    name, allowed_dir = entry
    if allowed_dir != "both" and direction != allowed_dir:
        out.append(Failure(f"{path}.tag",
                           f"{name} is a {allowed_dir} frame but arrived on the "
                           f"{direction} stream"))
    if st.closed_by is not None:
        out.append(Failure(path,
                           f"{name} arrives after {st.closed_by}; the session is "
                           f"closed and no further frame may be sent"))
    if not _check_map(f"{path}[{name}]", payload, FRAME_SPEC[name], out):
        return True

    handler = _HANDLERS.get(name)
    if handler is not None:
        handler(f"{path}[{name}]", payload, st, out)
    return True


def _h_helo(path, p, st, out):
    if st.helo is not None:
        out.append(Failure(path, "a session carries exactly one HELO"))
        return
    st.helo = p
    st.helo_path = path
    if p.get("ver") != PROTOCOL_VERSION:
        out.append(Failure(f"{path}.ver",
                           f"is {p.get('ver')!r}, not {PROTOCOL_VERSION!r} — "
                           f"the one fatal incompatibility "
                           f"(ERR VERSION_MISMATCH, close)"))
    total = 0
    for i, ch in enumerate(p.get("channels") or []):
        cpath = f"{path}.channels[{i}]"
        if not _check_map(cpath, ch, CHANNEL_SPEC, out):
            continue
        if ch["id"] in st.channel_ids:
            out.append(Failure(f"{cpath}.id",
                               f"channel id {ch['id']} is already used; ids "
                               f"MUST be unique within a session"))
        st.channel_ids.add(ch["id"])
        st.helo_channel_widths.append((i, ch["id"], ch["width"]))
        if ch["id"] <= EDGE_LANE_IDS["max"] and ch["width"] != EDGE_LANE_IDS["width"]:
            st.edge_lane_watch.add(ch["id"])
        total += ch["width"]
        for key, allowed in (("type", CHANNEL_TYPES), ("role", CHANNEL_ROLES),
                             ("radix", CHANNEL_RADIXES)):
            val = ch.get(key)
            if val is not None and val not in allowed:
                out.append(Failure(
                    f"{cpath}.{key}",
                    f"{val!r} is not a registered {key}; a consumer silently "
                    f"falls back to {CHANNEL_DEFAULTS[key]!r}, so the producer's "
                    f"intent is dropped without either side noticing "
                    f"(one of: {', '.join(allowed)})"))
    declared = p.get("channel_total_bits")
    if _is_uint(declared) and declared != total:
        out.append(Failure(f"{path}.channel_total_bits",
                           f"declares {declared} but the channel widths sum to "
                           f"{total}; the consumer slices SAMPLES by this"))


def _h_helo_ok(path, p, st, out):
    st.helo_ok = True


def _seq_check(path, p, st, out, stream):
    seq = p.get("seq")
    if not _is_uint(seq):
        return
    last = st.seq.get(stream)
    if last is not None and seq <= last:
        out.append(Failure(f"{path}.seq",
                           f"is {seq} after {last}; {stream} seq is monotonically "
                           f"increasing per session, so a consumer can detect "
                           f"drops by its gaps"))
    st.seq[stream] = seq


def _h_samples(path, p, st, out):
    _require_handshake(path, "SAMPLES", st, out)
    _seq_check(path, p, st, out, "SAMPLES")
    for i, s in enumerate(p.get("samples") or []):
        spath = f"{path}.samples[{i}]"
        if not _check_map(spath, s, SAMPLE_SPEC, out):
            continue
        if s["ts"] < st.last_sample_ts:
            out.append(Failure(f"{spath}.ts",
                               f"is {s['ts']} after {st.last_sample_ts}; "
                               f"SAMPLES timestamps are monotonic ns since "
                               f"HELO"))
        st.last_sample_ts = max(st.last_sample_ts, s["ts"])


def _h_annotations(path, p, st, out):
    _require_handshake(path, "ANNOTATIONS", st, out)
    _seq_check(path, p, st, out, "ANNOTATIONS")
    for i, a in enumerate(p.get("annotations") or []):
        apath = f"{path}.annotations[{i}]"
        if not _check_map(apath, a, ANNOTATION_SPEC, out):
            continue
        st.annotation_count += 1
        if a["ts_end"] < a["ts_start"]:
            out.append(Failure(f"{apath}.ts_end",
                               f"is {a['ts_end']}, before ts_start "
                               f"{a['ts_start']}; an annotation's span cannot "
                               f"run backwards"))
        if st.helo is not None and a["channel_id"] not in st.channel_ids:
            out.append(Failure(f"{apath}.channel_id",
                               f"{a['channel_id']} is not one of the channels "
                               f"HELO declared; the consumer has no row to "
                               f"anchor it to (ERR ANNOTATION_REF)"))
        _check_annotation_payload(apath, a, out)
        _check_event_id(apath, a, st, out)
        _check_text_res(apath, a, out)


def _check_event_id(apath, a, st, out):
    """An event_id is the handle a verdict points at (HWTX `event_refs`). It
    must name ONE event for the whole session. The same id again is allowed
    only for the same annotation: a keyframe resends an event, it does not make
    a new one. Two different annotations under one id make every reference to it
    ambiguous, and nothing downstream can tell which one was meant."""
    eid = a.get("event_id")
    if not isinstance(eid, str):
        return  # absent (optional), or a type failure _check_map already named
    if not eid:
        out.append(Failure(f"{apath}.event_id",
                           "is empty; an event_id is a handle, and an empty "
                           "string names nothing"))
        return
    ident = (a["channel_id"], a["ts_start"], a["ts_end"], a["kind"])
    prev = st.event_ids.get(eid)
    if prev is None:
        st.event_ids[eid] = ident
    elif prev != ident:
        out.append(Failure(f"{apath}.event_id",
                           f"{eid!r} already names a different annotation "
                           f"(channel_id, ts_start, ts_end, kind) = {prev}, "
                           f"not {ident}; an event_id names one event for the "
                           f"whole session"))


def _check_text_res(apath, a, out):
    res = a.get("text_res")
    if not isinstance(res, dict):
        return  # absent (optional), or a type failure _check_map already named
    if not _check_map(f"{apath}.text_res", res, ANNOTATION_TEXT_RES_SPEC, out):
        return
    for key in ("long", "short"):
        if not res[key]:
            out.append(Failure(f"{apath}.text_res.{key}",
                               "is empty; a resolution with no text shows "
                               "the reader nothing"))
    if len(res["short"]) > TEXT_SHORT_MAX:
        out.append(Failure(f"{apath}.text_res.short",
                           f"is {len(res['short'])} characters; the inline "
                           f"label is at most {TEXT_SHORT_MAX}, which is the "
                           f"whole reason a short resolution exists"))


def _check_annotation_payload(apath, a, out):
    kind = a["kind"]
    spec = ANNOTATION_PAYLOAD.get(kind)
    if spec is None:
        return  # COMPAT["unknown_kind"]
    payload = a.get("payload")
    if payload is None:
        out.append(Failure(f"{apath}.payload",
                           f"kind {kind!r} is a registered kind and MUST carry "
                           f"its role keys "
                           f"({', '.join(sorted(spec['req'])) or 'none'})"))
        return
    _check_map(f"{apath}.payload", payload, spec, out)
    if kind == "loss" and isinstance(payload, dict):
        stage = payload.get("loss_stage")
        if isinstance(stage, str) and stage not in LOSS_STAGES:
            out.append(Failure(f"{apath}.payload.loss_stage",
                               f"{stage!r} is not a known loss stage; the "
                               f"consumer buckets loss provenance by this, so "
                               f"an unknown stage is an unattributable loss "
                               f"(one of: {', '.join(LOSS_STAGES)})"))


def _h_edge_events(path, p, st, out):
    _require_handshake(path, "EDGE_EVENTS", st, out)
    _seq_check(path, p, st, out, "EDGE_EVENTS")
    words = p.get("words") or []
    for i, w in enumerate(words):
        if not _is_uint(w) or w > 0xFFFFFFFF:
            out.append(Failure(f"{path}.words[{i}]",
                               "EDGE_EVENT words are uint32"))
            break
    if st.edge_lane_watch:
        for i, w in enumerate(words):
            if _is_uint(w) and (w >> 27) & 0x1F in st.edge_lane_watch:
                st.edge_device_ids.setdefault((w >> 27) & 0x1F, f"{path}.words[{i}]")
    if words and not p.get("anchor_ns"):
        # Recorded because whether it is fatal depends on the session: a word's
        # ts_us is only the LOW 16 BITS of a microsecond count, so without an
        # absolute anchor the edges are placeable only inside a 65.536 ms
        # window. Mixed with absolute annotation timestamps that is a silent
        # divergence of the board's whole uptime (measured at 10 days,
        # LT-P2.105) — the edge lanes hold thousands of transitions and draw
        # nothing in the live viewport.
        st.edge_words_without_anchor.append(path)


def _h_sync(path, p, st, out):
    _require_handshake(path, "SYNC", st, out)
    st.sync_count += 1
    if st.sync_count > 1:
        out.append(Failure(path,
                           "a producer emits exactly one SYNC per consumer — "
                           "the single catch-up to live boundary"))


def _h_stats(path, p, st, out):
    _seq_check(path, p, st, out, "STATS")
    proto = p.get("proto_stats")
    if isinstance(proto, dict):
        keys = frozenset(proto.keys())
        unregistered = sorted(k for k in keys if k not in REGISTERED_PROTOCOLS)
        if not keys:
            out.append(Failure(
                f"{path}.proto_stats",
                "is empty — a producer streaming STATS carries at least one "
                "data logger, so an empty map is a producer that lost its "
                "registry, and the consumer renders no per-protocol rows at all"))
        elif unregistered:
            out.append(Failure(
                f"{path}.proto_stats",
                f"key(s) {', '.join(unregistered)} are not registered data-logger "
                f"protocols (one of: {', '.join(REGISTERED_PROTOCOLS)}) — the "
                f"consumer renders one row per key, so an unregistered key is a "
                f"phantom row (the DS-P3.120 retired `i2s`)"))
        elif st.proto_keys is not None and keys != st.proto_keys:
            missing = sorted(st.proto_keys - keys)
            extra = sorted(keys - st.proto_keys)
            out.append(Failure(
                f"{path}.proto_stats",
                "key set changed within the session"
                + (f"; dropped {', '.join(missing)}" if missing else "")
                + (f"; added {', '.join(extra)}" if extra else "")
                + " — a build's data-logger set is fixed for its lifetime, so a "
                  "row vanishing mid-session hides a live data logger and one "
                  "appearing is a phantom"))
        if keys and st.proto_keys is None:
            st.proto_keys = keys
        for name, counters in proto.items():
            cpath = f"{path}.proto_stats.{name}"
            _check_map(cpath, counters, STATS_PROTO_COUNTERS_SPEC, out)
    alarm = p.get("ring_alarm")
    if isinstance(alarm, dict):
        _check_map(f"{path}.ring_alarm", alarm, STATS_RING_ALARM_SPEC, out)


def _h_digest(path, p, st, out):
    _seq_check(path, p, st, out, "DIGEST")
    if p.get("schema") != DIGEST_SCHEMA:
        out.append(Failure(f"{path}.schema",
                           f"is {p.get('schema')!r}; this contract revision "
                           f"implements {DIGEST_SCHEMA!r} (a consumer that does "
                           f"not implement a body revision skips the frame)"))
    for name, stat in (p.get("protocols") or {}).items():
        _check_map(f"{path}.protocols.{name}", stat, DIGEST_PROTOCOL_STAT_SPEC, out)
    for i, r in enumerate(p.get("recent") or []):
        _check_map(f"{path}.recent[{i}]", r, DIGEST_RECENT_SPEC, out)
    verdict = p.get("verdict")
    if isinstance(verdict, dict):
        _check_map(f"{path}.verdict", verdict, DIGEST_VERDICT_SPEC, out)
        if verdict.get("status") not in VERDICT_STATUSES:
            out.append(Failure(f"{path}.verdict.status",
                               f"is {verdict.get('status')!r}, not one of "
                               f"{'|'.join(VERDICT_STATUSES)} — the verdict is "
                               f"the machine-readable 'is the bus healthy?' "
                               f"answer, so it has no third state"))
        for i, an in enumerate(verdict.get("anomalies") or []):
            _check_map(f"{path}.verdict.anomalies[{i}]", an, DIGEST_ANOMALY_SPEC, out)


def _h_eof(path, p, st, out):
    st.closed_by = "EOF"


def _h_err(path, p, st, out):
    st.closed_by = "ERR"


def _require_handshake(path, name, st, out):
    if st.helo is None:
        out.append(Failure(path,
                           f"{name} arrives before HELO; a consumer cannot "
                           f"place data it has no channel descriptors for"))
    elif not st.helo_ok:
        out.append(Failure(path,
                           f"{name} arrives before HELO_OK; the handshake is "
                           f"the one required consumer-to-producer message "
                           f"before the stream flows"))


_HANDLERS = {
    "HELO": _h_helo,
    "HELO_OK": _h_helo_ok,
    "SAMPLES": _h_samples,
    "ANNOTATIONS": _h_annotations,
    "EDGE_EVENTS": _h_edge_events,
    "SYNC": _h_sync,
    "STATS": _h_stats,
    "DIGEST": _h_digest,
    "EOF": _h_eof,
    "ERR": _h_err,
}


#: The revision that introduced the edge-lane rule below. A session recorded by
#: an older producer is judged without it (see check_session's ``revision``).
EDGE_LANE_RULE_SINCE = "2.8.0"


def _revision_tuple(revision):
    try:
        parts = tuple(int(x) for x in str(revision).split("."))
    except ValueError:
        parts = ()
    if len(parts) != 3:
        raise ValueError(f"revision {revision!r} is not MAJOR.MINOR.PATCH")
    return parts


def _applies(since, revision):
    return _revision_tuple(revision) >= _revision_tuple(since)


def _check_edge_lane_ids(st, out):
    """2.8.0 (DS-P3.124): a HELO channel at an id the session's EDGE_EVENTS
    words carry as device_id names that edge lane, so it must have width 0."""
    for i, cid, width in st.helo_channel_widths:
        word_path = st.edge_device_ids.get(cid)
        if word_path is None or width == EDGE_LANE_IDS["width"]:
            continue
        out.append(Failure(
            f"{st.helo_path}.channels[{i}].width",
            f"channel id {cid} has width {width}, but {word_path} carries "
            f"device_id {cid}; a consumer names that edge lane after this "
            f"channel, so it must be a width-0 edge-lane entry. Keep SAMPLES "
            f"and annotation channels above id {EDGE_LANE_IDS['max']} "
            f"(edge device_ids are 5 bits)"))


def check_session(frames, revision=CONTRACT_REVISION):
    """Judge an ordered, BIDIRECTIONAL session.

    `frames` is a list of ``(direction, tag, payload)`` in wire order, where
    direction is "p2c" or "c2p". This is the full contract: handshake ordering
    and direction can only be judged with both halves in view.

    `revision` is the contract revision the session was produced under. The
    default, the current revision, is right for a live producer. A session
    recorded earlier (committed evidence) is judged by the rules of its own
    revision: a producer obligation added later does not apply to it (today:
    the 2.8.0 edge-lane rule, EDGE_LANE_RULE_SINCE).
    """
    out = []
    skipped = []
    st = _Session()
    for i, (direction, tag, payload) in enumerate(frames):
        known = _check_frame(f"frame[{i}]", tag, payload, direction, st, out)
        if not known:
            skipped.append(tag)
    if st.helo is None:
        out.append(Failure("session",
                           "carries no HELO; the first frame a producer sends "
                           "opens the session"))
    if _applies(EDGE_LANE_RULE_SINCE, revision):
        _check_edge_lane_ids(st, out)
    if st.edge_words_without_anchor and st.annotation_count:
        for path in st.edge_words_without_anchor:
            out.append(Failure(
                f"{path}.anchor_ns",
                "EDGE_EVENT words carry only the LOW 16 BITS of a microsecond "
                "timestamp (a 65.536 ms window), so a session that also carries "
                "absolute-timestamped ANNOTATIONS MUST supply anchor_ns; "
                "without it the two layers land a whole board uptime apart"))
    return Result(out, skipped)


def check_stream(data, direction, revision=CONTRACT_REVISION):
    """Judge ONE direction's bytes — the check a consumer can run alone.

    A subset of check_session: whatever needs the other half of the
    conversation (HELO_OK ordering, frame direction) cannot be judged here, so
    a consumer's ACCEPT on a stream is necessary, not sufficient.
    """
    frames, failures = decode_stream(data)
    if failures:
        return Result(failures)
    st = _Session()
    if direction == "p2c":
        st.helo_ok = True  # the other half is not in this file
    out = []
    skipped = []
    for i, (tag, payload) in enumerate(frames):
        if not _check_frame(f"frame[{i}]", tag, payload, direction, st, out):
            skipped.append(tag)
    if direction == "p2c" and st.helo is None:
        out.append(Failure("session", "carries no HELO"))
    if _applies(EDGE_LANE_RULE_SINCE, revision):
        _check_edge_lane_ids(st, out)
    return Result(out, skipped)


# ---------------------------------------------------------------------------
# Fixtures: JSON is the source, .mp is the artifact
# ---------------------------------------------------------------------------
# A fixture is written as readable JSON and COMPILED to the exact bytes both
# repos consume. Committing only the bytes would make the fixtures unreviewable;
# committing only the JSON would make them unusable from C++. Committing both
# risks drift, so `emit --check` regenerates and requires byte equality.
#
# JSON encodings for values MessagePack has and JSON does not:
#   {"__bin__": "<hex>"}  -> msgpack bin (EDGE_EVENTS.batch)

DIRECTIONS = ("p2c", "c2p")


def _from_json(value):
    if isinstance(value, dict):
        if set(value) == {"__bin__"}:
            return bytes.fromhex(value["__bin__"])
        return {k: _from_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_json(v) for v in value]
    return value


def load_fixture(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def fixture_frames(fixture):
    """The fixture's frames as ``(direction, tag, payload)`` in wire order."""
    frames = []
    for frame in fixture["frames"]:
        if "raw" in frame:
            continue
        frames.append((frame["dir"], frame["tag"], _from_json(frame["payload"])))
    return frames


def encode_fixture(fixture):
    """Compile a fixture to ``{direction: bytes}``.

    A frame may be given as ``{"raw": <value>}`` to put something on the wire
    that is not a well-formed frame at all — that is how the FRAME_DECODE
    fixtures are expressed. ``corrupt.truncate_tail_bytes`` chops the finished
    stream, which is what a dropped connection actually looks like.
    """
    streams = {d: bytearray() for d in DIRECTIONS}
    for frame in fixture["frames"]:
        direction = frame["dir"]
        if "raw" in frame:
            obj = _from_json(frame["raw"])
        else:
            obj = [frame["tag"], _from_json(frame["payload"])]
        streams[direction] += msgpack.packb(obj, use_bin_type=True)
    corrupt = fixture.get("corrupt") or {}
    chop = corrupt.get("truncate_tail_bytes")
    if chop:
        target = corrupt.get("stream", "p2c")
        streams[target] = streams[target][:-chop]
    return {d: bytes(b) for d, b in streams.items()}


def check_fixture(fixture):
    """Judge a fixture THROUGH the byte layer.

    The fixture is compiled to bytes, decoded back, and the decoded frames are
    re-interleaved in the fixture's declared order before the session checks
    run. Going through the bytes is the point: a fixture that cannot survive
    encode/decode is not evidence about a wire protocol.
    """
    streams = encode_fixture(fixture)
    decoded = {}
    failures = []
    for direction in DIRECTIONS:
        frames, errs = decode_stream(streams[direction])
        decoded[direction] = frames
        failures.extend(Failure(f"{direction}.{f.field}", f.reason)
                        for f in errs)
    if failures:
        return Result(failures)
    queues = {d: list(decoded[d]) for d in DIRECTIONS}
    ordered = []
    for frame in fixture["frames"]:
        direction = frame["dir"]
        if not queues[direction]:
            continue
        tag, payload = queues[direction].pop(0)
        ordered.append((direction, tag, payload))
    return check_session(ordered)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def build_manifest():
    """Compile every fixture and describe it. The manifest is what a consumer
    in another language reads: which bytes to feed in, and what verdict it must
    reach — including, for a rejection, the field it must name. A rejection that
    fires for the wrong reason is a test that passes by accident."""
    entries = []
    for source in sorted(FIXTURE_DIR.glob("*.json")):
        fixture = load_fixture(source)
        streams = encode_fixture(fixture)
        result = check_fixture(fixture)
        entry = {
            "name": fixture["name"],
            "source": f"fixtures/{source.name}",
            "expect": fixture["expect"],
            "why": fixture["why"],
            "streams": {},
        }
        for direction in DIRECTIONS:
            data = streams[direction]
            if not data:
                continue
            entry["streams"][direction] = {
                "file": f"fixtures/{fixture['name']}.{direction}.mp",
                "bytes": len(data),
                "sha256": _sha256(data),
            }
        if fixture["expect"] == "reject":
            entry["expect_fields"] = fixture["expect_fields"]
            entry["observed_fields"] = result.fields
        entries.append(entry)
    return {
        "contract": CONTRACT_ID,
        "contract_revision": CONTRACT_REVISION,
        "protocol_version": PROTOCOL_VERSION,
        "note": ("Generated by wave_stream_contract.py emit. Do not hand-edit: "
                 "fixtures/*.json is the source, *.mp is the compiled wire "
                 "form, and `emit --check` proves they agree."),
        "fixtures": entries,
    }


def emit(check_only=False):
    """Regenerate the .mp streams, contract.json and manifest.json.

    Returns a list of paths whose committed content does not match what this
    module would generate — empty means the tree is coherent.
    """
    drift = []

    def _write(path, data):
        existing = path.read_bytes() if path.exists() else None
        if existing != data:
            drift.append(str(path.relative_to(HERE)))
            if not check_only:
                path.write_bytes(data)

    for source in sorted(FIXTURE_DIR.glob("*.json")):
        fixture = load_fixture(source)
        for direction, data in encode_fixture(fixture).items():
            path = FIXTURE_DIR / f"{fixture['name']}.{direction}.mp"
            if not data:
                continue
            _write(path, data)
    _write(CONTRACT_JSON,
           (json.dumps(contract_json(), indent=2) + "\n").encode("utf-8"))
    _write(MANIFEST,
           (json.dumps(build_manifest(), indent=2) + "\n").encode("utf-8"))
    return drift


def verify():
    """Run every fixture against the manifest. Returns (ok, lines)."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ok = True
    lines = []
    for entry in manifest["fixtures"]:
        fixture = load_fixture(HERE / entry["source"])
        result = check_fixture(fixture)
        expect = entry["expect"]
        got = "accept" if result.accepted else "reject"
        if got != expect:
            ok = False
            lines.append(f"✗ {entry['name']}: expected {expect}, got {got}")
            for failure in result.failures:
                lines.append(f"    {failure}")
            continue
        if expect == "reject":
            wanted = set(entry["expect_fields"])
            if not wanted <= set(result.fields):
                ok = False
                lines.append(f"✗ {entry['name']}: rejected, but not for the "
                             f"recorded reason")
                lines.append(f"    expected field(s): "
                             f"{', '.join(sorted(wanted))}")
                lines.append(f"    named field(s):    "
                             f"{', '.join(result.fields) or '(none)'}")
                continue
        for direction, meta in entry["streams"].items():
            path = HERE / meta["file"]
            data = path.read_bytes()
            if _sha256(data) != meta["sha256"]:
                ok = False
                lines.append(f"✗ {entry['name']}: {meta['file']} does not match "
                             f"its manifest digest")
        lines.append(f"✓ {entry['name']}: {expect}")
    return ok, lines


# ---------------------------------------------------------------------------
# contract.json — the language-neutral projection
# ---------------------------------------------------------------------------

def contract_json():
    """Project this module into data, so a non-Python implementation can read
    the rules instead of re-deriving them from a header. Generated, never
    hand-written: a projection that can drift is a second source of truth."""
    return {
        "contract": CONTRACT_ID,
        "contract_revision": CONTRACT_REVISION,
        "protocol_version": PROTOCOL_VERSION,
        "digest_schema": DIGEST_SCHEMA,
        "compatibility_policy": COMPAT,
        "tags": {str(tag): {"name": name, "direction": direction,
                            "payload": FRAME_SPEC[name]}
                 for tag, (name, direction) in sorted(TAGS.items())},
        "err_codes": list(ERR_CODES),
        "channel": {
            "spec": CHANNEL_SPEC,
            "types": list(CHANNEL_TYPES),
            "roles": list(CHANNEL_ROLES),
            "radixes": list(CHANNEL_RADIXES),
            "defaults": CHANNEL_DEFAULTS,
        },
        "sample": SAMPLE_SPEC,
        "annotation": ANNOTATION_SPEC,
        "annotation_text_res": ANNOTATION_TEXT_RES_SPEC,
        "text_short_max": TEXT_SHORT_MAX,
        "edge_anchor_reach_us": EDGE_ANCHOR_REACH_US,
        "edge_lane_ids": EDGE_LANE_IDS,
        "annotation_payload": {k: ANNOTATION_PAYLOAD[k]
                               for k in sorted(ANNOTATION_PAYLOAD)},
        "registered_protocols": list(REGISTERED_PROTOCOLS),
        "protocol_events": {k: list(v) for k, v in
                            sorted(PROTOCOL_EVENTS.items())},
        "loss_stages": list(LOSS_STAGES),
        "verdict_statuses": list(VERDICT_STATUSES),
        "time_units": {k: TIME_UNITS[k] for k in sorted(TIME_UNITS)},
    }


# ---------------------------------------------------------------------------
# Time units (2.4.0, LT-P2.251)
# ---------------------------------------------------------------------------

#: The unit of every time-like key on the wire. A time with no stated unit is
#: how two sides drift without a single field changing shape: RouteWave moved to
#: ps internally (ADR-008) while every time on this wire stayed whole ns.
#: SAMPLES, ANNOTATIONS and DIGEST times are whole ns, so two samples closer
#: than 1 ns share a ts on the wire; EDGE_EVENTS place in edge-clock ticks and
#: can be placed below 1 ns by a consumer. (A batch entry's ts_delta is inside
#: the binary `batch` and counts edge-clock ticks too.)
TIME_UNITS = {
    "SAMPLES.samples[].ts": "ns since HELO",
    "ANNOTATIONS.annotations[].ts_start": "ns since HELO",
    "ANNOTATIONS.annotations[].ts_end": "ns since HELO",
    "DIGEST.recent[].ts": "ns since HELO",
    "DIGEST.window_s": "s",
    "EDGE_EVENTS.anchor_ns": "ns (the producer's absolute epoch)",
    "EDGE_EVENTS.anchor_ticks": "ticks of the edge clock",
    "EDGE_EVENTS.clk_mhz": "MHz",
    "HELO.sample_clk_hz": "Hz (the edge clock, when given)",
    "HELO_OK.max_rate_hint_hz": "Hz",
    "RATE_HINT.max_rate_hz": "Hz",
    "STATS.clk_ddr_ticks": "ticks of the DDR clock (a counter)",
    "ANNOTATIONS.payload[eye_point].freq_hz": "Hz",
    "ANNOTATIONS.payload[eye_point].phase_ps": "ps offset from the nominal phase, not a position",
    "ANNOTATIONS.payload[eye_closed].freq_hz": "Hz",
    "ANNOTATIONS.payload[eye_closed].phase_ps": "ps offset from the nominal phase, not a position",
    "ANNOTATIONS.payload[eye_summary].max_freq_hz": "Hz",
    "ANNOTATIONS.payload[edge_maxfreq].min_interval_ticks": "ticks of the edge clock (a half-period, a duration)",
    "ANNOTATIONS.payload[edge_maxfreq].freq_hz": "Hz, null for the re-armed / no-edge sentinel",
    "ANNOTATIONS.payload[eye_summary].curve[].freq_hz": "Hz",
    "ANNOTATIONS.payload[eye_summary].curve[].eye_width_ps": "ps, the open-eye width (a duration, not a position)",
}

#: What makes a key time-like: its name. A new key named this way needs a row.
_TIME_LIKE = re.compile(r"(^ts$|^ts_|_ns$|_ps$|_us$|_ms$|_s$|_ticks$|_hz$|_mhz$)")


def _list_items():
    """The item shape of every list-typed field, by its TIME_UNITS path. None
    means the items are scalars, so they carry no keys."""
    return {
        "HELO.channels": CHANNEL_SPEC,
        "SAMPLES.samples": SAMPLE_SPEC,
        "ANNOTATIONS.annotations": ANNOTATION_SPEC,
        "EDGE_EVENTS.words": None,
        "DIGEST.recent": DIGEST_RECENT_SPEC,
        "ANNOTATIONS.payload[eye_summary].curve": EYE_CURVE_POINT_SPEC,
        "DIGEST.verdict.anomalies": DIGEST_ANOMALY_SPEC,
    }


#: A map whose keys are data (protocol names, enum values), not field names.
#: ("each", spec): every value has that shape. ("each", None): values are scalars.
#: ("shape", spec): the map is one record of that shape.
#: _PER_KIND: an annotation payload, walked per kind from ANNOTATION_PAYLOAD.
_PER_KIND = "per-kind"


def _map_shapes():
    """How the keys of every map-typed field are declared, by its TIME_UNITS
    path (2.4.2)."""
    return {
        "HELO.channels[].enum_map": ("each", None),
        "ANNOTATIONS.annotations[].payload": _PER_KIND,
        "ANNOTATIONS.annotations[].text_res": ("shape", ANNOTATION_TEXT_RES_SPEC),
        "STATS.proto_stats": ("each", STATS_PROTO_COUNTERS_SPEC),
        "STATS.ring_alarm": ("shape", STATS_RING_ALARM_SPEC),
        "STATS.proto_decode_errs": ("each", None),
        "DIGEST.protocols": ("each", DIGEST_PROTOCOL_STAT_SPEC),
        "DIGEST.verdict": ("shape", DIGEST_VERDICT_SPEC),
    }


def _shapes():
    """(path, shape) for every top-level shape: frames and annotation payloads."""
    out = [(name, shape) for name, shape in FRAME_SPEC.items()]
    out += [(f"ANNOTATIONS.payload[{kind}]", ANNOTATION_PAYLOAD[kind])
            for kind in sorted(ANNOTATION_PAYLOAD)]
    return out


def _walk(prefix, shape, decl, keys, undeclared):
    items, maps = decl
    for part in ("req", "opt"):
        for key, kind in shape.get(part, {}).items():
            path = f"{prefix}.{key}"
            keys.append(path)
            if kind == "list":
                if path not in items:
                    undeclared["list"].append(path)
                elif items[path] is not None:
                    _walk(f"{path}[]", items[path], decl, keys, undeclared)
            elif kind == "map":
                if path not in maps:
                    undeclared["map"].append(path)
                    continue
                how = maps[path]
                if how == _PER_KIND:
                    continue  # walked as ANNOTATIONS.payload[<kind>] by _shapes()
                mode, inner = how
                if inner is not None:
                    _walk(path if mode == "shape" else f"{path}.<name>",
                          inner, decl, keys, undeclared)


def _walk_all():
    keys, undeclared = [], {"list": [], "map": []}
    decl = (_list_items(), _map_shapes())
    for prefix, shape in _shapes():
        _walk(prefix, shape, decl, keys, undeclared)
    return keys, undeclared


def time_like_keys():
    """Every time-like key in this contract's shapes, by its TIME_UNITS name,
    including the keys of list items (2.4.1) and of map shapes (2.4.2)."""
    keys, _ = _walk_all()
    return [k for k in keys if _TIME_LIKE.search(k.rsplit(".", 1)[-1])]


def undeclared_list_fields():
    """List-typed fields with no declared item shape: their keys would be invisible."""
    _, undeclared = _walk_all()
    return undeclared["list"]


def undeclared_map_fields():
    """Map-typed fields with no declared shape in _map_shapes(): their keys would
    be invisible (2.4.2)."""
    _, undeclared = _walk_all()
    return undeclared["map"]


def time_unit_gaps(units=None):
    """(missing, stale): time-like keys with no unit, and unit rows naming no key."""
    units = TIME_UNITS if units is None else units
    keys = time_like_keys()
    return ([k for k in keys if k not in units], sorted(set(units) - set(keys)))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_check(paths):
    rc = 0
    for raw in paths:
        path = Path(raw)
        if path.suffix == ".json":
            fixture = load_fixture(path)
            result = check_fixture(fixture)
            expect = fixture.get("expect", "accept")
            good = (result.accepted and expect == "accept") or \
                   (not result.accepted and expect == "reject")
            print(f"{'OK ' if good else 'BAD'} {path} "
                  f"expect={expect} got={result.verdict.lower()}")
            for failure in result.failures:
                print(f"  - {failure}")
            if not good:
                rc = 1
            continue
        direction = "c2p" if ".c2p." in path.name else "p2c"
        result = check_stream(path.read_bytes(), direction)
        if result.accepted:
            print(f"ACCEPTED {path} ({direction})")
        else:
            rc = 1
            print(f"REJECTED {path} ({direction})")
            for failure in result.failures:
                print(f"  - {failure}")
    return rc


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--emit-contract-json" in argv:
        print(json.dumps(contract_json(), indent=2))
        return 0
    if not argv:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: wave_stream_contract.py "
              "{check <path...> | emit [--check] | verify}", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "check":
        return _cmd_check(rest)
    if cmd == "emit":
        drift = emit(check_only="--check" in rest)
        if drift and "--check" in rest:
            print("generated artifacts are stale vs fixtures/*.json:")
            for path in drift:
                print(f"  - {path}")
            print("run: wave_stream_contract.py emit")
            return 1
        for path in drift:
            print(f"wrote {path}")
        if not drift:
            print("up to date")
        return 0
    if cmd == "verify":
        ok, lines = verify()
        for line in lines:
            print(line)
        return 0 if ok else 1
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
