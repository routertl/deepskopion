# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P3.264: DE25 rootfs provenance stamps the real source revision and payload SHA.

Before DS-P3.264, ``tools/de25_linux_image.py:normalize_provenance`` hashed only
``lock_inputs(root)`` (target yml + static boot-chain files) and wrote that
digest's first 8 hex characters into ``RR_SW_GIT_HASH`` (with
``RR_SW_GIT_REV=inputs-<sha>``). Two builds at different commits that differed
only in the capture daemon therefore carried the same ``RR_SW_GIT_HASH`` —
printing a hash that looked like a git revision and was not one.

These tests assert at the CLI surface (``de25_linux_image.py normalize-provenance``):
1. Two commits differing only in the daemon stamp distinct ``RR_SW_GIT_HASH`` /
   ``RR_SW_GIT_REV`` matching their real git commits and preserve ``RR_SW_PAYLOAD_SHA``.
2. When the source revision cannot be determined (non-git directory or unborn
   HEAD), ``normalize-provenance`` refuses non-zero and writes no fabricated
   ``RR_SW_GIT_HASH``.
3. The silicon pre-gate documentation in ``sw/linux/README.md`` names
   ``RR_SW_PAYLOAD_SHA`` as the field in ``/etc/rr-sw-provenance`` that
   identifies the deployed software content.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "de25_linux_image.py"
PREGATE_DOC = ROOT / "sw" / "linux" / "README.md"
_GIT_ENV_STRIP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")


def _clean_git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_STRIP}
    env["RR_TARGET"] = "targets/de25_datalogger.yml"
    return env


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_clean_git_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _seed_lock_inputs(repo: Path) -> None:
    """Populate the exact lock_inputs() files so pre-fix normalize_provenance can run."""
    files = {
        "targets/de25_datalogger.yml": "target: de25_datalogger\n",
        "sw/linux/de25/system_memory_map.yml": "lwh2f: []\n",
        "sw/linux/de25/kernel_de25.config-fragment": "# config\n",
        "tools/de25_linux_image.py": "# tool placeholder\n",
    }
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _parse_stamp(path: Path) -> dict[str, str]:
    kv: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        kv[k] = v
    return kv


