# rr unknown-llm (AI-generated)
"""Executable DE-L0/DS-P2.92 contract for the staged standalone aarch64
cross-toolchain: staging is pinned + atomic, and the "which gcc does rr
linux actually invoke" detector genuinely distinguishes the staged
standalone toolchain from a vendor-bundled one (rather than always
reporting the same verdict regardless of what PATH contains).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "tools/de25_toolchain_stage.py"
REAL_CACHE = ROOT / "sw/linux/de25/.cache"
ARCHIVE_NAME = (
    "arm-gnu-toolchain-13.3.rel1-x86_64-aarch64-none-linux-gnu.tar.xz"
)


def _has_real_cache() -> bool:
    return (REAL_CACHE / ARCHIVE_NAME).is_file()


requires_cached_archive = pytest.mark.skipif(
    not _has_real_cache(),
    reason=(
        "DS-P4.11: no cached toolchain archive on this host and this test must not "
        "trigger a fresh 144 MB download of a shared fixture — run "
        "'python3 tools/de25_toolchain_stage.py stage' once first"
    ),
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )


def _stage_tool():
    sys.path.insert(0, str(ROOT))
    try:
        from tools import de25_toolchain_stage
        return de25_toolchain_stage
    finally:
        sys.path.pop(0)


def _fake_hashed_install(python: Path, requirements: Path) -> None:
    """Offline stand-in for pip; the resulting venv is probed for real."""
    text = requirements.read_text(encoding="utf-8")
    assert "--require-hashes" not in text  # the option belongs to the caller
    site = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=True).stdout.strip()
    site_path = Path(site)
    if "setuptools==80.9.0" in text:
        (site_path / "pkg_resources.py").write_text("# staged fixture\n", encoding="utf-8")
        dist = site_path / "setuptools-80.9.0.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: setuptools\nVersion: 80.9.0\n",
            encoding="utf-8")
    if "swig==4.1.1" in text:
        swig = python.parent / "swig"
        swig.write_text("#!/bin/sh\necho 'SWIG Version 4.1.1'\n", encoding="utf-8")
        swig.chmod(0o755)


def test_host_requirements_pin_both_uboot_python_couplings(tmp_path, monkeypatch):
    """DS-P2.105 regression contract, including the requested mutation.

    This asserts the file pip actually consumes, not duplicate constants in
    the stager.  Removing the setuptools line models the measured mutation:
    a fresh Python 3.12 venv may then resolve setuptools 81+, where binman's
    ``import pkg_resources`` fails.
    """
    requirements = ROOT / "tools/de25_uboot_host_requirements.txt"
    text = requirements.read_text(encoding="utf-8")
    assert "swig==4.1.1" in text
    assert "setuptools==80.9.0" in text
    assert text.count("--hash=sha256:") == 2
    stage_tool = _stage_tool()
    assert {item["sha256"] for item in stage_tool.PYTHON_INPUTS.values()} == {
        line.split("sha256:", 1)[1].strip()
        for line in text.splitlines() if "--hash=sha256:" in line
    }

    calls = []

    def install(python, req):
        calls.append(req.read_text(encoding="utf-8"))
        _fake_hashed_install(python, req)
    monkeypatch.setattr(stage_tool, "_install_host_requirements", install)
    good = tmp_path / "good"
    stage_tool._stage_host_python(good, requirements)
    assert stage_tool._host_python_ok(good / "host-python/bin/python3")

    mutated = "\n".join(
        line for line in text.splitlines()
        if "setuptools==80.9.0" not in line and
        "062d34222ad13e0cc312a4c02d73f059" not in line)
    mutated_requirements = tmp_path / "requirements-mutated.txt"
    mutated_requirements.write_text(mutated + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="pkg_resources"):
        stage_tool._stage_host_python(tmp_path / "mutated", mutated_requirements)
    assert len(calls) == 2


def test_host_python_install_enforces_hashes_and_no_dependencies(monkeypatch, tmp_path):
    stage_tool = _stage_tool()
    observed = []
    monkeypatch.setattr(stage_tool.subprocess, "run",
                        lambda argv, **kwargs: observed.append((argv, kwargs)))
    python = tmp_path / "venv/bin/python3"
    requirements = tmp_path / "requirements.txt"

    stage_tool._install_host_requirements(python, requirements)

    argv, kwargs = observed[0]
    assert argv[:4] == [str(python), "-m", "pip", "install"]
    assert "--require-hashes" in argv
    assert "--no-deps" in argv
    assert argv[-2:] == ["-r", str(requirements)]
    assert kwargs == {"check": True}


def test_build_environment_reaches_cross_compiler_python_and_openssl(tmp_path):
    """The environment handed to ``rr linux uboot`` contains every stage."""
    stage_tool = _stage_tool()

    dest = tmp_path / "stage"
    env = stage_tool.build_env(dest, {"PATH": "/usr/bin"})

    assert env["PATH"].split(os.pathsep)[:2] == [
        str(dest / "host-python/bin"), str(dest / "bin")]
    assert str(dest / "host-openssl/usr/include") in env["HOSTCFLAGS"]
    assert str(dest / "host-openssl/usr/lib/x86_64-linux-gnu") in env["HOSTLDFLAGS"]


def test_build_environment_puts_no_staged_library_on_ld_library_path(tmp_path):
    """DS-P1.94: on LD_LIBRARY_PATH the staged libcrypto was loaded by every
    program in `rr linux uboot`, and git's HTTPS helper died on the declared
    host. The host tools reach it through -rpath; LD_LIBRARY_PATH is left as
    the caller had it."""
    stage_tool = _stage_tool()
    dest = tmp_path / "stage"
    assert "LD_LIBRARY_PATH" not in stage_tool.build_env(dest, {"PATH": "/usr/bin"})
    kept = stage_tool.build_env(dest, {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/opt/x"})
    assert kept["LD_LIBRARY_PATH"] == "/opt/x"
    assert f"-Wl,-rpath,{dest / 'host-openssl/usr/lib/x86_64-linux-gnu'}" in kept["HOSTLDFLAGS"]


def test_pinned_openssl_needs_no_newer_glibc_than_the_declared_host():
    """DS-P1.94: every pinned package's measured glibc_max is within the
    declared host's glibc, and the declared host is the one the preflight
    declaration names. Red on the Ubuntu 24.04 pin (glibc_max 2.38)."""
    import yaml
    stage_tool = _stage_tool()
    decl = yaml.safe_load((Path(stage_tool.ROOT) / "tools/preflight/supported_environment.yml")
                          .read_text(encoding="utf-8"))
    assert decl["host"]["os"]["id"] == "ubuntu"
    assert str(decl["host"]["os"]["version"]) == stage_tool.DECLARED_HOST_OS_VERSION
    key = stage_tool._version_key
    for name, item in stage_tool.OPENSSL_INPUTS.items():
        assert "glibc_max" in item, f"{name} records no measured glibc_max"
        if item["glibc_max"] is not None:
            assert key(item["glibc_max"]) <= key(stage_tool.DECLARED_HOST_GLIBC), (
                f"{name} needs GLIBC_{item['glibc_max']}, the declared Ubuntu "
                f"{stage_tool.DECLARED_HOST_OS_VERSION} host has {stage_tool.DECLARED_HOST_GLIBC}")


def _glibc_patched_elf(tmp_path: Path, major: bytes) -> bytes:
    """A real host ELF whose GLIBC_2.x version needs read GLIBC_<major>.x."""
    data = Path(sys.executable).resolve().read_bytes()
    stage_tool = _stage_tool()
    assert stage_tool.elf_glibc_needs(data), "host interpreter has no verneed to patch"
    return data.replace(b"GLIBC_2.", b"GLIBC_" + major + b".")


def test_elf_glibc_needs_reads_the_version_needs(tmp_path):
    stage_tool = _stage_tool()
    real = stage_tool.elf_glibc_needs(Path(sys.executable).resolve().read_bytes())
    assert real and all(v.startswith("2.") for v in real)
    patched = stage_tool.elf_glibc_needs(_glibc_patched_elf(tmp_path, b"9"))
    assert patched and all(v.startswith("9.") for v in patched)
    assert stage_tool.elf_glibc_needs(b"not an elf") == []


def test_stage_refuses_openssl_that_needs_a_newer_glibc_than_the_host(tmp_path, monkeypatch):
    """DS-P1.94, the effect: an extracted OpenSSL whose libcrypto needs a newer
    glibc than this host's fails the stage, naming the library, instead of
    failing later inside an unrelated program."""
    stage_tool = _stage_tool()
    elf_dir = tmp_path / "elf"
    elf_dir.mkdir()
    (elf_dir / "libcrypto.so.3").write_bytes(_glibc_patched_elf(tmp_path, b"9"))
    dev = _build_deb(tmp_path, "ssl-dev-fixture", [
        "usr/include/openssl/evp.h",
        "usr/include/x86_64-linux-gnu/openssl/opensslconf.h",
        "usr/lib/x86_64-linux-gnu/libssl.so",
    ])
    root = tmp_path / "ssl-runtime-fixture-root"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "DEBIAN/control").write_text(
        "Package: ssl-runtime-fixture\nVersion: 1\nArchitecture: amd64\n"
        "Maintainer: test\nDescription: fixture\n", encoding="utf-8")
    lib = root / "usr/lib/x86_64-linux-gnu"
    lib.mkdir(parents=True)
    shutil.copy2(elf_dir / "libcrypto.so.3", lib / "libcrypto.so.3")
    runtime = tmp_path / "ssl-runtime-fixture.deb"
    subprocess.run(["dpkg-deb", "--build", str(root), str(runtime)], check=True, capture_output=True)
    fixtures = {stage_tool.OPENSSL_DEV_ARCHIVE: dev, stage_tool.OPENSSL_RUNTIME_ARCHIVE: runtime}

    def fetch(path, url, sha, label):
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixtures[path.name], path)
    monkeypatch.setattr(stage_tool, "_fetch", fetch)
    monkeypatch.setattr(stage_tool, "host_glibc", lambda: "2.35")
    dest = tmp_path / "stage"
    dest.mkdir()
    with pytest.raises(RuntimeError, match=r"libcrypto.so.3 needs GLIBC_9\."):
        stage_tool._stage_openssl(tmp_path / "cache", dest)
    assert not (dest / stage_tool.HOST_OPENSSL_DIR).exists(), "a refused stage was published"


@pytest.mark.skipif(os.environ.get("DS_NETWORK_TESTS") != "1",
                    reason="network check; opt in with DS_NETWORK_TESTS=1")
def test_pinned_runtime_glibc_max_matches_its_bytes(tmp_path):
    """DS-P1.94: glibc_max is measured, not typed: the pinned runtime
    package's libraries need exactly that highest GLIBC_ version."""
    stage_tool = _stage_tool()
    name = stage_tool.OPENSSL_RUNTIME_ARCHIVE
    item = stage_tool.OPENSSL_INPUTS[name]
    deb = tmp_path / name
    stage_tool._fetch(deb, item["url"], item["sha256"], name)
    out = tmp_path / "x"
    subprocess.run(["dpkg-deb", "-x", str(deb), str(out)], check=True)
    needs = sorted({v for p in out.rglob("*.so*") if p.is_file() and not p.is_symlink()
                    for v in stage_tool.elf_glibc_needs(p.read_bytes())},
                   key=stage_tool._version_key)
    assert needs[-1] == item["glibc_max"]


