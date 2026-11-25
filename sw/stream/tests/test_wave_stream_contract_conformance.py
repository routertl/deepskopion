# rr unknown-llm (AI-generated)
"""WS-C0 (DS-P1.29) — bind the deepskopion producer to the vendored contract.

`contracts/wave_stream_v1/` is the seam, and it is deliberately not owned by
this repo: it does not import deepskopion, and deepskopion does not get to edit
it to make itself pass. This file is the other half of that arrangement — the
producer declaring itself SUBJECT to the contract, so a change on either side
that pulls them apart fails here instead of on a user's screen.

It is the test that would have caught all three drifts WS-C0 found on landing:

  * `make_err` emitted the human explanation under `detail`; the contract and
    the consumer both read `message`, so the only frame whose purpose is to say
    why a session died decoded to an empty string;
  * annotation channels declared `type: "Annotation"` — not a registered
    ChannelType, so the consumer silently substituted `Signal`;
  * the eye and stats lanes declared `radix: "Decimal"` — not a registered
    Radix, so the consumer silently rendered them in hex.

None of the three raised anything anywhere. That is the failure mode a seam
pinned in two places has: both sides are internally consistent and quietly
disagree.

Scope: the producer's STATIC surface — its constants, its channel table and its
frame builders. Conformance of a LIVE capture session end to end is DS-WS1
(DS-P2.89), which this contract is a dependency of.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sw.stream import channels as ch
from sw.stream import decoders
from sw.stream import protocol as proto
from sw.stream import protocol_codes
from sw.stream import datalogger_registry
from sw.stream.data_source import build_proto_stats

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_DIR = _REPO_ROOT / "contracts" / "wave_stream_v1"
sys.path.insert(0, str(_CONTRACT_DIR))

import wave_stream_contract as C  # noqa: E402  (path set above)


# ---------------------------------------------------------------------------
# Identity and the frozen numbering
# ---------------------------------------------------------------------------

def test_producer_speaks_the_contract_wire_version():
    assert proto.PROTOCOL_VERSION == C.PROTOCOL_VERSION


def test_producer_tag_constants_are_the_contract_registry():
    """The D.14 guard on the producer side. deepskopion once shipped SYNC on
    tag 7 while the consumer had frozen 7 as EDGE_EVENTS; nothing in either repo
    could see the collision because each was self-consistent."""
    producer = {value: name[len("TAG_"):]
                for name, value in vars(proto).items()
                if name.startswith("TAG_") and isinstance(value, int)}
    contract = {tag: name for tag, (name, _dir) in C.TAGS.items()}
    assert producer == contract


def test_digest_body_schema_agrees():
    assert proto.DIGEST_SCHEMA == C.DIGEST_SCHEMA


# ---------------------------------------------------------------------------
# The channel table — what HELO actually puts on the wire
# ---------------------------------------------------------------------------

def test_every_channel_descriptor_uses_registered_enum_members():
    """An unregistered member is not rejected downstream: the consumer falls
    back to the default and the producer's intent is dropped in silence. That
    silence is why this is checked here rather than trusted there."""
    offenders = []
    for descriptor in ch.CHANNELS:
        for key, allowed in (("type", C.CHANNEL_TYPES),
                             ("role", C.CHANNEL_ROLES),
                             ("radix", C.CHANNEL_RADIXES)):
            value = descriptor.get(key)
            if value is not None and value not in allowed:
                offenders.append(
                    f"{descriptor['name']}.{key}={value!r} (falls back to "
                    f"{C.CHANNEL_DEFAULTS[key]!r})")
    assert offenders == []


def test_the_live_helo_is_accepted_by_the_contract():
    """Not a hand-written fixture: the real HELO the daemon opens a session
    with, judged by the contract's own checker."""
    helo = proto.make_helo(sample_clk_hz=100_000_000, channels=ch.CHANNELS,
                           channel_total_bits=ch.CHANNEL_TOTAL_BITS)
    result = C.check_session([("p2c", C.TAG_BY_NAME["HELO"], helo)])
    assert result.accepted, str(result)


def test_channel_total_bits_is_what_the_consumer_will_slice_by():
    assert ch.CHANNEL_TOTAL_BITS == sum(c["width"] for c in ch.CHANNELS)


# ---------------------------------------------------------------------------
# The frame builders
# ---------------------------------------------------------------------------

def _judge(tag_name, payload, direction="p2c"):
    """Judge ONE builder's output against that tag's payload shape."""
    out = []
    C._check_map(f"[{tag_name}]", payload, C.FRAME_SPEC[tag_name], out)
    return out


def test_make_err_carries_the_human_explanation_where_the_consumer_reads_it():
    """The drift: `detail` vs the contracted `message`. An ERR is fatal — the
    session ends on it — so losing its text costs the user the ONE sentence
    saying why."""
    payload = proto.make_err("TRANSPORT_LOST", "DMA ring reader went away")
    assert _judge("ERR", payload) == []
    assert payload["message"] == "DMA ring reader went away"


