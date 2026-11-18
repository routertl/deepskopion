#!/usr/bin/env python3
"""Stage the standalone aarch64 cross-toolchain the DE25 Linux flow needs
(DS-P2.92).

The Vitis-bundled ``aarch64-linux-gnu-gcc``
(``/tools/Xilinx/Vitis/2024.1/gnu/aarch64/lin/aarch64-linux/bin``) satisfies
``rr linux build`` today, but the-environment-is-a-test-input rule forbids
that bundled copy becoming a *requirement*: a bench without Vitis installed
must still be able to build the DE25 boot chain. This script downloads,
checksum-verifies, and atomically stages a standalone ARM GNU toolchain
release (``aarch64-none-linux-gnu``) independent of any Vitis install,
following the same pinned-fetch/atomic-publish shape as
``tools/de25_linux_image.py``'s CPython/msgpack staging.

``status`` additionally answers the acceptance question directly: given the
*installed* RouteRTL SDK's own cross-toolchain resolution order (imported
live, never re-implemented here — a copy would silently drift), which
``aarch64-linux-gnu-gcc`` would ``rr linux build`` actually invoke right now
— the staged standalone toolchain, or a bundled vendor copy?
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]

TOOLCHAIN_VERSION = "13.3.rel1"
TOOLCHAIN_ARCHIVE = (
    "arm-gnu-toolchain-13.3.rel1-x86_64-aarch64-none-linux-gnu.tar.xz"
)
# The developer.arm.com download page 302-redirects here; fetched directly
# to avoid an extra hop (and a URL path segment public-artifact hygiene
# scanning mistakes for a removable-media mount point, e.g. /media/<user>/).
TOOLCHAIN_URL = (
    "https://armkeil.blob.core.windows.net/developer/Files/downloads/gnu/"
    "13.3.rel1/binrel/" + TOOLCHAIN_ARCHIVE
)
# Verified 2026-08-22 against the ARM-published detached checksum sidecar
# (same host, .sha256asc suffix), not merely self-computed from the
# download — the same independent-source standard as PYTHON_SHA256 in
# tools/de25_linux_image.py.
TOOLCHAIN_SHA256 = (
    "322f0b4482fc0d9fa0bb468134841f08d8c554c54ff5aa29a13a7a24bf7e1eb5"
)
# Top-level directory name inside the archive; also the CROSS_COMPILE prefix
# this toolchain's binaries carry (distinct from Vitis's "aarch64-linux-gnu-"
# — see the "not a requirement" note above).
TOOLCHAIN_DIR_NAME = "arm-gnu-toolchain-13.3.rel1-x86_64-aarch64-none-linux-gnu"
CROSS_PREFIX = "aarch64-none-linux-gnu-"
# The RouteRTL SDK's socfpga backend hard-codes CROSS_COMPILE to this exact
# prefix (sdk.cli.commands.linux_steps._socfpga::_AARCH64_CROSS) with no
# project.yml/env override — apt's gcc-aarch64-linux-gnu and Vitis's bundled
# copy both ship under it. The standalone ARM release ships under
# CROSS_PREFIX above instead, so without a same-name alias it could never be
# found by that hard-coded prefix even if it won PATH precedence over Vitis.
COMPAT_PREFIX = "aarch64-linux-gnu-"

# U-Boot host prerequisites for the declared host, Ubuntu 22.04 (jammy;
# tools/preflight/supported_environment.yml host.os). These are fixed archive
# URLs, not `apt install`: staging is rootless and each byte string is checked
# before dpkg-deb may extract it.
#
# DS-P1.94: the pin was Ubuntu 24.04's 3.0.13-0ubuntu3.15 (libssl3t64), whose
# libcrypto.so.3 needs GLIBC_2.38. On the declared 22.04 host (glibc 2.35)
# every program that loaded it failed, and build_env() put it on
# LD_LIBRARY_PATH for all of `rr linux uboot`, so the step died at its first
# `git clone` (git-remote-https: "GLIBC_2.38' not found"; DE-G0 on the bench,
# 2026-11-07). Earlier DE-G0 step-4 passes ran on Ubuntu 24.04, an undeclared
# host. The sha256 pins below are those of the signed jammy-updates Packages
# index (InRelease verified with the Ubuntu archive key, 2026-11-07).
OPENSSL_VERSION = "3.0.2-0ubuntu1.30"
OPENSSL_DEV_ARCHIVE = f"libssl-dev_{OPENSSL_VERSION}_amd64.deb"
OPENSSL_RUNTIME_ARCHIVE = f"libssl3_{OPENSSL_VERSION}_amd64.deb"
#: glibc of the declared host (Ubuntu 22.04 ships glibc 2.35). Every pinned
#: library's `glibc_max` must not exceed it; test_de25_toolchain_stage.py
#: holds this constant to the declaration's host.os.version (DS-P1.94).
DECLARED_HOST_OS_VERSION = "22.04"
DECLARED_HOST_GLIBC = "2.35"
# Launchpad's librarian, not an archive pool: a pool keeps only the current
# version, and security.ubuntu.com dropped the previous pin's debs once a newer
# build superseded them (both URLs 404'd on 2026-11-06). The librarian keeps
# every published version and serves the same bytes.
# tools/tests/test_de25_pinned_urls_permanent_ds_p1_91.py guards this (DS-P1.91).
# `glibc_max` is the highest GLIBC_ symbol version the package's shared
# libraries need (ELF verneed), measured from these exact bytes.
OPENSSL_BASE_URL = "https://launchpad.net/ubuntu/+archive/primary/+files"
OPENSSL_INPUTS = {
    OPENSSL_DEV_ARCHIVE: {
        "url": f"{OPENSSL_BASE_URL}/{OPENSSL_DEV_ARCHIVE}",
        "sha256": "8a47dfa7f9bd4d54ac9dc9a1db14891a9133841bf44f12c3db086f2d4321bac6",
        "glibc_max": None,  # headers and the libssl.so/libcrypto.so links only
    },
    OPENSSL_RUNTIME_ARCHIVE: {
        "url": f"{OPENSSL_BASE_URL}/{OPENSSL_RUNTIME_ARCHIVE}",
        "sha256": "d1b4636e32ac6d3b95d583ac05ac548b1aa248414bae1c51b14f54be0f8fd7bc",
        "glibc_max": "2.34",  # libcrypto.so.3; libssl.so.3 needs 2.14
    },
}
HOST_REQUIREMENTS = ROOT / "tools/de25_uboot_host_requirements.txt"
HOST_PYTHON_DIR = "host-python"
HOST_OPENSSL_DIR = "host-openssl"
MANIFEST_NAME = "stage-manifest.json"
PYTHON_INPUTS = {
    "swig-4.1.1-py2.py3-none-manylinux_2_5_x86_64.manylinux1_x86_64.whl": {
        "sha256": "3f9da5564718bb81234dd497dc265025b0456c6c9378dfa8206cda56a7fa65ba"
    },
    "setuptools-80.9.0-py3-none-any.whl": {
        "sha256": "062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922"
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_pinned(archive: Path) -> None:
    _fetch(archive, TOOLCHAIN_URL, TOOLCHAIN_SHA256, "aarch64 toolchain")


def _fetch(archive: Path, url: str, expected: str, label: str) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        actual = _sha256(archive)
        if actual != expected:
            raise RuntimeError(
                f"corrupt pinned input: {archive}\n"
                f"  expected sha256 {expected}\n"
                f"  observed sha256 {actual}\n"
                "Refusing to replace the previous toolchain cache with stale "
                "output. Delete the corrupt cache file and rerun stage."
            )
        return

    fd, tmp_name = tempfile.mkstemp(
        prefix="arm-gnu-toolchain-", suffix=".download", dir=archive.parent
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        urllib.request.urlretrieve(url, tmp)  # pinned, checksum-verified URL
        actual = _sha256(tmp)
        if actual != expected:
            raise RuntimeError(
                f"downloaded {label} checksum mismatch: expected {expected}, "
                f"observed {actual}"
            )
        os.replace(tmp, archive)
    finally:
        tmp.unlink(missing_ok=True)


def _staged_linkname(member: tarfile.TarInfo) -> str:
    """The link target as it must read INSIDE the staged tree.

    The archive stores link targets relative to its own ROOT — e.g.
    ``bin/aarch64-none-linux-gnu-ld`` points at
    ``arm-gnu-toolchain-…/aarch64-none-linux-gnu/bin/ld.bfd``. Staging strips
    that top-level directory from every member NAME, so copying the link
    target verbatim leaves it pointing at a directory that no longer exists
    one level up: every such symlink DANGLES, and the toolchain can compile
    but not LINK ("collect2: fatal error: cannot find 'ld'"). Measured
    off-bench 2026-08-30 while building ATF for DE-G0; invisible on this
    bench, which has a Vitis-bundled aarch64 gcc on PATH and never used the
    staged one.

    Absolute targets, and relative targets that do not start with the
    stripped directory, are left exactly as the archive wrote them.
    """
    link = member.linkname
    if link.startswith("/"):
        return link
    parts = Path(link).parts
    if not parts or parts[0] != TOOLCHAIN_DIR_NAME:
        return link
    inside = Path(*parts[1:])
    # Re-express relative to the LINK's own directory so the staged tree
    # stays relocatable — an absolute path would break the atomic
    # temp-dir -> dest rename this function is unpacking into.
    return os.path.relpath(inside, _rel_dir(member))


def _rel_dir(member: tarfile.TarInfo) -> Path:
    """The member's directory, relative to the stripped archive root."""
    rel = Path(member.name).relative_to(TOOLCHAIN_DIR_NAME)
    return rel.parent


