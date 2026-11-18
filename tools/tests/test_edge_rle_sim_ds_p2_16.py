"""DS-P2.16 — pytest entry points for the cocotb guards of edge Stream-RLE mode.

The guards are cocotb tests; this module runs each one through ``rr sim run``
(the sanctioned executor, ROUTERTL-001) and fails with the cocotb failure text,
so a pytest-only harness (the test-power receipt runner) can execute them in a
clean checkout. Each case is one real nvc simulation. Same harness as
test_edge_wire_identity_sim_ds_t2_84.py (fresh nvc work library per run,
``rr workspace sync`` first, a sim with no result is "did not run", never a
pass).

No duplicate-design-unit override is passed: DS-P2.200 refreshed the vendored
libs/routertl/edge_detector to the SDK unit, so rr has no differing
``edge_detector`` to refuse (test_sdk_builtin_vendored_current_ds_p2_200.py).

* ``test_edge_streamer`` (edge_datalogger alone): the RLE word stream, one
  testcase per compressor arm — PACK, LONG, IDLE, the 255-tick partial-PACK
  flush, the disable flush, re-SYNC after a lost word.
* ``test_datalogger_to_ddr::test_edge_rle_to_ddr``: gather -> classifier ->
  framer -> DDR. Each RLE word lands as its own EVT_EDGE_RLE frame.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORK_LOCK = ROOT / "hw" / "sim" / "work.lock"
NVC_WORK_DIRS = (ROOT / "hw" / "sim" / "work" / "nvc", ROOT / "sim" / "work" / "nvc")


@pytest.fixture(scope="module")
def synced_workspace():
    rr = shutil.which("rr")
    if rr is None:
        pytest.fail("rr is not on PATH: the DS-P2.16 sim guards cannot run")
    proc = subprocess.run([rr, "workspace", "sync"], cwd=ROOT, text=True,
                          capture_output=True, timeout=300)
    if proc.returncode != 0:
        pytest.fail(f"rr workspace sync failed (exit {proc.returncode}):\n"
                    f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    return rr


def _run_cocotb(rr: str, module: str, testcase: str) -> str | None:
    """Run one cocotb testcase; return its failure message, or None if it passed."""
    results = ROOT / "sim" / "results" / module / "results.xml"
    if results.exists():
        results.unlink()
    for work in NVC_WORK_DIRS:
        shutil.rmtree(work, ignore_errors=True)
    lock = WORK_LOCK.read_bytes() if WORK_LOCK.exists() else None
    env = os.environ.copy()
    env["RR_SIM_QUEUE_WAIT"] = "1"
    env["COCOTB_TEST_FILTER"] = rf"(?:^|\.){testcase}$"
    try:
        proc = subprocess.run(
            [rr, "sim", "run", module, "--pass-env", "COCOTB_TEST_FILTER"],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=1200)
    finally:
        if lock is not None:
            WORK_LOCK.write_bytes(lock)
    if not results.is_file():
        pytest.fail(f"SIM DID NOT RUN: rr sim run {module} (exit {proc.returncode}) "
                    f"wrote no results.xml\n{proc.stdout[-3000:]}\n{proc.stderr[-1500:]}")
    for case in ET.parse(results).iter("testcase"):
        if case.get("name") != testcase:
            continue
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if bad is None:
            if case.find("skipped") is not None:
                pytest.fail(f"SIM DID NOT RUN: {module}.{testcase} was skipped")
            return None
        return f"{bad.get('message') or ''}\n{bad.text or ''}"
    pytest.fail(f"SIM DID NOT RUN: {module}.{testcase} is not in {results}")


def test_event_mode_stamp_lag(synced_workspace):
    failure = _run_cocotb(synced_workspace, "test_edge_streamer", "test_event_mode_stamp_lag")
    assert failure is None, failure


def test_rle_pack_words(synced_workspace):
    failure = _run_cocotb(synced_workspace, "test_edge_streamer", "test_rle_pack_words")
    assert failure is None, failure


def test_rle_long_words(synced_workspace):
    failure = _run_cocotb(synced_workspace, "test_edge_streamer", "test_rle_long_words")
    assert failure is None, failure


def test_rle_idle_word(synced_workspace):
    failure = _run_cocotb(synced_workspace, "test_edge_streamer", "test_rle_idle_word")
    assert failure is None, failure


def test_rle_partial_pack_flush(synced_workspace):
    failure = _run_cocotb(synced_workspace, "test_edge_streamer", "test_rle_partial_pack_flush")
    assert failure is None, failure


def test_rle_disable_flush(synced_workspace):
    failure = _run_cocotb(synced_workspace, "test_edge_streamer", "test_rle_disable_flush")
    assert failure is None, failure


def test_rle_resync_after_lost_word(synced_workspace):
    failure = _run_cocotb(synced_workspace, "test_edge_streamer",
                          "test_rle_resync_after_lost_word")
    assert failure is None, failure


def test_rle_words_reach_ddr_as_own_frames(synced_workspace):
    failure = _run_cocotb(synced_workspace, "test_datalogger_to_ddr", "test_edge_rle_to_ddr")
    assert failure is None, failure
