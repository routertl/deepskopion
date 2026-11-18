"""DS-P3.150: package install and target switch leave a clean checkout.

INPUT DOMAIN: every ``targets/*.yml`` file is a supported committed default,
not merely a transient selection layered over ``de25_datalogger``.

OUTPUT CONTRACT: from a fresh checkout committed with any one of those targets
active, ``rr pkg install`` succeeds and leaves ``git status --short`` empty.
``rr target set`` to a different vendor may rewrite ``project.yml`` but must
not rewrite ``ip.lock`` or add, remove, or modify a tracked file in ``libs/``.

PROHIBITION: target-specific package resolution must never discard the
Intel/Altera FIFO binding stubs Quartus needs for the untaken GEN_XPM branch.

RouteRTL 4.9.3 (RTL-T2.288) vendors every ``sources_per_vendor`` slice into
``libs/`` and records ``synth_only_files_by_vendor``. This file is a
pre-commit gate: a commit that reintroduces vendor-slice drift is refused.

DS-P2.180: the replays run side by side (``pkg_replays``), one clone and one
``rr`` process each, so the gate's wall clock does not scale with the number
of targets times bench load. Every target is still replayed and judged.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

import pytest
import yaml


ROOT = Path(
    os.environ.get("DS_PKG_INSTALL_SOURCE", Path(__file__).resolve().parents[2])
).resolve()
TARGETS = tuple(sorted((ROOT / "targets").glob("*.yml")))
#: DS-P1.103: targets/ holds exactly the shipped products (the committed defaults this guard replays).
PRODUCTS = {"de25_datalogger", "pfsoc_datalogger", "zybo_datalogger"}


def _resolved_target(target: Path, root: Path = ROOT) -> dict:
    """The target as rr resolves it.

    A target without ``extends:`` is read as written, exactly as before. An
    ``extends:`` target (DS-P2.264: ``de25_p2writer`` extends
    ``de25_datalogger``) is an overlay on its base and is resolved by rr's own
    loader (``sdk.cli.project_config._load_target_file``: dicts deep-merged,
    lists replaced), so its inherited hardware and packages are checked. Only
    if rr is not importable here is that rule applied in the test: the base,
    with the child's own top-level keys replacing the base's.
    """
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if "extends" not in raw:
        return raw
    try:
        from sdk.cli.project_config import _load_target_file
    except ImportError:
        merged = dict(_resolved_target(root / "targets" / f"{raw['extends']}.yml", root))
        merged.update({k: v for k, v in raw.items() if k != "extends"})
        return merged
    return _load_target_file(str(target.resolve()), source=__name__, base_dir=root)

DDR_STUBS = {
    "libs/routertl/rr_ddr_stream/rtl/support/intel/rr_fifo_async_mixed_xpm_body_stub.vhd",
    "libs/routertl/rr_ddr_stream/rtl/support/intel/rr_fifo_async_xpm_body_stub.vhd",
    "libs/routertl/rr_ddr_stream/rtl/support/intel/rr_fifo_sync_xpm_body_stub.vhd",
}


def _run(*argv: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


_STAGE_PATTERNS = (
    # DS-P3.241: the first line of a replay failure names the stage that failed, so the gate's
    # one-line pytest summary says where the replay stopped (the 2026-11-11 land log kept no
    # more than that line). Patterns are rr's own messages; first match wins.
    ("index fetch", re.compile(r"catalog fetch failure|not found in index catalog|"
                               r"index descriptor|IndexUnavailable|catalog could not be fetched")),
    ("clone", re.compile(r"Cache clone failed|git checkout \S+ failed|unable to access|"
                         r"Could not resolve host|CloneAuthError|clone failed")),
)


def _failure_stage(installer_output: str) -> str:
    """The rr stage a failed ``rr pkg install`` stopped in: index fetch, clone or install."""
    for stage, pattern in _STAGE_PATTERNS:
        if pattern.search(installer_output):
            return stage
    return "install"


def _lock() -> dict:
    return yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))


def _snapshot_index_tree(repo: Path) -> str:
    """Write-tree the repo's active index WITHOUT locking it, and return the tree OID.

    DS-T2.85: this runs as a pre-commit-gate test, i.e. while the very ``git
    commit`` that triggered the hook holds the worktree's ``index.lock``. A bare
    ``git write-tree`` has to refresh and re-persist the cache-tree extension of
    the index it reads, so it tries to acquire that same ``index.lock`` and dies
    with "Unable to create index.lock: File exists" — every snapshot case failed
    inside a commit while passing outside one (measured on the ds-t1-19 linked
    worktree, 2026-10-10). The commit form (plain vs pathspec) did not change it.

    The index this snapshot must capture is exactly the one a bare ``git
    write-tree`` would read: ``$GIT_INDEX_FILE`` when git set it for the hook
    (a partial commit's temporary index), else the worktree's own index. We copy
    that file to a throwaway path and ``write-tree`` against the COPY, so the only
    lock taken is on the copy — never on the live index the commit is holding.
    """
    env = dict(os.environ)
    gif = env.get("GIT_INDEX_FILE")
    if gif:
        active = Path(gif)
        if not active.is_absolute():
            active = (repo / active).resolve()
    else:
        resolved = subprocess.run(
            ["git", "rev-parse", "--git-path", "index"],
            cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            check=False,
        )
        assert resolved.returncode == 0, (
            f"could not resolve the index path:\n{resolved.stdout}")
        active = (repo / resolved.stdout.strip()).resolve()
    assert active.is_file(), f"active git index does not exist: {active}"

    fd, tmp_index = tempfile.mkstemp(prefix="ds_p3150_idx_")
    os.close(fd)
    try:
        shutil.copyfile(active, tmp_index)
        env["GIT_INDEX_FILE"] = tmp_index
        tree = subprocess.run(
            ["git", "write-tree"],
            cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, check=False,
        )
        assert tree.returncode == 0, f"could not snapshot Git index:\n{tree.stdout}"
        return tree.stdout.strip()
    finally:
        for leftover in (tmp_index, tmp_index + ".lock"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


def _staged_archive() -> bytes:
    """Tar of the exact staged tree (the index), as a commit would ship it."""
    tree_oid = _snapshot_index_tree(ROOT)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", tree_oid],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert archive.returncode == 0, (
        "could not archive Git index:\n"
        + archive.stderr.decode("utf-8", errors="replace")
    )
    return archive.stdout


def _pointer(target: Path) -> str:
    """project.yml's `target:` value for *target* (DS-P1.103)."""
    try:
        return target.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return f"targets/{target.name}"


def _index_snapshot(
    destination: Path, target: Path, archive: bytes | None = None
) -> None:
    """Commit the exact staged tree with ``target`` as its active default.

    ``archive`` is a ``_staged_archive()`` taken once for the whole replay
    matrix (DS-P2.180); every clone is still extracted from it fresh.
    """
    if archive is None:
        archive = _staged_archive()
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as payload:
        payload.extractall(destination, filter="data")

    (destination / "project.yml").write_text(
        "# DS-P3.150 fresh-clone target fixture.\n"
        # DS-P1.103: the target's real repo path (bench targets live in bench/targets/);
        # a fixture target outside the repo keeps the targets/<name> form.
        f"target: {_pointer(target)}\n",
        encoding="utf-8",
    )
    target_data = _resolved_target(target)
    target_pkgs = set(target_data.get("packages", {}).keys())
    # DS-P1.103: a `packages_lock: target` target is locked in targets/<t>.ip.lock, not ip.lock.
    lock_path = (destination / "targets" / f"{target.stem}.ip.lock"
                 if target_data.get("packages_lock") == "target" else destination / "ip.lock")
    if lock_path.is_file():
        lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
        existing_pkgs = {k for k in lock.keys() if k != "_meta"}
        if target_pkgs != existing_pkgs:
            # DS-P3.175: this install's result used to be discarded. When it
            # failed (registry catalog unreachable, so ``ctu-fee`` never
            # resolved), the clone was committed with the stale lock and the
            # replay reported a misleading `` M ip.lock`` instead of the cause.
            # DS-P3.176: with no ``rr`` on PATH the install used to be skipped
            # without a word, so the clone was committed with the stale lock
            # and the replay could pass vacuously. A missing rr now stops it.
            rr = shutil.which("rr")
            assert rr is not None, (
                f"rr is not on PATH: the fixture rr pkg install for "
                f"{target.stem} cannot run, and committing the clone with the "
                f"stale ip.lock would make the replay pass vacuously"
            )
            pre = _run(rr, "pkg", "install", cwd=destination)
            assert pre.returncode == 0, (
                f"[stage: {_failure_stage(pre.stdout)}] "
                f"fixture rr pkg install for {target.stem} exited "
                f"{pre.returncode} before the clone was committed:\n"
                f"{pre.stdout}"
            )
    init = _run("git", "init", "--quiet", cwd=destination)
    assert init.returncode == 0, init.stdout
    assert _run("git", "add", "-A", cwd=destination).returncode == 0
    commit = _run(
        "git",
        "-c",
        "user.name=DS-P3.150 guard",
        "-c",
        "user.email=guard.invalid",
        "commit",
        "--quiet",
        "-m",
        f"fresh clone with {target.stem} as committed default",
        cwd=destination,
    )
    assert commit.returncode == 0, commit.stdout


def test_snapshot_index_tree_survives_held_index_lock(tmp_path, monkeypatch) -> None:
    """DS-T2.85 regression: _snapshot_index_tree must succeed while the live
    index.lock is held — the exact condition during the pre-commit hook's own
    ``git commit``. The mutant/baseline is inline: a bare ``git write-tree`` on
    the same repo FAILS under the held lock (the pre-DS-T2.85 code path), so if
    the fix ever regresses to writing-tree the live index this reproduces the
    original breakage. Hermetic: a throwaway repo, its own lock, never the live
    worktree.
    """
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()

    def _g(*args: str) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(
            ["git", *args], cwd=repo, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        assert done.returncode == 0, f"git {' '.join(args)} failed:\n{done.stdout}"
        return done

    _g("init", "--quiet")
    _g("config", "user.email", "guard.invalid")
    _g("config", "user.name", "DS-T2.85 guard")
    (repo / "f").write_text("one\n", encoding="utf-8")
    _g("add", "f")
    _g("commit", "--quiet", "-m", "c1")
    (repo / "f").write_text("two\n", encoding="utf-8")
    _g("add", "f")  # a staged change for the snapshot to capture

    git_dir = Path(_g("rev-parse", "--git-dir").stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (repo / git_dir).resolve()
    lock = git_dir / "index.lock"
    lock.write_text("", encoding="utf-8")  # the in-flight commit holds this
    try:
        # MUTANT / baseline — the pre-DS-T2.85 path (bare write-tree on the live
        # index) dies under the held lock.
        bare = subprocess.run(
            ["git", "write-tree"], cwd=repo, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        assert bare.returncode != 0 and "index.lock" in bare.stdout, (
            "expected a bare `git write-tree` to fail while index.lock is held "
            f"(rc={bare.returncode}, out={bare.stdout!r}); if this passes, the "
            "regression no longer reproduces the DS-T2.85 hazard")

        # FIX — the lock-free snapshot succeeds and captures the STAGED tree.
        oid = _snapshot_index_tree(repo)
        assert re.fullmatch(r"[0-9a-f]{40}", oid), (
            f"_snapshot_index_tree did not return a tree OID: {oid!r}")
        blob = subprocess.run(
            ["git", "cat-file", "-p", f"{oid}:f"], cwd=repo, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        assert blob.returncode == 0 and blob.stdout == "two\n", (
            f"snapshot tree did not capture the staged blob: {blob.stdout!r}")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def test_index_snapshot_refuses_a_failed_fixture_install(
    tmp_path: Path, monkeypatch
) -> None:
    """DS-P3.175: a fixture ``rr pkg install`` that fails stops the replay.

    The staged lock pins ``a/b`` while the target declares ``c/d``, so the
    snapshot must install before committing. The ``rr`` on PATH fails the
    way the real one does when the registry catalog is unreachable. Without
    the check that exit code is dropped, the clone is committed with the
    stale lock, and the cause is lost. Hermetic: a fake ``rr``, a one-file
    archive, no registry access.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_rr = bin_dir / "rr"
    fake_rr.write_text(
        "#!/bin/sh\n"
        "echo 'Package c/d not found in index catalog.'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_rr.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        lock = b"_meta: {}\na/b: {}\n"
        info = tarfile.TarInfo("ip.lock")
        info.size = len(lock)
        tar.addfile(info, io.BytesIO(lock))
    target = tmp_path / "pinned.yml"
    target.write_text("packages:\n  c/d: '^1'\n", encoding="utf-8")

    refused = None
    try:
        _index_snapshot(tmp_path / "clone", target, payload.getvalue())
    except AssertionError as error:
        refused = str(error)
    assert refused is not None, (
        "a failed fixture rr pkg install did not stop the replay: "
        "the clone was committed with the stale lock"
    )
    assert "fixture rr pkg install for pinned exited 1" in refused
    assert "not found in index catalog" in refused


def test_index_snapshot_refuses_when_rr_is_not_on_path(
    tmp_path: Path, monkeypatch
) -> None:
    """DS-P3.176: no ``rr`` on PATH stops the replay and names the missing rr.

    The staged lock pins ``a/b`` while the target declares ``c/d``, so the
    snapshot needs the fixture install. Every PATH entry that holds an ``rr``
    is dropped, so ``git`` and the shell stay reachable and ``rr`` does not.
    Before the fix the install was skipped silently and the clone was
    committed with the stale lock. Hermetic: no registry access.
    """
    kept = [
        entry
        for entry in os.environ["PATH"].split(os.pathsep)
        if entry and not (Path(entry) / "rr").exists()
    ]
    monkeypatch.setenv("PATH", os.pathsep.join(kept))
    assert shutil.which("rr") is None, "rr is still reachable on the test PATH"
    assert shutil.which("git") is not None, "git is required for the fixture"

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        lock = b"_meta: {}\na/b: {}\n"
        info = tarfile.TarInfo("ip.lock")
        info.size = len(lock)
        tar.addfile(info, io.BytesIO(lock))
    target = tmp_path / "pinned.yml"
    target.write_text("packages:\n  c/d: '^1'\n", encoding="utf-8")

    refused = None
    try:
        _index_snapshot(tmp_path / "clone", target, payload.getvalue())
    except AssertionError as error:
        refused = str(error)
    assert refused is not None, (
        "rr missing from PATH did not stop the replay: the fixture install "
        "was skipped silently and the clone was committed with the stale lock"
    )
    assert "rr is not on PATH" in refused, refused
    assert "pinned" in refused, refused
    assert not (tmp_path / "clone" / ".git").exists(), (
        "the clone was committed even though rr was missing"
    )


# DS-P3.241: rr output for each stage. The index lines are what rr printed on 2026-11-11 when the
# registry catalog was unreachable (connection refused); the clone lines are rr's own clone
# failure messages (routertl_core/package_resolver.py); the install line matches no stage.
_STAGE_OUTPUTS = {
    "index fetch": (
        "Transient catalog fetch failure (1/3): <urlopen error [Errno 111] Connection refused>; "
        "retrying in 0.27s\n"
        "\u274c Package ctu-fee/ctucanfd_ip_core not found in index catalog.\n"
    ),
    "clone": (
        "  Cache clone failed \u2014 falling back to the network\n"
        "git checkout b76a8888ecd1 failed for https://example.invalid/core.git:\n"
        "fatal: unable to access 'https://example.invalid/core.git/': Could not resolve host\n"
    ),
    "install": "\u274c ip.lock conflict: open-logic/olo_base pinned 4.4.1, target asks ^5.0.0\n",
}


@pytest.mark.parametrize("stage", sorted(_STAGE_OUTPUTS))
def test_failure_stage_names_the_rr_stage(stage: str) -> None:
    assert _failure_stage(_STAGE_OUTPUTS[stage]) == stage


@pytest.mark.parametrize("stage", sorted(_STAGE_OUTPUTS))
def test_failed_fixture_install_names_its_stage_on_the_first_line(
    stage: str, tmp_path: Path, monkeypatch
) -> None:
    """DS-P3.241: a fixture install that fails in a given rr stage says so on its first line.

    pytest's one-line summary carries only the first line of the message, and on 2026-11-11 that
    line was all the land log kept. A fake ``rr`` prints the stage's real output and exits 1.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out_file = tmp_path / "rr_output.txt"
    out_file.write_text(_STAGE_OUTPUTS[stage], encoding="utf-8")
    fake_rr = bin_dir / "rr"
    fake_rr.write_text(f"#!/bin/sh\ncat '{out_file}'\nexit 1\n", encoding="utf-8")
    fake_rr.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        lock = b"_meta: {}\na/b: {}\n"
        info = tarfile.TarInfo("ip.lock")
        info.size = len(lock)
        tar.addfile(info, io.BytesIO(lock))
    target = tmp_path / "pinned.yml"
    target.write_text("packages:\n  c/d: '^1'\n", encoding="utf-8")

    with pytest.raises(AssertionError) as refused:
        _index_snapshot(tmp_path / "clone", target, payload.getvalue())
    first = str(refused.value).splitlines()[0]
    assert first.startswith(f"[stage: {stage}] fixture rr pkg install for pinned exited 1"), first


def test_every_target_file_is_in_the_install_matrix() -> None:
    """Positive control: the dynamic matrix includes both vendor slices."""
    assert TARGETS
    vendors = {
        _resolved_target(path)["hardware"]["vendor"]
        for path in TARGETS
    }
    assert {"altera", "xilinx"} <= vendors


def test_de25_xpm_body_stubs_are_tracked_hash_pinned_synth_inputs() -> None:
    """Done-when (c): DE25 binder support exists here and in the lock."""
    entry = _lock()["routertl/rr_ddr_stream"]
    target = yaml.safe_load(
        (ROOT / "targets/de25_datalogger.yml").read_text(encoding="utf-8")
    )
    vendor = target["hardware"]["vendor"]
    assert vendor == "altera"
    assert set(entry["synth_only_files_by_vendor"][vendor]) == DDR_STUBS
    assert DDR_STUBS <= set(entry["synth_only_files"])
    assert set(entry["synth_only_files_by_vendor"]["xilinx"]).isdisjoint(DDR_STUBS)
    tracked = _run("git", "ls-files", *sorted(DDR_STUBS))
    assert tracked.returncode == 0, tracked.stdout
    assert set(tracked.stdout.splitlines()) == DDR_STUBS
    for relpath in DDR_STUBS:
        digest = hashlib.sha256((ROOT / relpath).read_bytes()).hexdigest()
        assert entry["file_hashes"].get(relpath) == digest, (
            f"un-pinned or stale rr_ddr_stream Intel stub: {relpath}"
        )


def _install_replay(clone: Path, target: Path, archive: bytes) -> dict:
    """Snapshot ``target`` as the committed default, then ``rr pkg install``.

    Only the CLI work happens here; every verdict stays in the test that
    reads the returned record, so each target still fails on its own.
    """
    rr = shutil.which("rr")
    assert rr is not None, "rr is required in the fresh-clone deployment lane"
    _index_snapshot(clone, target, archive)
    before = _run("git", "status", "--short", cwd=clone)
    active = yaml.safe_load((clone / "project.yml").read_text(encoding="utf-8"))
    installed = _run(rr, "pkg", "install", cwd=clone)
    status = _run("git", "status", "--short", cwd=clone)
    return {
        "clone": clone,
        "before": before,
        "active": active,
        "installed": installed,
        "status": status,
    }


#: Where `rr target set` keeps the choice since RouteRTL RTL-P2.2179
#: (gitignored state; project.yml is no longer edited).
_LOCAL_ACTIVE_TARGET = Path(".routertl") / "local" / "active-target"


def _active_target(clone: Path) -> dict:
    """The active target as rr resolves it when RR_TARGET is unset:
    ``.routertl/local/active-target`` first (RouteRTL RTL-P2.2179, which
    stopped `rr target set` editing project.yml), else project.yml's
    ``target:``. Both layouts are real: the published 5.3.0 that the public
    CI floor leg installs still rewrites project.yml."""
    local = clone / _LOCAL_ACTIVE_TARGET
    if local.is_file():
        value = local.read_text(encoding="utf-8", errors="replace").strip()
        if value:
            return {"target": value}
    return yaml.safe_load((clone / "project.yml").read_text(encoding="utf-8")) or {}


def _target_set_replay(clone: Path, archive: bytes) -> dict:
    """Snapshot de25_datalogger, then ``rr target set zybo_datalogger``."""
    rr = shutil.which("rr")
    assert rr is not None, "rr is required in the fresh-clone deployment lane"
    _index_snapshot(clone, ROOT / "targets" / "de25_datalogger.yml", archive)
    switched = _run(rr, "target", "set", "zybo_datalogger", cwd=clone)
    active = _active_target(clone)
    status = _run("git", "status", "--short", cwd=clone)
    return {
        "clone": clone,
        "switched": switched,
        "active": active,
        "status": status,
    }


def _default_target() -> Path:
    project_yml = yaml.safe_load((ROOT / "project.yml").read_text(encoding="utf-8"))
    return ROOT / project_yml.get("target", "targets/de25_datalogger.yml")


def _libraries_cache_file(clone: Path, target: Path) -> Path:
    """The package-library cache rr writes for ``target`` in ``clone``.

    RouteRTL RTL-P2.2270 made the cache per target:
    ``.routertl_cache/libraries/<target>.json`` for a project with targets, named
    by ``routertl_core.lib_cache.libraries_cache_path``. The name is taken from
    that helper, keyed on the target FILE, so it does not depend on RR_TARGET.
    An rr without the helper (the public-CI floor, before RTL-P2.2270) writes the
    flat ``.routertl_cache/libraries.json``. Callers assert the returned file
    exists, so neither branch can pass on a missing cache (DS-P2.263).
    """
    try:
        from routertl_core.lib_cache import libraries_cache_path
    except ImportError:
        return clone / ".routertl_cache" / "libraries.json"
    return libraries_cache_path(clone, {"_target_file": str(clone / "targets" / target.name)})


# DS-P2.180: every fresh clone downloads the ~9.3 MB registry catalog into its
# own empty .routertl_cache. With all ~19 replays at once the registry cut the
# transfers short (``IncompleteRead: 79344 bytes read, 9264392 more expected``,
# measured 2026-10-15) and the CAN replays could not resolve ctu-fee. Four at a
# time keeps each clone genuinely fresh (no seeded cache) at a registry load
# like four agents committing together.
_REPLAY_CONCURRENCY = 4


def _replay_workers(jobs: int) -> int:
    """Concurrent replays: ``_REPLAY_CONCURRENCY``, never more than the jobs."""
    return max(1, min(jobs, _REPLAY_CONCURRENCY))


def _replay_all(
    jobs: dict[str, Callable[[], dict]], workers: int
) -> tuple[dict[str, Future], dict[str, tuple[float, float]]]:
    """Run every replay job concurrently and record each one's wall-clock span.

    DS-P2.180: run one after another, the 22 replays took 5 min at load 36
    and 37-44 min at load 95-100, which outlasted the dispatch budget for any
    DS commit. Each replay is an independent clone with its own ``rr``
    process, so running them side by side keeps the full matrix and every
    assertion and only changes the wall clock. Under a saturated scheduler
    each runnable process gets a fair share, so N replays in parallel get
    roughly N times the CPU of one. rr serialises its package cache itself
    (``_pkg_cache_lock``).
    """
    spans: dict[str, tuple[float, float]] = {}

    def timed(name: str, job: Callable[[], dict]) -> dict:
        start = time.monotonic()
        try:
            return job()
        finally:
            spans[name] = (start, time.monotonic())

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {name: pool.submit(timed, name, job) for name, job in jobs.items()}
    return futures, spans


@pytest.fixture(scope="module")
def pkg_replays(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[dict[str, Future], dict[str, tuple[float, float]], int]:
    """Every clean-clone replay of the staged tree, run concurrently once."""
    root = tmp_path_factory.mktemp("ds_p3150_replays")
    archive = _staged_archive()
    jobs: dict[str, Callable[[], dict]] = {
        f"install:{target.stem}": (
            lambda t=target: _install_replay(root / f"fresh-{t.stem}", t, archive)
        )
        for target in TARGETS
    }
    jobs["target-set"] = lambda: _target_set_replay(
        root / "fresh-target-set", archive
    )
    jobs["can-free-default"] = lambda: _install_replay(
        root / "fresh-can-free-default", _default_target(), archive
    )
    workers = _replay_workers(len(jobs))
    futures, spans = _replay_all(jobs, workers)
    return futures, spans, workers


def _peak_concurrency(spans: dict[str, tuple[float, float]]) -> int:
    events = sorted(
        [(start, 1) for start, _ in spans.values()]
        + [(end, -1) for _, end in spans.values()]
    )
    running = peak = 0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    return peak


# The replay matrix the gate schedules: one install per target plus the
# target-set and CAN-free-default replays (DS-P1.103: the bench CAN-opt-in
# replay runs in the _bench.py sibling's own fixture).
_REPLAY_JOBS = len(TARGETS) + 2


def test_the_replayed_defaults_are_exactly_the_shipped_products() -> None:
    """DS-P1.103: the input domain (every targets/*.yml) is the three products."""
    assert {t.stem for t in TARGETS} == PRODUCTS


def test_replay_all_runs_jobs_side_by_side() -> None:
    """DS-P2.180: four jobs that each wait for the others only finish if the
    runner gives them separate workers. Run one after another, the barrier
    breaks after its timeout and every job fails.
    """
    barrier = threading.Barrier(4, timeout=20)

    def job() -> dict:
        barrier.wait()
        return {}

    jobs = {name: job for name in "abcd"}
    futures, spans = _replay_all(jobs, _replay_workers(_REPLAY_JOBS))
    for name, future in futures.items():
        assert future.exception() is None, (
            f"replay job {name!r} did not run alongside its siblings: "
            f"{future.exception()!r}"
        )
    assert set(spans) == set(jobs)


def test_replay_all_caps_the_registry_burst() -> None:
    """DS-P2.180: the runner never has more than four replays in flight.

    Eight jobs that each hold for a moment would all overlap if the runner
    allowed it; the cap is what keeps the fresh-clone catalog downloads to
    four at a time.
    """
    def job() -> dict:
        time.sleep(0.3)
        return {}

    futures, spans = _replay_all(
        {f"job{n}": job for n in range(8)}, _replay_workers(_REPLAY_JOBS)
    )
    assert all(future.exception() is None for future in futures.values())
    peak = _peak_concurrency(spans)
    assert peak <= 4, (
        f"the replay runner put {peak} jobs in flight at once; "
        "DS-P2.180 caps the registry burst at 4 concurrent fresh clones"
    )


def test_clean_clone_replays_ran_concurrently(pkg_replays) -> None:
    """DS-P2.180: the real ``rr`` replays overlapped in time.

    Measured from the spans of the replays this module ran, not from the
    runner in isolation: a fixture that went back to one replay at a time
    shows a peak of 1 here and fails. The floor is a literal, not derived
    from the configured worker count, so lowering that count fails too.
    """
    futures, spans, workers = pkg_replays
    assert set(spans) == set(futures)
    peak = _peak_concurrency(spans)
    floor = min(4, len(spans))
    assert peak >= floor, (
        f"clean-clone replays ran {peak} at a time (workers={workers}, "
        f"jobs={len(spans)}); DS-P2.180 needs >= {floor} side by side or the "
        "gate outlasts a dispatch budget under bench load"
    )


def test_clean_clone_replays_stay_under_the_registry_cap(pkg_replays) -> None:
    """DS-P2.180: no more than four fresh clones fetch the catalog at once.

    Each replay downloads the ~9.3 MB registry catalog. With every replay at
    once the registry cut the transfers short and the CAN replays failed, so
    the parallel gate is only reliable while its burst stays bounded.
    """
    futures, spans, workers = pkg_replays
    assert set(spans) == set(futures)
    peak = _peak_concurrency(spans)
    assert peak <= 4, (
        f"clean-clone replays ran {peak} at a time (workers={workers}); "
        "DS-P2.180 caps the registry burst at 4 concurrent fresh clones"
    )


@pytest.mark.parametrize("target", TARGETS, ids=lambda path: path.stem)
def test_rr_pkg_install_leaves_each_target_clone_clean(
    pkg_replays, target: Path
) -> None:
    """Real CLI guard over every supported committed target (RTL-T2.288)."""
    replay = pkg_replays[0][f"install:{target.stem}"].result()
    clone = replay["clone"]

    # Positive control: a broken fixture that starts dirty makes the post-state
    # meaningless, and a target pointer that was not changed repeats the old
    # default-only false green for every parameter.
    before = replay["before"]
    assert before.returncode == 0 and before.stdout == "", before.stdout
    active = replay["active"]
    assert active == {"target": f"targets/{target.name}"}

    installed = replay["installed"]
    assert installed.returncode == 0, (
        f"[stage: {_failure_stage(installed.stdout)}] "
        f"rr pkg install exited {installed.returncode} for {target.stem}:\n"
        f"{installed.stdout}"
    )
    status = replay["status"]
    assert status.returncode == 0, status.stdout
    assert status.stdout == "", (
        f"[stage: clean status] rr pkg install dirtied a fresh {target.stem} clone:\n"
        f"{status.stdout}\ninstaller output:\n{installed.stdout}"
    )
    missing = [path for path in sorted(DDR_STUBS) if not (clone / path).is_file()]
    assert not missing, (
        "rr pkg install removed a DE25-required XPM-body stub:\n"
        + "\n".join(missing)
    )
    if target.name == _default_target().name:
        cache_file = _libraries_cache_file(clone, target)
        assert cache_file.is_file(), f"missing library cache: {cache_file}"
        libs = json.loads(cache_file.read_text(encoding="utf-8"))
        assert "ctu_can_fd_rtl" not in libs, (
            f"Regression DS-P2.121: rr pkg install on default target {target.stem} "
            f"included ctu_can_fd_rtl in library closure, but default targets must be CTU-free"
        )


def test_target_set_does_not_rewrite_lock_or_libs(pkg_replays) -> None:
    """Caller surface: ``rr target set zybo_datalogger`` must not prune Intel stubs.

    On RouteRTL 4.9.2 this command alone rewrote ``ip.lock`` and deleted the
    three committed ``*_xpm_body_stub.vhd`` files. GP-R0 for Zybo starts here.
    """
    replay = pkg_replays[0]["target-set"].result()
    clone = replay["clone"]

    switched = replay["switched"]
    assert switched.returncode == 0, (
        f"rr target set zybo_datalogger exited {switched.returncode}:\n"
        f"{switched.stdout}"
    )
    active = replay["active"]
    assert active.get("target") == "targets/zybo_datalogger.yml", active
    status = replay["status"]
    assert status.returncode == 0, status.stdout
    drifted = [
        line
        for line in status.stdout.splitlines()
        if line[3:].startswith("ip.lock") or "/libs/" in f"/{line[3:]}"
        or line[3:] == "libs" or line[3:].startswith("libs/")
    ]
    assert drifted == [], (
        "rr target set zybo_datalogger rewrote the committed package slice:\n"
        f"{status.stdout}"
    )
    missing = [path for path in sorted(DDR_STUBS) if not (clone / path).is_file()]
    assert not missing, (
        "rr target set zybo_datalogger deleted a DE25-required XPM-body stub:\n"
        + "\n".join(missing)
    )


def test_precommit_gate_runs_pkg_install_clean_clone() -> None:
    """Arming: a guard nothing invokes is the false-green this ticket filed."""
    hook = (ROOT / "hooks" / "pre-commit.local").read_text(encoding="utf-8")
    assert "test_pkg_install_clean_clone_p3_150.py" in hook
    assert "DS-P3.150" in hook


def _check_ctu_package_declarations(targets_dir: Path, can_opt_in: set[str]) -> None:
    """The per-target CTU opt-in loop (DS-P2.121); takes the directory so the
    DS-P2.264 extends case can be driven through it."""
    for target_path in targets_dir.glob("*.yml"):
        target_data = _resolved_target(target_path, targets_dir.parent)
        has_ctu = "ctu-fee/ctucanfd_ip_core" in target_data.get("packages", {})
        if target_path.name in can_opt_in:
            assert has_ctu, (
                f"{target_path.name} is a CAN opt-in target and must declare "
                "ctu-fee/ctucanfd_ip_core in packages:"
            )
            canfd_libs = target_data.get("sources", {}).get("libraries", {})
            assert "ctu_can_fd_rtl" not in canfd_libs, (
                f"{target_path.name} must compile the real CTU IP core from "
                "packages, not the vendor stub"
            )
        else:
            assert not has_ctu, (
                f"{target_path.name} must NOT declare ctu-fee/ctucanfd_ip_core in packages: "
                "(clause 2: pulled ONLY by the CAN opt-in)"
            )


def test_target_package_declarations_ctu_opt_in() -> None:
    """DS-P2.121 (+DS-P2.135): target package declarations enforce CTU CAN FD
    opt-in.

    - The CAN opt-in targets — canfd_demo (Zybo) and de25_canfd (its DE25
      analogue, DS-P2.135) — MUST declare ctu-fee/ctucanfd_ip_core in
      packages:, and sources.libraries must NOT compile the dead stub.
    - All other targets (de25_datalogger, zybo_datalogger, classic_eye,
      eye_loopback, de25_core_baseline) are CAN-free and must NOT declare
      ctu-fee/ctucanfd_ip_core.
    """
    targets_dir = ROOT / "targets"
    # DS-P1.103 split: the shipped products, with no opt-in exemption; every one is seen. The
    # opt-in half (the bench CTU targets) runs in the _bench.py sibling.
    assert targets_dir.is_dir()
    _check_ctu_package_declarations(targets_dir, set())
    seen = {p.stem for p in targets_dir.glob("*.yml")}
    assert PRODUCTS <= seen, f"products not found: {sorted(PRODUCTS - seen)}"

    # Stub assertions for default targets
    for target_name in ("zybo_datalogger.yml", "de25_datalogger.yml"):
        tdata = yaml.safe_load((targets_dir / target_name).read_text(encoding="utf-8"))
        libs = tdata.get("sources", {}).get("libraries", {})
        assert "ctu_can_fd_rtl" in libs, f"{target_name} must provide ctu_can_fd_rtl library stub"
        assert "tools/sim/vendor_stubs/ctu_can_fd/can_top_apb_stub.vhd" in libs["ctu_can_fd_rtl"], (
            f"{target_name} must point ctu_can_fd_rtl to can_top_apb_stub.vhd"
        )


def test_zybo_datalogger_omits_ctu_dt_kernel_and_pblock() -> None:
    """DS-P2.121: zybo_datalogger target configuration omits all CTU artefacts."""
    zybo_target = yaml.safe_load((ROOT / "targets" / "zybo_datalogger.yml").read_text(encoding="utf-8"))

    # Assert pblock_ctu.xdc omitted
    xdc_files = zybo_target.get("sources", {}).get("xdc", [])
    assert not any("pblock_ctu" in x for x in xdc_files), (
        f"zybo_datalogger must omit pblock_ctu.xdc, found: {xdc_files}"
    )

    # Assert kernel_ctucanfd.config-fragment omitted
    kernel_config = zybo_target.get("linux", {}).get("kernel_config", "")
    assert "kernel_ctucanfd" not in kernel_config, (
        f"zybo_datalogger must omit kernel_ctucanfd fragment, found: {kernel_config}"
    )

    # Assert CTU device-tree nodes omitted
    dt_nodes = zybo_target.get("linux", {}).get("dt_nodes", [])
    for node in dt_nodes:
        path = node.get("path", "")
        props = node.get("properties", {})
        assert props.get("compatible") != "ctu,ctucanfd", (
            f"zybo_datalogger must not declare ctu,ctucanfd node: {path}"
        )
        assert not path.startswith("/axi/can@8"), (
            f"zybo_datalogger must not declare CTU CAN DT node: {path}"
        )

    # Assert model string identifies zybo_datalogger
    root_node = next((n for n in dt_nodes if n.get("path") == "/"), None)
    assert root_node is not None
    assert root_node.get("properties", {}).get("model") == (
        "deepskopion zybo_datalogger (Digilent Zybo Z7-20, xc7z020)"
    )


def test_de25_datalogger_omits_ctu_dt_nodes() -> None:
    """DS-P2.121 Finding 2: de25_datalogger target configuration omits all CTU DT nodes."""
    de25_target = yaml.safe_load((ROOT / "targets" / "de25_datalogger.yml").read_text(encoding="utf-8"))
    dt_nodes = de25_target.get("linux", {}).get("dt_nodes", [])
    for node in dt_nodes:
        path = node.get("path", "")
        props = node.get("properties", {})
        assert "ctu" not in str(props.get("compatible", "")).lower(), (
            f"de25_datalogger must not declare ctu,ctucanfd node: {path}"
        )
        assert not str(path).startswith("/soc@0/can@"), (
            f"de25_datalogger must not declare CTU CAN DT node: {path}"
        )


def test_default_datalogger_config_and_pkg_carry_one_can_datalogger() -> None:
    """DS-P1.104 (DS-D.53, owner 2026-11-13): the default datalogger config and the
    generated package carry exactly one CAN datalogger, device_id 20. It replaces the
    DS-P2.121 Finding 4 clause that kept them CAN-free; the CAN datalogger is project
    RTL, and the CTU core stays out of the default target (DS-D.43, next test)."""
    import re
    config_path = ROOT / "tools" / "datalogger-builder" / "config" / "datalogger_config.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    dataloggers = config.get("Dataloggers", {})
    burst = dataloggers.get("Burst", {})
    assert burst.get("Can") == {20: {"RX": 28}}, (
        f"Default mix {config_path.name} must carry exactly one CAN datalogger, id 20 (DS-D.53)"
    )

    pkg_path = ROOT / "hw" / "rtl" / "pkg" / "datalogger_config_pkg.vhd"
    pkg_text = pkg_path.read_text(encoding="utf-8")
    m = re.search(r"constant\s+C_CAN_DATALOGGERS\s*:\s*integer\s*:=\s*(\d+)\s*;", pkg_text)
    assert m is not None, "C_CAN_DATALOGGERS constant missing from datalogger_config_pkg.vhd"
    assert int(m.group(1)) == 1, (
        f"C_CAN_DATALOGGERS in {pkg_path.name} is {m.group(1)}, expected 1 (DS-D.53)"
    )


def test_clean_clone_of_can_free_default_target_has_no_ctu_fee(pkg_replays) -> None:
    """DS-P2.121 regression guard: clean clone of CAN-free default target has no ctu-fee.

    Decision DS-D.43: default target (project.yml -> de25_datalogger) builds without
    the CTU CAN FD core. DS-D.53 put the CAN datalogger (project RTL) back in the
    default mix and left this CTU clause in force. RouteRTL 5.0 (RTL-P2.1325) vendors declared packages across
    all targets into libs/ but scopes .routertl_cache/libraries.json strictly to the
    active target's dependency closure. For the CAN-free default target, ctu_can_fd_rtl
    must NOT be present in libraries.json.
    """
    replay = pkg_replays[0]["can-free-default"].result()
    clone = replay["clone"]
    default_target = _default_target()

    installed = replay["installed"]
    assert installed.returncode == 0, (
        f"rr pkg install exited {installed.returncode}:\n{installed.stdout}"
    )
    status = replay["status"]
    assert status.returncode == 0, status.stdout
    assert status.stdout == "", f"dirty status after pkg install: {status.stdout}"

    cache_file = _libraries_cache_file(clone, default_target)
    assert cache_file.is_file(), f"missing library cache: {cache_file}"
    libs = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "ctu_can_fd_rtl" not in libs, (
        f"Regression DS-P2.121: ctu_can_fd_rtl must not be in library closure on "
        f"CAN-free default target {default_target.stem}, but found in: {list(libs.keys())}"
    )


def test_active_target_reads_the_local_choice_before_project_yml(tmp_path) -> None:
    """RouteRTL >= RTL-P2.2179: `rr target set` writes the local state file."""
    (tmp_path / "project.yml").write_text("target: targets/de25_datalogger.yml\n", encoding="utf-8")
    local = tmp_path / _LOCAL_ACTIVE_TARGET
    local.parent.mkdir(parents=True)
    local.write_text("targets/zybo_datalogger.yml\n", encoding="utf-8")
    assert _active_target(tmp_path) == {"target": "targets/zybo_datalogger.yml"}


def test_active_target_falls_back_to_project_yml(tmp_path) -> None:
    """RouteRTL 5.3.0: `rr target set` rewrites project.yml, no local file."""
    (tmp_path / "project.yml").write_text("target: targets/zybo_datalogger.yml\n", encoding="utf-8")
    assert _active_target(tmp_path) == {"target": "targets/zybo_datalogger.yml"}
    empty = tmp_path / _LOCAL_ACTIVE_TARGET
    empty.parent.mkdir(parents=True)
    empty.write_text("\n", encoding="utf-8")
    assert _active_target(tmp_path) == {"target": "targets/zybo_datalogger.yml"}


def _child_of(tmp_path: Path, base_body: str) -> Path:
    (tmp_path / "targets").mkdir()
    (tmp_path / "targets" / "base.yml").write_text(base_body, encoding="utf-8")
    child = tmp_path / "targets" / "child.yml"
    child.write_text("extends: base\nproject:\n  output_dir: build/child\n", encoding="utf-8")
    return child


def test_an_extends_target_inherits_its_base_hardware_vendor(tmp_path) -> None:
    """DS-P2.264 red-first: the install matrix sees an extends target's vendor."""
    child = _child_of(tmp_path, "hardware:\n  vendor: altera\n")
    assert _resolved_target(child, tmp_path)["hardware"]["vendor"] == "altera"


def test_an_extends_target_of_a_ctu_opt_in_base_is_seen_as_opted_in(tmp_path) -> None:
    """DS-P2.264 red-first: the CTU opt-in check is not vacuous for an extends
    target; a child whose base declares ctu-fee/ctucanfd_ip_core carries it."""
    child = _child_of(tmp_path, "packages:\n  ctu-fee/ctucanfd_ip_core: '*'\n")
    assert "ctu-fee/ctucanfd_ip_core" in _resolved_target(child, tmp_path).get("packages", {})
    # Through the check itself: the child is not a named opt-in target, so the
    # inherited CTU package must be refused (a raw read of the child would pass).
    with pytest.raises(AssertionError, match="child.yml must NOT declare"):
        _check_ctu_package_declarations(tmp_path / "targets", {"base.yml"})
