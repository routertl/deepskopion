# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.352: the routertl-vhdl-strict-provisional count can only go down.

Unit tests drive tools/lint/strict_lint_ratchet.py on findings files; the leg tests run the hook's own DS-P2.352 leg,
cut out of hooks/pre-commit.local, in a scratch repository with a stand-in rr whose findings file the test chooses,
so the assertion is on the commit being refused, not on a detector's return value.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "lint" / "strict_lint_ratchet.py"
HOOK = ROOT / "hooks" / "pre-commit.local"
LEG_START = "# ── DS-P2.352 — the strict lint count can only go down ──"
PROFILE = "routertl-vhdl-strict-provisional"
SHA = "a" * 64


def findings(*pairs) -> dict:
    return {"profile": PROFILE, "findings": [{"file": f, "rule_id": r, "line": 1} for f, r in pairs]}


def baseline(path: Path, counts: dict, sha: str = SHA) -> Path:
    path.write_text(json.dumps({"schema": "deepskopion.strict-lint-baseline/v1", "profile": PROFILE,
                                "profile_sha256": sha, "src": "hw/rtl",
                                "total": sum(n for r in counts.values() for n in r.values()), "counts": counts}))
    return path


def ratchet(tmp: Path, action: str, found: dict, base: Path, *extra: str, sha: str = SHA):
    f = tmp / "findings.json"
    f.write_text(json.dumps(found))
    return subprocess.run([sys.executable, str(TOOL), action, "--findings", str(f), "--profile-sha", sha,
                           "--baseline", str(base), *extra], capture_output=True, text=True, check=False)


BASE = {"hw/rtl/a.vhd": {"RTV-V-008": 2}, "hw/rtl/b.vhd": {"RTV-V-002": 1}}
SAME = findings(("hw/rtl/a.vhd", "RTV-V-008"), ("hw/rtl/a.vhd", "RTV-V-008"), ("hw/rtl/b.vhd", "RTV-V-002"))


def test_equal_counts_hold(tmp_path):
    r = ratchet(tmp_path, "check", SAME, baseline(tmp_path / "b.json", BASE))
    assert r.returncode == 0 and "holds: 3 findings" in r.stdout, r.stdout


def test_a_rise_in_one_file_and_rule_is_refused_naming_both(tmp_path):
    more = findings(*[(f["file"], f["rule_id"]) for f in SAME["findings"]], ("hw/rtl/b.vhd", "RTV-V-002"))
    r = ratchet(tmp_path, "check", more, baseline(tmp_path / "b.json", BASE))
    assert r.returncode == 1, r.stdout
    assert "hw/rtl/b.vhd: RTV-V-002 1 -> 2" in r.stdout, r.stdout


def test_a_finding_in_a_file_the_baseline_does_not_list_is_a_rise(tmp_path):
    more = findings(*[(f["file"], f["rule_id"]) for f in SAME["findings"]], ("hw/rtl/new.vhd", "RTV-V-009"))
    r = ratchet(tmp_path, "check", more, baseline(tmp_path / "b.json", BASE))
    assert r.returncode == 1 and "hw/rtl/new.vhd: RTV-V-009 0 -> 1" in r.stdout, r.stdout


def test_a_swap_between_rules_in_one_file_is_still_a_rise(tmp_path):
    swapped = findings(("hw/rtl/a.vhd", "RTV-V-008"), ("hw/rtl/a.vhd", "RTV-V-004"), ("hw/rtl/b.vhd", "RTV-V-002"))
    r = ratchet(tmp_path, "check", swapped, baseline(tmp_path / "b.json", BASE))
    assert r.returncode == 1 and "hw/rtl/a.vhd: RTV-V-004 0 -> 1" in r.stdout, r.stdout


def test_a_fall_the_baseline_does_not_record_is_refused(tmp_path):
    fewer = findings(("hw/rtl/a.vhd", "RTV-V-008"), ("hw/rtl/b.vhd", "RTV-V-002"))
    r = ratchet(tmp_path, "check", fewer, baseline(tmp_path / "b.json", BASE))
    assert r.returncode == 1 and "Lower the baseline" in r.stdout and "RTV-V-008 2 -> 1" in r.stdout, r.stdout


def test_update_lowers_the_baseline_then_check_holds(tmp_path):
    b = baseline(tmp_path / "b.json", BASE)
    fewer = findings(("hw/rtl/a.vhd", "RTV-V-008"), ("hw/rtl/b.vhd", "RTV-V-002"))
    assert ratchet(tmp_path, "update", fewer, b).returncode == 0
    assert json.loads(b.read_text())["counts"]["hw/rtl/a.vhd"] == {"RTV-V-008": 1}
    assert ratchet(tmp_path, "check", fewer, b).returncode == 0


