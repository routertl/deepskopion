"""
test_stimulus_core_seam_zybo_edge_finite — DS-P1.90, STIM-SEAM-REQ-7.

Zynq wiring (tb_stimulus_core_seam_zybo.vhd). The finite canonical edge
vector the GP-R0 edge_scl case judges (STIM_EDGE_CONTROL 0x3, EMIO_MUX_SEL
bit[7]) must reach capture_pin(42) with exactly the levels and
inter-transition deltas of de_c0_stimulus_contract.EDGE_TRANSITIONS, and stop
there. The edge leg does not pass through the DS-P1.90 EMIO mux; this proves
it is unaffected on the Zynq wiring. The absolute offset from the enable
write includes the AXI write latency and is reported, not compared, as the
live judge compares levels and deltas only.

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_zybo_edge_finite
"""
from __future__ import annotations

import cocotb
import cocotb.utils
from cocotb.triggers import ClockCycles, Timer, with_timeout

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    SEAM_RED_TIMEOUT_NS, SEL_BIT_STIM_EDGE, SLOT_EMIO_MUX_SEL,
    SLOT_STIM_EDGE_CONTROL, requires, start,
)
from _zybo_seam import run

# de_c0_stimulus_contract.EDGE_TRANSITIONS, hard-coded (ROUTERTL-002).
EXPECTED_LEVELS = [1, 0, 1, 0]
EXPECTED_DELTAS_NS = [200, 320, 280]
EDGE_FINITE_CONTROL = 0x3


@cocotb.test()
@requires("STIM-SEAM-REQ-7")
async def test_zybo_seam_edge_finite_vector_is_exact(dut):
    axi = await start(dut)
    await axi.write(SLOT_EMIO_MUX_SEL, SEL_BIT_STIM_EDGE.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)
    t_write = int(cocotb.utils.get_sim_time(unit="ns"))
    await axi.write(SLOT_STIM_EDGE_CONTROL, EDGE_FINITE_CONTROL.to_bytes(4, "little"))

    seen = []
    for _ in range(4):
        await with_timeout(dut.PROBE_CAPTURE_EDGE0.value_change, 2 * SEAM_RED_TIMEOUT_NS, "ns")
        seen.append((int(cocotb.utils.get_sim_time(unit="ns")),
                     int(dut.PROBE_CAPTURE_EDGE0.value)))
    levels = [lvl for _, lvl in seen]
    deltas = [b[0] - a[0] for a, b in zip(seen, seen[1:])]
    dut._log.info("edge transitions %s (first at +%d ns from the enable write)",
                  seen, seen[0][0] - t_write)
    assert levels == EXPECTED_LEVELS, f"edge levels {levels}, expected {EXPECTED_LEVELS}"
    assert deltas == EXPECTED_DELTAS_NS, f"edge deltas {deltas} ns, expected {EXPECTED_DELTAS_NS}"

    await Timer(2, unit="us")
    assert int(dut.PROBE_CAPTURE_EDGE0.value) == 0, "finite edge mode emitted a periodic tail"


if __name__ == "__main__":
    run("test_stimulus_core_seam_zybo_edge_finite")