def _unpack_members(package, members, temp: Path) -> None:
    """Write `members` under `temp`, stripping the archive's top-level dir."""
    for member in members:
        rel = Path(member.name).relative_to(TOOLCHAIN_DIR_NAME)
        if rel == Path(".") or rel.is_absolute() or ".." in rel.parts:
            continue
        target = temp / rel
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.issym() or member.islnk():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(_staged_linkname(member))
        elif member.isfile():
            stream = package.extractfile(member)
            if stream is None:
                raise RuntimeError(
                    f"could not read toolchain archive member: {member.name}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stream.read())
            target.chmod(member.mode & 0o777)


def _refuse_dangling_links(root: Path) -> None:
    """A toolchain that cannot LINK must not be published as staged.

    `bin/<prefix>gcc` existing was the whole of the previous acceptance check,
    and it was satisfied by a tree whose every symlink was broken. The linker
    is the specific thing that was missing, so it is named here rather than
    left to a generic scan alone.
    """
    # DS-P2.229: host-openssl/ and host-python/ are U-Boot host inputs that
    # stage_all extracts from sha256-pinned packages recorded in
    # stage-manifest.json, not part of the cross toolchain. The pinned
    # libssl-dev ships a dangling doc link (changelog.gz -> ../libssl3t64/
    # changelog.gz), which says nothing about whether the toolchain links.
    host_dirs = tuple(root / d for d in (HOST_OPENSSL_DIR, HOST_PYTHON_DIR))
    dangling = sorted(
        str(p.relative_to(root)) for p in root.rglob("*")
        if p.is_symlink() and not p.exists()
        and not any(h == p or h in p.parents for h in host_dirs)
    )
    if dangling:
        linker = {f"bin/{CROSS_PREFIX}ld", f"bin/{COMPAT_PREFIX}ld"} & set(dangling)
        why = (" (including the linker, so this toolchain can compile but not "
               "link: collect2: cannot find 'ld')" if linker else "")
        raise RuntimeError(
            f"{len(dangling)} staged symlink(s) in the cross toolchain do not "
            f"resolve{why}: {', '.join(dangling[:5])}"
        )
    for name in (f"{CROSS_PREFIX}ld", f"{CROSS_PREFIX}gcc"):
        binary = root / "bin" / name
        if not binary.exists():
            raise RuntimeError(f"staged toolchain has no working bin/{name}")


def stage(cache: Path, dest: Path) -> Path:
    """Fetch + verify + atomically unpack the standalone toolchain to
    ``dest`` (the directory that will directly contain ``bin/``)."""
    archive = cache / TOOLCHAIN_ARCHIVE
    _fetch_pinned(archive)

    gcc = dest / "bin" / f"{CROSS_PREFIX}gcc"
    if gcc.is_file():
        # Unpacking is expensive, so an already-staged tree is a no-op — but it
        # is AUDITED first. The broken tree this audit exists for was itself a
        # cache hit: `bin/<prefix>gcc` present, every symlink dangling. Auditing
        # only the freshly-unpacked tree would let exactly that one through
        # untouched, which is the state the rehearsal actually met.
        _refuse_dangling_links(dest)
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".de25-toolchain-", dir=dest.parent))
    try:
        with tarfile.open(archive, "r:xz") as package:
            prefix = TOOLCHAIN_DIR_NAME + "/"
            members = [
                m for m in package.getmembers() if m.name.startswith(prefix)
            ]
            if not any(
                m.name == prefix + f"bin/{CROSS_PREFIX}gcc" for m in members
            ):
                raise RuntimeError(
                    "verified toolchain archive has no "
                    f"bin/{CROSS_PREFIX}gcc"
                )
            _unpack_members(package, members, temp)
        _add_compat_symlinks(temp / "bin")
        _refuse_dangling_links(temp)
        old = dest.with_name(dest.name + ".previous")
        if old.exists():
            shutil.rmtree(old)
        if dest.exists():
            os.replace(dest, old)
        os.replace(temp, dest)
        if old.exists():
            shutil.rmtree(old)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return dest


