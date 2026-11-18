"""DS-P1.59 — guard the daemon-live DE-C0 witness JUDGE (de_c0_live_witness).

This suite guards the JUDGE, not silicon. It proves the judge has teeth: it
PASSES a correct capture ONLY when an accepting RW-WS1 checker is supplied, and
it goes RED (FAIL or BLOCKED) for each defect class it must catch — a WS-C0-
nonconformant stream, a wrong decoded payload, the clause-6 mutation (one I2C
byte / one edge transition corrupted while the capture is unchanged), a missing
capture, and — fail-closed — a missing/incapable RW-WS1 checker OR the checker
simply omitted. None of these assertions pass if the mechanism they guard is
removed.

Scope honesty: the captures here are oracle wire round-tripped through JSON (the
board-free ``capture_correctness`` decode driven through the real ``serve_one``
socket, in the ``live_capture_client --out`` shape). That is a
transport/round-trip conformance of the JUDGE and is GREEN WITH NO BOARD — it
does NOT stand in for the silicon witness. The real committed silicon captures
(silicon evidence captures ``cap_*.live.json``, produced
off the production daemon on fresh DE25 silicon once the DS-T1.9 SIGBUS fix let
the daemon survive the ring-drain) are re-judged by their own suite,
``test_de_c0_silicon_capture.py``, which binds this judge to those files. This
suite is the board-free judge-teeth layer that pairs with it. The RW-WS1 checker
used below is a self-contained POSITIVE CONTROL that
computes the true annotation counts from the wire — it proves PASS is reachable
only through an accepting checker, and pairs with the fail-closed tests that
prove a missing/omitted/incapable checker can never reach PASS.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from sw.stream.testbench import capture_correctness as cc
from sw.stream.testbench import de_c0_live_witness as W


# A self-contained accepting RW-WS1 checker: reads the p2c MessagePack stream
# the harness writes, computes the REAL channel/kind/annotation counts from the
# wire (channel names from the HELO map, tags 0=HELO / 6=ANNOTATIONS), and emits
# the JSON verdict ``run_rw_ws1`` demands. It is a POSITIVE CONTROL — it proves a
# PASS is reachable only with an accepting checker present; the fail-closed tests
# below prove every other checker state (absent / omitted / non-JSON) is non-PASS.
_ACCEPTING_CHECKER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import sys, json
    from collections import Counter
    import msgpack
    path = sys.argv[2]
    up = msgpack.Unpacker(raw=False); up.feed(open(path, "rb").read())
    helo, anns = {}, []
    for tag, payload in up:
        if tag == 0: helo = payload
        elif tag == 6: anns += list(payload.get("annotations", []))
    name_by_id = {c["id"]: c["name"] for c in helo.get("channels", [])}
    chan_ids = sorted({a["channel_id"] for a in anns if a.get("channel_id") is not None})
    out = {"checker": "routewave_cli stream-conformance", "verdict": "accept",
           "channels": [{"id": i, "name": name_by_id.get(i)} for i in chan_ids],
           "annotation_kinds": dict(Counter(a["kind"] for a in anns)),
           "observed": {"annotations": len(anns), "parse_errors": 0},
           "routewave_version": "positive-control-fake"}
    print(json.dumps(out)); sys.exit(0)
''')


def _accepting_checker(tmp_path) -> Path:
    p = tmp_path / "routewave_cli_accept"
    p.write_text(_ACCEPTING_CHECKER)
    p.chmod(0o755)
    return p


def _live_doc(case) -> dict:
    """A live_capture_client-shaped {helo, captured} for ``case`` — the real
    daemon wire (decode -> serve_one socket), JSON round-tripped."""
    batch = cc.decode_case(case)
    helo, captured, _session, _rc = cc.capture_session([batch])
    return json.loads(json.dumps({"helo": helo, "captured": captured}, default=str))


def _load(case):
    doc = _live_doc(case)
    return doc["helo"], [(int(t), p) for t, p in doc["captured"]]


def _cases():
    return {c.name: c for c in cc.known_cases()}


@pytest.mark.parametrize("name", ["uart_marker", "i2c_write", "spi_mosi", "edge_scl"])
def test_good_capture_passes_only_with_accepting_checker(name, tmp_path):
    case = _cases()[name]
    helo, captured = _load(case)
    v = W.judge_case(case, helo, captured, checker_path=_accepting_checker(tmp_path))
    assert v.status == "PASS", f"{name}: {v.detail}; observed={v.observed!r}"
    assert v.wsc0 == "ACCEPT"
    assert "RW-WS1" not in v.rw_ws1 or "accept" in v.rw_ws1


@pytest.mark.parametrize("name", ["uart_marker", "i2c_write", "spi_mosi", "edge_scl"])
def test_omitting_checker_is_blocked_not_passed(name):
    """Finding-2 regression: clause 4 requires RW-WS1, so a good capture with NO
    checker must be BLOCKED (non-PASS), never PASS. BASELINE (pre-fix): judge_case
    with no checker returned PASS and the CLI exited 0 with PASS=4 — the
    'detector nobody called' green run the landing refused. MUTATION: revert the
    fail-closed branch and this test goes RED (status becomes PASS)."""
    case = _cases()[name]
    helo, captured = _load(case)
    v = W.judge_case(case, helo, captured)                 # no checker_path
    assert v.status == "BLOCKED", f"{name}: expected BLOCKED, got {v.status}"
    assert v.status != "PASS"
    assert "clause 4" in v.rw_ws1


def test_i2c_capture_is_the_ack_bound_single_transaction():
    """The exact shape DS-P2.101 could not produce: one ACK-bound transaction,
    not three NACKed one-byte ones."""
    case = _cases()["i2c_write"]
    helo, captured = _load(case)
    v = W.judge_case(case, helo, captured)
    assert v.observed == ["start", (0xA0, True), (0xAB, True), (0xCD, True),
                          (0x3E, True), "stop"]


def test_edge_capture_is_transition_exact():
    case = _cases()["edge_scl"]
    helo, captured = _load(case)
    v = W.judge_case(case, helo, captured)
    assert v.observed == [(10_000, 1), (10_200, 0), (10_520, 1), (10_800, 0)]


def test_mutation_one_i2c_byte_fails_same_capture(tmp_path):
    """Clause 6: corrupt ONE expected I2C byte, capture unchanged -> FAIL."""
    case = _cases()["i2c_write"]
    helo, captured = _load(case)
    good = W.judge_case(case, helo, captured, checker_path=_accepting_checker(tmp_path))
    assert good.status == "PASS"
    exp = list(case.expected)
    exp[2] = (0xAB ^ 0x01, True)                 # one data byte wrong
    bad = W.judge_case(case, helo, captured, expected=exp,
                       checker_path=_accepting_checker(tmp_path))
    assert bad.status == "FAIL"


def test_mutation_one_edge_transition_fails_same_capture():
    """Clause 6: corrupt ONE expected edge transition, capture unchanged -> FAIL."""
    case = _cases()["edge_scl"]
    helo, captured = _load(case)
    exp = list(case.expected)
    exp[1] = (exp[1][0] + 40, exp[1][1])         # move one transition in time
    bad = W.judge_case(case, helo, captured, expected=exp)
    assert bad.status == "FAIL"


def test_wrong_channel_capture_fails():
    """A decoded payload on the wrong lane is absent from this case's channel
    and the exact-payload read fails closed (the D.14 defect class)."""
    case = _cases()["uart_marker"]
    helo, captured = _load(case)
    from dataclasses import replace
    wrong = replace(case, channel_id=999)        # nothing decoded on 999
    v = W.judge_case(wrong, helo, captured)
    assert v.status == "FAIL"


def test_empty_capture_set_is_nonzero_exit(tmp_path):
    """No captures at all -> every known case FAILs "no capture", exit 1.
    A witness that confirmed nothing is missing evidence, not success."""
    verdicts, code = W.run_all({})
    assert code == 1
    assert verdicts and all(v.status == "FAIL" for v in verdicts)
    assert {v.protocol for v in verdicts} == {"uart", "i2c", "spi", "edge"}


def test_missing_capture_for_a_case_fails(tmp_path):
    """Fail-closed: a case with no provided capture is a FAIL, not a skip."""
    case = _cases()["uart_marker"]
    p = tmp_path / "uart.json"
    p.write_text(json.dumps(_live_doc(case)))
    verdicts, code = W.run_all({"uart": p})       # i2c/spi/edge missing
    assert code == 1
    missing = [v for v in verdicts if v.protocol != "uart"]
    assert missing and all(v.status == "FAIL" for v in missing)


def test_rw_ws1_missing_checker_is_fail_closed():
    """A missing/incapable RW-WS1 checker is a hard FAIL, never a silent pass."""
    case = _cases()["uart_marker"]
    helo, captured = _load(case)
    v = W.judge_case(case, helo, captured, checker_path=Path("/nonexistent/routewave_cli"))
    assert v.status == "FAIL"
    assert "RW-WS1" in v.rw_ws1 and "not found" in v.rw_ws1


def test_rw_ws1_non_json_checker_is_fail_closed(tmp_path):
    """A checker present but WITHOUT the stream-conformance surface (prints
    non-JSON) is a hard FAIL — the DS-P3.148 discipline."""
    fake = tmp_path / "routewave_cli"
    fake.write_text("#!/bin/sh\necho 'Error: Unknown command: stream-conformance'\nexit 1\n")
    fake.chmod(0o755)
    case = _cases()["uart_marker"]
    helo, captured = _load(case)
    ok, detail = W.run_rw_ws1(helo, captured, case, fake)
    assert not ok and "RW-WS1" in detail


def test_cli_exit_code_is_nonzero_on_failure(tmp_path):
    """Assert at the CLI surface a caller reaches: with ALL four captures present
    and only SPI corrupted (its last MOSI annotation dropped), the process exits
    non-zero AND spi is the failing case while the other three pass — so the
    non-zero exit is caused by the corruption, not by a missing capture."""
    cases = _cases()
    paths = {}
    for case in cases.values():
        doc = _live_doc(case)
        if case.protocol == "spi":
            for tag, payload in doc["captured"]:
                if tag == 6:  # TAG_ANNOTATIONS — drop the last MOSI byte
                    payload["annotations"] = payload.get("annotations", [])[:-1]
        p = tmp_path / f"{case.protocol}.json"
        p.write_text(json.dumps(doc))
        paths[case.protocol] = p

    checker = _accepting_checker(tmp_path)
    verdicts, code = W.run_all(paths, checker_path=checker)
    assert code == 1
    by_proto = {v.protocol: v for v in verdicts}
    assert by_proto["spi"].status == "FAIL"
    assert all(by_proto[p].status == "PASS" for p in ("uart", "i2c", "edge"))

    # and the same through the argv/main surface
    argv = ["--routewave-checker", str(checker)]
    for proto_name, p in paths.items():
        argv += ["--capture", f"{proto_name}={p}"]
    assert W.main(argv) == 1


# ── DS-P1.59 FINAL: live/backfill (SYNC) isolation for the daemon-live path ────
def test_split_live_isolates_post_sync_backfill():
    """``live_capture_client.split_live`` keeps only the frames AFTER the
    daemon's catch-up→live boundary (TAG_SYNC), so replayed backfill from an
    earlier arm cannot pollute the exact-payload check.

    This is the mechanism the DS-P1.59 daemon-live witness relies on: connect,
    let the daemon replay backfill + emit SYNC, THEN arm — so the armed burst is
    the live tail. Silicon-measured (2026-08-29): dropping the pre-SYNC frames
    turned a polluted UART capture ([0xFF]+10 bytes across two arms) into the
    exact 10-byte payload.

    Falsifiable both ways: a capture whose burst landed in backfill (nothing
    after SYNC) yields an EMPTY live slice — which fails closed against the
    expected payload, never a silent pass."""
    from sw.stream import protocol as proto
    from sw.stream.testbench.live_capture_client import split_live

    backfill = [(proto.TAG_ANNOTATIONS, {"seq": 0, "annotations": [{"channel_id": 100,
                 "kind": "uart_byte", "payload": {"byte": 0xFF}}]})]
    live = [(proto.TAG_ANNOTATIONS, {"seq": 0, "annotations": [{"channel_id": 100,
             "kind": "uart_byte", "payload": {"byte": 0xA3}}]})]
    captured = backfill + [(proto.TAG_SYNC, {})] + live
    kept, dropped = split_live(captured)
    assert dropped == 2 and kept == live

    # No SYNC → everything is live (nothing to discard).
    kept2, dropped2 = split_live(live)
    assert dropped2 == 0 and kept2 == live

    # Burst stuck in backfill (nothing after SYNC) → empty live slice, which the
    # exact-payload extractor turns into a fail-closed empty observation.
    only_backfill = backfill + [(proto.TAG_SYNC, {})]
    kept3, dropped3 = split_live(only_backfill)
    assert kept3 == [] and dropped3 == 2
    assert cc._extract_uart(kept3, 100, 50_000_000) == []