def _build_deb(tmp_path: Path, name: str, files: list[str],
               links: dict[str, str] | None = None) -> Path:
    root = tmp_path / (name + "-root")
    (root / "DEBIAN").mkdir(parents=True)
    (root / "DEBIAN/control").write_text(
        f"Package: {name}\nVersion: 1\nArchitecture: amd64\n"
        "Maintainer: test\nDescription: fixture\n",
        encoding="utf-8")
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    for rel, target in (links or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    deb = tmp_path / (name + ".deb")
    subprocess.run(["dpkg-deb", "--build", str(root), str(deb)],
                   check=True, capture_output=True)
    return deb


def test_stage_all_executes_host_stages_and_records_every_sha(tmp_path, monkeypatch):
    """Exercise production orchestration with only downloads/install offline."""
    stage_tool = _stage_tool()
    dev = _build_deb(tmp_path, "ssl-dev-fixture", [
        "usr/include/openssl/evp.h",
        "usr/include/x86_64-linux-gnu/openssl/opensslconf.h",
        "usr/lib/x86_64-linux-gnu/libssl.so",
    ])
    runtime = _build_deb(tmp_path, "ssl-runtime-fixture", [])
    fixtures = {
        stage_tool.OPENSSL_DEV_ARCHIVE: dev,
        stage_tool.OPENSSL_RUNTIME_ARCHIVE: runtime,
    }
    fetched = []

    def fetch(path, url, sha, label):
        fetched.append((path.name, url, sha, label))
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixtures[path.name], path)
    monkeypatch.setattr(stage_tool, "_fetch", fetch)
    monkeypatch.setattr(stage_tool, "stage", lambda cache, dest: dest.mkdir(parents=True))
    monkeypatch.setattr(stage_tool, "_install_host_requirements", _fake_hashed_install)
    dest = tmp_path / "stage"
    manifest = stage_tool.stage_all(tmp_path / "cache", dest)

    recorded = manifest["inputs"]
    assert set(recorded) == {
        stage_tool.TOOLCHAIN_ARCHIVE,
        stage_tool.OPENSSL_DEV_ARCHIVE,
        stage_tool.OPENSSL_RUNTIME_ARCHIVE,
        ("swig-4.1.1-py2.py3-none-manylinux_2_5_x86_64."
         "manylinux1_x86_64.whl"),
        "setuptools-80.9.0-py3-none-any.whl",
    }
    assert all(len(item["sha256"]) == 64 for item in recorded.values())
    assert {item[0] for item in fetched} == set(stage_tool.OPENSSL_INPUTS)
    assert {(name, url, sha) for name, url, sha, _ in fetched} == {
        (name, item["url"], item["sha256"])
        for name, item in stage_tool.OPENSSL_INPUTS.items()
    }
    assert (dest / "host-python/bin/swig").is_file()
    assert (dest / "host-openssl/usr/include/openssl/evp.h").is_file()
    on_disk = json.loads(
        (dest / stage_tool.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert on_disk == manifest


def _fake_cross_toolchain(dest: Path, prefix: str) -> None:
    (dest / "bin").mkdir(parents=True, exist_ok=True)
    for tool in ("gcc", "ld"):
        (dest / "bin" / f"{prefix}{tool}").write_text("#!/bin/sh\n", encoding="utf-8")


def test_stage_all_then_stage_accepts_the_pinned_libssl_dev_doc_link(tmp_path, monkeypatch):
    """DS-P2.229: the pinned libssl-dev ships a dangling
    usr/share/doc/libssl-dev/changelog.gz (3.0.13-0ubuntu3.15 pointed it at
    ../libssl3t64/changelog.gz; the DS-P1.94 jammy pin ships one too). stage_all extracts it into host-openssl/, and
    the next stage() (run by tools/de25_linux_image.py stage) audited the whole
    tree and refused it as "cannot link", so `rr linux rootfs` failed on every
    checkout staged the documented way (measured 2026-11-07)."""
    stage_tool = _stage_tool()
    real_stage = stage_tool.stage
    link = "usr/share/doc/libssl-dev/changelog.gz"
    dev = _build_deb(tmp_path, "ssl-dev-fixture", [
        "usr/include/openssl/evp.h",
        "usr/include/x86_64-linux-gnu/openssl/opensslconf.h",
        "usr/lib/x86_64-linux-gnu/libssl.so",
    ], links={link: "../libssl3t64/changelog.gz"})
    runtime = _build_deb(tmp_path, "ssl-runtime-fixture", ["usr/share/doc/libssl3t64/changelog.Debian.gz"])
    fixtures = {stage_tool.OPENSSL_DEV_ARCHIVE: dev, stage_tool.OPENSSL_RUNTIME_ARCHIVE: runtime}

    def fetch(path, url, sha, label):
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixtures[path.name], path)
    monkeypatch.setattr(stage_tool, "_fetch", fetch)
    monkeypatch.setattr(stage_tool, "stage",
                        lambda cache, dest: _fake_cross_toolchain(dest, stage_tool.CROSS_PREFIX))
    monkeypatch.setattr(stage_tool, "_install_host_requirements", _fake_hashed_install)
    dest = tmp_path / "stage"
    stage_tool.stage_all(tmp_path / "cache", dest)
    staged = dest / stage_tool.HOST_OPENSSL_DIR / link
    assert staged.is_symlink() and not staged.exists(), "fixture lost the package's dangling link"

    monkeypatch.setattr(stage_tool, "_fetch_pinned", lambda archive: None)
    assert real_stage(tmp_path / "cache", dest) == dest


def test_a_dangling_link_in_the_cross_toolchain_is_still_refused(tmp_path, monkeypatch):
    """DS-P2.229 keeps DS-P1.45's audit for the toolchain itself, and names the
    link without blaming the linker when the linker is not the one dangling."""
    stage_tool = _stage_tool()
    monkeypatch.setattr(stage_tool, "_fetch_pinned", lambda archive: None)
    dest = tmp_path / "stage"
    _fake_cross_toolchain(dest, stage_tool.CROSS_PREFIX)
    (dest / "lib").mkdir()
    (dest / "lib/libfoo.so").symlink_to("libfoo.so.1")
    with pytest.raises(RuntimeError) as exc:
        stage_tool.stage(tmp_path / "cache", dest)
    assert "lib/libfoo.so" in str(exc.value)
    assert "'ld'" not in str(exc.value) and "not link" not in str(exc.value)


def test_a_dangling_linker_is_reported_as_unable_to_link(tmp_path, monkeypatch):
    """DS-P1.45's original case: bin/<prefix>ld dangling means the toolchain
    can compile but not link, and the message says so."""
    stage_tool = _stage_tool()
    monkeypatch.setattr(stage_tool, "_fetch_pinned", lambda archive: None)
    dest = tmp_path / "stage"
    _fake_cross_toolchain(dest, stage_tool.CROSS_PREFIX)
    ld = dest / "bin" / f"{stage_tool.CROSS_PREFIX}ld"
    ld.unlink()
    ld.symlink_to("../aarch64-none-linux-gnu/bin/ld")
    with pytest.raises(RuntimeError, match="not link"):
        stage_tool.stage(tmp_path / "cache", dest)


def _isolated_cache(tmp_path: Path) -> Path:
    """Copy the real, already-verified archive into an isolated tmp cache so
    tests can corrupt it without touching the shared bench cache other test
    runs and ``rr linux build`` itself rely on."""
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy2(REAL_CACHE / ARCHIVE_NAME, cache / ARCHIVE_NAME)
    return cache


@requires_cached_archive
def test_stage_extracts_a_real_aarch64_standalone_gcc(tmp_path):
    """@requires DE-L0-BUILD-001"""
    cache = _isolated_cache(tmp_path)
    dest = tmp_path / "toolchain"
    _stage_tool().stage(cache, dest)

    gcc = dest / "bin/aarch64-none-linux-gnu-gcc"
    assert gcc.is_file()
    version = subprocess.run(
        [str(gcc), "--version"], capture_output=True, text=True, check=True,
    ).stdout
    assert "aarch64-none-linux-gnu" in version or "Arm GNU Toolchain" in version

    # The staged compiler driver itself must be a real host-native ELF
    # binary, not e.g. a truncated/placeholder file.
    elf = gcc.read_bytes()[:5]
    assert elf[:4] == b"\x7fELF" and elf[4] == 2, "staged gcc is not ELF64"


@requires_cached_archive
def test_corrupt_cached_archive_fails_without_touching_a_prior_good_stage(tmp_path):
    """@requires DE-L0-NEG-002 — mirrors test_de25_linux_image.py's
    corrupt-pinned-input guard for this second checksum-pinned input."""
    cache = _isolated_cache(tmp_path)
    dest = tmp_path / "toolchain"
    stage_tool = _stage_tool()
    stage_tool.stage(cache, dest)
    sentinel = dest / "bin/aarch64-none-linux-gnu-gcc"
    good_bytes = sentinel.read_bytes()

    archive = cache / ARCHIVE_NAME
    damaged = bytearray(archive.read_bytes())
    damaged[len(damaged) // 2] ^= 0xFF
    archive.write_bytes(damaged)

    # Force re-extraction to actually exercise the corrupt-input path: stage()
    # no-ops when the compiler already exists, so remove the prior stage's
    # dest first (a fresh worktree has no dest yet either).
    shutil.rmtree(dest)
    with pytest.raises(RuntimeError) as error:
        stage_tool.stage(cache, dest)
    assert "corrupt pinned input" in str(error.value)
    assert "Refusing to replace" in str(error.value)
    assert not dest.exists()
    assert good_bytes  # sanity: the first stage really did produce bytes


def _fake_toolchain(tmp_path: Path, cross_prefix: str) -> Path:
    fake_toolchain = tmp_path / "toolchain"
    fake_bin = fake_toolchain / "bin"
    fake_bin.mkdir(parents=True)
    fake_gcc = fake_bin / f"{cross_prefix}gcc"
    fake_gcc.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    fake_gcc.chmod(0o755)
    (fake_bin / "aarch64-linux-gnu-gcc").symlink_to(fake_gcc.name)
    return fake_toolchain


def test_resolution_detector_reports_standalone_when_nothing_else_shadows_it(
    tmp_path, monkeypatch,
):
    """@requires DE-L0-BUILD-001

    BASELINE for this guard: before DS-P2.92, no detector of this kind
    existed at all — there was nothing to catch a silent fall-through to a
    vendor-bundled compiler. This test proves the detector can report
    "standalone" at all (not just always "vendor-bundled" or always
    erroring): with no vendor-bundled toolchain present (the real bench's
    Vitis install is monkeypatched out, not merely path-stripped, since
    make_env() finds it via an absolute filesystem check that ignores PATH),
    the SDK's own PATH search must select our staged toolchain.
    """
    sys.path.insert(0, str(ROOT))
    try:
        from tools.de25_toolchain_stage import _resolved_gcc_path, CROSS_PREFIX
    finally:
        sys.path.pop(0)
    import sdk.cli.commands.linux_steps._cross_compile as cross_compile

    monkeypatch.setattr(cross_compile, "_vitis_toolchain_paths", lambda: [])
    fake_toolchain = _fake_toolchain(tmp_path, CROSS_PREFIX)

    resolved, verdict = _resolved_gcc_path(fake_toolchain)

    assert verdict == "standalone", (resolved, verdict)
    assert resolved is not None and resolved.startswith(str(fake_toolchain))


def _vendor_standin(tmp_path: Path) -> Path:
    vendor_dir = tmp_path / "vendor-vitis-standin/bin"
    vendor_dir.mkdir(parents=True)
    vendor_gcc = vendor_dir / "aarch64-linux-gnu-gcc"
    vendor_gcc.write_text("#!/bin/sh\necho fake-vendor\n", encoding="utf-8")
    vendor_gcc.chmod(0o755)
    return vendor_gcc


def test_the_sdk_lets_a_staged_standalone_toolchain_beat_a_vendor_one(
    tmp_path, monkeypatch,
):
    """@requires DE-L0-NEG-002

    The DS-P2.92 gap is closed upstream (RTL-P2.1158, 2026-09-03): make_env()
    moves any on-disk Vitis bin/ to the END of PATH, so a standalone toolchain
    exported ahead of it wins. With a vendor-bundled ``aarch64-linux-gnu-gcc``
    present the way make_env() really finds Vitis, the verdict stays
    "standalone". If this starts failing, the SDK has gone back to prepending
    the vendor directory.
    """
    sys.path.insert(0, str(ROOT))
    try:
        from tools.de25_toolchain_stage import _resolved_gcc_path, CROSS_PREFIX
    finally:
        sys.path.pop(0)
    import sdk.cli.commands.linux_steps._cross_compile as cross_compile

    vendor_gcc = _vendor_standin(tmp_path)
    monkeypatch.setattr(
        cross_compile, "_vitis_toolchain_paths", lambda: [str(vendor_gcc.parent)]
    )
    fake_toolchain = _fake_toolchain(tmp_path, CROSS_PREFIX)

    resolved, verdict = _resolved_gcc_path(fake_toolchain)

    assert verdict == "standalone", (resolved, verdict)
    assert resolved is not None and resolved.startswith(str(fake_toolchain))


def test_resolution_detector_catches_a_vendor_toolchain_shadowing_the_standalone_one(
    tmp_path, monkeypatch,
):
    """@requires DE-L0-NEG-002

    MUTATION of the guard above: put make_env() back to its pre-RTL-P2.1158
    behaviour (the vendor directory prepended ahead of the whole inherited
    PATH) and confirm the detector flips to "vendor-bundled" rather than
    staying "standalone" — i.e. the detector is sensitive to the exact failure
    mode DS-P2.92 exists to catch, not just always green.
    """
    sys.path.insert(0, str(ROOT))
    try:
        from tools.de25_toolchain_stage import _resolved_gcc_path, CROSS_PREFIX
    finally:
        sys.path.pop(0)
    import sdk.cli.commands.linux_steps._cross_compile as cross_compile

    vendor_gcc = _vendor_standin(tmp_path)

    def prepending_make_env(env_extra=None):
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join([str(vendor_gcc.parent), env.get("PATH", "")])
        return env

    monkeypatch.setattr(cross_compile, "make_env", prepending_make_env)
    fake_toolchain = _fake_toolchain(tmp_path, CROSS_PREFIX)

    resolved, verdict = _resolved_gcc_path(fake_toolchain)

    assert verdict == "vendor-bundled", (resolved, verdict)
    assert resolved == str(vendor_gcc)


@requires_cached_archive
def test_staged_toolchain_on_this_bench_beats_vitis(tmp_path):
    """@requires DE-L0-NEG-002

    Records the state of THIS bench since the SDK fix (RTL-P2.1158): a real
    ``rr linux sdcard --refresh`` run on 2026-08-22 built U-Boot with
    ``/tools/Xilinx/Vitis/2024.1/.../aarch64-linux-gnu-gcc`` even with the
    standalone toolchain staged (DS-P2.92), because the SDK's ``make_env()``
    prepended any on-disk Vitis bin/. It now appends it, so the staged
    toolchain wins and ``status`` reports "standalone" and exits 0. If this
    starts failing with "vendor-bundled", the gap has reopened.
    """
    dest = ROOT / "sw/linux/de25/toolchain" / (
        "arm-gnu-toolchain-13.3.rel1-x86_64-aarch64-none-linux-gnu"
    )
    if not (dest / "bin/aarch64-none-linux-gnu-gcc").is_file():
        pytest.skip("standalone toolchain not staged on this bench yet")
    run = _run("status", "--dest", str(dest))
    assert "verdict: standalone" in run.stdout, run.stdout + run.stderr
    assert run.returncode == 0, run.stdout + run.stderr
