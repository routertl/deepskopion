# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.65 — Arbitrary datalogger mix contract regression test.

The ticket matrix: each profile runs `rr sim sweep` (datalogger_mix_bench.bench_failure)
with the live generator and the full deepskopion_core bench, which checks
elaboration, per-protocol traffic smoke, present-protocol telemetry and
zero telemetry for every absent protocol:
  - default mix (2 I2C, 2 UART, 1 SPI, 1 JTAG, 4 EDGE)
  - zero-instance corners: zero_i2c, zero_uart, zero_spi, zero_jtag, zero_edge
  - asymmetric mixes: 5 UART + 2 I2C, 0 UART + 5 I2C + 1 SPI
  - README example (2 I2C, 2 UART, 1 SPI, 0 JTAG, 4 EDGE)
  - maximum ID mix (27 EDGE, 1 I2C, 1 UART, 1 SPI, 1 JTAG = 31 IDs)

test_datalogger_mix_mechanisms.py holds one isolated bench per mechanism.
"""

from __future__ import annotations

import pytest

import datalogger_mix_bench as bench

PROFILES = [
    "default",
    "zero_i2c",
    "zero_uart",
    "zero_spi",
    "zero_jtag",
    "zero_edge",
    "mix_5uart_2i2c",
    "mix_0uart_5i2c_1spi",
    "readme_example",
    "max_31_id",
]


@pytest.fixture(scope="module", autouse=True)
def _ensure_workspace_synced():
    yield from bench.workspace_synced()


@pytest.mark.parametrize("profile", PROFILES)
def test_datalogger_mix_contract(profile: str):
    """Each mix elaborates and passes traffic + telemetry."""
    failure = bench.bench_failure(profile)
    assert failure is None, failure
