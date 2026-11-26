# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.398: recording an armer's writes leaves the process environment as it found it.

``_armer_writes`` (the self-test coverage bench's recorder) sets DS_MSS_FABRIC to arm like the PolarFire build.
Left set, it made every later armer test in the same pytest process arm as MSS fabric: run after
tools/tests/test_selftest_reach_readme_ds_p1_112.py, six sw/stream armer tests failed (bisected 2026-11-26; the same six
fail with DS_MSS_FABRIC=1 alone).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COVERAGE = ROOT / "hw/sim/cocotb/tests/datalogger/infra/default_mix/test_selftest_coverage.py"
POLLUTER = "tools/tests/test_selftest_reach_readme_ds_p1_112.py"
POLLUTING_TEST = "test_the_polarfire_arm_is_recorded_without_a_device"
VICTIMS = ["sw/stream/tests/test_arm_datalogger_defaults.py", "sw/stream/tests/test_de_c0_production_armer.py"]
VICTIM_TESTS = {
    "test_disable_precedes_enable_so_calibration_actually_resets", "test_arming_is_reported_for_the_boot_log",
    "test_all_protocols_arm_without_reading_the_ring", "test_uart_and_spi_drive_the_xilinx_oracle_payloads",
    "test_datalogger_enable_is_a_reset_then_enable_pulse", "test_uart_enables_its_datalogger_at_8n1_before_the_stimulus",
}

#: Runs pytest in the child with every host serial device refused: if the leak returns, the victims arm as MSS
#: fabric, whose transmit path opens /dev/ttyS1, which on a bench host is a real serial port.
_CHILD = r"""
import builtins, io, os, sys
import pytest

def _refuse(path):
    p = os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
    p = p.decode() if isinstance(p, bytes) else p
    if p.startswith("/dev/tty"):
        raise OSError(f"DS-P2.398 child: host device {p} refused")

_open, _os_open = builtins.open, os.open
def _guarded_open(file, *a, **k):
    _refuse(file)
    return _open(file, *a, **k)
def _guarded_os_open(path, *a, **k):
    _refuse(path)
    return _os_open(path, *a, **k)
builtins.open = io.open = _guarded_open
os.open = _guarded_os_open
sys.exit(pytest.main(sys.argv[1:]))
"""


def _coverage_module():
    spec = importlib.util.spec_from_file_location("_ds_selftest_coverage_p2_398", COVERAGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def clean_process_state(monkeypatch):
    """Restore DS_MSS_FABRIC and sys.path explicitly, whatever the test does (the bench module prepends paths)."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    original = os.environ.get("DS_MSS_FABRIC")
    yield
    if original is None:
        os.environ.pop("DS_MSS_FABRIC", None)
    else:
        os.environ["DS_MSS_FABRIC"] = original


def _set(value: str | None) -> None:
    if value is None:
        os.environ.pop("DS_MSS_FABRIC", None)
    else:
        os.environ["DS_MSS_FABRIC"] = value


@pytest.mark.parametrize("before", [None, "0", "1"])
@pytest.mark.parametrize("mss", [True, False])
def test_recording_an_armer_restores_ds_mss_fabric(clean_process_state, before, mss):
    _set(before)
    _coverage_module()._armer_writes("arm_uart", mss=mss)
    assert os.environ.get("DS_MSS_FABRIC") == before


@pytest.mark.parametrize("before", [None, "0"])
def test_a_failing_armer_still_restores_ds_mss_fabric(clean_process_state, before):
    _set(before)
    with pytest.raises(AttributeError):
        _coverage_module()._armer_writes("no_such_armer", mss=True)
    assert os.environ.get("DS_MSS_FABRIC") == before


@pytest.mark.parametrize("before", [None, "0"])
def test_a_setup_failure_before_arming_leaves_ds_mss_fabric_alone(clean_process_state, monkeypatch, before):
    """An exception in the recorder's setup, before its try, must not leave the flag set (T1 rv-50cf1f6926)."""
    _set(before)
    mod = _coverage_module()

    def broken_recorder():
        raise RuntimeError("recorder setup failed")

    monkeypatch.setattr(mod, "_Recorder", broken_recorder)
    with pytest.raises(RuntimeError, match="recorder setup failed"):
        mod._armer_writes("arm_uart", mss=True)
    assert os.environ.get("DS_MSS_FABRIC") == before


def test_the_armer_tests_pass_after_the_readme_reach_tests_in_one_process(tmp_path):
    """The effect: the bisected order in one pytest process, child options controlled, host serial devices refused,
    and the report proving the polluting test ran first and all six victims ran and passed."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("DS_MSS_FABRIC", "PYTEST_ADDOPTS") and not k.startswith("PYTEST_XDIST")}
    report = tmp_path / "report.xml"
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, "-q", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:randomly",
         "-o", "addopts=", "-m", "not nightly", f"--junitxml={report}", POLLUTER, *VICTIMS],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    said = proc.stdout[-3000:] + proc.stderr[-2000:]
    cases = list(ET.parse(report).getroot().iter("testcase"))
    names = [c.get("name").split("[")[0] for c in cases]
    bad = [c.get("name") for c in cases if any(c.find(t) is not None for t in ("failure", "error", "skipped"))]
    assert proc.returncode == 0 and bad == [], said
    assert POLLUTING_TEST in names and VICTIM_TESTS <= set(names), names
    first_victim = min(names.index(n) for n in VICTIM_TESTS)
    assert names.index(POLLUTING_TEST) < first_victim, names
