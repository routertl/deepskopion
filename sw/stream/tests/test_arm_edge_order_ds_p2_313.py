# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.313: arm_edge switches EMIO_MUX_SEL to the stimulus line before it enables the edge logger.

Enabled first, the logger read the header pin (held high by the header pull-up) and recorded a rise at enable and a
fall at the mux switch before the stimulus (DE25 silicon 2026-11-15; the core-level proof is the cocotb test
hw/sim/cocotb/tests/datalogger/infra/default_mix/test_arm_edge_order_ds_p2_313.py). This host test pins the write
order with hard-coded words, so a reorder fails here without a simulator.
"""
from __future__ import annotations

from sw.stream import stimulus_armer as A


class _Rb:
    def __init__(self):
        self.writes = []

    def write_u32(self, off, val):
        self.writes.append((int(off), int(val)))


def test_arm_edge_writes_the_mux_before_it_enables_the_logger(monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda _s: None)
    rb = _Rb()
    A.arm_edge(rb)
    assert rb.writes == [
        (0x24, 0x80),     # EMIO_MUX_SEL: edge stimulus onto the edge logger inputs
        (0x20, 0x800),    # DATALOGGER_CONFIG: logger id 1, enable=0
        (0x20, 0x801),    # DATALOGGER_CONFIG: logger id 1, enable=1
        (0x244, 0x0),     # STIM_EDGE_CONTROL: 0 first, so the finite vector restarts (DS-P1.108)
        (0x244, 0x3),     # STIM_EDGE_CONTROL: finite canonical vector
    ]