def _host_python_ok(python: Path) -> bool:
    if not python.is_file():
        return False
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(python.parent), env.get("PATH", "")])
    proc = subprocess.run(
        [str(python), "-c", "import importlib.metadata as m; import pkg_resources; "
         "import subprocess; assert m.version('setuptools') == '80.9.0'; "
         "assert '4.1.1' in subprocess.check_output(['swig', '-version'], "
         "text=True)"],
        env=env, capture_output=True, text=True, check=False,
    )
    return proc.returncode == 0


def _install_host_requirements(python: Path, requirements: Path) -> None:
    """Install the hashed U-Boot host pins into an already-created venv."""
    subprocess.run([
        str(python), "-m", "pip", "install", "--disable-pip-version-check",
        "--no-deps", "--require-hashes", "-r", str(requirements),
    ], check=True)


def _stage_host_python(dest: Path, requirements: Path = HOST_REQUIREMENTS) -> None:
    """Create an isolated host-tools venv and install the two hashed pins.

    The provider's own RouteRTL venv is deliberately not mutated.  Prepending
    this venv to PATH makes U-Boot's sub-makes reach both its Python and SWIG.
    """
    python_dir = dest / HOST_PYTHON_DIR
    python = python_dir / "bin/python3"
    if _host_python_ok(python):
        return
    if not requirements.is_file():
        raise RuntimeError(f"U-Boot host requirements are missing: {requirements}")
    old = python_dir.with_name(python_dir.name + ".previous")
    try:
        if old.exists():
            shutil.rmtree(old)
        if python_dir.exists():
            os.replace(python_dir, old)
        # A venv is not relocatable: its entry-point shebangs name the absolute
        # creation path.  Build at the FINAL path (with rollback below) rather
        # than renaming a seemingly atomic temp venv whose `swig` would then
        # point at a vanished interpreter.
        subprocess.run([sys.executable, "-m", "venv",
                        python_dir], check=True)
        staged_python = python_dir / "bin/python3"
        # Python 3.10's ensurepip includes setuptools while 3.12's does not.
        # Remove it so deleting our explicit pin has the same (failing)
        # binman-import result on both hosts.
        subprocess.run([str(staged_python), "-m", "pip", "uninstall", "-y",
                        "setuptools"], check=True, capture_output=True, text=True)
        _install_host_requirements(staged_python, requirements)
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(
            [str(python_dir / "bin"), env.get("PATH", "")])
        probe = subprocess.run(
            [str(staged_python), "-c", "import importlib.metadata as m; "
             "import pkg_resources; import subprocess; "
             "assert m.version('setuptools') == '80.9.0'; "
             "assert '4.1.1' in subprocess.check_output("
             "['swig', '-version'], text=True)"],
            env=env, capture_output=True, text=True, check=False)
        if probe.returncode != 0:
            raise RuntimeError(
                "staged U-Boot Python cannot import binman's pkg_resources or "
                f"run pinned swig: {probe.stderr.strip()}")
        if old.exists():
            shutil.rmtree(old)
    except Exception:
        shutil.rmtree(python_dir, ignore_errors=True)
        if old.exists():
            os.replace(old, python_dir)
        raise


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def elf_glibc_needs(data: bytes) -> list[str]:
    """GLIBC_ symbol versions an ELF64 little-endian file needs: the vna_name
    of every Vernaux entry in its SHT_GNU_verneed section (DS-P1.94). Empty
    for a file that is not such an ELF or needs no versioned glibc symbol."""
    if data[:4] != b"\x7fELF" or data[4:5] != b"\x02" or data[5:6] != b"\x01":
        return []
    u16 = lambda o: int.from_bytes(data[o:o + 2], "little")
    u32 = lambda o: int.from_bytes(data[o:o + 4], "little")
    u64 = lambda o: int.from_bytes(data[o:o + 8], "little")
    shoff, shentsize, shnum = u64(0x28), u16(0x3A), u16(0x3C)
    sections = [(u32(shoff + i * shentsize + 4), u64(shoff + i * shentsize + 0x18),
                 u32(shoff + i * shentsize + 0x28), u32(shoff + i * shentsize + 0x2C))
                for i in range(shnum)]  # (sh_type, sh_offset, sh_link, sh_info)
    names: list[str] = []
    for sh_type, off, link, count in sections:
        if sh_type != 0x6FFFFFFE:      # SHT_GNU_verneed
            continue
        strtab = sections[link][1]
        entry = off
        for _ in range(count):
            vn_cnt, vn_aux, vn_next = u16(entry + 2), u32(entry + 8), u32(entry + 12)
            aux = entry + vn_aux
            for _ in range(vn_cnt):
                name_off, aux_next = u32(aux + 8), u32(aux + 12)
                end = data.index(b"\0", strtab + name_off)
                name = data[strtab + name_off:end].decode("ascii", "replace")
                if name.startswith("GLIBC_") and name[6:7].isdigit():
                    names.append(name[6:])
                if not aux_next:
                    break
                aux += aux_next
            if not vn_next:
                break
            entry += vn_next
    return sorted(set(names), key=_version_key)