@pytest.mark.parametrize("tag_name,payload,direction", [
    ("SYNC", proto.make_sync(), "p2c"),
    ("EOF", proto.make_eof("user_stop"), "p2c"),
    ("CONTROL", proto.make_control(datalogger_id=3, cfg_word=9), "c2p"),
    # DS-P2.136: op=stim rides the same tag, so it owes CONTROL's required keys.
    ("CONTROL", proto.make_stim_control("spi"), "c2p"),
    ("CONTROL_ACK", proto.make_control_ack(3, 9, ok=True), "p2c"),
    ("CONTROL_ACK", proto.make_control_ack(0xFFFFFFFF, 0, ok=False,
                                           reason="unknown datalogger id"), "p2c"),
    ("EDGE_EVENTS", proto.make_edge_events(7, [0x1001_0000], anchor_ns=8_450_030),
     "p2c"),
])
def test_builder_output_matches_its_tag_payload_shape(tag_name, payload,
                                                      direction):
    assert _judge(tag_name, payload, direction) == []


def test_a_session_built_from_the_live_builders_is_accepted():
    """End to end over the producer's own builders — handshake, boundary marker,
    a decoded annotation, telemetry, close — judged as a session rather than
    frame by frame, so ordering and direction are covered too."""
    session = [
        ("p2c", C.TAG_BY_NAME["HELO"],
         proto.make_helo(100_000_000, ch.CHANNELS, ch.CHANNEL_TOTAL_BITS)),
        ("c2p", C.TAG_BY_NAME["HELO_OK"], {"consumer": "routewave/1.7.0"}),
        ("p2c", C.TAG_BY_NAME["SYNC"], proto.make_sync()),
        ("p2c", C.TAG_BY_NAME["ANNOTATIONS"], {
            "seq": 0,
            "annotations": [{
                "ts_start": 1000, "ts_end": 1080, "channel_id": 100,
                "kind": "uart_byte", "text": "0x55 'U'", "color": "#4a8c2a",
                "is_error": False, "payload": {"byte": 0x55, "ascii": "U"},
            }],
        }),
        ("p2c", C.TAG_BY_NAME["STATS"],
         proto.make_stats(1, daemon_ver="0.3.1",
                          stats={"proto_stats": build_proto_stats(lambda _o: 0)})),
        ("p2c", C.TAG_BY_NAME["EOF"], proto.make_eof("user_stop")),
    ]
    result = C.check_session(session)
    assert result.accepted, str(result)


# ---------------------------------------------------------------------------
# The vocabularies that must stay single-sourced across the seam
# ---------------------------------------------------------------------------

def test_stats_proto_stats_key_set_is_drawn_from_the_contract_vocabulary():
    """The consumer renders one row per key. DS-P3.120 shipped a hardcoded list
    that had a retired `i2s` in it and had lost the live `can` and `jtag`.

    Contract 2.3.0 (DS-P1.61) pins the registered VOCABULARY, not this build's
    selection: which data loggers a build carries is ours to choose
    (datalogger_config.yml), what we call them is not. The "no live logger
    missing" half is producer-side and lives in test_proto_stats_registry.py
    (emitted set == registry == datalogger_config.yml)."""
    active = set(datalogger_registry.ACTIVE_PROTOCOLS)
    assert active, "the producer carries no data logger at all"
    assert active <= set(C.REGISTERED_PROTOCOLS), (
        f"producer data logger(s) {sorted(active - set(C.REGISTERED_PROTOCOLS))} "
        f"are not registered in the wave_stream contract — a new protocol is a "
        f"contract revision + re-vendor, not a local edit")
    assert set(build_proto_stats(lambda _o: 0)) == active


@pytest.mark.parametrize("protocol,table", [
    ("uart", "UART_EVENTS"), ("i2c", "I2C_EVENTS"), ("spi", "SPI_EVENTS"),
    ("jtag", "JTAG_EVENTS"), ("can", "CAN_EVENTS"),
])
def test_event_vocabulary_is_the_generated_rtl_table(protocol, table):
    """The contract publishes the closed set of event names a consumer can
    receive. Its upstream source is the RTL datalogger contract yml via the
    generated protocol_codes — so the contract must not be able to fall behind
    a datalogger that gained or lost an event."""
    generated = tuple(sorted(set(getattr(protocol_codes, table).values())))
    assert C.PROTOCOL_EVENTS[protocol] == generated


def test_loss_stage_taxonomy_is_the_producer_taxonomy():
    """`loss_stage` is how the consumer buckets loss provenance; a stage the
    contract does not list is an unattributable loss."""
    from sw.stream import zs_msg
    produced = {decoders.error_stage_code(event_id) for event_id in (
        zs_msg.ERR_EVT_INTERNAL_FIFO_OVERFLOW, zs_msg.ERR_EVT_ARBITER_DROP,
        zs_msg.ERR_EVT_RING_FULL, zs_msg.ERR_EVT_DECODE_ERROR)}
    assert produced == set(C.LOSS_STAGES)


