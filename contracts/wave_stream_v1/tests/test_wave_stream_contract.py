"""WS-C0 (DS-P1.29) — the contract's own gate.

Two things are being guarded here, and only one of them is "the checker works".

1.  The fixtures behave as recorded — the valid session is ACCEPTED, and every
    malformed one is REJECTED **naming the field that made it malformed**. A
    rejection that fires for an incidental reason is a test that passes by
    accident, and it stops guarding the defect it was written for the moment the
    incidental reason changes.

2.  The checker can go RED. A contract whose checker never rejects anything
    conformant-looking is decoration. `test_every_semantic_rule_has_a_fixture`
    and the mutation sweep below exist so that the ACCEPT on the golden session
    is earned: every required key in the contract is knocked out of the golden
    session in turn and the session must stop being accepted.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CONTRACT_DIR = HERE.parent
sys.path.insert(0, str(CONTRACT_DIR))

import wave_stream_contract as C  # noqa: E402  (path set above)

MANIFEST = json.loads((CONTRACT_DIR / "manifest.json").read_text(encoding="utf-8"))
FIXTURES = {e["name"]: e for e in MANIFEST["fixtures"]}
GOLDEN = C.load_fixture(CONTRACT_DIR / "fixtures" / "session_golden.json")


# ---------------------------------------------------------------------------
# The recorded verdicts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_fixture_gets_its_recorded_verdict(name):
    entry = FIXTURES[name]
    fixture = C.load_fixture(CONTRACT_DIR / entry["source"])
    result = C.check_fixture(fixture)
    got = "accept" if result.accepted else "reject"
    assert got == entry["expect"], (
        f"{name}: expected {entry['expect']}, got {got}\n"
        + "\n".join(f"  {f}" for f in result.failures))


@pytest.mark.parametrize("name", sorted(n for n in FIXTURES
                                        if FIXTURES[n]["expect"] == "reject"))
def test_rejection_names_the_field_that_broke(name):
    """The load-bearing half. A consumer acts on WHICH field failed."""
    entry = FIXTURES[name]
    result = C.check_fixture(C.load_fixture(CONTRACT_DIR / entry["source"]))
    missing = set(entry["expect_fields"]) - set(result.fields)
    assert not missing, (
        f"{name} was rejected, but not for the recorded reason.\n"
        f"  expected field(s): {sorted(missing)}\n"
        f"  named field(s):    {result.fields}")


def test_at_least_one_fixture_is_accepted_and_most_are_not():
    """A suite of nothing but rejections proves the checker says no, not that
    it can say yes; a suite of nothing but acceptances proves the reverse."""
    verdicts = [e["expect"] for e in MANIFEST["fixtures"]]
    assert "accept" in verdicts
    assert verdicts.count("reject") >= 15


# ---------------------------------------------------------------------------
# The compiled bytes are the artifact the other repo consumes
# ---------------------------------------------------------------------------

def test_committed_streams_match_their_source_json():
    """fixtures/*.json is the source; fixtures/*.mp is the compiled wire form.
    Two committed representations of one thing can drift, so the gate is that
    regenerating changes nothing."""
    drift = C.emit(check_only=True)
    assert not drift, (
        "committed artifacts are stale vs fixtures/*.json: "
        + ", ".join(drift)
        + " — run: python3 contracts/wave_stream_v1/wave_stream_contract.py emit")


def test_manifest_digests_match_the_committed_bytes():
    """A consumer in another language trusts the manifest digest to know it got
    the bytes this contract judged."""
    for entry in MANIFEST["fixtures"]:
        for meta in entry["streams"].values():
            data = (CONTRACT_DIR / meta["file"]).read_bytes()
            assert len(data) == meta["bytes"], meta["file"]
            assert C._sha256(data) == meta["sha256"], meta["file"]


def test_streams_are_consumable_without_the_json():
    """The C++ half of "consumable by both repos": bytes in, verdict out, no
    Python data structures and no fixture JSON involved."""
    result = C.check_stream(
        (CONTRACT_DIR / "fixtures" / "session_golden.p2c.mp").read_bytes(), "p2c")
    assert result.accepted, str(result)
    bad = C.check_stream(
        (CONTRACT_DIR / "fixtures" / "reject_tag_body_collision.p2c.mp").read_bytes(),
        "p2c")
    assert not bad.accepted
    assert "frame[1][SYNC].live" in bad.fields


def test_contract_json_is_a_projection_not_a_second_source():
    committed = json.loads((CONTRACT_DIR / "contract.json").read_text(encoding="utf-8"))
    assert committed == C.contract_json()


# ---------------------------------------------------------------------------
# The compatibility policy
# ---------------------------------------------------------------------------

def test_unknown_tag_is_skipped_and_the_session_survives():
    """The rule the D.14 collision was born from. The golden session carries a
    tag no revision of this contract knows; it must be skipped, and everything
    after it must still be judged."""
    result = C.check_fixture(GOLDEN)
    assert result.accepted, str(result)
    assert 200 in result.skipped_tags
    # and the frames after the unknown tag were still checked: break one and the
    # session must fail, which it cannot do if parsing stopped at tag 200.
    broken = copy.deepcopy(GOLDEN)
    tail = [f for f in broken["frames"] if f.get("tag") == 2][-1]
    tail["payload"]["samples"][0]["ts"] = 1
    assert not C.check_fixture(broken).accepted


def test_unknown_key_is_ignored():
    """Additive fields are non-breaking — that is what lets the protocol grow
    without a flag day."""
    grown = copy.deepcopy(GOLDEN)
    for frame in grown["frames"]:
        if "payload" in frame:
            frame["payload"]["a_field_from_the_future"] = 1
    assert C.check_fixture(grown).accepted


def test_every_tag_in_the_registry_has_a_payload_shape_and_a_direction():
    for tag, (name, direction) in C.TAGS.items():
        assert direction in ("p2c", "c2p", "both"), name
        assert name in C.FRAME_SPEC, f"tag {tag} ({name}) has no payload shape"
    assert len(set(C.TAGS)) == len(C.TAGS)
    assert len({n for n, _ in C.TAGS.values()}) == len(C.TAGS), "duplicate name"


def test_tag_registry_is_the_frozen_numbering():
    """Hard-coded on purpose. Deriving this from the same table it guards would
    make it tautological, and the numbering IS the contract: renumbering a tag
    is the one change no consumer can detect (D.14)."""
    assert {t: n for t, (n, _) in C.TAGS.items()} == {
        0: "HELO", 1: "HELO_OK", 2: "SAMPLES", 3: "RATE_HINT", 4: "EOF",
        5: "ERR", 6: "ANNOTATIONS", 7: "EDGE_EVENTS", 8: "CONTROL", 9: "SYNC",
        10: "CONTROL_ACK", 11: "STATS", 12: "DIGEST"}


def test_the_policy_covers_every_extension_axis():
    """The policy is the deliverable, so its absence is a test failure, not a
    documentation gap."""
    assert set(C.COMPAT) == {
        "unknown_tag", "tag_registry", "unknown_key", "unknown_enum_member",
        "unknown_kind", "body_schema", "version_mismatch"}
    for axis, text in C.COMPAT.items():
        assert len(text) > 40, axis


# ---------------------------------------------------------------------------
# The checker can go RED — proven by mutation, not asserted
# ---------------------------------------------------------------------------

def _golden_frames():
    return [(i, f) for i, f in enumerate(copy.deepcopy(GOLDEN)["frames"])
            if "payload" in f and f["tag"] in C.TAGS]


def test_dropping_any_required_key_from_the_golden_session_is_rejected():
    """Sweep: every required key of every frame in the good session is deleted
    in turn and the session must stop being accepted. A key nobody checks is a
    key the contract does not actually contain."""
    survived = []
    for index, frame in _golden_frames():
        name = C.TAGS[frame["tag"]][0]
        for key in C.FRAME_SPEC[name]["req"]:
            mutant = copy.deepcopy(GOLDEN)
            del mutant["frames"][index]["payload"][key]
            if C.check_fixture(mutant).accepted:
                survived.append(f"{name}.{key}")
    assert not survived, f"deleting these changed nothing: {survived}"


def test_dropping_any_required_annotation_role_key_is_rejected():
    """The same sweep one level down, over the cross-repo role keys — the field
    contract that breaks silently rather than loudly."""
    survived = []
    for index, frame in _golden_frames():
        if C.TAGS[frame["tag"]][0] != "ANNOTATIONS":
            continue
        for a_index, annotation in enumerate(frame["payload"]["annotations"]):
            spec = C.ANNOTATION_PAYLOAD.get(annotation["kind"])
            if not spec:
                continue
            for key in spec["req"]:
                mutant = copy.deepcopy(GOLDEN)
                del (mutant["frames"][index]["payload"]
                     ["annotations"][a_index]["payload"][key])
                if C.check_fixture(mutant).accepted:
                    survived.append(f"{annotation['kind']}.{key}")
    assert survived == [], f"deleting these changed nothing: {survived}"


def test_the_golden_session_exercises_every_registered_tag_a_producer_sends():
    """Coverage of the fixture itself: a contract clause with no fixture behind
    it has never been executed by either repo."""
    used = {f["tag"] for f in GOLDEN["frames"] if "tag" in f}
    expected = set(C.TAGS) - {C.TAG_BY_NAME["ERR"]}
    assert expected <= used, (
        f"golden session never exercises tag(s): "
        f"{sorted(expected - used)}")


def test_the_golden_session_covers_every_protocol_the_target_captures():
    kinds = {a["kind"]
             for f in GOLDEN["frames"] if f.get("tag") == C.TAG_BY_NAME["ANNOTATIONS"]
             for a in f["payload"]["annotations"]}
    for protocol in ("can", "uart", "i2c", "spi", "jtag"):
        assert any(k.startswith(protocol + "_") for k in kinds), protocol
    # edge traffic rides EDGE_EVENTS rather than an annotation kind
    assert any(f.get("tag") == C.TAG_BY_NAME["EDGE_EVENTS"]
               for f in GOLDEN["frames"])
    # loss provenance in BOTH flavours, and the storm collapse
    losses = [a for f in GOLDEN["frames"]
              if f.get("tag") == C.TAG_BY_NAME["ANNOTATIONS"]
              for a in f["payload"]["annotations"] if a["kind"] == "loss"]
    assert {a["payload"]["in_band"] for a in losses} == {True, False}
    assert "error_run" in kinds


def test_stats_fixture_carries_the_nulls_that_mean_no_counter():
    """A protocol with no regbank slot reports null, never a fabricated 0. The
    fixture has to contain one or the consumer's "n/a" path is untested."""
    stats = next(f["payload"] for f in GOLDEN["frames"]
                 if f.get("tag") == C.TAG_BY_NAME["STATS"])
    assert stats["proto_stats"]["can"] == {"pkts": None, "errs": None,
                                           "drops": None}
    assert stats["arb_grants"] is None
    assert stats["proto_stats"]["edge"]["drops"] is None


def test_registered_protocols_are_exactly_the_described_vocabulary():
    """proto_stats may be keyed by any REGISTERED protocol (2.3.0, DS-P1.61).
    The registry is the set the contract describes — every protocol with an
    event vocabulary, plus edge (raw transitions, no events). A name in one and
    not the other is a protocol half-added to the contract."""
    assert set(C.REGISTERED_PROTOCOLS) == set(C.PROTOCOL_EVENTS) | {"edge"}
    assert "i2s" not in C.REGISTERED_PROTOCOLS  # the DS-P3.120 phantom row


# ---------------------------------------------------------------------------
# Decode layer
# ---------------------------------------------------------------------------

def test_a_corrupt_stream_is_reported_not_raised():
    """A consumer facing a corrupt producer has to report, not crash."""
    frames, failures = C.decode_stream(b"\xc1\xc1\xc1")
    assert failures and "FRAME_DECODE" in str(failures[0])
    assert frames == []


def test_trailing_bytes_are_a_truncated_stream():
    good = (CONTRACT_DIR / "fixtures" / "session_golden.p2c.mp").read_bytes()
    _frames, failures = C.decode_stream(good[:-5])
    assert failures
    assert "truncated" in str(failures[0])


# ---------------------------------------------------------------------------
# 2.4.0 (LT-P2.251): every time on the wire states its unit
# ---------------------------------------------------------------------------

def test_every_time_like_key_has_a_unit_and_every_unit_a_key():
    missing, stale = C.time_unit_gaps()
    assert not missing, f"time-like keys with no unit in TIME_UNITS: {missing}"
    assert not stale, f"TIME_UNITS rows naming no key in the contract: {stale}"


def test_a_missing_unit_row_is_caught():
    # The seeded mismatch: drop one row and the check must name it.
    seeded = dict(C.TIME_UNITS)
    del seeded["SAMPLES.samples[].ts"]
    missing, _ = C.time_unit_gaps(seeded)
    assert missing == ["SAMPLES.samples[].ts"]
    seeded = dict(C.TIME_UNITS, **{"SAMPLES.samples[].tss": "ns since HELO"})
    _, stale = C.time_unit_gaps(seeded)
    assert stale == ["SAMPLES.samples[].tss"]


def test_a_new_time_like_key_needs_a_row():
    spec = copy.deepcopy(C.FRAME_SPEC)
    spec["SAMPLES"]["opt"]["window_ns"] = "uint"
    original = C.FRAME_SPEC
    C.FRAME_SPEC = spec
    try:
        missing, _ = C.time_unit_gaps()
    finally:
        C.FRAME_SPEC = original
    assert missing == ["SAMPLES.window_ns"]


def test_the_wire_times_a_consumer_places_are_whole_ns():
    # The 1 ns limit the spec states: SAMPLES, ANNOTATIONS and DIGEST times are ns.
    for key in ("SAMPLES.samples[].ts", "ANNOTATIONS.annotations[].ts_start",
                "ANNOTATIONS.annotations[].ts_end", "DIGEST.recent[].ts"):
        assert C.TIME_UNITS[key] == "ns since HELO", key
    assert C.TIME_UNITS["EDGE_EVENTS.anchor_ticks"] == "ticks of the edge clock"
    assert C.contract_json()["time_units"] == dict(sorted(C.TIME_UNITS.items()))


# ---------------------------------------------------------------------------
# 2.4.1: a time-like key inside a list item is walked too
# ---------------------------------------------------------------------------

CURVE_WIDTH = "ANNOTATIONS.payload[eye_summary].curve[].eye_width_ps"


def test_a_time_like_key_inside_a_list_item_is_walked():
    # eye_summary.curve[] items carry eye_width_ps (ps). 2.4.0 never saw it.
    assert CURVE_WIDTH in C.time_like_keys()
    assert C.TIME_UNITS[CURVE_WIDTH].startswith("ps")


def test_a_list_item_key_without_a_row_is_caught():
    seeded = dict(C.TIME_UNITS)
    del seeded[CURVE_WIDTH]
    missing, _ = C.time_unit_gaps(seeded)
    assert missing == [CURVE_WIDTH]


def test_every_list_field_declares_its_items():
    # A list whose items have no declared shape hides its keys from the walk,
    # which is how eye_width_ps had no unit.
    assert C.undeclared_list_fields() == []
    spec = copy.deepcopy(C.ANNOTATION_PAYLOAD)
    spec["eye_summary"]["opt"]["steps"] = "list"
    original = C.ANNOTATION_PAYLOAD
    C.ANNOTATION_PAYLOAD = spec
    try:
        assert C.undeclared_list_fields() == ["ANNOTATIONS.payload[eye_summary].steps"]
    finally:
        C.ANNOTATION_PAYLOAD = original


# ---------------------------------------------------------------------------
# 2.4.2: map-typed fields declare their shapes, and the walk descends into them
# ---------------------------------------------------------------------------

def _with(name, value):
    """Context: replace module constant C.<name> for the duration."""
    class _Swap:
        def __enter__(self):
            self.original = getattr(C, name)
            setattr(C, name, value)

        def __exit__(self, *exc):
            setattr(C, name, self.original)
    return _Swap()


def test_every_map_field_declares_its_shape():
    # A map whose shape is written only inside a validator hides its keys from
    # the walk, as DIGEST.verdict and DIGEST.protocols did before 2.4.2.
    assert C.undeclared_map_fields() == []
    spec = copy.deepcopy(C.FRAME_SPEC)
    spec["STATS"]["opt"]["ring_detail"] = "map"
    with _with("FRAME_SPEC", spec):
        assert C.undeclared_map_fields() == ["STATS.ring_detail"]


@pytest.mark.parametrize("const, seeded_key, path", [
    ("DIGEST_VERDICT_SPEC", "decided_ns", "DIGEST.verdict.decided_ns"),
    ("DIGEST_ANOMALY_SPEC", "since_ns", "DIGEST.verdict.anomalies[].since_ns"),
    ("DIGEST_PROTOCOL_STAT_SPEC", "window_ns", "DIGEST.protocols.<name>.window_ns"),
    ("STATS_PROTO_COUNTERS_SPEC", "last_ns", "STATS.proto_stats.<name>.last_ns"),
    ("STATS_RING_ALARM_SPEC", "active_ms", "STATS.ring_alarm.active_ms"),
])
def test_a_time_like_key_inside_a_map_shape_needs_a_row(const, seeded_key, path):
    assert C.time_unit_gaps() == ([], [])
    spec = copy.deepcopy(getattr(C, const))
    spec["opt"][seeded_key] = "uint"
    with _with(const, spec):
        missing, _ = C.time_unit_gaps()
    assert missing == [path]


@pytest.mark.parametrize("const", [
    "DIGEST_VERDICT_SPEC", "DIGEST_ANOMALY_SPEC", "DIGEST_PROTOCOL_STAT_SPEC",
    "STATS_PROTO_COUNTERS_SPEC", "STATS_RING_ALARM_SPEC",
])
def test_the_validators_check_the_lifted_shapes(const):
    # The walk and the validator read one constant: a required key added to the
    # constant is demanded of the golden session, which does not carry it.
    assert C.check_fixture(GOLDEN).accepted
    spec = copy.deepcopy(getattr(C, const))
    spec["req"]["seeded_required_key"] = "uint"
    with _with(const, spec):
        assert not C.check_fixture(GOLDEN).accepted, const


# ---------------------------------------------------------------------------
# A recorded session is judged by the revision it was recorded under
# ---------------------------------------------------------------------------

def _edge_lane_violation():
    import json as _json
    fx = _json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                      / "reject_edge_lane_on_sample_channel.json").read_text(encoding="utf-8"))
    return [(f["dir"], f["tag"], f["payload"]) for f in fx["frames"]]


def test_the_edge_lane_rule_binds_producers_from_its_revision_on():
    """The rule is new in 2.8.0. A live producer (the default revision) that
    breaks it is rejected; the same session recorded under 1.0.0 is judged by
    1.0.0's rules (committed evidence from 2026-08-29/30)."""
    frames = _edge_lane_violation()
    assert not C.check_session(frames).accepted
    assert not C.check_session(frames, revision=C.EDGE_LANE_RULE_SINCE).accepted
    assert C.check_session(frames, revision="1.0.0").accepted
    assert C.check_session(frames, revision="2.7.9").accepted


def test_a_malformed_revision_is_refused():
    import pytest as _pytest
    with _pytest.raises(ValueError):
        C.check_session(_edge_lane_violation(), revision="2.8")
