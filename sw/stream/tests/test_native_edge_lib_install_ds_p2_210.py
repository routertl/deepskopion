# rr unknown-llm (AI-generated)
"""DS-P2.210: the daemon finds an installed native edge library and says which path runs.

The DE25 image installs libds_edge_drain.so at edge_frames.SYSTEM_LIB
(tools/de25_linux_image.py, tested in sw/linux/de25/test_de25_linux_image.py).
These tests hold the daemon side: load() reaches SYSTEM_LIB when nothing
else is set, and the ddr start line names the C path when the library
resolves, the Python path when it does not, and `--native-edge-drain on`
refuses to start without it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import sw.stream.__main__ as daemon
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.native import edge_frames
from sw.stream.regbank import FakeRegbank, MmioRegbank, Regs

NATIVE = Path(__file__).resolve().parents[1] / "native"
RING_BYTES = 1 << 16


@pytest.fixture(scope="module")
def host_lib(tmp_path_factory) -> Path:
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.fail("no C compiler (cc/gcc) on PATH; the native edge path cannot be built")
    so = tmp_path_factory.mktemp("native") / edge_frames.LIB_NAME
    subprocess.run([cc, "-O2", "-std=c11", "-fPIC", "-shared", "-o", str(so),
                    str(NATIVE / "edge_drain.c"), "-lm"],
                   check=True, capture_output=True, text=True)
    return so


def test_the_image_install_path_is_the_last_place_load_looks(host_lib, tmp_path, monkeypatch):
    monkeypatch.delenv(edge_frames.ENV_LIB, raising=False)
    assert edge_frames.SYSTEM_LIB == Path("/usr/local/lib/deepskopion/libds_edge_drain.so")
    assert edge_frames.candidates()[-1] == edge_frames.SYSTEM_LIB
    monkeypatch.setattr(edge_frames, "SYSTEM_LIB", host_lib)
    beside = Path(edge_frames.__file__).with_name(edge_frames.LIB_NAME)
    assert not beside.exists(), f"a host build sits beside edge_frames.py: {beside}"
    lib = edge_frames.load(required=True)
    assert lib is not None and lib.path == host_lib


class _Regbank(FakeRegbank):
    """FakeRegbank in MmioRegbank's place: the CLI reads its class defaults."""

    def __init__(self, **_kw):
        super().__init__()
        self.set(Regs.RING_BASE_LOW, 0xA000_0000)
        self.set(Regs.RING_BYTES, RING_BYTES)


for _name in dir(MmioRegbank):
    if _name.startswith("DEFAULT_"):
        setattr(_Regbank, _name, getattr(MmioRegbank, _name))


def _start(monkeypatch, capsys, *flags):
    """Run the daemon's ddr start-up with fake hardware; (exit code, source, stderr)."""
    started = {}

    def serve(_endpoint, source, **_kw):
        started["source"] = source
        return 0

    monkeypatch.setattr(daemon, "MmioRegbank", _Regbank)
    monkeypatch.setattr(daemon, "RealDdrMmap", lambda base_addr, size: FakeDdrMmap(size))
    for name in ("serve_one", "serve_fanout", "serve_one_events"):
        monkeypatch.setattr(daemon, name, serve)
    rc = daemon.main(["--source", "ddr", "--drain-mode", "poll",
                      "--listen", "127.0.0.1:7475", *flags])
    return rc, started.get("source"), capsys.readouterr().err


def test_the_start_line_names_the_c_path_when_the_library_resolves(
        host_lib, monkeypatch, capsys):
    monkeypatch.delenv(edge_frames.ENV_LIB, raising=False)
    monkeypatch.setattr(edge_frames, "SYSTEM_LIB", host_lib)
    rc, source, err = _start(monkeypatch, capsys)
    assert rc == 0, err
    assert f"ddr source: edge frames walked and packed in C ({host_lib})" in err
    assert source._native_edges is not None


def test_the_start_line_names_the_python_path_without_the_library(
        tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(edge_frames.ENV_LIB, raising=False)
    monkeypatch.setattr(edge_frames, "SYSTEM_LIB", tmp_path / edge_frames.LIB_NAME)
    rc, source, err = _start(monkeypatch, capsys)
    assert rc == 0, err
    assert "ddr source: edge frames walked and packed in Python" in err
    assert source._native_edges is None


def test_native_on_refuses_to_start_without_the_library(tmp_path, monkeypatch, capsys):
    missing = tmp_path / edge_frames.LIB_NAME
    monkeypatch.delenv(edge_frames.ENV_LIB, raising=False)
    monkeypatch.setattr(edge_frames, "SYSTEM_LIB", missing)
    rc, source, err = _start(monkeypatch, capsys, "--native-edge-drain", "on")
    assert rc == 1 and source is None
    assert "--native-edge-drain on" in err and str(missing) in err
