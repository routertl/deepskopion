#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Run the DS-X3 active-target simulation baseline and reject every SKIP.

``rr sim run`` correctly makes a failed cocotb test fail the tag, but cocotb
normally treats an explicitly skipped scenario as a successful process.  That
is useful for exploratory suites and wrong for DS-X3: a missing scenario must
not turn the pre-hardware baseline green.  This front door therefore checks
every per-module result table after the queued run completes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import target_file  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
TAG = "xilinx_baseline"
# DS-P2.273: the tag lives on one Xilinx target. Without --target, rr resolved it on project.yml's active target
# (de25_datalogger), which has no such tag, so the baseline could not run. DS-P1.103: that target is the shipped
# zybo_datalogger (canfd_demo, which carried the same 22-module tag, moved to bench/targets/).
TARGET_NAME = "zybo_datalogger"


def _tag_modules() -> list[str]:
    target = yaml.safe_load(
        target_file(TARGET_NAME, ROOT).read_text(encoding="utf-8")
    )
    return target["simulation"]["tags"][TAG]


def _result_problem(result: dict, module: str) -> str | None:
    if result.get("module") != module:
        return f"module field is {result.get('module')!r}"
    total = result.get("total")
    if not isinstance(total, int) or total <= 0:
        return f"invalid total {total!r}"
    for field in ("failed", "errors", "skipped", "undetermined", "undetermined_xpass"):
        if result.get(field, 0) != 0:
            return f"{field}={result.get(field)!r}"
    if result.get("passed") != total:
        return f"passed={result.get('passed')!r}, total={total!r}"
    statuses = [test.get("status") for test in result.get("tests", [])]
    if len(statuses) != total or any(status != "pass" for status in statuses):
        return f"test statuses={statuses!r}"
    if not result.get("session_id"):
        return "missing session_id"
    return None


def main() -> int:
    modules = _tag_modules()
    env = os.environ.copy()
    # This gate is deliberately never an inline simulator invocation.  --wait
    # is equally important: queue submission is not evidence of a pass.
    env["RR_QUEUE_SIMS"] = "1"
    command = [
        "rr",
        "sim",
        "run",
        "--target",
        TARGET_NAME,
        "--tag",
        TAG,
        "--wait",
        "--attest-no-defects-found",
        (
            "DS-X3 active-target baseline: deterministic protocol, gather, "
            "regbank, framer, loss/backpressure and DDR attacks found no "
            "additional DUT defect"
        ),
    ]

    started_at = time.time()
    proc = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
    return_code = proc.wait()
    if return_code != 0:
        print(
            f"DS-X3 FAIL: queued rr simulation gate exited {return_code}.",
            file=sys.stderr,
        )
        return return_code

    # Successful ``rr queue wait`` intentionally prints only job completion,
    # not the child's captured log.  Certify the machine-readable artifacts rr
    # writes for every module instead.  Freshness plus one shared session_id
    # proves these are all from this tag invocation, not stale prior greens.
    results = []
    problems = []
    for module in modules:
        path = ROOT / "sim" / "results" / module / "latest.json"
        if not path.is_file():
            problems.append(f"{module}: missing {path.relative_to(ROOT)}")
            continue
        if path.stat().st_mtime < started_at - 1.0:
            problems.append(f"{module}: stale latest.json (predates this gate)")
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{module}: unreadable latest.json: {exc}")
            continue
        if problem := _result_problem(result, module):
            problems.append(f"{module}: {problem}")
        results.append(result)

    sessions = {result.get("session_id") for result in results}
    if len(sessions) != 1:
        problems.append(
            f"results span {len(sessions)} session ids: "
            f"{sorted(map(repr, sessions))!r}"
        )
    if problems:
        print(
            "DS-X3 FAIL: every scenario must execute in one fresh session and "
            "pass; " + "; ".join(problems),
            file=sys.stderr,
        )
        return 1

    total = sum(result["total"] for result in results)
    print(
        f"DS-X3 PASS: {len(modules)} modules, {total} scenarios, "
        "0 FAIL, 0 SKIP."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
