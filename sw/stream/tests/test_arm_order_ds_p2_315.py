# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.315: arm_uart, arm_i2c and arm_spi switch EMIO_MUX_SEL before they reset and enable their data logger.

Enabled on the header pin, the UART logger calibrated its baud on the header-to-stimulus step and the I2C logger
recorded a stop_error at the switch (core-level proof: the cocotb test
hw/sim/cocotb/tests/datalogger/infra/default_mix/test_arm_order_uart_i2c_spi_ds_p2_315.py). This host test pins the
write order with hard-coded offsets, so a reorder fails here without a simulator.
"""
from __future__ import annotations

import pytest

from sw.stream import stimulus_armer as A

EMIO_MUX_SEL, DATALOGGER_CONFIG = 0x24, 0x20
LOGGER_ID = {"uart": 12, "i2c": 4, "spi": 8}


class _Rb:
    def __init__(self):
        self.writes = []

    def write_u32(self, off, val):
        self.writes.append((int(off), int(val)))


@pytest.mark.parametrize("protocol", ["uart", "i2c", "spi"])
def test_mux_is_written_before_the_logger_reset_and_enable(monkeypatch, protocol):
    monkeypatch.setattr(A.time, "sleep", lambda _s: None)
    monkeypatch.setenv("DS_MSS_FABRIC", "0")
    rb = _Rb()
    A.ARMERS[protocol](rb)
    offs = [o for o, _ in rb.writes]
    cfg = [i for i, (o, v) in enumerate(rb.writes) if o == DATALOGGER_CONFIG and (v >> 11) == LOGGER_ID[protocol]]
    assert offs.count(EMIO_MUX_SEL) == 1, rb.writes
    assert len(cfg) == 2, rb.writes
    assert offs.index(EMIO_MUX_SEL) < cfg[0], f"{protocol}: EMIO_MUX_SEL after the logger reset/enable: {rb.writes}"
