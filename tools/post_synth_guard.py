#!/usr/bin/env python3
"""Run the DE25 post-synth guard tests against the build trees rr synth made.

DS-P3.193.  Some guards read files that exist only after ``rr synth run`` of a
DE25 target: the IOPLL primitive that fixes the fabric clock dividers
(DS-P2.155, M=64 / C=32, 50 MHz -> 100 MHz) is generated into
``build/rr_ip_regen/`` (DS-P2.196).  The commit gate has no such tree, so those
tests skip there.  This runner finds the DE25 build trees the build queue
produced and runs those tests against each one, so a regenerated primitive
that drifts is reported after the synth that made it.

Selection: every DE25 queue job (target name ``de25*``, stage synth/impl/
bitstream, state done or failed) that finished within ``--since-hours``, one
check per build directory.  When none finished in the window the newest such
job is checked again.  A build directory that no longer holds a PLL tree is
listed and not checked.

A tree that is checked must PASS every post-synth test: a skip there means the
test did not see the tree, which is a red, not a pass.

Exit codes: 0 every checked tree passed; 1 a test failed or skipped on a tree;
3 no DE25 build tree exists to check (nothing was tested); 2 usage or queue
error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Tests that read a DE25 build tree. Each must PASS against a tree it is given.
POST_SYNTH_TESTS = (
    "tools/tests/test_de25_fabric_pll_100mhz_ds_p2_155.py::"
    "test_generated_pll_primitive_locks_50mhz_ref_to_100mhz_out",
)

# Where a build directory keeps the fabric IOPLL generation root, newest flow
# first: rr regenerates it (DS-P2.196); older checkouts carry it under ip/.
PLL_ROOTS = (
    Path("build/rr_ip_regen/ip/de25_fabric_pll"),
    Path("ip/de25_fabric_pll"),
)
PLL_PRIMITIVE_GLOB = "de25_fabric_pll/altera_iopll_*/synth/de25_fabric_pll_altera_iopll_*.v"

STAGES = {"synth", "impl", "bitstream"}
STATES = {"done", "failed"}

NO_TREE = 3


@dataclass
class Job:
    id: str
    stage: str
    state: str
    exit_code: object
    target: str
    finished_at: float
    cwd: str


def de25_jobs(queue: dict) -> list[Job]:
    """DE25 build jobs from ``rr queue status --all --json`` output, newest first."""
    out = []
    for j in queue.get("data", {}).get("jobs", []):
        target = str(j.get("target_name") or "")
        if not target.startswith("de25"):
            continue
        if j.get("stage") not in STAGES or j.get("state") not in STATES:
            continue
        if not j.get("cwd") or not j.get("finished_at"):
            continue
        out.append(Job(str(j["id"]), j["stage"], j["state"], j.get("exit_code"),
                       target, float(j["finished_at"]), str(j["cwd"])))
    return sorted(out, key=lambda job: job.finished_at, reverse=True)


def select(jobs: list[Job], now: float, since_hours: float) -> list[Job]:
    """Newest job per build directory within the window, else the newest job."""
    window = [j for j in jobs if now - j.finished_at <= since_hours * 3600]
    chosen, seen = [], set()
    for job in window or jobs[:1]:
        if job.cwd not in seen:
            seen.add(job.cwd)
            chosen.append(job)
    return chosen


def pll_root(cwd: str) -> Path | None:
    """The build directory's IOPLL generation root that holds a primitive."""
    for rel in PLL_ROOTS:
        root = Path(cwd) / rel
        if any(root.glob(PLL_PRIMITIVE_GLOB)):
            return root
    return None


def run_tests(root: Path, python: str = sys.executable) -> tuple[bool, str]:
    """Run POST_SYNTH_TESTS against ``root``. True only if every one PASSED."""
    # DS-P3.311: the cron reads the shared checkout; a missing fetched package is a named red, never a sync there.
    env = dict(os.environ, DS_FABRIC_PLL_DIR=str(root), PYTHONDONTWRITEBYTECODE="1", DS_FETCH_CHECK_ONLY="1")
    cmd = [python, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider", *POST_SYNTH_TESTS]
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                          errors="replace", check=False)
    log = proc.stdout + proc.stderr
    passed = {line.split(" ", 1)[1].strip() for line in log.splitlines()
              if line.startswith("PASSED ")}
    ok = proc.returncode == 0 and all(t in passed for t in POST_SYNTH_TESTS)
    return ok, log


def load_queue(path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    proc = subprocess.run(["rr", "queue", "status", "--all", "--json"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          errors="replace", check=False, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"rr queue status --all --json exited {proc.returncode}: "
                           f"{proc.stderr.strip()[-300:]}")
    return json.loads(proc.stdout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since-hours", type=float, default=26.0,
                    help="check every DE25 build directory with a job finished this recently")
    ap.add_argument("--queue-json", help="read queue history from this file instead of rr")
    ap.add_argument("--build-dir", action="append", default=[],
                    help="check this build directory (repeatable); skips the queue")
    ap.add_argument("--digest", help="also write the report to this file")
    args = ap.parse_args(argv)

    if args.build_dir:
        chosen = [Job("-", "-", "-", None, "-", time.time(), d) for d in args.build_dir]
    else:
        try:
            chosen = select(de25_jobs(load_queue(args.queue_json)), time.time(),
                            args.since_hours)
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"post-synth guard: ERROR reading the build queue: {exc}")
            return 2

    lines, reds, checked = [], 0, 0
    for job in chosen:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.finished_at))
        head = f"job {job.id} {job.stage} {job.state} {job.target} {when} {job.cwd}"
        root = pll_root(job.cwd)
        if root is None:
            lines.append(f"NO-TREE  {head}: no IOPLL primitive under {', '.join(map(str, PLL_ROOTS))}")
            continue
        checked += 1
        ok, log = run_tests(root)
        lines.append(f"{'PASS' if ok else 'RED '}     {head} ({root})")
        if not ok:
            reds += 1
            lines.extend("    " + line for line in log.strip().splitlines()[-25:])

    if checked == 0:
        verdict, rc = "NO DE25 BUILD TREE to check (nothing tested)", NO_TREE
    elif reds:
        verdict, rc = f"RED: {reds} of {checked} DE25 build tree(s) failed the post-synth tests", 1
    else:
        verdict, rc = f"PASS: {checked} DE25 build tree(s), every post-synth test passed", 0
    report = "\n".join([f"post-synth guard (DS-P3.193): {verdict}", *lines])
    print(report)
    if args.digest:
        Path(args.digest).parent.mkdir(parents=True, exist_ok=True)
        Path(args.digest).write_text(report + "\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main())