def host_glibc() -> str | None:
    """This host's glibc version, e.g. "2.35"; None when it is not glibc."""
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError):
        return None
    parts = value.split()
    return parts[1] if len(parts) == 2 and parts[0] == "glibc" else None


def _refuse_libraries_newer_than(root: Path, glibc: str) -> None:
    """Refuse a staged shared library that needs a newer glibc than ``glibc``:
    every program that loads it fails on this host (DS-P1.94)."""
    newer = []
    for path in sorted(root.rglob("*.so*")):
        if path.is_symlink() or not path.is_file():
            continue
        needs = elf_glibc_needs(path.read_bytes())
        if needs and _version_key(needs[-1]) > _version_key(glibc):
            newer.append(f"{path.relative_to(root)} needs GLIBC_{needs[-1]}")
    if newer:
        raise RuntimeError(
            f"the pinned OpenSSL host packages need a newer glibc than this host's "
            f"{glibc}, so no program could load them here: {'; '.join(newer)}")


def _stage_openssl(cache: Path, dest: Path) -> None:
    if platform.machine() not in {"x86_64", "amd64"}:
        raise RuntimeError("pinned U-Boot OpenSSL host packages require x86_64")
    for name, item in OPENSSL_INPUTS.items():
        _fetch(cache / name, item["url"], item["sha256"], name)
    output = dest / HOST_OPENSSL_DIR
    required = (
        output / "usr/include/openssl/evp.h",
        output / "usr/include/x86_64-linux-gnu/openssl/opensslconf.h",
        output / "usr/lib/x86_64-linux-gnu/libssl.so",
    )
    if all(path.exists() for path in required):
        return
    if shutil.which("dpkg-deb") is None:
        raise RuntimeError("dpkg-deb is required to extract pinned libssl .debs without root")
    temp = Path(tempfile.mkdtemp(prefix=".de25-host-openssl-", dir=dest.parent))
    try:
        for name in OPENSSL_INPUTS:
            subprocess.run(["dpkg-deb", "-x", str(cache / name), str(temp)], check=True)
        missing = [str(path.relative_to(output)) for path in required
                   if not (temp / path.relative_to(output)).exists()]
        if missing:
            raise RuntimeError("verified OpenSSL packages omit: " + ", ".join(missing))
        glibc = host_glibc()
        if glibc is not None:
            _refuse_libraries_newer_than(temp, glibc)
        old = output.with_name(output.name + ".previous")
        if old.exists():
            shutil.rmtree(old)
        if output.exists():
            os.replace(output, old)
        os.replace(temp, output)
        if old.exists():
            shutil.rmtree(old)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _manifest() -> dict[str, object]:
    inputs = {
        TOOLCHAIN_ARCHIVE: {"sha256": TOOLCHAIN_SHA256, "url": TOOLCHAIN_URL},
        **{name: dict(item) for name, item in OPENSSL_INPUTS.items()},
        **{name: dict(item) for name, item in PYTHON_INPUTS.items()},
    }
    return {"version": 1, "inputs": inputs}


