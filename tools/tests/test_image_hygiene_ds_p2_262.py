"""DS-P2.262: the published Zybo canfd_demo uImage carried build-host data.

public_artifact_hygiene found, among other lines, build-host home paths in
  A  python3.12 config-3.12-*/Makefile, _sysconfigdata*.pyc, libstdc++*-gdb.py
  B  host-made cpython-310 bytecode in the staged daemon tree
  D  etc/deepskopion-native-edge (the compiler's full path)
  E  libds_edge_drain.so (debug sections from the linked libgcc objects)
These tests pin the producer-side fixes. None of them reads the bench's extracted
python overlay: each builds its own fixture tree.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import py_compile
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
CLEANER = ROOT / "sw" / "linux" / "clean_python_overlay.py"
sys.path.insert(0, str(ROOT / "tools"))
import native_edge_lib as N  # noqa: E402

# Built at run time so this file does not itself carry the patterns the
# public-artifact scanner refuses (as its own tests do).
HOST = "/" + "home" + "/someone/work/proj"  # a build-host prefix, any user name
UPSTREAM = "C:/" + "Users" + "/Barney"      # CPython ntpath docstring literal


def _run(*args):
    return subprocess.run([sys.executable, str(CLEANER), *args], capture_output=True,
                          text=True, errors="replace")


def _overlay(tmp: Path, dirty: bool) -> Path:
    ov = tmp / "python_overlay"
    lib = ov / "usr" / "lib"
    (lib / "python3.12").mkdir(parents=True)
    (lib / "libpython3.12.so.1.0").write_bytes(b"\x7fELF ... frozen ntpath '" + UPSTREAM.encode() + b"' ...")
    (ov / "etc").mkdir()
    if dirty:
        (lib / "python3.12" / "config-3.12-arm-linux-gnueabi").mkdir()
        (lib / "python3.12" / "config-3.12-arm-linux-gnueabi" / "Makefile").write_text(
            f"srcdir= {HOST}/sw/linux/buildroot/output/build/python3-3.12.10\n")
        (lib / "libstdc++.so.6.0.32-gdb.py").write_text(f"libdir = '{HOST}/lib'\n")
        (ov / "etc" / "deepskopion-native-edge").write_text(
            f"native_edge_compiler={HOST}/sw/linux/buildroot/output/host/bin/arm-gcc\n")
        (ov / "etc" / "other.bin").write_bytes(b"\x00/root/build/x\x00")
    else:
        (ov / "etc" / "deepskopion-native-edge").write_text("native_edge_compiler=arm-gcc\n")
    return ov


# --- A, D, E: one check over the whole python overlay --------------------------------

def test_check_refuses_build_host_paths_and_build_only_files(tmp_path):
    ov = _overlay(tmp_path, dirty=True)
    r = _run("--check", str(ov))
    assert r.returncode == 1, r.stdout + r.stderr
    for name in ("config-3.12-arm-linux-gnueabi/Makefile", "libstdc++.so.6.0.32-gdb.py",
                 "etc/deepskopion-native-edge", "etc/other.bin"):
        assert name in r.stdout, f"{name} not refused:\n{r.stdout}"


def test_check_accepts_a_clean_overlay_and_upstream_constants(tmp_path):
    r = _run("--check", str(_overlay(tmp_path, dirty=False)))
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_check_pattern_names_no_user():
    text = CLEANER.read_text(encoding="utf-8")
    assert not re.search(r"/home/[a-z][a-z0-9_-]*/", text), "a user name is spelled in the cleaner"


def test_fix_drops_build_only_files_and_rewrites_sysconfigdata(tmp_path):
    ov = _overlay(tmp_path, dirty=True)
    src = tmp_path / "_sysconfigdata__linux_arm-linux-gnueabi.py"
    want_keys = {"prefix", "srcdir", "CONFIG_ARGS", "LIBDIR"}
    src.write_text(
        "build_time_vars = {'prefix': '/usr', "
        f"'srcdir': '{HOST}/sw/linux/buildroot/output/build/python3-3.12.10', "
        f"'CONFIG_ARGS': \"'--prefix=/usr' '-I{HOST}/sw/linux/buildroot/output/host/include'\", "
        "'LIBDIR': '/usr/lib'}\n")
    pyc = ov / "usr" / "lib" / "python3.12" / src.with_suffix(".pyc").name
    py_compile.compile(str(src), cfile=str(pyc), doraise=True)
    (ov / "etc" / "other.bin").unlink()
    (ov / "etc" / "deepskopion-native-edge").write_text("native_edge_compiler=arm-gcc\n")
    r = _run("--fix", str(ov), "--python", sys.executable)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (ov / "usr/lib/python3.12/config-3.12-arm-linux-gnueabi/Makefile").exists()
    assert not (ov / "usr/lib/libstdc++.so.6.0.32-gdb.py").exists()
    loader = importlib.machinery.SourcelessFileLoader("_sysconfigdata_t", str(pyc))
    spec = importlib.util.spec_from_loader("_sysconfigdata_t", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    got = mod.build_time_vars
    assert set(got) == want_keys
    assert got["prefix"] == "/usr" and got["LIBDIR"] == "/usr/lib"
    assert got["srcdir"] == "/buildroot/output/build/python3-3.12.10"
    assert "/home/" not in repr(got)
    assert _run("--check", str(ov)).returncode == 0


def test_extract_runs_the_cleaner_with_the_buildroot_python():
    text = (ROOT / "sw" / "linux" / "extract_python_overlay.sh").read_text(encoding="utf-8")
    assert "clean_python_overlay.py" in text and "--fix" in text, "extract never cleans the overlay"
    assert "buildroot/output/host/bin/python3" in text


def test_every_target_using_the_shared_python_overlay_checks_it():
    users = []
    for t in all_target_files(ROOT):
        rootfs = ((yaml.safe_load(t.read_text(encoding="utf-8")) or {}).get("linux") or {}).get("rootfs") or {}
        if rootfs.get("python_overlay") == "sw/linux/python_overlay":
            users.append(t.name)
            assert "clean_python_overlay.py --check sw/linux/python_overlay" in (rootfs.get("stage_command") or ""), \
                f"{t.name} stages the shared python overlay without checking it"
    assert users, "no target uses sw/linux/python_overlay"


# --- D, E: the native edge library and its record ------------------------------------

def test_the_record_names_the_compiler_not_its_path(tmp_path):
    cc = tmp_path / "bin" / "arm-buildroot-linux-gnueabi-gcc"
    cc.parent.mkdir()
    cc.write_text("#!/bin/sh\necho 'arm-buildroot-linux-gnueabi-gcc.br_real (Buildroot 2024.11.4) 13.3.0'\n")
    cc.chmod(0o755)
    ident = N.compiler_identity(cc)
    assert ident["native_edge_compiler"] == "arm-buildroot-linux-gnueabi-gcc"
    assert ident["native_edge_compiler_version"].startswith("arm-buildroot")


def test_the_library_is_linked_without_debug_sections(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"x")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(N.subprocess, "run", fake_run)
    monkeypatch.setattr(N, "problems", lambda *a, **k: [])
    N.build(ROOT, N.PROFILES["zybo"], tmp_path / "cc", tmp_path / "out.so")
    assert "-Wl,--strip-debug" in seen["cmd"], seen["cmd"]


# --- B: host bytecode never reaches the staged daemon tree ---------------------------

_SHARE = ROOT / "sw" / "linux" / "rootfs_overlay" / "usr" / "local" / "share" / "deepskopion"


def test_stage_daemon_removes_bytecode_from_the_staged_tree():
    if shutil.which("rsync") is None:
        pytest.skip("rsync unavailable")
    subprocess.run(["bash", "sw/linux/stage_daemon.sh"], cwd=ROOT, check=True,
                   capture_output=True)
    planted = _SHARE / "sw" / "stream" / "__pycache__" / "planted.cpython-310.pyc"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_bytes(b"\x00")
    subprocess.run(["bash", "sw/linux/stage_daemon.sh"], cwd=ROOT, check=True,
                   capture_output=True)
    left = [str(p.relative_to(_SHARE)) for p in _SHARE.rglob("*") if p.name == "__pycache__" or p.suffix == ".pyc"]
    assert not left, f"stage_daemon.sh left bytecode in the staged tree: {left}"


def test_the_deployed_tree_test_writes_no_bytecode():
    if shutil.which("rsync") is None:
        pytest.skip("rsync unavailable")
    sys.path.insert(0, str(ROOT / "sw" / "linux" / "tests"))
    import test_deployed_tree_self_contained as T
    for d in [p for p in _SHARE.rglob("__pycache__")]:
        shutil.rmtree(d)
    try:
        T.test_staged_tree_imports_the_daemon_with_nothing_but_itself()
    except pytest.skip.Exception:
        pass
    left = [str(p.relative_to(_SHARE)) for p in _SHARE.rglob("__pycache__")]
    assert not left, f"the deployed-tree test wrote bytecode into the real overlay: {left}"
