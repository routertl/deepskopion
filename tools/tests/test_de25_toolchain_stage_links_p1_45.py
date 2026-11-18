"""DE-G0 (DS-P1.45) — a staged cross-toolchain must be able to LINK.

Measured off-bench 2026-08-30 while building ARM Trusted Firmware for the DE25
boot chain on the rehearsal host, which has no vendor toolchain of its own:

    aarch64-linux-gnu-gcc -static t.c -o t.out
    collect2: fatal error: cannot find 'ld'

`tools/de25_toolchain_stage.py` strips the archive's top-level directory from
every member NAME, but wrote each symlink's target verbatim — and this
archive stores link targets relative to its own ROOT
(`bin/<prefix>-ld -> arm-gnu-toolchain-…/aarch64-none-linux-gnu/bin/ld.bfd`).
After stripping, every one of those points one level above the staged tree, so
they all DANGLE. The compiler still runs; only linking dies, and the failure
names `ld` rather than the staging step, which is why it reads as a missing
host package.

`stage()`'s entire acceptance check was `bin/<prefix>gcc` existing — true of a
tree in which nothing resolves. This bench never noticed because rr resolves
`aarch64-linux-gnu-gcc` to the Vitis-bundled copy on PATH here, so the staged
toolchain has never been the one that built anything locally. That is the same
class as DE-G0's other findings: a step that works only where local state
stands in for what the repository ships.

The guards build a REAL `.tar.xz` with the same shape and stage it, rather than
asserting on the linkname string: what matters is whether the staged symlink
RESOLVES, and only the filesystem can answer that.
"""
from pathlib import Path
import importlib.util
import os
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "de25_toolchain_stage", ROOT / "tools" / "de25_toolchain_stage.py")
TC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(TC)


def _archive(tmp_path: Path) -> Path:
    """A toolchain archive shaped like the real one.

    Two link members, both as ARM ships them: the compat `ld` inside the
    triple dir and the `<prefix>-ld` in `bin/`, each targeting a path that
    starts with the archive's top-level directory.
    """
    top = TC.TOOLCHAIN_DIR_NAME
    inner = f"{top}/aarch64-none-linux-gnu/bin"
    staging = tmp_path / "src"
    (staging / "bin").mkdir(parents=True)
    (staging / "inner").mkdir(parents=True)
    real_ld = staging / "inner" / "ld.bfd"
    real_ld.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real_ld.chmod(0o755)
    gcc = staging / "bin" / f"{TC.CROSS_PREFIX}gcc"
    gcc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gcc.chmod(0o755)

    archive = tmp_path / "toolchain.tar.xz"
    with tarfile.open(archive, "w:xz") as tar:
        tar.add(real_ld, arcname=f"{inner}/ld.bfd")
        tar.add(gcc, arcname=f"{top}/bin/{TC.CROSS_PREFIX}gcc")
        for name, target in (
                (f"{inner}/ld", f"{inner}/ld.bfd"),
                (f"{top}/bin/{TC.CROSS_PREFIX}ld", f"{inner}/ld.bfd")):
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return archive