def _expected_payload_sha(rootfs: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in rootfs.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(rootfs).as_posix()
        if rel == "etc/rr-sw-provenance":
            continue
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def test_two_daemon_commits_stamp_distinct_git_revisions_and_payload_shas(tmp_path):
    """Two commits differing only in the daemon produce distinct RR_SW_GIT_HASH /
    RR_SW_GIT_REV naming the real commits, and keep RR_SW_PAYLOAD_SHA."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_lock_inputs(repo)
    daemon_src = repo / "sw/stream/__main__.py"
    daemon_src.parent.mkdir(parents=True, exist_ok=True)
    daemon_src.write_text("DAEMON_VERSION = 1\n", encoding="utf-8")

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Author")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "commit A: daemon v1")
    rev_a = _git(repo, "rev-parse", "HEAD")

    rootfs_a = tmp_path / "rootfs_a"
    (rootfs_a / "usr/local/share/deepskopion/sw/stream").mkdir(parents=True)
    (rootfs_a / "usr/local/share/deepskopion/sw/stream/__main__.py").write_text(
        "DAEMON_VERSION = 1\n", encoding="utf-8"
    )
    run_a = subprocess.run(
        [sys.executable, str(TOOL), "normalize-provenance",
         "--root", str(repo), "--rootfs", str(rootfs_a)],
        cwd=ROOT, env=_clean_git_env(), capture_output=True, text=True,
    )
    assert run_a.returncode == 0, run_a.stdout + run_a.stderr

    # Second commit changes ONLY the capture daemon; lock_inputs() are untouched.
    daemon_src.write_text("DAEMON_VERSION = 2\n", encoding="utf-8")
    _git(repo, "add", "sw/stream/__main__.py")
    _git(repo, "commit", "-m", "commit B: daemon v2")
    rev_b = _git(repo, "rev-parse", "HEAD")
    assert rev_a != rev_b

    rootfs_b = tmp_path / "rootfs_b"
    (rootfs_b / "usr/local/share/deepskopion/sw/stream").mkdir(parents=True)
    (rootfs_b / "usr/local/share/deepskopion/sw/stream/__main__.py").write_text(
        "DAEMON_VERSION = 2\n", encoding="utf-8"
    )
    run_b = subprocess.run(
        [sys.executable, str(TOOL), "normalize-provenance",
         "--root", str(repo), "--rootfs", str(rootfs_b)],
        cwd=ROOT, env=_clean_git_env(), capture_output=True, text=True,
    )
    assert run_b.returncode == 0, run_b.stdout + run_b.stderr

    stamp_a = _parse_stamp(rootfs_a / "etc/rr-sw-provenance")
    stamp_b = _parse_stamp(rootfs_b / "etc/rr-sw-provenance")

    assert stamp_a["RR_SW_GIT_HASH"] == rev_a[:8], (
        f"stamp A RR_SW_GIT_HASH={stamp_a['RR_SW_GIT_HASH']!r} != real commit {rev_a[:8]!r}"
    )
    assert stamp_b["RR_SW_GIT_HASH"] == rev_b[:8], (
        f"stamp B RR_SW_GIT_HASH={stamp_b['RR_SW_GIT_HASH']!r} != real commit {rev_b[:8]!r}"
    )
    assert stamp_a["RR_SW_GIT_HASH"] != stamp_b["RR_SW_GIT_HASH"], (
        "two commits differing only in the daemon stamped the same RR_SW_GIT_HASH"
    )
    assert stamp_a["RR_SW_GIT_REV"] == rev_a
    assert stamp_b["RR_SW_GIT_REV"] == rev_b
    assert not stamp_a["RR_SW_GIT_REV"].startswith("inputs-")
    assert not stamp_b["RR_SW_GIT_REV"].startswith("inputs-")

    assert stamp_a["RR_SW_PAYLOAD_SHA"] == _expected_payload_sha(rootfs_a)
    assert stamp_b["RR_SW_PAYLOAD_SHA"] == _expected_payload_sha(rootfs_b)
    assert stamp_a["RR_SW_PAYLOAD_SHA"] != stamp_b["RR_SW_PAYLOAD_SHA"]
    assert re.fullmatch(r"[0-9a-f]{64}", stamp_a["RR_SW_PAYLOAD_SHA"])


def test_normalize_provenance_refuses_when_source_revision_is_unknown(tmp_path):
    """When root has no git checkout or no resolvable HEAD, refuse to stamp
    instead of fabricating an RR_SW_GIT_HASH that looks like a git revision."""
    # Case 1: non-git directory with valid lock_inputs().
    non_git = tmp_path / "non_git"
    non_git.mkdir()
    _seed_lock_inputs(non_git)
    rootfs_1 = tmp_path / "rootfs_1"
    (rootfs_1 / "usr/bin").mkdir(parents=True)
    (rootfs_1 / "usr/bin/daemon").write_text("payload", encoding="utf-8")

    res_1 = subprocess.run(
        [sys.executable, str(TOOL), "normalize-provenance",
         "--root", str(non_git), "--rootfs", str(rootfs_1)],
        cwd=ROOT, env=_clean_git_env(), capture_output=True, text=True,
    )
    assert res_1.returncode != 0, (
        "normalize-provenance must refuse a non-git root instead of fabricating RR_SW_GIT_HASH"
    )
    assert not (rootfs_1 / "etc/rr-sw-provenance").exists(), (
        "refused normalize-provenance must not write /etc/rr-sw-provenance"
    )
    assert "git" in res_1.stderr.lower()

    # Case 2: git checkout with unborn HEAD (no commits yet).
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _seed_lock_inputs(unborn)
    _git(unborn, "init")
    rootfs_2 = tmp_path / "rootfs_2"
    (rootfs_2 / "usr/bin").mkdir(parents=True)
    (rootfs_2 / "usr/bin/daemon").write_text("payload", encoding="utf-8")

    res_2 = subprocess.run(
        [sys.executable, str(TOOL), "normalize-provenance",
         "--root", str(unborn), "--rootfs", str(rootfs_2)],
        cwd=ROOT, env=_clean_git_env(), capture_output=True, text=True,
    )
    assert res_2.returncode != 0, (
        "normalize-provenance must refuse an unborn git HEAD instead of fabricating RR_SW_GIT_HASH"
    )
    assert not (rootfs_2 / "etc/rr-sw-provenance").exists()


def test_silicon_pregate_doc_names_software_content_field():
    """The silicon pre-gate documentation names RR_SW_PAYLOAD_SHA as the field
    in /etc/rr-sw-provenance that identifies the deployed software content."""
    text = PREGATE_DOC.read_text(encoding="utf-8")
    marker = '## "Is the board running what I built?"'
    assert marker in text, f"{PREGATE_DOC} missing provenance pre-gate section"
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]
    assert "/etc/rr-sw-provenance" in section
    assert "RR_SW_PAYLOAD_SHA" in section, (
        "silicon pre-gate doc must name RR_SW_PAYLOAD_SHA as the field identifying software content"
    )
    assert "RR_SW_GIT_HASH" in section
