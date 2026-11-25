# rr unknown-llm (AI-generated)
"""DS-P3.153: registry packages may not hide edits behind lock hashes.

INPUT DOMAIN: an ``ip.lock`` entry with a Git URL is a registry package whose
``local_path`` is a byte-for-byte materialisation of ``commit``.  SDK builtins
and explicitly vendored local packages are outside this contract.

OUTPUT CONTRACT: every installed file below each registry package's
``local_path`` has the same SHA-256 as ``git show <commit>:<relative-path>`` in
that package's upstream repository, and ``file_hashes`` records that same
digest.

PROHIBITION: a locally edited package file must never become trusted merely
because ``ip.lock.file_hashes`` was regenerated over it.  That was the exact
DS-T3.32 escape: ``rr pkg verify`` blessed two edited rr_ddr_stream tops even
though a force-install silently replaced them with the defective upstream
blobs.

This test intentionally requires upstream Git access.  A fresh-clone/package
lane has that same prerequisite; inability to fetch is a failure, not a skip.
Set ``DS_REGISTRY_PACKAGE_ROOT`` only for seeded baseline/mutation snapshots.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse

import yaml


ROOT = Path(
    os.environ.get(
        "DS_REGISTRY_PACKAGE_ROOT", Path(__file__).resolve().parents[2]
    )
).resolve()
GIT_URL_PREFIXES = ("git@", "http://", "https://", "ssh://")


def _run(*argv: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )


def _registry_entries() -> list[tuple[str, dict]]:
    lock = yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))
    return [
        (name, entry)
        for name, entry in lock.items()
        if isinstance(entry, dict)
        and str(entry.get("url", "")).startswith(GIT_URL_PREFIXES)
    ]


# rr's own install bookkeeping, written by `rr pkg add` / workspace sync into
# the ROOT of each package dir (gitignored, never upstream content). Counting
# it as an overlay made every package "mismatch" after any rr pkg operation
# (DS-P2.128).  Only that root stamp is generated metadata: a same-named file
# anywhere deeper is package content and is compared like any other source
# file (DS-P2.140), so the exemption cannot hide an overlay.
RR_BOOKKEEPING = {".routertl_commit"}


def _package_files(local_path: str) -> list[str]:
    package_root = ROOT / local_path
    if not package_root.is_dir():
        return []
    return sorted(
        str(path.relative_to(ROOT))
        for path in package_root.rglob("*")
        if path.is_file()
        and not (path.parent == package_root and path.name in RR_BOOKKEEPING)
    )


def _fetch_commit(repo: Path, url: str, commit: str) -> None:
    repo.mkdir()
    initialised = _run("git", "init", "--bare", "--quiet", str(repo))
    assert initialised.returncode == 0, initialised.stderr.decode(errors="replace")
    candidates = [url]
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.hostname == "bitbucket.org":
        # Registry releases expose the canonical HTTPS URL. Private Bitbucket
        # repositories use the same upstream through the bench's SSH deploy
        # key, so prefer that noninteractive transport for the Git oracle.
        candidates.insert(0, f"git@bitbucket.org:{parsed.path.lstrip('/')}")

    failures: list[str] = []
    for candidate in candidates:
        fetched = _run(
            "git",
            "-C",
            str(repo),
            "fetch",
            "--quiet",
            "--depth=1",
            "--filter=blob:none",
            candidate,
            commit,
        )
        if fetched.returncode == 0:
            return
        failures.append(
            f"{candidate}: {fetched.stderr.decode(errors='replace').strip()}"
        )

    raise AssertionError(
        f"could not fetch locked upstream {url} @ {commit}:\n"
        + "\n".join(failures)
    )


def _normalised_url(url: str) -> str:
    """Mirror RouteRTL's public SSH/HTTPS cache-key normalisation."""
    if url.startswith("git@"):
        host, path = url.removeprefix("git@").split(":", 1)
        url = f"https://{host}/{path}"
    elif url.startswith("ssh://"):
        parsed = urlparse(url)
        if parsed.hostname:
            url = f"https://{parsed.hostname}{parsed.path}"
    url = url.rstrip("/")
    return url[:-4] if url.endswith(".git") else url


