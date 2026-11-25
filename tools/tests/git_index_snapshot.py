# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""The tree of a repository's index, written without taking the live ``index.lock``.

``git write-tree`` refreshes and re-persists the cache-tree extension of the index it reads, so it takes that
index's ``index.lock``. Under ``pytest -n`` another worker's git command in the same worktree can hold the lock at
that moment, and write-tree dies with "Unable to create ... index.lock" (ds-python-suite, 2026-11-20 23:00, gw5).
The same happens inside a ``git commit`` that holds the lock for its hook (DS-T2.85).

``index_tree`` copies the active index (``$GIT_INDEX_FILE`` when set, else the worktree's own) to a throwaway
file and runs write-tree against the copy, so the only lock taken is on the copy. Git replaces an index by an
atomic rename, so the copy is one consistent index.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def active_index(repo: Path, env: dict[str, str] | None = None) -> Path:
    env = dict(os.environ if env is None else env)
    gif = env.get("GIT_INDEX_FILE")
    if gif:
        p = Path(gif)
        return p if p.is_absolute() else (Path(repo) / p).resolve()
    got = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-path", "index"],
                         capture_output=True, text=True, errors="replace", check=True)
    return Path(got.stdout.strip())


def index_tree(repo: Path, env: dict[str, str] | None = None) -> str:
    """The tree OID of ``repo``'s active index; never locks the live index."""
    env = dict(os.environ if env is None else env)
    src = active_index(repo, env)
    if not src.is_file():
        raise FileNotFoundError(f"git index does not exist: {src}")
    fd, tmp = tempfile.mkstemp(prefix="ds_index_copy_")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        env["GIT_INDEX_FILE"] = tmp
        got = subprocess.run(["git", "-C", str(repo), "write-tree"], capture_output=True, text=True,
                             errors="replace", env=env, check=False)
        if got.returncode != 0:
            raise RuntimeError(f"git write-tree on a copy of {src} failed: {got.stderr.strip()}")
        return got.stdout.strip()
    finally:
        for leftover in (tmp, tmp + ".lock"):
            try:
                os.unlink(leftover)
            except OSError:
                pass
