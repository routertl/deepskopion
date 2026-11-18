#!/usr/bin/env python3
"""PS-side self-test orchestrator (DS-P3.44).

Runs every scenario registered in `scenarios.py`, emits a verdict JSON,
prints a PASS/FAIL summary on stdout. Designed to run from the Linux
shell on the booted Zybo Z7-20:

    python3 -m sw.stream.testbench.run_self_test

Or directly:

    /usr/local/share/deepskopion/testbench/run_self_test.py

Exit code:
    0 — every scenario PASS (zero FAIL and zero SKIP).
    1 — any FAIL or SKIP.
    2 — runtime error (regbank open failed, etc.). Distinguishes
        "test red" from "harness broken."

Output:
    stdout — one line per scenario + final summary.
    JSON  — /tmp/self_test/<utc-iso>.json (full per-scenario record,
            including expected vs got dicts for failures).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

from sw.stream.testbench.emio_inject import SelfTestBench
from sw.stream.testbench.provenance import (
    abi_verdict,
    clock_is_trustworthy,
    gather_provenance,
    read_deploy_stamp,
    testbench_git_rev,
    verdict_filename,
)
from sw.stream.testbench.scenarios import Scenario, Verdict, all_scenarios, make_oracle


def _utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _summarise(verdicts: list[tuple[Scenario, Verdict]]) -> dict:
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for _, v in verdicts:
        counts[v.status] = counts.get(v.status, 0) + 1
    return counts


def run_all(out_dir: Path = Path("/tmp/self_test")) -> int:
    """Run every scenario; write verdict JSON; return exit code."""
    log = logging.getLogger("self_test")

    log.info("opening bench (regbank + UART + I2C + SPI)…")
    try:
        bench = SelfTestBench()
    except Exception as exc:
        log.error("bench open failed: %s", exc)
        traceback.print_exc()
        return 2

    # ── RTL-P2.654: ABI-contract self-check BEFORE any scenario runs ──
    # A bitstream whose RTL contract requires the new driver sequence, run
    # against an old testbench (or vice-versa), produces pure version-skew.
    # If we let scenarios run they'd report FAIL and the skew would be
    # mis-filed as a DUT defect. Catch it up front and exit 2 (harness
    # broken), NEVER 1 (test red) — a harness that can't tell it's testing a
    # mismatched DUT fabricates bugs.
    bitstream_identity = bench.regbank.identity()
    abi = abi_verdict(bench.regbank.contract_abi_ver())
    if abi["state"] == "mismatch":
        bench.close()
        log.error("ABI MISMATCH — %s", abi["detail"])
        out_dir.mkdir(parents=True, exist_ok=True)
        clock_ok = clock_is_trustworthy()
        prov = gather_provenance(
            bitstream_identity,
            deploy_stamp=read_deploy_stamp(),
            tb_git_rev=testbench_git_rev(),
            clock_ok=clock_ok,
        )
        path = verdict_filename(
            out_dir, _utc_iso(), clock_ok=clock_ok, monotonic_ns=time.monotonic_ns(),
        )
        path.write_text(json.dumps({
            "result": "ERROR",
            "error": "abi_mismatch",
            "abi": abi,
            "provenance": prov,
            "summary": {"PASS": 0, "FAIL": 0, "SKIP": 0},
            "scenarios": [],
        }, indent=2))
        print(f"\n❌ ERROR: {abi['detail']}")
        print("   The bitstream and this testbench are a mismatched pair —")
        print("   not running scenarios (skew is not a DUT verdict).")
        print(f"   verdict JSON: {path}")
        return 2
    if abi["state"] == "unversioned":
        log.warning("ABI: %s — proceeding (cannot enforce a contract the "
                    "bitstream doesn't carry)", abi["detail"])

    started_at = _utc_iso()
    started_mono = time.monotonic()
    verdicts: list[tuple[Scenario, Verdict]] = []

    try:
        # Oracle is lazily built — only Layer-1 scenarios need it, but
        # constructing it now lets us fail-fast if /dev/mem mapping the
        # ring isn't possible (e.g. permission, missing kernel cmdline
        # mem= reservation).
        try:
            oracle = make_oracle(bench)
        except Exception as exc:
            log.warning("ring oracle unavailable (%s); Layer-1 scenarios will SKIP", exc)
            oracle = None

        for sc in all_scenarios():
            t0 = time.monotonic()
            if sc.layer == 1 and oracle is None:
                v = Verdict.skipped("ring oracle unavailable (Layer 1 needs DDR mmap)")
            else:
                try:
                    v = sc.fn(bench, oracle)
                except Exception as exc:
                    tb = traceback.format_exc().rstrip()
                    v = Verdict.failed(
                        f"scenario raised {type(exc).__name__}: {exc}",
                        got={"traceback": tb},
                    )
            v.duration_s = round(time.monotonic() - t0, 4)
            verdicts.append((sc, v))
            log.info("[%-4s] %-40s %s — %s", v.status, sc.req_id, sc.name, v.detail)
    finally:
        bench.close()

    finished_at = _utc_iso()
    duration_s = round(time.monotonic() - started_mono, 3)
    counts = _summarise(verdicts)

    # Write the verdict JSON. RTL-P3.636/P3.637: the header now self-
    # describes BOTH halves of the matched pair (bitstream identity + this
    # testbench's git_rev + ABI contract + the deploy stamp), and the
    # filename is collision-free + ordered even on an RTC-less board.
    out_dir.mkdir(parents=True, exist_ok=True)
    clock_ok = clock_is_trustworthy()
    provenance = gather_provenance(
        bitstream_identity,
        deploy_stamp=read_deploy_stamp(),
        tb_git_rev=testbench_git_rev(),
        clock_ok=clock_ok,
    )
    out_path = verdict_filename(
        out_dir, started_at, clock_ok=clock_ok, monotonic_ns=time.monotonic_ns(),
    )
    out_path.write_text(json.dumps({
        "started_at":  started_at,
        "finished_at": finished_at,
        "duration_s":  duration_s,
        "provenance":  provenance,
        "abi":         abi,
        "summary":     counts,
        "scenarios": [
            {
                "req_id":     sc.req_id,
                "name":       sc.name,
                "layer":      sc.layer,
                "status":     v.status,
                "detail":     v.detail,
                "duration_s": v.duration_s,
                "expected":   v.expected,
                "got":        v.got,
            }
            for sc, v in verdicts
        ],
    }, indent=2))

    # Console summary.
    print()
    print(f"=== self-test summary ({finished_at}) — {duration_s}s ===")
    for sc, v in verdicts:
        print(f"  [{v.status:<4}] {sc.req_id:<14} {sc.name}")
        if v.status != "PASS":
            print(f"          → {v.detail}")
    print()
    print(f"  PASS={counts['PASS']}  FAIL={counts.get('FAIL', 0)}  "
          f"SKIP={counts.get('SKIP', 0)}  total={len(verdicts)}")
    print(f"  verdict JSON: {out_path}")

    # DS-X5: a skipped capture check is missing evidence, not success.  In
    # particular, failure to construct the DDR oracle used to skip all of
    # Layer 1 and still return zero.  Fail closed on every non-PASS verdict so
    # the process result agrees with the coverage shown in the summary/JSON.
    if counts.get("FAIL", 0) > 0 or counts.get("SKIP", 0) > 0:
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="deepskopion PS-side self-test (DS-P3.44)")
    p.add_argument("--out-dir", type=Path, default=Path("/tmp/self_test"),
                   help="Directory for the verdict JSON. Default: /tmp/self_test")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable DEBUG logging on the bench drivers.")
    args = p.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)-5s %(name)-25s %(message)s",
        datefmt="%H:%M:%S",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    return run_all(args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
