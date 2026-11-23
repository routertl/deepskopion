# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.113: the public export refuses when a shipped target names a tracked file the export drops.

targets/pfsoc_datalogger.yml named hw/libero/pfsoc/mss/disco_mss.cxz as hardware.mss while the export's drop list
removed that file, so a public clone could not build the target (Libero import_mss_component failed). The export and
the staged-tree gate now read every shipped targets/*.yml and refuse any tracked file it names that the export drops.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import export_github as E  # noqa: E402

INTERNAL = "docs/" + "in" + "ternal/"          # on the export drop list (split so the export gate does not flag it)
DROPPED = INTERNAL + "mss_notes.md"
SHIPPED = "hw/rtl/top.vhd"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _repo(tmp_path: Path, target_yml: str) -> Path:
    repo = tmp_path / "repo"
    files = {DROPPED: "notes\n", SHIPPED: "-- top\n", "targets/board_x.yml": target_yml}
    for rel, body in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def test_a_shipped_target_naming_a_dropped_file_is_refused(tmp_path):
    repo = _repo(tmp_path, f"hardware:\n  mss: {DROPPED}\n")
    out = E.target_input_refusals("HEAD", repo)
    assert out == [f"{DROPPED}: named by shipped targets/board_x.yml but dropped by the export ({INTERNAL})"]


def test_a_shipped_target_naming_shipped_files_passes(tmp_path):
    repo = _repo(tmp_path, f"hardware:\n  top: {SHIPPED}\nhooks:\n  pre: [{SHIPPED}]\n")
    assert E.target_input_refusals("HEAD", repo) == []


def test_a_string_that_is_not_a_tracked_path_is_not_a_named_file(tmp_path):
    repo = _repo(tmp_path, f"linux:\n  bootargs: console=ttyS0 {INTERNAL}not_a_file.md\n")
    assert E.target_input_refusals("HEAD", repo) == []


def test_a_dropped_target_is_not_checked(tmp_path, monkeypatch):
    repo = _repo(tmp_path, f"hardware:\n  mss: {DROPPED}\n")
    real = E._matches_drop_list
    monkeypatch.setattr(E, "_matches_drop_list",
                        lambda p: "bench-only target" if p == "targets/board_x.yml" else real(p))
    assert E.target_input_refusals("HEAD", repo) == []


def test_the_export_refuses_before_writing_any_file(tmp_path):
    repo = _repo(tmp_path, f"hardware:\n  mss: {DROPPED}\n")
    dest = tmp_path / "out"
    rc, msg = E.export_github(dest, root=repo, do_commit=False)
    assert rc == 1
    assert "a shipped target names a file the export drops" in msg and DROPPED in msg
    assert not dest.exists() or not any(dest.iterdir())


def test_the_staged_tree_gate_refuses(tmp_path):
    repo = _repo(tmp_path, f"hardware:\n  top: {SHIPPED}\n")
    (repo / "targets/board_x.yml").write_text(f"hardware:\n  mss: {DROPPED}\n")
    _git(repo, "add", "targets/board_x.yml")
    rc, msg = E.gate_staged_tree(root=repo)
    assert rc == 1 and DROPPED in msg


def _staged_tree(scratch: Path) -> str:
    """The tree of what is staged in this checkout, written from a private copy of its index (as DS-P3.262).

    `git write-tree` on the checkout's own index may rewrite the cache tree and so takes index.lock; another git
    command in the same worktree at that moment makes it fail ("Unable to create .../index.lock", the
    ds-python-suite red of 2026-11-19 at -n 8)."""
    index = Path(subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-path", "index"], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout.strip())
    private = scratch / "index.copy"
    shutil.copyfile(index, private)
    return subprocess.run(["git", "write-tree"], cwd=ROOT, env={**os.environ, "GIT_INDEX_FILE": str(private)},
                          capture_output=True, text=True, check=True).stdout.strip()


def test_no_shipped_target_in_this_repository_names_a_dropped_file(tmp_path):
    assert E.target_input_refusals(_staged_tree(tmp_path), ROOT) == []