def _route_rtl_cache_path(url: str) -> Path | None:
    if os.environ.get("ROUTERTL_NO_PKG_CACHE") == "1":
        return None
    override = os.environ.get("ROUTERTL_PKG_CACHE")
    if override:
        root = Path(override).expanduser()
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        root = base / "routertl" / "packages"
    normalised = _normalised_url(url)
    digest = hashlib.sha256(normalised.encode()).hexdigest()[:16]
    stem = re.sub(r"[^A-Za-z0-9_.-]", "-", normalised.rsplit("/", 1)[-1])[:40]
    return root / f"{stem}-{digest}.git"


def _upstream_source(
    scratch: Path, url: str, commit: str
) -> tuple[Path, str]:
    """Use rr's immutable upstream mirror when warm; fetch when cold."""
    cache = _route_rtl_cache_path(url)
    if cache and cache.is_dir():
        present = _run(
            "git", "-C", str(cache), "cat-file", "-e", f"{commit}^{{commit}}"
        )
        if present.returncode == 0:
            return cache, commit
    _fetch_commit(scratch, url, commit)
    return scratch, "FETCH_HEAD"


def test_only_the_package_root_install_stamp_is_exempt(tmp_path: Path, monkeypatch):
    """Generated-metadata vs source classification, without network access."""
    package = tmp_path / "libs" / "vendor" / "pkg"
    (package / "rtl").mkdir(parents=True)
    (package / ".routertl_commit").write_text("0123abc\n", encoding="utf-8")
    (package / "rtl" / ".routertl_commit").write_text("overlay\n", encoding="utf-8")
    (package / "rtl" / "core.vhd").write_text("-- core\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)

    assert _package_files("libs/vendor/pkg") == [
        "libs/vendor/pkg/rtl/.routertl_commit",
        "libs/vendor/pkg/rtl/core.vhd",
    ]


def test_registry_package_files_equal_their_locked_upstream_blobs(tmp_path: Path):
    """Caller surface: Git itself must reproduce every committed package byte."""
    entries = _registry_entries()
    assert entries, "positive control failed: ip.lock contains no Git registry packages"

    mirrors: dict[tuple[str, str], tuple[Path, str]] = {}
    compared = 0
    mismatches: list[str] = []

    for package, entry in entries:
        local_path = entry["local_path"].rstrip("/")
        installed = _package_files(local_path)
        if not installed:
            continue

        url = entry["url"]
        commit = entry["commit"]
        key = (url, commit)
        if key not in mirrors:
            scratch = tmp_path / f"upstream-{len(mirrors)}.git"
            mirrors[key] = _upstream_source(scratch, url, commit)
        mirror, revision = mirrors[key]

        locked_hashes = entry.get("file_hashes", {})
        for path in installed:
            prefix = f"{local_path}/"
            assert path.startswith(prefix), f"{path} escaped {local_path}"
            upstream_path = path.removeprefix(prefix)
            shown = _run(
                "git", "-C", str(mirror), "show", f"{revision}:{upstream_path}"
            )
            if shown.returncode != 0:
                mismatches.append(
                    f"{package}: {path} is absent at {commit}:{upstream_path}"
                )
                continue

            compared += 1
            actual = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            upstream = hashlib.sha256(shown.stdout).hexdigest()
            locked = locked_hashes.get(path)
            if actual != upstream or (locked is not None and locked != upstream):
                mismatches.append(
                    f"{package}: {path}\n"
                    f"  worktree {actual}\n"
                    f"  ip.lock  {locked or '<missing>'}\n"
                    f"  upstream {upstream} ({commit}:{upstream_path})"
                )

    assert compared > 0, "positive control failed: no upstream blobs were compared"
    assert not mismatches, (
        "hidden registry-package overlay(s) detected; upstream the change and "
        "repin instead of blessing local bytes in ip.lock:\n" + "\n".join(mismatches)
    )