def stage_all(cache: Path, dest: Path) -> dict[str, object]:
    """Stage every cross and host prerequisite consumed by U-Boot."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage(cache, dest)
    _stage_host_python(dest)
    _stage_openssl(cache, dest)
    manifest = _manifest()
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    temp = dest / (MANIFEST_NAME + ".tmp")
    temp.write_text(encoded, encoding="utf-8")
    os.replace(temp, dest / MANIFEST_NAME)
    return manifest


def build_env(dest: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment the GP-R0 provider must hand to ``rr linux uboot``."""
    env = dict(os.environ if base is None else base)
    host_python = dest / HOST_PYTHON_DIR / "bin"
    toolchain = dest / "bin"
    env["PATH"] = os.pathsep.join([str(host_python), str(toolchain),
                                   env.get("PATH", "")])
    ssl = dest / HOST_OPENSSL_DIR / "usr"
    include = [ssl / "include", ssl / "include/x86_64-linux-gnu"]
    lib = ssl / "lib/x86_64-linux-gnu"
    env["HOSTCFLAGS"] = " ".join(f"-I{path}" for path in include)
    # DS-P1.94: no LD_LIBRARY_PATH. The U-Boot host tools find the staged
    # libraries through the -rpath above; on LD_LIBRARY_PATH they were loaded
    # by every program in the step, git's HTTPS helper included.
    env["HOSTLDFLAGS"] = f"-L{lib} -Wl,-rpath,{lib}"
    return env


