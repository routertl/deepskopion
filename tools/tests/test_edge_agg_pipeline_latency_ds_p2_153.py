"""DS-P2.153 — pytest gate over the aggregator input-pipeline cocotb suite.

PF-F0's measured 100 MHz miss (WNS -5.5..-6.2 ns, Synplify fmax 79.5 MHz —
docs/evidence/pf-f0/DS-P2.151-pf-f0-fit-timing.md) sits on ONE path class: the
combinational ``f_event_timestamp`` datapath fanning out to every batcher's
``TIMESTAMP_64`` and on into ``state_r`` / the 1024-bit ``payload_buf``
variable-slot write. DS-D.45 keeps clk_sys at 100 MHz and pipelines the
aggregator instead (three declared register stages — EV input, registered
arb grant, FR output store-and-forward — in the SHARED vendor-neutral
``edge_batcher_aggregator``; EBA-REQ-40).

This gate runs the REAL simulation (the cocotb suite in
hw/sim/cocotb/tests/datalogger/infra/edge_batcher_aggregator/
test_edge_agg_pipeline_latency_p2_153.py) through its sanctioned
``run_simulation`` entry point and judges the recorded verdict file — the
surface ``rr sim``'s result_collector actually writes, not a helper's opinion.
It is RED on the pre-DS-P2.153 zero-stage aggregator (the latency probe
measures 0 extra cycles where the contract declares 3 — the probe parses the
C_EBA_*_PIPE_STAGES constants out of the RTL, so the declaration itself is
enforced), GREEN only with all three declared register stages present, and
RED again on the cheap mutants (live timestamp re-sample; half-throughput
stage).

No skip paths: a gate that cannot run the simulator must fail loudly, not
report a hollow green (fort_test_power refuses skip/xfail anyway).
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
COCOTB_TEST = (ROOT / "hw" / "sim" / "cocotb" / "tests" / "datalogger" / "infra"
               / "edge_batcher_aggregator"
               / "test_edge_agg_pipeline_latency_p2_153.py")
RESULTS = ROOT / "sim" / "results" / COCOTB_TEST.stem / "latest.json"
RESULTS_XML = RESULTS.with_name("results.xml")
N_COCOTB_TESTS = 3


def _cocotb_failures() -> str:
    """Every per-test <failure> message/text the sim's result_collector wrote —
    the genuine assertion identities, immune to stdout-tail truncation."""
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


def test_aggregator_input_pipeline_cocotb_suite_is_green() -> None:
    # A fresh/isolated checkout (fort_test_power clones one per revision) has
    # no libs/ materialised; run_simulation refuses on ip.lock drift. The sync
    # is idempotent and cache-backed (~seconds when already synced).
    rr = shutil.which("rr")
    assert rr, "rr CLI not on PATH — this gate cannot run the sanctioned sim"
    sync = _run([rr, "workspace", "sync"], timeout=600)
    assert sync.returncode == 0, (
        "rr workspace sync failed — cannot stage the sim's locked packages:\n"
        + sync.stdout[-2000:] + sync.stderr[-2000:])

    if RESULTS.exists():
        RESULTS.unlink()
    if RESULTS_XML.exists():
        RESULTS_XML.unlink()
    proc = _run([sys.executable, str(COCOTB_TEST)], timeout=900)
    assert RESULTS.exists(), (
        "aggregator input-pipeline cocotb suite did not complete "
        f"(rc={proc.returncode}); sim output tail:\n"
        + proc.stdout[-4000:] + proc.stderr[-2000:])

    verdict = json.loads(RESULTS.read_text())
    assert (proc.returncode == 0
            and verdict.get("passed") == N_COCOTB_TESTS
            and verdict.get("failed") == 0
            and verdict.get("errors") == 0
            and verdict.get("skipped") == 0
            and verdict.get("total") == N_COCOTB_TESTS), (
        f"aggregator input-pipeline cocotb suite not green: "
        f"passed={verdict.get('passed')} failed={verdict.get('failed')} "
        f"errors={verdict.get('errors')} skipped={verdict.get('skipped')} "
        f"total={verdict.get('total')} (expected {N_COCOTB_TESTS} passes); "
        f"recorded cocotb failures:\n{_cocotb_failures()}")