def test_update_refuses_to_raise_and_leaves_the_baseline_untouched(tmp_path):
    b = baseline(tmp_path / "b.json", BASE)
    before = b.read_text()
    more = findings(*[(f["file"], f["rule_id"]) for f in SAME["findings"]], ("hw/rtl/b.vhd", "RTV-V-002"))
    r = ratchet(tmp_path, "update", more, b)
    assert r.returncode == 1 and "Refused" in r.stdout, r.stdout
    assert b.read_text() == before


def test_a_different_profile_file_cannot_be_judged(tmp_path):
    r = ratchet(tmp_path, "check", SAME, baseline(tmp_path / "b.json", BASE), sha="b" * 64)
    assert r.returncode == 2 and "could not judge" in r.stdout and "--rebaseline" in r.stdout, r.stdout


def test_rebaseline_records_the_new_profile_and_its_counts(tmp_path):
    b = baseline(tmp_path / "b.json", BASE)
    more = findings(*[(f["file"], f["rule_id"]) for f in SAME["findings"]], ("hw/rtl/b.vhd", "RTV-V-002"))
    assert ratchet(tmp_path, "update", more, b, "--rebaseline", sha="b" * 64).returncode == 0
    doc = json.loads(b.read_text())
    assert doc["profile_sha256"] == "b" * 64 and doc["total"] == 4
    assert ratchet(tmp_path, "check", more, b, sha="b" * 64).returncode == 0


def test_an_unreadable_baseline_cannot_be_judged(tmp_path):
    bad = tmp_path / "b.json"
    bad.write_text("{not json")
    r = ratchet(tmp_path, "check", SAME, bad)
    assert r.returncode == 2 and "could not judge" in r.stdout, r.stdout


def test_the_committed_baseline_is_consistent():
    doc = json.loads((ROOT / "tools/lint/strict_provisional_baseline.json").read_text())
    assert doc["profile"] == PROFILE and doc["src"] == "hw/rtl"
    assert len(doc["profile_sha256"]) == 64 and doc["total"] == sum(n for r in doc["counts"].values() for n in r.values())
    assert all(f.startswith("hw/rtl/") for f in doc["counts"])


# ── the hook leg, cut out of hooks/pre-commit.local ──────────────────────────────────────────────────────────────

def _leg() -> str:
    text = HOOK.read_text(encoding="utf-8")
    start = text.index(LEG_START)
    end = text.index("\n# ── ", start + len(LEG_START))
    return text[start:end]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _scratch(tmp: Path) -> tuple[Path, Path, Path]:
    """A repo with tools/lint (the real ratchet), hw/rtl/a.vhd and a baseline of 2 findings under a stub profile; an
    rr stand-in whose 'interpreter' reports the stub profile file and whose `linting --findings X` copies the file the
    test puts at <tmp>/next_findings.json (and logs every call)."""
    repo = tmp / "work"
    (repo / "tools" / "lint").mkdir(parents=True)
    (repo / "hw" / "rtl").mkdir(parents=True)
    shutil.copy(TOOL, repo / "tools" / "lint" / TOOL.name)
    (repo / "hw" / "rtl" / "a.vhd").write_text("-- a\n")
    (repo / "docs.md").write_text("# doc\n")
    prof = tmp / "profile.yml"
    prof.write_text("name: stub\n")
    sha = hashlib.sha256(prof.read_bytes()).hexdigest()
    baseline(repo / "tools" / "lint" / "strict_provisional_baseline.json", {"hw/rtl/a.vhd": {"RTV-V-008": 2}}, sha)
    # The ratchet finds rr's profile file through rr's interpreter line; this interpreter answers `-c` with the stub
    # profile path and otherwise runs the rr stub as bash.
    interp = tmp / "fakepy"
    interp.write_text(f'#!/bin/bash\nif [ "$1" = "-c" ]; then echo "{prof}"; exit 0; fi\nexec /bin/bash "$@"\n')
    interp.chmod(0o755)
    stub = tmp / "rr"
    stub.write_text(f"""#!{interp}
echo "rr $*" >> "{tmp}/rr_calls.txt"
while [ $# -gt 0 ]; do [ "$1" = "--findings" ] && {{ cp "{tmp}/next_findings.json" "$2" 2>/dev/null; }}; shift; done
exit 1
""")
    stub.chmod(0o755)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    return repo, stub, tmp / "next_findings.json"