def _add_compat_symlinks(bin_dir: Path) -> None:
    """Alias every ``aarch64-none-linux-gnu-*`` binary as
    ``aarch64-linux-gnu-*`` so the SDK's hard-coded ``CROSS_COMPILE`` prefix
    (COMPAT_PREFIX) can find this standalone toolchain by name — the naming
    mismatch is a real, separate blocker from PATH precedence (see
    ``status()``)."""
    for path in sorted(bin_dir.glob(f"{CROSS_PREFIX}*")):
        if not (path.is_file() or path.is_symlink()):
            continue
        suffix = path.name[len(CROSS_PREFIX):]
        alias = bin_dir / f"{COMPAT_PREFIX}{suffix}"
        if alias.exists() or alias.is_symlink():
            alias.unlink()
        alias.symlink_to(path.name)


def _resolved_gcc_path(toolchain_dir: Path) -> tuple[str | None, str]:
    """Ask the INSTALLED RouteRTL SDK's own cross-toolchain PATH resolution
    (never a reimplementation) which ``aarch64-linux-gnu-gcc`` ``rr linux
    build`` would actually invoke right now, with the staged standalone
    toolchain's bin/ prepended to PATH exactly as a real invocation would see
    it if that bin/ were exported.

    Returns ``(resolved_path_or_None, verdict)`` where verdict is one of
    "standalone", "vendor-bundled", or "missing".
    """
    from sdk.cli.commands.linux_steps._cross_compile import make_env

    # Reproduce what a bench operator who staged this toolchain and exported
    # its bin/ would actually experience: toolchain_bin joins the AMBIENT
    # PATH make_env() will copy via os.environ.copy() — it is NOT injected
    # after the fact, because a real ``rr linux build`` invocation never
    # gets that chance either. make_env() then unconditionally prepends any
    # on-disk Vitis bin/ in front of that whole inherited PATH, which is
    # exactly the precedence this function measures.
    toolchain_bin = str(toolchain_dir / "bin")
    saved_path = os.environ.get("PATH")
    os.environ["PATH"] = os.pathsep.join([toolchain_bin, saved_path or ""])
    try:
        env = make_env()
    finally:
        if saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved_path

    proc = subprocess.run(
        ["which", "aarch64-linux-gnu-gcc"], env=env,
        capture_output=True, text=True, check=False,
    )
    resolved = proc.stdout.strip() or None
    if resolved is None:
        return None, "missing"
    if resolved.startswith(str(toolchain_dir)):
        return resolved, "standalone"
    return resolved, "vendor-bundled"


