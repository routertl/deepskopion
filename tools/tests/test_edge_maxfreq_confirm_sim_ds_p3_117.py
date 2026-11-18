"""DS-P3.117 — pytest entry point for the edge_datalogger max-frequency
confirmation gate (EDGE-REQ-64).

The guard is a cocotb test; this module runs it through ``rr sim run`` (the
sanctioned executor, ROUTERTL-001) with the same harness as the DS-T2.84 sim
guards, so a pytest-only runner (the test-power receipt) can execute it in a
clean checkout. One real nvc simulation, about 1-3 min including the queue.

Host requirements: ``rr`` with nvc and the routertl SDK (see
test_edge_wire_identity_sim_ds_t2_84 for the workspace-sync and nvc work-library
handling this reuses). A sim that produces no result fails as "did not run",
never as a pass.

The guard: elaborated with G_MAXFREQ_CONFIRM = 4, a runt pulse wider than the
glitch floor in the middle of a half-period must not raise the latched max
frequency, a genuinely faster line must latch only once it lasts 4 intervals,
and after a host re-arm nothing may latch before 4 intervals have closed.
"""

from __future__ import annotations

from test_edge_wire_identity_sim_ds_t2_84 import _run_cocotb, synced_workspace  # noqa: F401


def test_maxfreq_runt_above_floor_cannot_raise_max(synced_workspace):
    failure = _run_cocotb(synced_workspace, "test_edge_datalogger_maxfreq_confirm",
                          "test_maxfreq_runt_above_floor_cannot_raise_max")
    assert failure is None, failure
