# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.287: under the per-target lock layout the commit hook's lock leg fails closed.

routertl 5.5.0 (RTL-P2.2112) keeps one lock per target, targets/<t>.routertl.lock, and `rr lock --update` moves
a tracked root routertl.lock there and deletes it. The DS-P3.187 leg read only the root file and, when it was
not tracked, printed "routertl.lock is not tracked here; nothing to check" and passed. These tests run the
hook's own leg, cut out of hooks/pre-commit.local, in a scratch repository with targets, with a stand-in rr
whose verdict follows the target it is asked about. Each must-fail case is red on the DS-P3.187 leg.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "pre-commit.local"
LEG_START = "# ── DS-P3.187 — routertl.lock stays in step with the tree ──"
TARGETS = ("alpha", "beta")


def _lock(target: str) -> str:
    return f"""version: 1
target: {target}
sources:
- path: hw/rtl/{target}.vhd
  sha256: aa
- path: hw/rtl/core.vhd
  sha256: bb
"""


def _hook_leg() -> str:
    text = HOOK.read_text(encoding="utf-8")
    return text[text.index(LEG_START):]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path, *, target_locks: bool = True, root_lock: bool = False) -> Path:
    work = tmp_path / "work"
    (work / "tools").mkdir(parents=True)
    shutil.copy(ROOT / "tools" / "lock_check_leg.py", work / "tools")
    files = {"project.yml": "target: targets/alpha.yml\n", "hw/rtl/core.vhd": "-- core\n",
             "docs/guide.md": "# Guide\n"}
    for t in TARGETS:
        files[f"targets/{t}.yml"] = f"project:\n  name: {t}\n"
        files[f"hw/rtl/{t}.vhd"] = f"-- {t}\n"
        if target_locks:
            files[f"targets/{t}.routertl.lock"] = _lock(t)
    if root_lock:
        files["routertl.lock"] = _lock("alpha")
    for rel, body in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    return work


def _fake_rr(tmp_path: Path, stale: str | None = None) -> Path:
    """rr stand-in: `lock --check` with RR_TARGET=<path> answers for that target; `lock --all-targets --check`
    answers for every target. *stale* names the one target whose lock is out of step."""
    log = tmp_path / "rr_calls.txt"
    script = tmp_path / "rr"
    stale_s = stale or ""
    script.write_text(f"""#!/bin/sh
echo "RR_TARGET=${{RR_TARGET:-}} $@" >> {log}
stale="{stale_s}"
case " $* " in
  *" --all-targets "*)
    if [ -n "$stale" ]; then
      printf '  Lockfile mismatch — 1 difference(s):\\n     sources: hw/rtl/core.vhd (sha256: bb → cc)\\n'
      printf '  --all-targets: failed for %s\\n' "$stale"; exit 1
    fi
    echo "  Lockfile is up to date."; exit 0 ;;
esac
t=$(basename "${{RR_TARGET:-}}" .yml)
if [ -n "$stale" ] && [ "$t" = "$stale" ]; then
  printf '  Lockfile mismatch — 1 difference(s):\\n     sources: hw/rtl/core.vhd (sha256: bb → cc)\\n'; exit 1
fi
echo "  Lockfile is up to date."; exit 0
""", encoding="utf-8")
    script.chmod(0o755)
    return script


def _run_leg(repo: Path, rr: Path) -> subprocess.CompletedProcess[str]:
    script = f'set -u\nDS_GUARD_PY="{sys.executable}"\n{_hook_leg()}\necho LEG_PASSED\n'
    env = {"PATH": "/usr/bin:/bin", "DS_RR_BIN": str(rr), "HOME": str(repo)}
    return subprocess.run(["bash", "-c", script], cwd=repo, text=True,
                          capture_output=True, check=False, env=env)


def _stage_edit(repo: Path, rel: str) -> None:
    path = repo / rel
    path.write_text(path.read_text(encoding="utf-8") + "-- edit\n", encoding="utf-8")
    _git(repo, "add", rel)