def status(toolchain_dir: Path) -> int:
    gcc = toolchain_dir / "bin" / f"{CROSS_PREFIX}gcc"
    if not gcc.is_file():
        print(f"DE25 TOOLCHAIN STATUS: not staged (expected {gcc})")
        return 1

    version = subprocess.run(
        [str(gcc), "--version"], capture_output=True, text=True, check=False,
    ).stdout.splitlines()[0] if gcc.is_file() else ""
    print(f"DE25 TOOLCHAIN STATUS: staged at {toolchain_dir}")
    print(f"  {version}")

    try:
        resolved, verdict = _resolved_gcc_path(toolchain_dir)
    except ImportError as exc:
        print(
            f"  rr linux resolution: UNKNOWN — RouteRTL SDK not importable "
            f"({exc})"
        )
        return 0

    print(f"  rr linux 'aarch64-linux-gnu-gcc' resolution: {resolved}")
    print(f"  verdict: {verdict}")
    if verdict == "vendor-bundled":
        print(
            "  DS-P2.92: the installed RouteRTL SDK's "
            "sdk.cli.commands.linux_steps._cross_compile::make_env() "
            "unconditionally prepends any on-disk Vitis toolchain bin/ "
            "ahead of PATH, and _socfpga.py's SocfpgaArch.cross_prefix is a "
            "hard-coded literal with no project.yml/env override — so a "
            "staged standalone toolchain cannot win over an installed Vitis "
            "bundle through project-level configuration alone. This is an "
            "SDK-side gap (routertl, not deepskopion) tracked for a "
            "follow-up ticket; not something this repo's config can fix."
        )
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--cache", type=Path)
    stage_parser.add_argument("--dest", type=Path)

    status_parser = sub.add_parser("status")
    status_parser.add_argument("--dest", type=Path)

    args = parser.parse_args(argv)

    default_cache = ROOT / "sw/linux/de25/.cache"
    default_dest = ROOT / "sw/linux/de25/toolchain" / TOOLCHAIN_DIR_NAME

    if args.command == "stage":
        cache = (args.cache or default_cache).resolve()
        dest = (args.dest or default_dest).resolve()
        try:
            stage_all(cache, dest)
        except (OSError, RuntimeError, subprocess.SubprocessError,
                tarfile.TarError) as exc:
            print(f"DE25 TOOLCHAIN STAGE FAILED: {exc}")
            return 1
        print(f"DE25 standalone aarch64-none-linux-gnu toolchain staged: {dest}")
        print(f"  {TOOLCHAIN_ARCHIVE} sha256={TOOLCHAIN_SHA256}")
        print(f"  U-Boot host manifest: {dest / MANIFEST_NAME}")
        return 0

    dest = (args.dest or default_dest).resolve()
    return status(dest)


if __name__ == "__main__":
    raise SystemExit(main())
