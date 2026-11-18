"""
test_stimulus_edge_muxsel — DS-P2.100 / DS-D.38 option a fabric-side
stimulus generator, edge/pulse source + tb-local EMIO_MUX_SEL bit[7] mux
tap coverage (STIM-REQ-7), proven against the REAL fabric RTL. See
test_stimulus_uart_frame.py's docstring for why this suite is one
@cocotb.test() per file.

Note (Finding C of the DS-P2.100 landing review): the mux tap exercised
here (tb_stimulus_gen_loop.vhd:166, CAPTURE_EDGE0 <= stim_edge_w when
STIM_EDGE_SEL='1' else PMOD_EDGE_FALLBACK) is a TB-LOCAL replica of the
real deepskopion_core.vhd capture_pin(42) priority-chain tap — it proves the
generator/mux topology, but not the real core's C_INDEX_EMIO_MUX_SEL
regbank-bit-7 wiring. That's proven separately, against the REAL core, by
hw/sim/cocotb/tests/stimulus_core_seam/test_stimulus_core_seam_edge_gate.py
(tb_stimulus_core_seam.vhd, which instantiates tich.deepskopion_core
directly) — this file and that one are complementary, not redundant.

Run (from the deepskopion dir):  rr sim run test_stimulus_edge_muxsel
All tests together:               rr sim --tag stimulus_gen_loop
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, Timer

from _stim_common import (  # noqa: E402 (sets up sys.path for sdk.* below)
    reset_dut, run_stimulus_gen_loop, start_clock,
)

from sdk.cocotb_helpers import requires  # noqa: E402


@cocotb.test()
@requires("STIM-REQ-7")
async def test_edge_mux_sel_zero_falls_back_to_pmod(dut):
    """Proves the tb-local EMIO_MUX_SEL bit[7] tap is a real selector, not
    a permanently-engaged short: with the bit clear, the free-running
    generator must NOT reach the datalogger tap — PMOD_EDGE_FALLBACK wins."""
    await start_clock(dut)
    await reset_dut(dut)

    dut.STIM_EDGE_CONTROL.value = (2 << 4) | 1  # enabled, fast toggle
    dut.STIM_EDGE_SEL.value = 0  # bit[7] clear
    dut.PMOD_EDGE_FALLBACK.value = 1
    await Timer(1, unit="ns")  # let the combinational mux settle before the first read

    for _ in range(200):
        assert dut.CAPTURE_EDGE0.value == 1, "EDGE0 tap leaked the stim source with bit[7]=0"
        await ClockCycles(dut.CLK, 1)


if __name__ == "__main__":
    run_stimulus_gen_loop("test_stimulus_edge_muxsel")
