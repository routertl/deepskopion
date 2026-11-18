"""DS-T2.84 — pytest entry points for the cocotb guards of the EDGE wire fix.

The guards are cocotb tests; this module runs each one through ``rr sim run``
(the sanctioned executor, ROUTERTL-001) and fails with the cocotb failure text,
so a pytest-only harness (the test-power receipt runner) can execute them in a
clean checkout. Each case is one real nvc simulation, about 40-75 s.

Host requirements: ``rr`` with nvc and the routertl SDK. A fresh clone has no
vendored packages under ``libs/``, so ``rr workspace sync`` runs first; without
it ``rr sim run`` stops in its closure preflight. Each run starts from an empty
nvc work library: the edge_datalogger and tb_datalogger_to_ddr benches compile
different subsets of the ``olo`` library into the same ``hw/sim/work/nvc``, and
nvc refuses the second bench on a checksum mismatch (measured). ``rr sim run
--clean`` does not help: it removes ``./sim_build`` and ``./work`` only. A sim
that does not produce a result for the testcase fails as "did not run", never
as a pass.

The two guards:

* ``test_edge_to_ddr`` (tb_datalogger_to_ddr: gather -> classifier -> edge
  aggregator / framer -> DDR, parsed with sw.stream.zs_msg). With capture
  enabled and no read requested, no EDGE_BATCH entry may be S=1; the ENABLED
  lifecycle marker must land as its own 0x0608 frame.
* ``test_coincident_edge_is_an_edge_not_a_readback`` (edge_datalogger). An edge
  on the cycle of a read must come out as an S=0 edge, not a second readback.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Every sim rewrites this pid file. It is untracked and ignored since
# DS-P3.188; restoring it keeps the checkout as the run found it.
WORK_LOCK = ROOT / "hw" / "sim" / "work.lock"
# nvc work libraries of the queued and the standalone runner (gitignored).
NVC_WORK_DIRS = (ROOT / "hw" / "sim" / "work" / "nvc", ROOT / "sim" / "work" / "nvc")


@pytest.fixture(scope="module")
def synced_workspace():
    rr = shutil.which("rr")
    if rr is None:
        pytest.fail("rr is not on PATH: the DS-T2.84 sim guards cannot run")
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


def test_lifecycle_frame_reaches_ddr_as_its_own_frame(synced_workspace):
    failure = _run_cocotb(synced_workspace, "test_datalogger_to_ddr", "test_edge_to_ddr")
    assert failure is None, failure


def test_coincident_edge_is_an_edge_not_a_readback(synced_workspace):
    failure = _run_cocotb(synced_workspace, "test_edge_datalogger",
                          "test_coincident_edge_is_an_edge_not_a_readback")
    assert failure is None, failure
