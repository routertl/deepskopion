"""DS-P1.56 — falsifiable tests for can_live_capture_check.py.

Board-free by construction: builds synthetic ANNOTATIONS payloads shaped
exactly like what a real DE25 live capture produces (see the
2026-08-26 DS-P1.56 witness doc for the real values these mirror), and
proves the checker has POWER, not just a green bar:

  * BASELINE — the parser binds to test_can_datalogger.py's actual
    `bfm.send(can_id=..., data=[...])` literal, not a hardcoded copy.
  * MUTATION — a byte-corrupted capture (the exact DS-P1.56 negative-proof
    shape) must turn `check()` red; a kind-sequence corruption must too.
  * Positive control — a genuinely matching capture passes, so the negative
    cases above are proven against a check that CAN pass, not one that is
    vacuously always red.

Deploy environment: pure pytest, no board/network/external repo. Not wired
into any commit gate (hooks.pre_commit.tests is []), same status as
test_capture_correctness.py's board-free suite. Run with
`python3 -m pytest sw/stream/tests/test_can_live_capture_check.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

from sw.stream.testbench import can_live_capture_check as clc

CAN_ID = 0x123
DATA = [0xAB, 0xCD]


def _ann(channel_id, kind, payload, ts=0):
    return {"channel_id": channel_id, "kind": kind, "payload": payload, "ts_start": ts}


def _good_annotations(can_id=CAN_ID, data=DATA):
    """Mirror the exact token shape a real DE25 live capture produced for
    ID=0x123 data=[0xAB,0xCD] (2026-08-26 DS-P1.56 witness): can_enabled
    (filtered out by check()), frame_begin, control, 2x id, Nx data, 2x crc,
    ack_observed, frame_end on channel 105, plus the reassembled summary on
    channel 106."""
    id_hi = (can_id >> 8) & 0x07
    id_lo = can_id & 0xFF
    t = 0
    anns = [_ann(105, "can_enabled", {"event": "enabled"}, t)]
    t += 1
    anns.append(_ann(105, "can_frame_begin", {"event": "frame_begin"}, t)); t += 1
    anns.append(_ann(105, "can_control", {"ide": False, "rtr": False, "dlc": len(data)}, t)); t += 1
    anns.append(_ann(105, "can_id", {"id_byte": id_hi}, t)); t += 1
    anns.append(_ann(105, "can_id", {"id_byte": id_lo}, t)); t += 1
    for b in data:
        anns.append(_ann(105, "can_data", {"data": b}, t)); t += 1
    anns.append(_ann(105, "can_crc", {"crc_byte": 0xFE}, t)); t += 1
    anns.append(_ann(105, "can_crc", {"crc_byte": 0x78}, t)); t += 1
    anns.append(_ann(105, "can_ack_observed", {"event": "ack_observed"}, t)); t += 1
    anns.append(_ann(105, "can_frame_end", {"event": "frame_end"}, t)); t += 1
    anns.append(_ann(106, "can_frame", {
        "id": can_id, "ide": False, "rtr": False, "dlc": len(data), "data": list(data),
        "crc": 0x7F3C, "ack": True, "errors": [],
    }, t))
    return anns


# ── positive control: proves the negative cases below are meaningful ────────

def test_matching_capture_passes():
    ok, detail = clc.check(_good_annotations(), CAN_ID, DATA)
    assert ok, detail


# ── #5 negative proof: a corrupted byte must turn the check red ─────────────

def test_corrupted_data_byte_fails():
    """DS-P1.56 done-when 5: corrupt one expected byte, witness the run FAIL.
    Mirrors the real hardware negative-proof run (data[0] 0xAB -> 0x54)."""
    anns = _good_annotations(data=[0xAB, 0xCD])
    # flip the observed can_data annotation's byte, matching what a real
    # corrupted-stimulus DE25 run actually produces (id/crc/ack unchanged,
    # since only the TX'd data byte was corrupted, not id or CRC computation
    # inputs used by the reassembler for anything but its own data field).
    for a in anns:
        if a["channel_id"] == 105 and a["kind"] == "can_data" and a["payload"]["data"] == 0xAB:
            a["payload"]["data"] = 0x54
        if a["channel_id"] == 106:
            a["payload"]["data"] = [0x54, 0xCD]

    ok, detail = clc.check(anns, CAN_ID, DATA)
    assert not ok, "corrupted data byte must fail the check, not pass silently"
    assert "0x54" in detail or "84" in detail or "mismatch" in detail


def test_wrong_id_fails():
    anns = _good_annotations(can_id=0x124, data=DATA)
    ok, detail = clc.check(anns, CAN_ID, DATA)
    assert not ok, detail


def test_dropped_frame_end_fails():
    """A dropped frame-boundary event must be caught, not silently ignored --
    the same class of gap the WS-C0/RW-WS1 wire-conformance check is blind to
    (WS-C0 checks wire SHAPE; this checks PAYLOAD content), so this checker
    earns its keep by catching what WS-C0 structurally cannot."""
    anns = [a for a in _good_annotations() if a["kind"] != "can_frame_end"]
    ok, detail = clc.check(anns, CAN_ID, DATA)
    assert not ok, detail


def test_missing_reassembled_frame_fails():
    anns = [a for a in _good_annotations() if a["channel_id"] != 106]
    ok, detail = clc.check(anns, CAN_ID, DATA)
    assert not ok
    assert "no reassembled" in detail


def test_reassembled_frame_carrying_errors_fails():
    anns = _good_annotations()
    anns[-1]["payload"]["errors"] = ["crc_error"]
    ok, detail = clc.check(anns, CAN_ID, DATA)
    assert not ok
    assert "errors" in detail


# ── baseline binding: the literal is PARSED, not hand-copied ────────────────

def test_parse_baseline_frame_binds_to_the_sim_oracle_literal():
    """The ticket's BASELINE BINDING instruction: bind to
    test_can_datalogger.py's actual call, so a future change to that baseline
    frame is a visible, deliberate re-binding here, not a silent drift."""
    can_id, data = clc.parse_baseline_frame()
    assert can_id == 0x123
    assert data == [0xAB, 0xCD]


def test_parse_baseline_frame_is_not_substring_matched():
    """MUTATION: a source file where the call literal is gone (e.g. moved
    into a comment, or the test renamed) must fail loudly, not silently
    parse a stale/wrong value."""
    fake_source_no_call = (
        "# standard_frame_end_to_end used to call "
        "bfm.send(can_id=0x123, data=[0xAB, 0xCD]) but not any more\n"
        "async def standard_frame_end_to_end(dut):\n"
        "    pass\n"
    )
    try:
        clc.parse_baseline_frame(fake_source_no_call)
        assert False, "expected AssertionError when the call literal is absent from the function body"
    except AssertionError as e:
        assert "no longer calls" in str(e)


def test_parse_baseline_frame_picks_up_a_changed_literal():
    fake_source = (
        "async def standard_frame_end_to_end(dut):\n"
        "    meta = await bfm.send(can_id=0x456, data=[0x11, 0x22, 0x33], ack=0)\n"
    )
    can_id, data = clc.parse_baseline_frame(fake_source)
    assert can_id == 0x456
    assert data == [0x11, 0x22, 0x33]


# ── lifecycle event filtering ─────────────────────────────────────────────

def test_can_enabled_lifecycle_event_is_filtered_not_counted():
    """test_can_datalogger.py itself filters can_enabled/EV_ENABLED out of the
    frame-shape binding (`frame = [m for m in decode(got) if not (m[0]==
    T_EVENT and m[1]==EV_ENABLED)]`) -- this checker must do the same, or
    every real capture (which always carries one) would spuriously fail."""
    anns = _good_annotations()
    assert any(a["kind"] == "can_enabled" for a in anns)
    ok, detail = clc.check(anns, CAN_ID, DATA)
    assert ok, detail


# ── DS-P3.148: released RouteWave RW-WS1 caller surface ───────────────────

def _capture_file(path: Path, anns=None) -> Path:
    if anns is None:
        anns = _good_annotations()
    doc = {
        "helo": {
            "ver": "wave_stream_v1",
            "producer": "deepskopion-daemon/test",
            "sample_clk_hz": 100_000_000,
            "channels": [
                {"id": 105, "name": "can_msg", "width": 0,
                 "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},
                {"id": 106, "name": "can_frame", "width": 0,
                 "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},
            ],
            "channel_total_bits": 0,
        },
        "captured": [[6, {"seq": 0, "annotations": anns}]],
    }
    path.write_text(json.dumps(doc))
    return path


def _fake_rw_ws1(path: Path, *, omit_can_data=False) -> Path:
    kinds = {
        "can_enabled": 1,
        "can_frame_begin": 1,
        "can_control": 1,
        "can_id": 2,
        "can_data": 2,
        "can_crc": 2,
        "can_ack_observed": 1,
        "can_frame_end": 1,
        "can_frame": 1,
    }
    if omit_can_data:
        kinds.pop("can_data")
    result = {
        "contract": "wave_stream_v1",
        "checker": "routewave_cli stream-conformance",
        "routewave_version": "test",
        "verdict": "accept",
        "channels": [
            {"id": 105, "name": "can_msg"},
            {"id": 106, "name": "can_frame"},
        ],
        "annotation_kinds": kinds,
        "observed": {"annotations": 12, "parse_errors": 0},
    }
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "assert sys.argv[1] == 'stream-conformance'\n"
        f"print({json.dumps(json.dumps(result))})\n"
    )
    path.chmod(0o755)
    return path


def test_cli_cross_checks_same_capture_with_rw_ws1(tmp_path, capsys):
    """Positive control: the public CLI reaches the external checker and binds
    its materialised channels/kinds to the byte-exact Python judgement."""
    capture = _capture_file(tmp_path / "capture.json")
    checker = _fake_rw_ws1(tmp_path / "routewave_cli")

    assert clc.main([str(capture), "--routewave-checker", str(checker)]) == 0
    out = capsys.readouterr().out
    assert "RouteWave RW-WS1: PASS" in out
    assert "ID=0x123 data=['0xab', '0xcd']" in out


def test_cli_fails_if_rw_ws1_did_not_materialise_can_data(tmp_path, capsys):
    """MUTATION: deleting the released checker's can_data observation must
    fail at the CLI exit-code surface, even though it still says accept."""
    capture = _capture_file(tmp_path / "capture.json")
    checker = _fake_rw_ws1(tmp_path / "routewave_cli", omit_can_data=True)

    assert clc.main([str(capture), "--routewave-checker", str(checker)]) == 1
    assert "RouteWave RW-WS1: FAIL" in capsys.readouterr().out


def test_cli_byte_mutation_fails_before_a_green_rw_ws1_can_mask_it(tmp_path, capsys):
    """MUTATION: WS-C0 only constrains shape, so a byte mutation remains a
    conformant stream. The combined caller must nevertheless exit non-zero."""
    anns = _good_annotations()
    for ann in anns:
        if ann["kind"] == "can_data" and ann["payload"].get("data") == 0xAB:
            ann["payload"]["data"] = 0x54
        if ann["channel_id"] == 106:
            ann["payload"]["data"] = [0x54, 0xCD]
    capture = _capture_file(tmp_path / "capture.json", anns)
    checker = _fake_rw_ws1(tmp_path / "routewave_cli")

    assert clc.main([str(capture), "--routewave-checker", str(checker)]) == 1
    out = capsys.readouterr().out
    assert "result: FAIL" in out
    assert "RouteWave RW-WS1: PASS" not in out