def _unpack(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:xz") as package:
        members = [m for m in package.getmembers()
                   if m.name.startswith(TC.TOOLCHAIN_DIR_NAME + "/")]
        TC._unpack_members(package, members, dest)


def test_staged_symlinks_resolve_inside_the_staged_tree(tmp_path):
    """THE GUARD: the linker the toolchain ships must be reachable."""
    dest = tmp_path / "staged"
    dest.mkdir()
    _unpack(_archive(tmp_path), dest)

    ld = dest / "bin" / f"{TC.CROSS_PREFIX}ld"
    assert ld.is_symlink(), "the archive's ld link was not staged at all"
    assert ld.exists(), (
        f"the staged linker does not resolve: {ld} -> {os.readlink(ld)}. The "
        "archive's link target is relative to the archive ROOT, and staging "
        "strips that directory — a toolchain that compiles but cannot link.")
    assert (dest / "aarch64-none-linux-gnu" / "bin" / "ld").exists()


def test_the_staged_tree_stays_relocatable(tmp_path):
    """The links must survive the atomic temp-dir → dest rename.

    `stage()` unpacks into a temporary directory and `os.replace`s it into
    place, so an ABSOLUTE link target — the other obvious way to fix the
    dangle — would point into a path that no longer exists the moment staging
    succeeds. Moving the tree here is that rename.
    """
    dest = tmp_path / "staged"
    dest.mkdir()
    _unpack(_archive(tmp_path), dest)
    moved = tmp_path / "moved"
    os.replace(dest, moved)

    ld = moved / "bin" / f"{TC.CROSS_PREFIX}ld"
    assert not os.path.isabs(os.readlink(ld)), (
        f"staged link is absolute ({os.readlink(ld)}) — it would break on the "
        "temp-dir rename stage() performs")
    assert ld.exists(), "the staged linker stopped resolving after a move"


def test_publishing_a_toolchain_that_cannot_link_is_refused(tmp_path):
    """MUTATION, in-repo: reinstate the verbatim linkname, refuse the tree.

    `bin/<prefix>gcc` exists in this tree, which is exactly what the old
    acceptance check asked — so this test is what separates "staged" from
    "usable", and it fails on the pre-fix behaviour rather than on a string.
    """
    dest = tmp_path / "staged"
    dest.mkdir()
    with tarfile.open(_archive(tmp_path), "r:xz") as package:
        for member in package.getmembers():
            rel = Path(member.name).relative_to(TC.TOOLCHAIN_DIR_NAME)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                target.symlink_to(member.linkname)      # the pre-fix write
            elif member.isfile():
                target.write_bytes(package.extractfile(member).read())

    assert (dest / "bin" / f"{TC.CROSS_PREFIX}gcc").is_file(), (
        "fixture is wrong: the old check must PASS on this tree, or the "
        "mutation proves nothing")
    with pytest.raises(RuntimeError, match="do not resolve"):
        TC._refuse_dangling_links(dest)


def _cache_hit_tree(dest: Path) -> None:
    """A tree in exactly the shape the rehearsal met: `bin/<prefix>gcc` is a
    real file (so the cache-hit early return fires) and the linker beside it is
    a symlink that resolves to nothing."""
    (dest / "bin").mkdir(parents=True)
    gcc = dest / "bin" / f"{TC.CROSS_PREFIX}gcc"
    gcc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gcc.chmod(0o755)
    (dest / "bin" / f"{TC.CROSS_PREFIX}ld").symlink_to(
        "../aarch64-none-linux-gnu/bin/ld.bfd")   # nothing is there


def test_stage_audits_an_ALREADY_staged_tree_before_returning_it(
        tmp_path, monkeypatch):
    """LANDING-REFUSAL REGRESSION (2026-08-30, finding 4).

    `stage()` returned as soon as `bin/<prefix>gcc` existed, which is BEFORE
    `_refuse_dangling_links`. So the audit only ever saw a tree it had just
    unpacked — and the tree it was written for is a CACHE HIT, not a fresh
    unpack. Reproduced on the shipped code: with the tree below, `stage()`
    returned success while `_refuse_dangling_links(dest)` refused the very
    same directory. The previous guard asserted on the helper, which the
    early return skipped, so it could not see this.

    MUTATION (verified): delete the `_refuse_dangling_links(dest)` call from
    the early-return branch and this test goes RED — `stage()` returns `dest`
    and `pytest.raises` never fires.
    """
    monkeypatch.setattr(TC, "_fetch_pinned", lambda archive: None)
    dest = tmp_path / "staged"
    _cache_hit_tree(dest)

    # The old acceptance condition is TRUE here, or the mutation proves nothing.
    assert (dest / "bin" / f"{TC.CROSS_PREFIX}gcc").is_file()

    with pytest.raises(RuntimeError, match="do not resolve"):
        TC.stage(tmp_path / "cache", dest)


def test_stage_still_returns_a_HEALTHY_cached_tree_without_unpacking(
        tmp_path, monkeypatch):
    """POSITIVE CONTROL for the test above: the audit must refuse a broken
    cache, not every cache. A staged tree whose links resolve is returned as-is
    — and no archive is opened, which is the whole point of the early return
    (there is no archive in the cache dir here, so an unpack would raise)."""
    monkeypatch.setattr(TC, "_fetch_pinned", lambda archive: None)
    dest = tmp_path / "staged"
    (dest / "bin").mkdir(parents=True)
    for name in (f"{TC.CROSS_PREFIX}gcc", f"{TC.CROSS_PREFIX}ld"):
        binary = dest / "bin" / name
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
    (dest / "bin" / "cc").symlink_to(f"{TC.CROSS_PREFIX}gcc")   # resolves

    assert TC.stage(tmp_path / "cache", dest) == dest
