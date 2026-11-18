# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.108: arm_edge clears STIM_EDGE_CONTROL before it writes the finite vector, so the vector replays.

``ds_stimulus_gen`` p_edge restarts the finite vector only on a bit[0] 0->1 transition; a second write of 0x3 over an
earlier play replays nothing (Zybo silicon 2026-11-15; the core-level proof is the cocotb test
hw/sim/cocotb/tests/datalogger/infra/default_mix/test_arm_edge_rearm_ds_p1_108.py). This host test pins the write
order with hard-coded words, so dropping the 0 write fails here without a simulator.
"""
from __future__ import annotations

from sw.stream import stimulus_armer as A


class _Rb:
    def __init__(self):
        self.writes = []

    def write_u32(self, off, val):
        self.writes.append((int(off), int(val)))


def test_arm_edge_writes_zero_before_the_finite_vector(monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda _s: None)
    rb = _Rb()
    A.arm_edge(rb)
    stim = [(o, v) for o, v in rb.writes if o == 0x244]
    assert stim == [(0x244, 0x0), (0x244, 0x3)], f"STIM_EDGE_CONTROL writes {stim}"
    assert rb.writes.index((0x244, 0x0)) == rb.writes.index((0x244, 0x3)) - 1, rb.writes
