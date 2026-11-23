# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.360: the public export refuses tracked files under a package that rr fetches from its own repository.

Owner ruling 2026-11-18: registry IP comes from `rr pkg add` / `rr workspace sync`, never as a vendored copy in
this repository. The export (tools/export_github.py) and the staged-tree gate the pre-commit hook runs read ip.lock:
every package whose url is not ``sdk://`` has its local_path filled by the fetch, so a tracked path under it is
refused. ``sdk://`` units (edge_counter, lfsr, ... from the RouteRTL SDK) are not fetched packages and pass.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "tests"))

import export_github as E  # noqa: E402

IP_LOCK = """version: 1
vendor/fetched_core:
  url: https://example.invalid/vendor/fetched-core.git
  commit: 0123456789abcdef0123456789abcdef01234567
  local_path: libs/vendor/fetched_core
  library: fc
routertl/sdk_unit:
  url: sdk://src/units/sdk_unit.vhd
  local_path: libs/routertl/sdk_unit
  library: tich
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    for rel, body in {"ip.lock": IP_LOCK, **files}.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def test_roots_are_the_local_paths_of_remote_fetched_packages_only(tmp_path):
    repo = _repo(tmp_path, {"README.md": "x\n"})
    assert E.managed_package_roots("HEAD", repo) == ["libs/vendor/fetched_core"]


def test_a_tracked_file_under_a_fetched_package_is_refused(tmp_path):
    repo = _repo(tmp_path, {"libs/vendor/fetched_core/rtl/core.vhd": "-- vendored\n"})
    refusals = E.managed_package_refusals("HEAD", repo)
    assert len(refusals) == 1
    assert refusals[0].startswith("libs/vendor/fetched_core/rtl/core.vhd: tracked file under managed package dir "
                                  "libs/vendor/fetched_core")


def test_the_export_refuses_before_writing_any_file(tmp_path):
    repo = _repo(tmp_path, {"libs/vendor/fetched_core/rtl/core.vhd": "-- vendored\n"})
    dest = tmp_path / "out"
    rc, msg = E.export_github(dest, root=repo, do_commit=False)
    assert rc == 1
    assert "managed package content is tracked" in msg and "libs/vendor/fetched_core/rtl/core.vhd" in msg
    assert not dest.exists() or not any(dest.iterdir())


def test_the_staged_tree_gate_refuses_a_staged_vendored_file(tmp_path):
    repo = _repo(tmp_path, {"README.md": "x\n"})
    p = repo / "libs/vendor/fetched_core/rtl/core.vhd"
    p.parent.mkdir(parents=True)
    p.write_text("-- vendored\n")
    _git(repo, "add", str(p.relative_to(repo)))
    rc, msg = E.gate_staged_tree(root=repo)
    assert rc == 1 and "libs/vendor/fetched_core/rtl/core.vhd" in msg


def test_an_sdk_unit_is_not_a_managed_package(tmp_path):
    repo = _repo(tmp_path, {"libs/routertl/sdk_unit/src/sdk_unit.vhd": "-- sdk unit\n"})
    assert E.managed_package_refusals("HEAD", repo) == []


def test_no_ip_lock_means_no_roots(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    assert E.managed_package_roots("HEAD", repo) == []


def test_this_repository_tracks_no_managed_package_content():
    # The index tree without locking the live index: a bare write-tree lost index.lock to another xdist worker
    # in the 2026-11-20 ds-python-suite run (tools/tests/git_index_snapshot.py).
    from git_index_snapshot import index_tree
    assert E.managed_package_refusals(index_tree(ROOT), ROOT) == []
