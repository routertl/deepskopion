#!/usr/bin/env python3
"""DS-P1.59 — judge a LIVE DE25 daemon capture against the DE-C0 oracle.

This is the daemon-live counterpart to the board-free
``capture_correctness.py`` and to DS-P2.101's stop-daemon driver. The stimulus
is armed on the board by the SHARED production armer
(``de_c0_production_armer``) WHILE the production ``python3 -m sw.stream``
daemon keeps running; a host-side ``live_capture_client`` records the daemon's
wire (HELO + ANNOTATIONS/EDGE_EVENTS) into a ``--out`` JSON per protocol. This
module judges that JSON with the EXACT SAME oracle the board-free harness uses —
only the transport changed (a real TCP daemon on real silicon instead of an
in-process ``serve_one`` over a FakeDdrMmap ring):

  * exact decoded payload + ordering, read from the captured wire frames through
    ``capture_correctness``'s own ``_extract_*`` readers and compared to the
    same ``CaptureCase.expected`` — which ``test_capture_correctness.py`` binds
    to the Xilinx sim-oracle literals (``test_datalogger_to_ddr.py``), so the live
    observation is compared EXACTLY against the Xilinx baseline, incl. the
    DS-P1.58 edge baseline (``test_edge_to_ddr`` / ``EDGE_TRANSITIONS``);
  * WS-C0 ``check_session`` ACCEPTs the reconstructed producer session (the
    vendored contract both products are bound to);
  * the released RW-WS1 C++ checker (``routewave_cli stream-conformance``)
    ACCEPTs the same producer stream and materialises this case's channel/kind
    counts — the DS-P3.148 fail-closed invocation, generalised from CAN-only
    channels to each DE-C0 case. Fail-closed: a missing/incapable checker, a
    non-JSON/reject verdict, or a kind/channel mismatch is a hard FAIL, never a
    silent pass. Clause 4 REQUIRES the released checker, so OMITTING
    ``--routewave-checker`` is not a pass either: the case is BLOCKED (clause 4
    unevaluated), which is non-PASS and exits non-zero. There is no green
    "boundary" mode — a bench with no built checker cannot reach PASS.

Fail-closed overall: any non-PASS case (FAIL or BLOCKED), or an empty case set,
exits non-zero — a capture check that confirmed nothing is missing evidence,
not success.

PROVENANCE — this module is the JUDGE for a daemon-live capture; it is NOT
itself a silicon witness. Its input is ``live_capture_client --out`` JSON. As of
DS-P1.59 the real DE25 silicon artifacts ARE committed in the repo, under
silicon evidence captures (``cap_uart.live.json``,
``cap_i2c.live.json``, ``cap_spi.live.json``, ``cap_edge.live.json`` — each a
``live_capture_client --live-only`` capture off the production daemon on fresh
silicon; the DS-T1.9 SIGBUS fix made the daemon survive the ring-drain that had
crashed it on 2026-08-28). Those files are re-judged by
``sw/stream/tests/test_de_c0_silicon_capture.py``, which binds THIS judge to the
committed captures: deleting or mutating one turns that suite RED. The tests in
``sw/stream/tests/test_de_c0_live_witness.py`` additionally exercise this judge
over oracle wire round-tripped through JSON — a transport/round-trip conformance
of the JUDGE, green with no board — and are a separate, board-free layer.

Usage (host, after arming each protocol on the board and capturing its wire)::

    python3 -m sw.stream.testbench.de_c0_live_witness \\
        --capture uart=/tmp/uart.json --capture i2c=/tmp/i2c.json \\
        --capture spi=/tmp/spi.json  --capture edge=/tmp/edge.json \\
        [--routewave-checker <path/to/routewave_cli>]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import msgpack

from sw.stream import channels as chans
from sw.stream import protocol as proto
from sw.stream.testbench import capture_correctness as cc
from sw.stream.testbench.capture_correctness import C  # vendored WS-C0 contract

WireFrame = Tuple[int, dict]

# case name -> the annotation channel whose name the RW-WS1 checker must
# materialise. Edge is EDGE_EVENTS (no annotation channel), bound by count only.
_CHANNEL_NAME = {c["id"]: c["name"] for c in chans.CHANNELS}


def load_capture(path: Path) -> Tuple[dict, List[WireFrame]]:
    """Return ``(helo, captured)`` from a ``live_capture_client --out`` JSON.
    ``captured`` is ``[(tag, payload), ...]`` in wire order — the shape the
    ``capture_correctness`` extractors and WS-C0 ``check_session`` expect."""
    doc = json.loads(Path(path).read_text())
    captured = [(int(tag), payload) for tag, payload in doc["captured"]]
    return doc["helo"], captured


def _reconstruct_session(helo: dict, captured: List[WireFrame]) -> list:
    """The bidirectional session WS-C0 ``check_session`` wants:
    ``[(direction, tag, payload), ...]`` — HELO, our HELO_OK, then the wire."""
    session = [("p2c", proto.TAG_HELO, helo),
               ("c2p", proto.TAG_HELO_OK, {"consumer": "de-c0-live-witness"})]
    session += [("p2c", tag, payload) for tag, payload in captured]
    return session


def _pack_producer_stream(helo: dict, captured: List[WireFrame]) -> bytes:
    """The producer→consumer (p2c) MessagePack byte stream a released RouteWave
    consumer receives: HELO followed by every captured frame. HELO_OK is
    deliberately absent (it travels consumer→producer)."""
    frames = [[proto.TAG_HELO, helo], *[[t, p] for t, p in captured]]
    return b"".join(msgpack.packb(f, use_bin_type=True) for f in frames)


def run_rw_ws1(helo: dict, captured: List[WireFrame], case: "cc.CaptureCase",
               checker_path: Path) -> Tuple[bool, str]:
    """DS-P3.148 fail-closed invocation, generalised to ``case``.

    PROHIBITIONS (each a hard FAIL): a missing/non-executable checker, a checker
    without the ``stream-conformance`` surface, non-zero exit, a non-JSON or
    reject verdict, a wrong checker identity, an expected annotation channel not
    materialised as its contract name, any annotation-kind count mismatch, an
    annotation total mismatch, or a non-zero parse-error count. An ``accept``
    string alone is intentionally insufficient."""
    checker = Path(checker_path)
    if not checker.is_file():
        return False, f"RW-WS1 checker not found: {checker}"

    wire = _pack_producer_stream(helo, captured)
    with tempfile.NamedTemporaryFile(prefix="ds_p1_59_", suffix=".p2c.mp") as f:
        f.write(wire)
        f.flush()
        try:
            pr = subprocess.run(
                [str(checker), "stream-conformance", f.name,
                 "--name", f"DS-P1.59/{case.name}", "--expect", "accept"],
                capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"RW-WS1 checker could not run: {exc}"

    try:
        res = json.loads(pr.stdout)
    except json.JSONDecodeError:
        return False, (f"RW-WS1 stdout not a JSON verdict (rc={pr.returncode}, "
                       f"stdout={pr.stdout[:160]!r}, stderr={pr.stderr[:160]!r})")
    if pr.returncode != 0:
        return False, (f"RW-WS1 exited {pr.returncode}: verdict="
                       f"{res.get('verdict')!r} field={res.get('violation_field')!r}")
    if res.get("checker") != "routewave_cli stream-conformance":
        return False, f"RW-WS1 unexpected checker identity: {res.get('checker')!r}"
    if res.get("verdict") != "accept":
        return False, f"RW-WS1 verdict is {res.get('verdict')!r}, not 'accept'"

    # Annotation-only channels the checker must have materialised as their name.
    anns = cc._wire_annotations(captured, case.channel_id)
    if case.channel_id in _CHANNEL_NAME and anns:
        obs_channels = {c.get("id"): c.get("name") for c in res.get("channels", [])}
        want = _CHANNEL_NAME[case.channel_id]
        obs = obs_channels.get(case.channel_id)
        # DS-P3.155: accept both legacy "i2c_frame" and per-datalogger "i2c_frame:4"
        if obs != want and not (case.channel_id == 101 and obs in ("i2c_frame", "i2c_frame:4")):
            return False, (f"RW-WS1 did not materialise channel {case.channel_id} "
                           f"as {want!r}: observed={obs!r}")
        want_kinds = Counter(a["kind"] for a in anns)
        obs_kinds = res.get("annotation_kinds", {})
        for kind, n in sorted(want_kinds.items()):
            if obs_kinds.get(kind) != n:
                return False, (f"RW-WS1 kind {kind!r} count mismatch: "
                               f"observed={obs_kinds.get(kind)!r} expected={n}")

    all_anns = [a for tag, payload in captured if tag == proto.TAG_ANNOTATIONS
                for a in payload.get("annotations", [])]
    obs = res.get("observed", {})
    if obs.get("annotations") != len(all_anns):
        return False, (f"RW-WS1 materialised {obs.get('annotations')!r} annotations; "
                       f"capture carried {len(all_anns)}")
    if obs.get("parse_errors") not in (0, None) and obs.get("parse_errors") != 0:
        return False, f"RW-WS1 parse_errors={obs.get('parse_errors')!r}"
    return True, f"accept; RouteWave {res.get('routewave_version', '?')}"


@dataclass
class LiveVerdict:
    name: str
    protocol: str
    status: str                 # PASS | FAIL
    detail: str = ""
    expected: Any = None
    observed: Any = None
    wsc0: str = ""              # ACCEPT | REJECT: ...
    rw_ws1: str = ""            # PASS/FAIL/SKIP detail
    fields: List[str] = field(default_factory=list)


def judge_case(case: "cc.CaptureCase", helo: dict, captured: List[WireFrame],
               *, expected: Any = None,
               checker_path: Optional[Path] = None,
               contract_revision: Optional[str] = None) -> LiveVerdict:
    """Judge ONE live capture against ``case`` — the same oracle as
    ``capture_correctness.run_case``, over real-daemon wire frames.

    ``expected`` overrides ``case.expected`` (the clause-6 mutation seam: corrupt
    the expectation while the capture is unchanged and the verdict must go FAIL).

    ``contract_revision`` is the wave_stream_v1 revision the capture was
    recorded under; None means the current one (a capture taken now). Committed
    evidence passes its own revision, so a rule added later does not turn a
    capture that was conformant when it was recorded into a FAIL.
    """
    exp = case.expected if expected is None else expected

    # (a) WS-C0 conformance of the reconstructed producer session.
    session = _reconstruct_session(helo, captured)
    result = C.check_session(session, revision=contract_revision or C.CONTRACT_REVISION)
    if not result.accepted:
        return LiveVerdict(case.name, case.protocol, "FAIL",
                           "capture is NOT WS-C0 conformant", expected=exp,
                           wsc0="REJECT: " + "; ".join(map(str, result.failures)),
                           fields=list(result.fields))

    # (b) exact decoded payload + ordering, read from the captured wire frames.
    #
    # ``case.compare_live`` is None for uart/i2c/spi -> plain exact equality,
    # unchanged. ``edge_scl`` supplies ``compare_edge_arm_anchored``, because its
    # payload IS timestamps and on silicon those carry a free-running epoch the
    # host cannot read; that comparator still requires exact levels, exact
    # nanosecond deltas and an exact post-arm transition COUNT. See its docstring
    # for the full DS-P1.44 rationale and for why the RTL alternative was not
    # taken. Board-free ``run_case`` is untouched and stays exactly equal.
    observed = case.extract(captured, case.channel_id, helo["sample_clk_hz"])
    if case.compare_live is None:
        matched = observed == exp
        detail = ("no expected data frames on the wire (nothing decoded)"
                  if not observed else
                  "decoded wire payload does not match expected, in order")
    else:
        matched, detail = case.compare_live(observed, exp)
    if not matched:
        return LiveVerdict(case.name, case.protocol, "FAIL", detail,
                           expected=exp, observed=observed, wsc0="ACCEPT")

    # (c) RW-WS1 released C++ checker. Clause 4 REQUIRES it, so omitting the
    # checker is NOT a pass — it is BLOCKED (clause 4 unevaluated), which
    # run_all counts as non-PASS and exits non-zero. Fail-closed: the only mode
    # that can run on a bench without a built checker must never report PASS,
    # or "the detector nobody called" turns a four-case run green (the DS-P1.59
    # landing-refusal defect, and the DS-P3.148 CAN omit-and-PASS shape).
    if checker_path is None:
        return LiveVerdict(case.name, case.protocol, "BLOCKED",
                           "RW-WS1 not run: clause 4 requires --routewave-checker; "
                           "a capture that skipped the released checker is not a pass",
                           expected=exp, observed=observed, wsc0="ACCEPT",
                           rw_ws1="BLOCKED (no --routewave-checker; clause 4 unevaluated)")

    ok, rw_detail = run_rw_ws1(helo, captured, case, checker_path)
    rw = ("PASS: " if ok else "FAIL: ") + rw_detail
    if not ok:
        return LiveVerdict(case.name, case.protocol, "FAIL",
                           "RW-WS1 released checker did not accept",
                           expected=exp, observed=observed, wsc0="ACCEPT",
                           rw_ws1=rw)

    # For a plain exact-equality case ``detail`` is the (unused) mismatch text, so
    # report the element count; for a ``compare_live`` case it is the comparator's
    # own PASS reasoning, which is the honest description (e.g. edge_scl observes
    # 6 transitions but matches a 4-transition arm-anchored vector — reporting
    # "6 elements exact" would over-claim).
    how = (f"{len(observed)} decoded wire element(s) exact"
           if case.compare_live is None else detail)
    return LiveVerdict(case.name, case.protocol, "PASS",
                       f"{how} + WS-C0 ACCEPT + RW-WS1 accept",
                       expected=exp, observed=observed, wsc0="ACCEPT", rw_ws1=rw)


def run_all(captures: Dict[str, Path],
            checker_path: Optional[Path] = None,
            contract_revision: Optional[str] = None) -> Tuple[List[LiveVerdict], int]:
    """Judge every DE-C0 case whose capture is provided. Fail-closed: a missing
    capture for any known case, or any non-PASS verdict, makes the exit 1."""
    cases = {c.name: c for c in cc.known_cases()}
    # protocol name -> case name (uart_marker/i2c_write/spi_mosi/edge_scl)
    by_proto = {c.protocol: c for c in cases.values()}
    verdicts: List[LiveVerdict] = []
    for proto_name, case in sorted(by_proto.items()):
        path = captures.get(proto_name) or captures.get(case.name)
        if path is None:
            verdicts.append(LiveVerdict(case.name, case.protocol, "FAIL",
                                        "no live capture provided for this case"))
            continue
        helo, captured = load_capture(Path(path))
        verdicts.append(judge_case(case, helo, captured, checker_path=checker_path,
                                   contract_revision=contract_revision))
    code = 0 if verdicts and all(v.status == "PASS" for v in verdicts) else 1
    return verdicts, code


def _parse_captures(pairs: List[str]) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--capture expects proto=PATH, got {pair!r}")
        proto_name, path = pair.split("=", 1)
        out[proto_name] = Path(path)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="DE-C0 live-daemon capture witness")
    p.add_argument("--capture", action="append", default=[], metavar="proto=PATH",
                   help="live_capture_client --out JSON for a protocol "
                        "(uart|i2c|spi|edge). Repeatable.")
    p.add_argument("--routewave-checker", type=Path, default=None,
                   help="released routewave_cli with the stream-conformance "
                        "surface (RW-WS1). REQUIRED for a PASS (clause 4); "
                        "omitting it makes every case BLOCKED and exits non-zero.")
    p.add_argument("--contract-revision", default=None,
                   help="wave_stream_v1 contract revision the capture was recorded "
                        "under (default: current CONTRACT_REVISION).")
    p.add_argument("--out", type=Path, default=None, help="write verdict JSON")
    args = p.parse_args(argv)

    captures = _parse_captures(args.capture)
    verdicts, code = run_all(captures, args.routewave_checker,
                             contract_revision=args.contract_revision)

    doc = {
        "harness": "de_c0_live_witness",
        "board_free": False,
        "contract": "wave_stream_v1",
        "rw_ws1_checker": str(args.routewave_checker) if args.routewave_checker else None,
        "summary": {"PASS": sum(v.status == "PASS" for v in verdicts),
                    "FAIL": sum(v.status == "FAIL" for v in verdicts),
                    "BLOCKED": sum(v.status == "BLOCKED" for v in verdicts)},
        "cases": [vars(v) for v in verdicts],
    }
    if args.out:
        args.out.write_text(json.dumps(doc, indent=2))

    print("=== DE-C0 LIVE capture witness (daemon-live; DS-P1.59) ===")
    for v in verdicts:
        print(f"  [{v.status:<4}] {v.protocol:<5} {v.name:<14} {v.detail}")
        print(f"          WS-C0={v.wsc0}  RW-WS1={v.rw_ws1}")
        if v.status != "PASS":
            print(f"          expected={v.expected!r}")
            print(f"          observed={v.observed!r}")
    print(f"  PASS={doc['summary']['PASS']} FAIL={doc['summary']['FAIL']} "
          f"BLOCKED={doc['summary']['BLOCKED']} total={len(verdicts)}")
    return code


if __name__ == "__main__":
    sys.exit(main())
