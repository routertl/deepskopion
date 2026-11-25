#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""DS-P1.56 — bind a live DE25 CAN capture to test_can_datalogger.py's baseline
frame literal (ID=0x123, data=[0xAB,0xCD]), the same way
capture_correctness.py binds UART/I2C/SPI to test_datalogger_to_ddr.py's
literals (DE-C0's done-condition #4 binding pattern). The baseline is
PARSED out of the sim oracle's `standard_frame_end_to_end` test, not
hand-copied, so drift on either side is a red run — per the ticket's
BASELINE BINDING instruction.

This checks a live capture (channels 105 `can_msg` / 106 `can_frame`)
recorded by ``sw.stream.testbench.live_capture_client`` against a real,
booted DE25: the per-byte token sequence on channel 105 (frame_begin, ctrl,
2x id, Nx data, 2x crc, ack_observed, frame_end) and the reassembled summary
on channel 106 (id/data/dlc/ack) must match the baseline exactly.

Usage:
    python3 -m sw.stream.testbench.can_live_capture_check /tmp/capture.json
    python3 -m sw.stream.testbench.can_live_capture_check /tmp/capture.json \
        --routewave-checker /path/to/routewave_cli

The second form is the DS-P3.148 fail-closed cross-check. It requires the
released CLI's ``stream-conformance`` JSON verdict, then binds the C++
consumer's materialised channel/kind counts to the same capture whose CAN
payload is checked byte-for-byte above. Merely loading an ``.ltrace`` file or
printing ``accept`` is not sufficient evidence.
"""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Tuple

import msgpack

_REPO_ROOT = Path(__file__).resolve().parents[3]
CAN_SIM_ORACLE = (_REPO_ROOT / "hw" / "sim" / "cocotb" / "tests" / "datalogger"
                   / "can" / "test_can_datalogger.py")


def parse_baseline_frame(oracle_text: Optional[str] = None) -> Tuple[int, List[int]]:
    """Parse (can_id, data) out of test_can_datalogger.py's
    `standard_frame_end_to_end` test's `bfm.send(can_id=..., data=[...])`
    call — the ticket's BASELINE BINDING literal (ID=0x123, data=[0xAB,0xCD]).
    Bound to the actual call literal (not a comment / docstring mention), so
    a future change to the baseline frame in the sim oracle is a visible,
    deliberate re-binding here rather than a silent drift."""
    text = oracle_text if oracle_text is not None else CAN_SIM_ORACLE.read_text()
    m = re.search(
        r"async def standard_frame_end_to_end.*?"
        r"bfm\.send\(can_id=(0x[0-9A-Fa-f]+),\s*data=(\[[^\]]*\])",
        text, re.DOTALL)
    assert m, (
        "test_can_datalogger.py's standard_frame_end_to_end no longer calls "
        "bfm.send(can_id=..., data=[...]) the way this binding expects — "
        "re-derive the parse, don't hand-edit the expected values here")
    can_id = ast.literal_eval(m.group(1))
    data = ast.literal_eval(m.group(2))
    return can_id, data


def load_can_annotations(capture_json_path: Path) -> List[dict]:
    """Every channel-105/106 ANNOTATIONS-frame annotation from a
    live_capture_client --out JSON, in wire order."""
    doc = json.loads(Path(capture_json_path).read_text())
    anns: List[dict] = []
    for tag, payload in doc["captured"]:
        if tag == 6:  # TAG_ANNOTATIONS
            anns.extend(a for a in payload.get("annotations", [])
                        if a.get("channel_id") in (105, 106))
    anns.sort(key=lambda a: a["ts_start"])
    return anns


def pack_producer_stream(capture_json_path: Path) -> Tuple[bytes, dict, List[dict]]:
    """Recreate the exact producer-to-consumer MessagePack object sequence
    recorded by ``live_capture_client``: HELO followed by every captured frame.

    JSON preserves dict insertion order, and MessagePack maps are unordered by
    the wire contract, so this is semantically the same stream the released
    RouteWave consumer received. HELO_OK is deliberately absent: it travelled
    consumer-to-producer and RW-WS1 replays only the producer half.
    """
    doc = json.loads(Path(capture_json_path).read_text())
    frames = [[0, doc["helo"]], *doc["captured"]]  # TAG_HELO == 0
    wire = b"".join(msgpack.packb(frame, use_bin_type=True) for frame in frames)
    return wire, doc, load_can_annotations(capture_json_path)


def run_routewave_checker(capture_json_path: Path,
                          checker_path: Path) -> Tuple[bool, str, Optional[dict]]:
    """Run the released RW-WS1 C++ caller surface over this same capture.

    INPUT DOMAIN: a ``live_capture_client`` JSON record containing one HELO and
    zero or more producer frames. PROHIBITIONS: a missing/non-executable
    checker, non-zero process exit, non-JSON/reject verdict, absent CAN lanes,
    parser errors, or any dropped/duplicated CAN annotation kind is a hard
    failure. An ``accept`` string alone is intentionally insufficient.
    """
    checker = Path(checker_path)
    if not checker.is_file():
        return False, f"checker not found: {checker}", None

    wire, doc, anns = pack_producer_stream(capture_json_path)
    with tempfile.NamedTemporaryFile(prefix="ds_p3_148_", suffix=".p2c.mp") as stream:
        stream.write(wire)
        stream.flush()
        try:
            proc = subprocess.run(
                [str(checker), "stream-conformance", stream.name,
                 "--name", "DS-P3.148", "--expect", "accept"],
                capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"checker could not run: {exc}", None

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, (f"checker stdout was not an RW-WS1 JSON verdict "
                       f"(rc={proc.returncode}, stdout={proc.stdout[:200]!r}, "
                       f"stderr={proc.stderr[:200]!r})"), None
    if proc.returncode != 0:
        return False, (f"checker exited {proc.returncode}: verdict="
                       f"{result.get('verdict')!r} field="
                       f"{result.get('violation_field')!r} diagnostic="
                       f"{result.get('diagnostic')!r}"), result
    if result.get("checker") != "routewave_cli stream-conformance":
        return False, f"unexpected checker identity: {result.get('checker')!r}", result
    if result.get("verdict") != "accept":
        return False, f"checker verdict is {result.get('verdict')!r}, not 'accept'", result

    channels = {c.get("id"): c.get("name") for c in result.get("channels", [])}
    expected_channels = {105: "can_msg", 106: "can_frame"}
    for channel_id, name in expected_channels.items():
        if channels.get(channel_id) != name:
            return False, (f"checker did not materialise channel {channel_id} "
                           f"as {name!r}: observed={channels.get(channel_id)!r}"), result

    expected_kinds = Counter(a["kind"] for a in anns)
    observed_kinds = result.get("annotation_kinds", {})
    for kind, count in sorted(expected_kinds.items()):
        if observed_kinds.get(kind) != count:
            return False, (f"checker annotation kind {kind!r} count mismatch: "
                           f"observed={observed_kinds.get(kind)!r} expected={count}"), result

    all_anns = [a for tag, payload in doc["captured"] if tag == 6
                for a in payload.get("annotations", [])]
    observed = result.get("observed", {})
    if observed.get("annotations") != len(all_anns):
        return False, (f"checker materialised {observed.get('annotations')!r} annotations; "
                       f"capture carried {len(all_anns)}"), result
    if observed.get("parse_errors") != 0:
        return False, f"checker reports parse_errors={observed.get('parse_errors')!r}", result

    digest = hashlib.sha256(wire).hexdigest()
    return True, (f"accept; RouteWave {result.get('routewave_version', '?')}; "
                  f"channels 105/106; {len(anns)} CAN annotations; "
                  f"p2c_sha256={digest}"), result


def expected_ch105_tokens(can_id: int, data: List[int]) -> List[Tuple[str, Optional[dict]]]:
    """Mirror test_can_datalogger.py's `expected` token list for a standard
    (non-extended) data frame: (T_EVENT FRAME_BEGIN), (T_CTRL), 2x(T_ID),
    Nx(T_DATA), 2x(T_CRC), (T_EVENT ACK_OBS), (T_EVENT FRAME_END) — see
    can_reassembler.py's wire-layout docstring for the STANDARD ID split
    (byte0 = 00000|id[10:8], byte1 = id[7:0])."""
    id_hi = (can_id >> 8) & 0x07
    id_lo = can_id & 0xFF
    out: List[Tuple[str, Optional[dict]]] = [
        ("can_frame_begin", None),
        ("can_control", {"ide": False, "rtr": False, "dlc": len(data)}),
        ("can_id", {"id_byte": id_hi}),
        ("can_id", {"id_byte": id_lo}),
    ]
    out += [("can_data", {"data": b}) for b in data]
    # CRC bytes are stimulus-computed (a real CRC-15 engine), not a hand
    # literal in the sim oracle either (it reads meta["crc"] back from the
    # BFM) -- this binding checks presence/count/ordering, not the value.
    out += [("can_crc", None), ("can_crc", None)]
    out += [("can_ack_observed", None), ("can_frame_end", None)]
    return out


def check(anns: List[dict], can_id: int, data: List[int]) -> Tuple[bool, str]:
    """Byte-exact + order-exact check. Returns (ok, detail)."""
    # Filter the lifecycle "enabled" event, matching test_can_datalogger.py's own
    # `frame = [m for m in decode(got) if not (m[0]==T_EVENT and m[1]==EV_ENABLED)]`
    # -- it is real and correctly emitted, just not part of the frame-shape binding.
    ch105 = [a for a in anns if a["channel_id"] == 105 and a["kind"] != "can_enabled"]
    ch106 = [a for a in anns if a["channel_id"] == 106]

    expected = expected_ch105_tokens(can_id, data)
    observed_kinds = [a["kind"] for a in ch105]
    expected_kinds = [k for k, _ in expected]
    if observed_kinds != expected_kinds:
        return False, (f"channel-105 kind sequence mismatch:\n"
                        f"  observed={observed_kinds}\n  expected={expected_kinds}")

    for a, (kind, exp_fields) in zip(ch105, expected):
        if exp_fields is None:
            continue
        for key, val in exp_fields.items():
            got = a["payload"].get(key)
            if got != val:
                return False, (f"{kind} field {key!r} mismatch: "
                                f"observed={got!r} expected={val!r}")

    if not ch106:
        return False, "no reassembled can_frame (channel 106) annotation observed"
    frame: dict[str, Any] = ch106[-1]["payload"]
    if frame.get("id") != can_id:
        return False, f"reassembled id mismatch: observed={frame.get('id')!r} expected={can_id:#x}"
    if frame.get("data") != data:
        return False, f"reassembled data mismatch: observed={frame.get('data')!r} expected={data!r}"
    if frame.get("dlc") != len(data):
        return False, f"reassembled dlc mismatch: observed={frame.get('dlc')!r} expected={len(data)}"
    if frame.get("ack") is not True:
        return False, f"reassembled ack mismatch: observed={frame.get('ack')!r} expected=True"
    if frame.get("errors"):
        return False, f"reassembled frame carries errors: {frame.get('errors')!r}"

    return True, "OK"


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: can_live_capture_check.py <capture.json> "
              "[--routewave-checker <routewave_cli>]", file=sys.stderr)
        return 2
    if len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] != "--routewave-checker"):
        print("usage: can_live_capture_check.py <capture.json> "
              "[--routewave-checker <routewave_cli>]", file=sys.stderr)
        return 2
    capture_path = Path(argv[0])
    can_id, data = parse_baseline_frame()
    anns = load_can_annotations(capture_path)
    ok, detail = check(anns, can_id, data)
    print(f"baseline (parsed from {CAN_SIM_ORACLE.name}): "
          f"ID={can_id:#x} data={[hex(x) for x in data]}")
    print(f"result: {'PASS' if ok else 'FAIL'} — {detail}")
    if not ok:
        return 1
    if len(argv) == 3:
        rw_ok, rw_detail, _ = run_routewave_checker(capture_path, Path(argv[2]))
        print(f"RouteWave RW-WS1: {'PASS' if rw_ok else 'FAIL'} — {rw_detail}")
        return 0 if rw_ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