def _run_leg(repo: Path, stub: Path) -> subprocess.CompletedProcess[str]:
    script = f'set -u\nDS_GUARD_PY="{sys.executable}"\n{_leg()}\necho LEG_PASSED\n'
    env = {"PATH": "/usr/bin:/bin", "DS_RR_BIN": str(stub), "HOME": str(repo)}
    return subprocess.run(["bash", "-c", script], cwd=repo, text=True, capture_output=True, check=False, env=env)


def _stage(repo: Path, rel: str, text: str) -> None:
    (repo / rel).write_text(text)
    _git(repo, "add", rel)


def test_leg_refuses_a_commit_that_adds_a_finding(tmp_path):
    repo, stub, nxt = _scratch(tmp_path)
    nxt.write_text(json.dumps(findings(*[("hw/rtl/a.vhd", "RTV-V-008")] * 3)))
    _stage(repo, "hw/rtl/a.vhd", "-- a edited\n")
    done = _run_leg(repo, stub)
    assert done.returncode == 1 and "LEG_PASSED" not in done.stdout, done.stdout + done.stderr
    assert "hw/rtl/a.vhd: RTV-V-008 2 -> 3" in done.stdout and "Commit aborted" in done.stdout, done.stdout


def test_leg_passes_when_the_count_equals_the_baseline(tmp_path):
    repo, stub, nxt = _scratch(tmp_path)
    nxt.write_text(json.dumps(findings(*[("hw/rtl/a.vhd", "RTV-V-008")] * 2)))
    _stage(repo, "hw/rtl/a.vhd", "-- a edited\n")
    done = _run_leg(repo, stub)
    assert done.returncode == 0 and "LEG_PASSED" in done.stdout and "holds: 2 findings" in done.stdout, done.stdout


def test_leg_skips_without_rr_when_nothing_under_hw_rtl_is_staged(tmp_path):
    repo, stub, nxt = _scratch(tmp_path)
    _stage(repo, "docs.md", "# doc edited\n")
    done = _run_leg(repo, stub)
    assert done.returncode == 0 and "LEG_PASSED" in done.stdout and "nothing under hw/rtl" in done.stdout
    assert not (tmp_path / "rr_calls.txt").exists()


def test_leg_refuses_unstaged_hw_rtl_changes(tmp_path):
    repo, stub, nxt = _scratch(tmp_path)
    nxt.write_text(json.dumps(findings(*[("hw/rtl/a.vhd", "RTV-V-008")] * 2)))
    _stage(repo, "hw/rtl/a.vhd", "-- a edited\n")
    (repo / "hw" / "rtl" / "a.vhd").write_text("-- a edited again, unstaged\n")
    done = _run_leg(repo, stub)
    assert done.returncode == 1 and "unstaged or untracked" in done.stdout, done.stdout
    assert not (tmp_path / "rr_calls.txt").exists()


def test_leg_fails_closed_when_rr_writes_no_findings(tmp_path):
    repo, stub, nxt = _scratch(tmp_path)
    _stage(repo, "hw/rtl/a.vhd", "-- a edited\n")
    done = _run_leg(repo, stub)
    assert done.returncode == 1 and "GATE COULD NOT RUN" in done.stdout, done.stdout

def test_leg_does_not_judge_an_untracked_generated_file(tmp_path):
    """The gate materializes the gitignored rom_info_pkg.vhd for lint before this leg; rr lints it, the commit does
    not contain it, so it is reported and not judged (found by the DS-P2.352 commit's own gate run)."""
    repo, stub, nxt = _scratch(tmp_path)
    (repo / ".gitignore").write_text("hw/rtl/pkg/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "ignore")
    (repo / "hw" / "rtl" / "pkg").mkdir()
    (repo / "hw" / "rtl" / "pkg" / "rom_info_pkg.vhd").write_text("-- generated\n")
    nxt.write_text(json.dumps(findings(*[("hw/rtl/a.vhd", "RTV-V-008")] * 2, *[("hw/rtl/pkg/rom_info_pkg.vhd", "RTV-V-018")] * 4)))
    _stage(repo, "hw/rtl/a.vhd", "-- a edited\n")
    done = _run_leg(repo, stub)
    assert done.returncode == 0 and "LEG_PASSED" in done.stdout, done.stdout
    assert "not judged (untracked): hw/rtl/pkg/rom_info_pkg.vhd (4 finding(s))" in done.stdout, done.stdout
