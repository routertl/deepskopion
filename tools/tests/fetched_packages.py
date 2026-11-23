# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""Fetched IP packages for tests that read their files (DS-P2.360).

Registry IP (ip.lock packages whose url is not ``sdk://``) is not tracked in this repository; `rr workspace sync` /
`rr pkg install` fetch it at the commit ip.lock pins. ip.lock's ``file_hashes`` is the tracked truth for the fetched
files. A test that reads a fetched file calls ``ensure_fetched``:

- every pinned file present and equal to its ip.lock hash: the package path is returned;
- pinned files absent (a fresh checkout, the land retest tree): ``rr workspace sync`` runs once (under a lock, so
  parallel workers do not race), then every pinned file must be present and equal, or the test FAILS;
- a pinned file present but different from its ip.lock hash: the test FAILS, and nothing is re-fetched over it.

With ``DS_FETCH_CHECK_ONLY=1`` (DS-P3.311) no sync runs: absent pinned files FAIL the test, named. A runner that
reads a checkout it must not write (the post-synth guard cron reads the shared checkout) sets it.

It never skips: a guard that reads IP it cannot obtain reports a failure, not a pass.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _entry(name: str, root: Path) -> dict:
    lock = yaml.safe_load((root / "ip.lock").read_text(encoding="utf-8")) or {}
    entry = lock.get(name)
    if not isinstance(entry, dict) or str(entry.get("url", "")).startswith("sdk://"):
        pytest.fail(f"{name} is not a fetched package in ip.lock")
    if not entry.get("file_hashes"):
        pytest.fail(f"ip.lock pins no file hashes for {name}")
    return entry


def _state(entry: dict, root: Path) -> tuple[list[str], list[str]]:
    missing, differ = [], []
    for rel, want in sorted(entry["file_hashes"].items()):
        p = root / rel
        if not p.is_file():
            missing.append(rel)
        elif hashlib.sha256(p.read_bytes()).hexdigest() != want:
            differ.append(rel)
    return missing, differ


def ensure_fetched(name: str, root: Path = ROOT) -> Path:
    entry = _entry(name, root)
    missing, differ = _state(entry, root)
    if differ:
        pytest.fail(f"{name}: fetched file(s) differ from their ip.lock hashes: {differ}")
    if missing and os.environ.get("DS_FETCH_CHECK_ONLY") == "1":
        pytest.fail(f"{name}: pinned files missing {missing}; DS_FETCH_CHECK_ONLY=1, so rr workspace sync was not run "
                    f"in {root}")
    if missing:
        lock_path = Path(tempfile.gettempdir()) / f"ds-fetch-{hashlib.sha256(str(root).encode()).hexdigest()[:16]}.lock"
        with open(lock_path, "w") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            missing, differ = _state(entry, root)
            if missing and not differ:
                try:
                    r = subprocess.run(["rr", "workspace", "sync"], cwd=root, stdin=subprocess.DEVNULL,
                                       capture_output=True, text=True, errors="replace", timeout=900, check=False)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    pytest.fail(f"{name}: pinned files missing {missing} and rr workspace sync could not run: {exc}")
                missing, differ = _state(entry, root)
                if missing or differ:
                    tail = "\n".join((r.stdout + r.stderr).splitlines()[-15:])
                    pytest.fail(f"{name}: rr workspace sync (rc {r.returncode}) left missing {missing}, "
                                f"different {differ}:\n{tail}")
    if differ:
        pytest.fail(f"{name}: fetched file(s) differ from their ip.lock hashes: {differ}")
    return root / entry["local_path"]


def fetched_package_roots(root: Path = ROOT) -> dict[str, str]:
    """ip.lock packages fetched from their own repository (url not sdk://, file hashes pinned) -> local_path."""
    lock = yaml.safe_load((root / "ip.lock").read_text(encoding="utf-8")) or {}
    return {name: str(e["local_path"]).strip("/") for name, e in lock.items()
            if isinstance(e, dict) and e.get("local_path") and e.get("file_hashes")
            and not str(e.get("url", "")).startswith("sdk://")}


def ensure_all_fetched(root: Path = ROOT) -> list[str]:
    """ensure_fetched for every fetched package; one `rr workspace sync` covers them all."""
    names = sorted(fetched_package_roots(root))
    for name in names:
        ensure_fetched(name, root)
    return names
