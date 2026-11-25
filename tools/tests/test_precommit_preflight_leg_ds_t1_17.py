# rr unknown-llm (AI-generated)
"""DS-T1.17 — the pre-commit gate runs the golden-path preflight suite, fail-closed.

DS-P2.121 removed CAN datalogger id 20 from datalogger_config.yml while
tools/preflight/tests was red, because nothing ran that suite. Every Zybo build
then stopped in preflight before Vivado, so the DS-P1.72 audit had no Zybo
constraint evidence. hooks/pre-commit.local now runs the suite as its own leg.

This guard executes that leg, cut out of the hook, with a stand-in interpreter
in place of $DS_GUARD_PY. The stand-in records its argv and exits with a chosen
code, so the leg is judged on what it runs and on the commit it refuses,
without nesting pytest or running the hook's other legs.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks/pre-commit.local"

_MARKER = "pre-commit no longer gates on the " + "golden-path preflight suite"


def _leg() -> str:
    lines = HOOK.read_text().splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith("# ── DS-T1.17")), None)
    assert start is not None, f"{_MARKER}: hooks/pre-commit.local has no DS-T1.17 leg"
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("# ── ")), len(lines))
    return "\n".join(lines[start:end])


def _run_leg(work: Path, suite_rc: int) -> tuple[int, list[str], str]:
    work.mkdir()
    calls = work / "argv.log"
    stand_in = work / "python3"
    stand_in.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexit {suite_rc}\n')
    stand_in.chmod(0o755)
    script = f'DS_GUARD_PY="{stand_in}"\n{_leg()}\nexit 0\n'
    out = subprocess.run(["bash", "-c", script], cwd=ROOT, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    argv = calls.read_text().splitlines() if calls.is_file() else []
    return out.returncode, argv, out.stdout


def test_precommit_runs_the_preflight_suite_and_refuses_the_commit_on_red(tmp_path) -> None:
    # suite green -> commit proceeds; suite red (1) or unable to run (2) -> refused
    for suite_rc, want_rc in ((0, 0), (1, 1), (2, 1)):
        rc, argv, out = _run_leg(tmp_path / f"suite_rc_{suite_rc}", suite_rc)
        assert any(call.split()[:2] == ["-m", "pytest"]
                   and "tools/preflight/tests" in call.split() for call in argv), (
            f"{_MARKER}: the DS-T1.17 leg never runs `-m pytest tools/preflight/tests`"
            f" (stand-in saw {argv})\n{out}"
        )
        assert rc == want_rc, (
            f"{_MARKER}: preflight suite exit {suite_rc} -> hook exit {rc}, "
            f"want {want_rc}\n{out}"
        )
