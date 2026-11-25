# rr unknown-llm (AI-generated)
"""DS-T1.16 — pytest gate: a UART datalogger re-arm clears power-up state.

On the PolarFire kit the UART datalogger (id 12) never emitted a byte. A debug
image read its internals off silicon: every edge buffered, the baud measured
right, calibrationDone set, and calibrationFail_r = 1 in explicit 8N1 mode,
where the RTL never sets it. That flop had no reset and depended on its VHDL
initial value, which Synplify ignores on PolarFire (FX1172); with it set,
startStreaming stays low and the frame generator never leaves IDLE. The
per-datalogger reset (DATALOGGER_CONFIG enable=0) did not reach it, so no
re-arm could recover. uart_frame_gen.parityFail_r had the same shape.

This gate runs the REAL simulation
(hw/sim/cocotb/tests/datalogger/uart/test_uart_rearm_clears_powerup_state_ds_t1_16.py)
through its sanctioned ``run_simulation`` entry point and judges the verdict
files the sim's result_collector writes. That test puts both flops in the state
silicon had them in, runs the kit's disable/enable 8N1 arm and the DE-C0 payload
at 115200, and reads the datalogger's own message stream. It is RED on the
pre-fix RTL (only the ENABLED lifecycle frame comes out, exactly the kit's
UART_PKT_CNT = 1) and RED if either reset is removed again.

No skip paths: a gate that cannot run the simulator fails loudly.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COCOTB_TEST = (ROOT / "hw" / "sim" / "cocotb" / "tests" / "datalogger" / "uart"
               / "test_uart_rearm_clears_powerup_state_ds_t1_16.py")
RESULTS = ROOT / "sim" / "results" / COCOTB_TEST.stem / "latest.json"
RESULTS_XML = RESULTS.with_name("results.xml")
N_COCOTB_TESTS = 1


def _cocotb_failures() -> str:
    """Every per-test <failure> message/text the sim's result_collector wrote."""
    if not RESULTS_XML.exists():
        return "(no results.xml)"
    out = []
    for tc in ET.parse(RESULTS_XML).iter("testcase"):
        for f in tc.iter("failure"):
            out.append(f"{tc.get('name')}: {f.get('message') or ''} "
                       f"{(f.text or '').strip()[-1500:]}")
    return "\n".join(out) or "(no failure nodes)"


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout,
        env={**os.environ, "RR_QUEUE_SIMS": "0", "RR_QUEUE_BUILDS": "0"},
    )


def test_uart_rearm_clears_powerup_calibration_state() -> None:
    # A fresh/isolated checkout has no libs/ materialised; run_simulation
    # refuses on ip.lock drift. The sync is idempotent and cache-backed.
    rr = shutil.which("rr")
    assert rr, "rr CLI not on PATH — this gate cannot run the sanctioned sim"
    sync = _run([rr, "workspace", "sync"], timeout=600)
    assert sync.returncode == 0, (
        "rr workspace sync failed — cannot stage the sim's locked packages:\n"
        + sync.stdout[-2000:] + sync.stderr[-2000:])

    for stale in (RESULTS, RESULTS_XML):
        if stale.exists():
            stale.unlink()
    proc = _run([sys.executable, str(COCOTB_TEST)], timeout=900)
    assert RESULTS.exists(), (
        "UART re-arm cocotb test did not complete "
        f"(rc={proc.returncode}); sim output tail:\n"
        + proc.stdout[-4000:] + proc.stderr[-2000:])

    verdict = json.loads(RESULTS.read_text())
    assert (proc.returncode == 0
            and verdict.get("passed") == N_COCOTB_TESTS
            and verdict.get("failed") == 0
            and verdict.get("errors") == 0
            and verdict.get("skipped") == 0
            and verdict.get("total") == N_COCOTB_TESTS), (
        f"UART re-arm cocotb test not green: "
        f"passed={verdict.get('passed')} failed={verdict.get('failed')} "
        f"errors={verdict.get('errors')} skipped={verdict.get('skipped')} "
        f"total={verdict.get('total')} (expected {N_COCOTB_TESTS} pass); "
        f"recorded cocotb failures:\n{_cocotb_failures()}")