def test_registered_role_keys_match_what_the_decoders_emit():
    """The role KEYS are the cross-repo field contract — the consumer builds
    per-protocol packet columns from them, so a renamed key empties a column
    instead of raising. Sampled across every protocol the golden target captures;
    the pairs are hard-coded because deriving them from the decoders would make
    the assertion agree with itself."""
    expected = {
        "uart_byte": {"byte"},
        "uart_stop_error": {"status"},
        "i2c_byte": {"data", "ack"},
        "i2c_start": {"event"},
        "spi_mosi": {"mosi"},
        "spi_miso": {"miso"},
        "spi_event": {"event"},
        "jtag_tdi": {"tdi"},
        "jtag_tdo": {"tdo"},
        "can_id": {"id_byte"},
        "can_data": {"data"},
        "can_crc": {"crc_byte"},
        "can_control": {"ide", "rtr", "dlc"},
        "can_frame_begin": {"event"},
        "loss": {"loss_stage", "lost_count", "domain", "in_band"},
    }
    for kind, keys in expected.items():
        assert set(C.ANNOTATION_PAYLOAD[kind]["req"]) == keys, kind


def test_decoded_frames_from_the_real_decoders_conform():
    """Run the actual per-protocol decoders and judge the annotations they
    produce. This is what makes the role-key table above a claim about the
    shipped decoders rather than about a copy of them."""
    from sw.stream import zs_msg

    def _header(domain, event_id):
        return zs_msg.make_header(datalogger_id=0, domain=domain,
                                  event_id=event_id,
                                  severity=zs_msg.SEV_INFO, payload_len=1)

    cases = [
        (zs_msg.DOMAIN_UART, protocol_codes.UART_TYPES["payload"], b"\x55"),
        (zs_msg.DOMAIN_I2C, protocol_codes.I2C_TYPES["payload"] << 1, b"\xa0"),
        (zs_msg.DOMAIN_SPI, protocol_codes.SPI_TYPES["payload_a"], b"\x9f"),
        (zs_msg.DOMAIN_JTAG, protocol_codes.JTAG_TYPES["payload_tdi"], b"\xa5"),
        (zs_msg.DOMAIN_CAN, protocol_codes.CAN_TYPES["id_payload"], b"\x01"),
        (zs_msg.DOMAIN_CAN, protocol_codes.CAN_TYPES["control"], b"\x02"),
    ]
    judged = 0
    for domain, event_id, payload in cases:
        frame = decoders.decode_frame(_header(domain, event_id), payload,
                                      100_000_000)
        assert frame is not None, (domain, event_id)
        spec = C.ANNOTATION_PAYLOAD.get(frame.kind)
        assert spec is not None, (
            f"{frame.kind!r} is decoded by a shipped decoder but is not a "
            f"registered kind in the contract")
        out = []
        C._check_map(f"[{frame.kind}].payload", frame.fields or {}, spec, out)
        assert out == [], [str(f) for f in out]
        judged += 1
    assert judged == len(cases)


def test_edge_maxfreq_from_the_real_decoder_conforms():
    """DS-P3.117: the edge datalogger's in-band EVT_EDGE_MAXFREQ frame, decoded by
    the shipped decoder, is a registered kind whose payload the contract accepts,
    for a measured line AND for the re-armed sentinel (freq_hz null). Judged as a
    session over the live builders, so the annotation reaches the consumer in the
    shape the consumer was promised."""
    from sw.stream import zs_msg
    from sw.stream.data_source import _DOMAIN_CHANNEL_MAP

    # The lane the daemon actually anchors a DOMAIN_EDGE annotation to.
    lane = _DOMAIN_CHANNEL_MAP[zs_msg.DOMAIN_EDGE]
    annotations = []
    for min_interval, want_freq in ((50, 1_000_000.0), (0xFFFFFF, None)):
        wire = zs_msg.pack_edge_maxfreq(
            zs_msg.EdgeMaxFreq(channel=3, min_interval=min_interval,
                               ival_width=24))
        header = zs_msg.parse_header(wire, 0)
        frame = decoders.decode_frame(header, wire[zs_msg.HEADER_SIZE:],
                                      100_000_000)
        assert frame.kind == "edge_maxfreq"
        assert frame.fields == {"channel": 3, "min_interval_ticks": min_interval,
                                "freq_hz": want_freq}
        spec = C.ANNOTATION_PAYLOAD.get(frame.kind)
        assert spec is not None, (
            "edge_maxfreq is decoded by a shipped decoder but is not a "
            "registered kind in the contract")
        annotations.append({
            "ts_start": 7300 + len(annotations), "ts_end": 7300 + len(annotations),
            "channel_id": lane, "kind": frame.kind, "text": frame.text,
            "color": frame.color, "is_error": frame.is_error,
            "payload": frame.fields})

    session = [
        ("p2c", C.TAG_BY_NAME["HELO"],
         proto.make_helo(100_000_000, ch.CHANNELS, ch.CHANNEL_TOTAL_BITS)),
        ("c2p", C.TAG_BY_NAME["HELO_OK"], {"consumer": "routewave/1.7.0"}),
        ("p2c", C.TAG_BY_NAME["ANNOTATIONS"], {"seq": 0, "annotations": annotations}),
        ("p2c", C.TAG_BY_NAME["EOF"], proto.make_eof("user_stop")),
    ]
    result = C.check_session(session)
    assert result.accepted, str(result)
