# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""git_index_snapshot.index_tree reads the index tree while another process holds the live index.lock.

The baseline shows the hazard on the same scratch repository: a bare ``git write-tree`` fails while index.lock is
held, so a pass of the helper there is evidence, not a lock that was never contended. The helper's tree must equal
the bare write-tree's tree once the lock is gone, and the live lock file must be left untouched.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_index_snapshot import active_index, index_tree  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "GIT_INDEX_FILE"}
    return subprocess.run(["git", "-C", str(repo), *args], env={**env, **GIT_ENV}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, errors="replace", check=check)


def test_index_tree_reads_the_index_while_its_lock_is_held(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "a")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")                       # staged, not committed: the index differs from HEAD
    lock = Path(str(active_index(repo)) + ".lock")
    lock.write_bytes(b"held by another git process\n")
    try:
        bare = _git(repo, "write-tree", check=False)
        assert bare.returncode != 0 and "index.lock" in bare.stderr, bare.stdout + bare.stderr
        tree = index_tree(repo)
        assert lock.read_bytes() == b"held by another git process\n"
    finally:
        lock.unlink()
    assert tree == _git(repo, "write-tree").stdout.strip()
    assert tree != _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    listed = _git(repo, "ls-tree", "--name-only", tree).stdout.split()
    assert listed == ["a.txt", "b.txt"]