def test_no_tracked_lock_at_all_fails_closed(tmp_path):
    repo = _repo(tmp_path, target_locks=False)
    _stage_edit(repo, "hw/rtl/core.vhd")
    r = _run_leg(repo, _fake_rr(tmp_path))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "LEG_PASSED" not in r.stdout


def test_one_target_lock_stale_fails(tmp_path):
    repo = _repo(tmp_path)
    _stage_edit(repo, "hw/rtl/core.vhd")
    r = _run_leg(repo, _fake_rr(tmp_path, stale="beta"))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "LEG_PASSED" not in r.stdout
    assert "beta" in r.stdout + r.stderr


def test_a_root_lock_beside_target_locks_fails(tmp_path):
    repo = _repo(tmp_path, root_lock=True)
    _stage_edit(repo, "hw/rtl/core.vhd")
    r = _run_leg(repo, _fake_rr(tmp_path))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "LEG_PASSED" not in r.stdout
    assert "routertl.lock" in r.stdout + r.stderr


def test_positive_control_every_target_lock_in_step_passes(tmp_path):
    repo = _repo(tmp_path)
    _stage_edit(repo, "hw/rtl/core.vhd")
    r = _run_leg(repo, _fake_rr(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "LEG_PASSED" in r.stdout


def _checked_targets(tmp_path: Path) -> list[str]:
    log = tmp_path / "rr_calls.txt"
    if not log.is_file():
        return []
    return sorted(Path(line.split()[0].split("=", 1)[1]).stem
                  for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("RR_TARGET=targets/"))


def test_a_change_to_one_targets_own_source_checks_only_that_target(tmp_path):
    repo = _repo(tmp_path)
    _stage_edit(repo, "hw/rtl/beta.vhd")
    r = _run_leg(repo, _fake_rr(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _checked_targets(tmp_path) == ["beta"]


def test_a_change_to_a_shared_source_checks_every_target(tmp_path):
    repo = _repo(tmp_path)
    _stage_edit(repo, "hw/rtl/core.vhd")          # both locks list it
    r = _run_leg(repo, _fake_rr(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _checked_targets(tmp_path) == ["alpha", "beta"]


def test_ip_lock_checks_every_target(tmp_path):
    repo = _repo(tmp_path)
    (repo / "ip.lock").write_text("packages: []\n", encoding="utf-8")
    _git(repo, "add", "ip.lock")
    r = _run_leg(repo, _fake_rr(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _checked_targets(tmp_path) == ["alpha", "beta"]


def test_an_ip_lock_only_change_that_leaves_a_target_lock_stale_is_refused(tmp_path):
    """DS-P1.101: 80f41869 changed ip.lock alone and every product lock went stale, unrefused."""
    repo = _repo(tmp_path)
    (repo / "ip.lock").write_text("packages: []\n", encoding="utf-8")
    _git(repo, "add", "ip.lock")
    r = _run_leg(repo, _fake_rr(tmp_path, stale="beta"))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "LEG_PASSED" not in r.stdout
    assert "beta" in r.stdout + r.stderr


def test_a_target_file_with_no_lock_checks_every_target(tmp_path):
    repo = _repo(tmp_path)
    (repo / "targets/gamma.yml").write_text("project:\n  name: gamma\n", encoding="utf-8")
    _git(repo, "add", "targets/gamma.yml")
    r = _run_leg(repo, _fake_rr(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _checked_targets(tmp_path) == ["alpha", "beta"]


def test_an_unresolvable_target_names_the_package_install_command(tmp_path):
    repo = _repo(tmp_path)
    _stage_edit(repo, "hw/rtl/beta.vhd")
    rr = tmp_path / "rr"
    rr.write_text("#!/bin/sh\necho 'project state resolution failed (PackageResolutionError)'\n"
                  "echo '  Failed to resolve project state.'\nexit 1\n", encoding="utf-8")
    rr.chmod(0o755)
    r = _run_leg(repo, rr)
    assert r.returncode != 0
    assert "rr pkg install --target beta" in r.stdout + r.stderr


# A fresh clone or git-land's landing tree has no libs/: the packages a lock pins are absent. The leg installs a
# target's pinned packages (`RR_TARGET=... rr pkg install`) before judging it, and fails closed when that does not
# make them present or changes a tracked file.
def _repo_with_pinned_package(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    lock = _lock("beta") + "dependencies:\n- name: vendor/pkg\n  local_path: libs/vendor/pkg\n  files:\n" \
                           "  - libs/vendor/pkg/pkg.vhd\n"
    (repo / "targets/beta.routertl.lock").write_text(lock, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "pinned package")
    _stage_edit(repo, "hw/rtl/beta.vhd")
    return repo


def _install_rr(tmp_path: Path, *, installs: bool = True, touches: str | None = None) -> Path:
    """rr stand-in: `pkg install` writes the pinned file (or not, or also edits a tracked file); `lock --check`
    reports an unresolvable target while the file is absent, as the real rr does."""
    log = tmp_path / "rr_calls.txt"
    rr = tmp_path / "rr"
    install = "mkdir -p libs/vendor/pkg && echo '-- pkg' > libs/vendor/pkg/pkg.vhd\n" if installs else ""
    touch = f"echo '-- touched' >> {touches}\n" if touches else ""
    rr.write_text(f'#!/bin/sh\necho "RR_TARGET=${{RR_TARGET:-}} $@" >> {log}\n'
                  f'if [ "$1" = pkg ]; then\n{install}{touch}exit 0\nfi\n'
                  "[ -f libs/vendor/pkg/pkg.vhd ] || { echo 'Failed to resolve project state.'; exit 1; }\n"
                  'echo "  Lockfile is up to date."; exit 0\n', encoding="utf-8")
    rr.chmod(0o755)
    return rr


def test_a_target_whose_pinned_package_is_absent_is_installed_then_judged(tmp_path):
    repo = _repo_with_pinned_package(tmp_path)
    r = _run_leg(repo, _install_rr(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "rr_calls.txt").read_text(encoding="utf-8").splitlines() == [
        "RR_TARGET=targets/beta.yml pkg install", "RR_TARGET=targets/beta.yml lock --check"], r.stdout
    assert "routertl locks hold for 1 target(s)" in r.stdout


def test_an_install_that_leaves_a_pinned_package_absent_fails_closed(tmp_path):
    repo = _repo_with_pinned_package(tmp_path)
    r = _run_leg(repo, _install_rr(tmp_path, installs=False))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "LEG_PASSED" not in r.stdout
    assert "GATE COULD NOT RUN" in r.stdout          # the leg's verdict 2, not a lock finding
    assert "still lacks pinned package(s): vendor/pkg" in r.stdout


def test_an_install_that_changes_a_tracked_file_fails_closed(tmp_path):
    repo = _repo_with_pinned_package(tmp_path)
    r = _run_leg(repo, _install_rr(tmp_path, touches="docs/guide.md"))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "LEG_PASSED" not in r.stdout
    assert "GATE COULD NOT RUN" in r.stdout          # the leg's verdict 2, not a lock finding
    assert "changed tracked files" in r.stdout


def test_an_install_that_fails_fails_closed(tmp_path):
    repo = _repo_with_pinned_package(tmp_path)
    rr = tmp_path / "rr"
    rr.write_text("#!/bin/sh\n[ \"$1\" = pkg ] && { echo 'fetch failed: no route to host'; exit 1; }\n"
                  "echo '  Lockfile is up to date.'; exit 0\n", encoding="utf-8")
    rr.chmod(0o755)
    r = _run_leg(repo, rr)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "GATE COULD NOT RUN" in r.stdout
    assert "rr pkg install exited 1 for beta" in r.stdout


def test_an_install_that_does_not_finish_fails_closed_at_the_timeout(tmp_path):
    repo = _repo_with_pinned_package(tmp_path)
    rr = tmp_path / "rr"
    rr.write_text("#!/bin/sh\n[ \"$1\" = pkg ] && { sleep 30; exit 0; }\necho '  Lockfile is up to date.'; exit 0\n",
                  encoding="utf-8")
    rr.chmod(0o755)
    script = (f'set -u\nDS_GUARD_PY="{sys.executable}"\nexport DS_LOCK_INSTALL_TIMEOUT_S=2\n{_hook_leg()}\n'
              'echo LEG_PASSED\n')
    r = subprocess.run(["bash", "-c", script], cwd=repo, text=True, capture_output=True, check=False,
                       env={"PATH": "/usr/bin:/bin", "DS_RR_BIN": str(rr), "HOME": str(repo)}, timeout=25)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "GATE COULD NOT RUN" in r.stdout
    assert "did not finish in 2 s" in r.stdout


def test_an_uninstalled_tree_is_not_judged_stale(tmp_path):
    # Measured 2026-11-13 on a fresh worktree with no libs/: `rr lock --check` for classic_eye printed "Lockfile
    # mismatch — 12 difference(s)" (its pinned package files absent), a STALE verdict for a lock that holds. The
    # leg installs the pins before it judges, so that tree reads as in step.
    repo = _repo_with_pinned_package(tmp_path)
    log = tmp_path / "rr_calls.txt"
    rr = tmp_path / "rr"
    rr.write_text(f'#!/bin/sh\necho "RR_TARGET=${{RR_TARGET:-}} $@" >> {log}\n'
                  "if [ \"$1\" = pkg ]; then mkdir -p libs/vendor/pkg && echo '-- pkg' > libs/vendor/pkg/pkg.vhd; "
                  "exit 0; fi\n"
                  "[ -f libs/vendor/pkg/pkg.vhd ] || { printf '  Lockfile mismatch — 1 difference(s):\\n"
                  "     sources: libs/vendor/pkg/pkg.vhd\\n'; exit 1; }\n"
                  'echo "  Lockfile is up to date."; exit 0\n', encoding="utf-8")
    rr.chmod(0o755)
    r = _run_leg(repo, rr)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "STALE" not in r.stdout
    assert log.read_text(encoding="utf-8").splitlines()[0] == "RR_TARGET=targets/beta.yml pkg install"


def test_a_staged_bench_target_file_checks_that_target_at_its_bench_path(tmp_path):
    # DS-P1.103: a bench target's file is bench/targets/<t>.yml and rr keeps its lock at targets/<t>.routertl.lock.
    # A commit that stages the bench file must check that target, with RR_TARGET naming the file that exists.
    repo = _repo(tmp_path)
    for rel, body in {"bench/targets/gamma.yml": "project:\n  name: gamma\n", "hw/rtl/gamma.vhd": "-- gamma\n",
                      "targets/gamma.routertl.lock": _lock("gamma")}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "bench target")
    (repo / "bench/targets/gamma.yml").write_text("project:\n  name: gamma\n  top_module: g\n", encoding="utf-8")
    _git(repo, "add", "bench/targets/gamma.yml")
    log = tmp_path / "rr_calls.txt"
    rr = tmp_path / "rr"
    rr.write_text(f'#!/bin/sh\necho "RR_TARGET=${{RR_TARGET:-}} $@" >> {log}\n'
                  '[ -f "${RR_TARGET:-}" ] || { echo "target file not found: ${RR_TARGET:-}"; exit 2; }\n'
                  'echo "  Lockfile is up to date."; exit 0\n', encoding="utf-8")
    rr.chmod(0o755)
    r = _run_leg(repo, rr)
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.is_file() and log.read_text(encoding="utf-8").splitlines() == [
        "RR_TARGET=bench/targets/gamma.yml lock --check"], r.stdout + r.stderr
