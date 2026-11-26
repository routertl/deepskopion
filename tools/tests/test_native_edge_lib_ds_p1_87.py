# rr unknown-llm (AI-generated)
"""DS-P1.87: the Zynq and PolarFire images carry the native edge library, built from source.

zybo_datalogger*, canfd_demo and pfsoc_datalogger boot ds_source=ddr, the
DdrRingDataSource path DS-P1.74 made native, but only the DE25 image shipped
libds_edge_drain.so (DS-P2.210): edge_frames.load found nothing on the other
two boards and the daemon ran the Python walk. These tests run the two
overlay build scripts against a fake Buildroot tree whose gcc writes an ELF
of the image's kind, and hold each piece:

  * extract_python_overlay.sh (Zynq) and pfsoc/build_python_overlay.sh install
    the library at edge_frames.SYSTEM_LIB inside the overlay, with a stamp
    naming its sha256, its sources' sha256 and the compiler;
  * the check every ddr target's stage_command runs refuses an overlay with
    no library, a library of the wrong float ABI or ISA, or one built from
    other sources, and prints the install command;
  * tools/native_edge_cross_check.py, which compared the cross-built
    libraries under qemu-user, reports a mismatch when the library is wrong.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files, has_bench  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
sys.path.insert(0, str(ROOT / "tools"))

import native_edge_lib as nel  # noqa: E402

from sw.stream.native import edge_frames  # noqa: E402

RV_ARCH_IMAGE = "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zifencei2p0_zmmul1p0"
RV_ARCH_GCC11 = "rv64i2p0_m2p0_a2p0_f2p0_d2p0_c2p0"


def make_elf(profile: nel.Profile, *, eflags: int | None = None, e_type: int = nel.ET_DYN,
             arch: str | None = None) -> bytes:
    """A header-only ELF of the profile's class and machine; RISC-V ones carry
    a .riscv.attributes section holding *arch*."""
    flags = profile.eflags if eflags is None else eflags
    ident = b"\x7fELF" + bytes([profile.elfclass, 1, 1]) + bytes(9)
    body = b""
    if profile.machine == nel.EM_RISCV:
        body = b"A\x00\x00\x00\x00riscv\x00\x01\x00\x00\x00\x00\x05" + (arch or RV_ARCH_IMAGE).encode() + b"\0"
    if profile.elfclass == nel.ELFCLASS32:
        ehsize, shentsize = 52, 40
        shdr = lambda t, off, size: struct.pack("<IIIIIIIIII", 0, t, 0, 0, off, size, 0, 0, 1, 0)  # noqa: E731
        head = lambda shoff, shnum: struct.pack(  # noqa: E731
            "<16sHHIIIIIHHHHHH", ident, e_type, profile.machine, 1, 0, 0, shoff, flags,
            ehsize, 0, 0, shentsize, shnum, 0)
    else:
        ehsize, shentsize = 64, 64
        shdr = lambda t, off, size: struct.pack("<IIQQQQIIQQ", 0, t, 0, 0, off, size, 0, 0, 1, 0)  # noqa: E731
        head = lambda shoff, shnum: struct.pack(  # noqa: E731
            "<16sHHIQQQIHHHHHH", ident, e_type, profile.machine, 1, 0, 0, shoff, flags,
            ehsize, 0, 0, shentsize, shnum, 0)
    if not body:
        return head(0, 0)
    shoff = ehsize + len(body)
    return (head(shoff, 2) + body + shdr(0, 0, 0)
            + shdr(nel.SHT_RISCV_ATTRIBUTES, ehsize, len(body)))


STUB_GCC = """#!/usr/bin/env python3
import os, sys
with open(os.environ["STUB_GCC_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    print(os.environ.get("STUB_GCC_VERSION", "stub-gcc (Buildroot 2024.11.4) 13.3.0"))
    sys.exit(0)
out = sys.argv[sys.argv.index("-o") + 1]
with open(os.environ["STUB_GCC_ELF"], "rb") as src, open(out, "wb") as dst:
    dst.write(src.read())
"""


def _exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _tree(tmp: Path) -> Path:
    """The repo pieces the scripts and the tool read, copied under tmp/repo."""
    repo = tmp / "repo"
    for rel in ("tools/native_edge_lib.py", *nel.SOURCES,
                "sw/linux/extract_python_overlay.sh", "sw/linux/clean_python_overlay.py",
                "sw/linux/pfsoc/build_python_overlay.sh",
                "sw/linux/pfsoc/buildroot_pfsoc_defconfig",
                # DS-P2.401: build_python_overlay.sh runs the linux-stage preflight first.
                "tools/preflight/ds_preflight.py", "tools/preflight/supported_environment.yml",
                "tools/active_target.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, repo / rel)
    return repo


def _stub_missing_host_programs(repo: Path, bin_dir: Path, path: str) -> None:
    """DS-P2.401: the preflight names Buildroot host programs; a program the child's PATH lacks gets a stub on the
    test PATH (an absolute one is re-pointed at its stub in the copied declaration), so the test does not depend on
    the host having Buildroot's programs. Found as the preflight finds it (shutil.which: executable, absolute names
    checked directly) on the child's PATH; programs found are left alone, since the scripts really run them."""
    import yaml
    decl_path = repo / "tools/preflight/supported_environment.yml"
    decl = yaml.safe_load(decl_path.read_text(encoding="utf-8"))
    programs = decl.setdefault("linux_host", {}).setdefault("buildroot_programs", [])
    for i, name in enumerate(programs):
        alts = name.split("|")
        if any(shutil.which(a, path=path) for a in alts):
            continue
        if alts[0].startswith("/"):  # outside PATH, so a bare call of the same name never reaches it
            stub = bin_dir.parent / "absolute-stubs" / Path(alts[0]).name
            programs[i] = str(stub)
        else:
            stub = bin_dir / alts[0]
        _exe(stub, "#!/bin/sh\nexit 0\n")
    decl_path.write_text(yaml.safe_dump(decl, sort_keys=False), encoding="utf-8")


def _fake_buildroot(br: Path, profile: nel.Profile, lib_elf: bytes, tmp: Path) -> dict:
    """output/target with a Python of the profile's kind and output/host's gcc stub."""
    target = br / "output/target"
    _exe(target / "usr/bin/python3.12", "#!/bin/false\n")
    (target / "usr/lib/python3.12/site-packages/msgpack").mkdir(parents=True)
    (target / "usr/lib/python3.12/os.py").write_text("")
    (target / "usr/lib/libpython3.12.so.1.0").write_bytes(make_elf(profile))
    (target / "lib").mkdir(parents=True)
    (target / "lib/libc.so.6").write_bytes(b"libc")
    _exe(target / "usr/sbin/dropbear", "#!/bin/false\n")
    (br / "configs").mkdir(parents=True)
    _exe(tmp / "bin/make", "#!/bin/sh\nexit 0\n")
    elf = tmp / "built.elf"
    elf.write_bytes(lib_elf)
    log = tmp / "gcc.log"
    _exe(br / "output/host/bin" / Path(profile.buildroot_cc).name, STUB_GCC)
    # DS-P2.262: a real Buildroot output has a host python3 (extract_python_overlay.sh uses it).
    (br / "output/host/bin/python3").symlink_to(sys.executable)
    return dict(os.environ, STUB_GCC_LOG=str(log), STUB_GCC_ELF=str(elf),
                PATH=f"{tmp / 'bin'}:{os.environ['PATH']}")


SCRIPTS = {
    "zybo": ("sw/linux/extract_python_overlay.sh", "sw/linux/buildroot", "sw/linux/python_overlay"),
    "pfsoc": ("sw/linux/pfsoc/build_python_overlay.sh", "sw/linux/pfsoc/buildroot",
              "sw/linux/pfsoc/python_overlay"),
}


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_the_overlay_build_installs_the_library_built_from_source(tmp_path, name):
    profile = nel.PROFILES[name]
    script, br, overlay_rel = SCRIPTS[name]
    assert overlay_rel == profile.overlay
    repo = _tree(tmp_path)
    lib_elf = make_elf(profile, arch=RV_ARCH_GCC11)
    env = _fake_buildroot(repo / br, profile, lib_elf, tmp_path)
    if name == "pfsoc":  # only the PolarFire script runs the preflight; Zybo keeps the host's own programs
        _stub_missing_host_programs(repo, tmp_path / "bin", env["PATH"])
    proc = subprocess.run(["bash", str(repo / script)], env=env, capture_output=True,
                          text=True, errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    overlay = repo / overlay_rel
    lib = overlay / nel.LIB_REL
    assert "/" + nel.LIB_REL == str(edge_frames.SYSTEM_LIB)
    assert lib.is_file(), f"{script} did not install {nel.LIB_REL}"
    assert lib.read_bytes() == lib_elf
    stamp = nel.read_stamp(overlay)
    assert stamp["native_edge_lib_sha256"] == hashlib.sha256(lib_elf).hexdigest()
    assert stamp["native_edge_source_sha256"] == nel.source_sha256(repo)
    assert stamp["native_edge_toolchain"] == "buildroot"
    # DS-P2.262: the record ships in the image, so it names the compiler, not its path.
    assert stamp["native_edge_compiler"] == Path(profile.buildroot_cc).name
    assert "/" not in stamp["native_edge_compiler"]
    build_line = [ln for ln in (tmp_path / "gcc.log").read_text().splitlines() if "-o" in ln]
    assert len(build_line) == 1, build_line
    for flag in (*nel.BASE_CFLAGS, *profile.cflags, nel.SOURCE, "-lm"):
        assert flag in build_line[0].split(), f"{flag} missing from {build_line[0]}"
    check = subprocess.run([sys.executable, str(repo / "tools/native_edge_lib.py"), "check",
                            "--profile", name], capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stderr


def _installed(tmp_path, name: str, lib_elf: bytes | None = None, version: str | None = None):
    profile = nel.PROFILES[name]
    repo = _tree(tmp_path)
    overlay = tmp_path / "overlay"
    (overlay / "usr/lib").mkdir(parents=True)
    (overlay / "usr/lib/libpython3.12.so.1.0").write_bytes(make_elf(profile))
    env = _fake_buildroot(tmp_path / "br", profile, lib_elf or make_elf(profile), tmp_path)
    if version:
        env["STUB_GCC_VERSION"] = version
    cc = tmp_path / "br/output/host/bin" / Path(profile.buildroot_cc).name
    proc = subprocess.run([sys.executable, str(repo / "tools/native_edge_lib.py"), "install",
                           "--profile", name, "--overlay", str(overlay), "--cc", str(cc)],
                          env=env, capture_output=True, text=True, timeout=60)
    return repo, overlay, proc


def _check(repo: Path, overlay: Path, name: str):
    return subprocess.run([sys.executable, str(repo / "tools/native_edge_lib.py"), "check",
                           "--profile", name, "--overlay", str(overlay)],
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("name", sorted(nel.PROFILES))
def test_the_image_check_refuses_an_overlay_without_the_library(tmp_path, name):
    repo, overlay, proc = _installed(tmp_path, name)
    assert proc.returncode == 0, proc.stderr
    assert _check(repo, overlay, name).returncode == 0
    (overlay / nel.LIB_REL).unlink()
    refused = _check(repo, overlay, name)
    assert refused.returncode == 1
    assert "would run the Python edge path" in refused.stderr
    assert f"python3 tools/native_edge_lib.py install --profile {name}" in refused.stderr


@pytest.mark.parametrize("name", sorted(nel.PROFILES))
def test_the_image_check_refuses_a_library_built_from_other_sources(tmp_path, name):
    repo, overlay, proc = _installed(tmp_path, name)
    assert proc.returncode == 0, proc.stderr
    src = repo / nel.SOURCE
    src.write_text(src.read_text() + "\n/* changed */\n")
    refused = _check(repo, overlay, name)
    assert refused.returncode == 1
    assert "changed since the install" in refused.stderr


def test_the_image_check_refuses_a_library_whose_bytes_are_not_the_stamped_ones(tmp_path):
    repo, overlay, proc = _installed(tmp_path, "zybo")
    assert proc.returncode == 0, proc.stderr
    lib = overlay / nel.LIB_REL
    lib.write_bytes(lib.read_bytes() + b"\0")
    refused = _check(repo, overlay, "zybo")
    assert refused.returncode == 1 and "not the one its stamp records" in refused.stderr


def test_a_hard_float_arm_build_is_refused_for_the_soft_float_zynq_image(tmp_path):
    """The Zynq Buildroot toolchain is soft-float (ld-linux.so.3): an
    arm-linux-gnueabihf build sets EF_ARM_ABI_FLOAT_HARD and would not load."""
    zybo = nel.PROFILES["zybo"]
    hard = make_elf(zybo, eflags=0x05000000 | nel.EF_ARM_ABI_FLOAT_HARD)
    repo, overlay, proc = _installed(tmp_path, "zybo", lib_elf=hard)
    assert proc.returncode == 1
    assert "e_flags 0x5000400" in proc.stderr and "soft-float" in proc.stderr
    assert not (overlay / nel.LIB_REL).exists()


@pytest.mark.parametrize("why,lib", [
    ("single-float ABI", dict(eflags=nel.EF_RISCV_RVC | 0x2)),
    ("no RVC", dict(eflags=nel.EF_RISCV_FLOAT_ABI_DOUBLE)),
    ("vector extension", dict(arch="rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_v1p0")),
    ("32-bit", dict(eflags=None)),
])
def test_a_riscv_build_the_image_python_cannot_load_is_refused(tmp_path, why, lib):
    pfsoc = nel.PROFILES["pfsoc"]
    if why == "32-bit":
        elf = make_elf(nel.PROFILES["zybo"])
    else:
        elf = make_elf(pfsoc, **lib)
    repo, overlay, proc = _installed(tmp_path, "pfsoc", lib_elf=elf)
    assert proc.returncode == 1, f"{why}: {proc.stdout}"
    assert "cannot load" in proc.stderr
    assert not (overlay / nel.LIB_REL).exists()


def test_a_compiler_that_is_not_buildroot_s_is_stamped_interim(tmp_path):
    repo, overlay, proc = _installed(
        tmp_path, "pfsoc", lib_elf=make_elf(nel.PROFILES["pfsoc"], arch=RV_ARCH_GCC11),
        version="riscv64-linux-gnu-gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0")
    assert proc.returncode == 0, proc.stderr
    stamp = nel.read_stamp(overlay)
    assert stamp["native_edge_toolchain"] == "interim, not the Buildroot toolchain"
    assert stamp["native_edge_compiler_version"].startswith("riscv64-linux-gnu-gcc (Ubuntu 11.4.0")
    # DS-P2.262: the record holds the name only; hash the fixture's own compiler.
    cc = tmp_path / "br/output/host/bin" / Path(nel.PROFILES["pfsoc"].buildroot_cc).name
    assert stamp["native_edge_compiler"] == cc.name
    assert "/" not in stamp["native_edge_compiler"]
    assert stamp["native_edge_compiler_sha256"] == hashlib.sha256(cc.read_bytes()).hexdigest()


DDR_TARGETS = {
    "zybo_datalogger": "zybo", "pfsoc_datalogger": "pfsoc",
    # DS-P1.103: the bench CTU target only where bench/ exists; the export carries the two products.
    **({"canfd_demo": "zybo"} if has_bench(ROOT) else {}),
}


def test_every_ddr_target_on_these_overlays_runs_the_check():
    """Every target whose rootfs takes a Buildroot python_overlay and boots
    ds_source=ddr fails its image build without the library."""
    overlays = {p.overlay: name for name, p in nel.PROFILES.items()}
    seen = {}
    for path in all_target_files(ROOT):
        target = yaml.safe_load(path.read_text(encoding="utf-8"))
        rootfs = (target.get("linux") or {}).get("rootfs") or {}
        profile = overlays.get(rootfs.get("python_overlay"))
        if profile is None or "ds_source=ddr" not in str(target["linux"].get("bootargs", "")):
            continue
        seen[path.stem] = profile
        assert f"python3 tools/native_edge_lib.py check --profile {profile}" in rootfs["stage_command"], path
    assert seen == DDR_TARGETS


def test_the_tool_and_the_de25_image_agree_on_the_library():
    import importlib.util
    spec = importlib.util.spec_from_file_location("de25_linux_image", ROOT / "tools/de25_linux_image.py")
    de25 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(de25)
    assert nel.LIB_REL == de25.NATIVE_EDGE_LIB_REL
    assert nel.SOURCES == de25.NATIVE_EDGE_SOURCES
    assert nel.source_sha256(ROOT) == de25.native_edge_source_sha256(ROOT)
    assert nel.SOURCE_DATE_EPOCH == de25.SOURCE_DATE_EPOCH


def _host_lib(out: Path, source: Path) -> Path:
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.fail("no C compiler (cc/gcc) on PATH")
    subprocess.run([cc, "-O2", "-std=c11", "-fPIC", "-shared", "-o", str(out), str(source), "-lm"],
                   check=True, capture_output=True, text=True)
    return out


def test_the_cross_check_passes_a_correct_library_and_fails_a_wrong_one(tmp_path):
    good = _host_lib(tmp_path / "good.so", ROOT / nel.SOURCE)
    mutant_src = tmp_path / "edge_drain.c"
    text = (ROOT / nel.SOURCE).read_text()
    assert text.count("(ts_us << 8)") == 1
    mutant_src.write_text(text.replace("(ts_us << 8)", "(ts_us << 9)"))
    shutil.copy2(ROOT / "sw/stream/native/edge_drain.h", tmp_path / "edge_drain.h")
    bad = _host_lib(tmp_path / "bad.so", mutant_src)
    run = lambda lib: subprocess.run(  # noqa: E731
        [sys.executable, str(ROOT / "tools/native_edge_cross_check.py"), "--lib", str(lib),
         "--no-target-python"], capture_output=True, text=True, timeout=900)
    ok = run(good)
    assert ok.returncode == 0, ok.stdout[-3000:] + ok.stderr[-3000:]
    assert ok.stdout.count("MATCH") == 12 and "PASS: 0 mismatch(es)" in ok.stdout
    wrong = run(bad)
    assert wrong.returncode == 1 and "DIFF" in wrong.stdout


def test_the_recorded_library_sha256s_name_the_current_sources():
    """sw/stream/native/README.md records the sha256 each Buildroot toolchain
    gives for the current sources; a source change must update that table."""
    doc = (ROOT / "sw/stream/native/README.md").read_text(encoding="utf-8")
    assert f"`native_edge_source_sha256`\n`{nel.source_sha256(ROOT)[:8]}…`" in doc, (
        "edge_drain.c/.h changed: rebuild with both Buildroot toolchains and "
        "update the library sha256 table in sw/stream/native/README.md")
    for profile in nel.PROFILES:
        assert f"| `{profile}` | `" in doc.split("native_edge_lib_sha256")[1], profile


BUILDROOT_SCRIPTS = ("sw/linux/build_buildroot.sh", "sw/linux/pfsoc/build_python_overlay.sh")


def _buildroot_ref(rel: str) -> str:
    import re
    text = (ROOT / rel).read_text(encoding="utf-8")
    clones = re.findall(r"git clone[^\n]*?-b\s+(\S+)", text)
    assert clones == ['"$BUILDROOT_REF"'], f"{rel}: Buildroot clone ref {clones}"
    refs = re.findall(r'^BUILDROOT_REF="([^"]+)"$', text, re.M)
    assert len(refs) == 1, f"{rel}: BUILDROOT_REF assignments {refs}"
    return refs[0]


@pytest.mark.parametrize("rel", BUILDROOT_SCRIPTS)
def test_each_buildroot_script_clones_a_release_tag_not_a_branch(rel):
    """DS-P3.200: a branch (2024.11.x) moves with every point release and
    changes the compiler that builds the overlay and libds_edge_drain.so."""
    import re
    ref = _buildroot_ref(rel)
    assert re.fullmatch(r"\d{4}\.\d{2}(\.\d+)?|[0-9a-f]{40}", ref), (
        f"{rel}: BUILDROOT_REF={ref!r} is not a Buildroot release tag or a commit")


def test_the_lib_sha256_table_states_the_pinned_buildroot_release():
    refs = {_buildroot_ref(rel) for rel in BUILDROOT_SCRIPTS}
    assert len(refs) == 1, f"the two overlays pin different Buildroot releases: {refs}"
    doc = (ROOT / "sw/stream/native/README.md").read_text(encoding="utf-8")
    assert f"Both compilers are Buildroot {refs.pop()} " in doc


def test_the_overlay_preflight_still_refuses_a_program_the_host_lacks(tmp_path):
    """DS-P2.401: the stub helper stands in only for programs the host lacks, so the preflight's refusal stays
    reachable: a declared program with no stub stops the pfsoc overlay script before Buildroot, named in the report."""
    profile = nel.PROFILES["pfsoc"]
    script, br, _ = SCRIPTS["pfsoc"]
    repo = _tree(tmp_path)
    env = _fake_buildroot(repo / br, profile, make_elf(profile, arch=RV_ARCH_GCC11), tmp_path)
    _stub_missing_host_programs(repo, tmp_path / "bin", env["PATH"])
    for name in ("sed", "grep", "tar"):  # present on any host that runs this test, so never replaced by a stub
        assert not (tmp_path / "bin" / name).exists(), name
    import yaml
    decl_path = repo / "tools/preflight/supported_environment.yml"
    decl = yaml.safe_load(decl_path.read_text(encoding="utf-8"))
    decl["linux_host"]["buildroot_programs"] += ["ds-p2-401-absent-a", "ds-p2-401-absent-b"]
    decl_path.write_text(yaml.safe_dump(decl, sort_keys=False), encoding="utf-8")
    make_log = tmp_path / "make.log"
    _exe(tmp_path / "bin/make", f'#!/bin/sh\necho "make $*" >> "{make_log}"\nexit 0\n')
    proc = subprocess.run(["bash", str(repo / script)], env=env, capture_output=True,
                          text=True, errors="replace", timeout=120)
    said = proc.stdout[-3000:] + proc.stderr[-3000:]
    assert proc.returncode == 1, said
    found = next((ln for ln in proc.stderr.splitlines() if "found    :" in ln), "")
    assert "ds-p2-401-absent-a" in found and "ds-p2-401-absent-b" in found, said
    assert "rr linux tools install" in proc.stderr, said
    assert not make_log.exists(), f"Buildroot's make ran before the preflight refused: {make_log.read_text()}"
    assert not (tmp_path / "gcc.log").exists(), f"the overlay build ran past the preflight: {said}"
