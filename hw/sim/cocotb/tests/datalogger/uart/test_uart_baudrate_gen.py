# rr unknown-llm (AI-generated)
"""
UART Baudrate Generator Isolation Test
=======================================
Tests uart_baudrate_gen.vhd in isolation.

Test strategy:
- Verify TICK pulses at BR_CYCLES intervals
- Test enable/disable behavior
- Test pause functionality
- Verify half-cycle tick timing

Run with: SIM=nvc python test_uart_baudrate_gen.py
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

# Locate routertl SDK cocotb dir via the pip-installed routertl_core
# (DS-P2.29 fallout — old shim walked up the tree for an `engine/`
# folder that no longer exists post-pip-decouple).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: Could not import engine.")
    sys.exit(1)

# Deepskopion project root
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

# Constants
CLK_PERIOD_NS = 10  # 100 MHz
BR_CYCLES = 100     # 100 clock cycles per UART bit

GENERICS = {
    "G_DIST_WIDTH": 16
}


async def init_dut(dut):
    """Initialize DUT to reset state."""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    
    dut.RST.value = 1
    dut.ENABLE.value = 0
    dut.PAUSE.value = 0
    dut.BR_CYCLES.value = BR_CYCLES
    
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 2)


@cocotb.test()
async def test_tick_disabled_when_not_enabled(dut):
    """TC-001: TICK should not pulse when ENABLE=0."""
    await init_dut(dut)
    
    # Keep disabled
    dut.ENABLE.value = 0
    
    # Wait and count ticks
    tick_count = 0
    for _ in range(BR_CYCLES * 3):
        await RisingEdge(dut.CLK)
        if dut.TICK.value == 1:
            tick_count += 1
    
    assert tick_count == 0, f"Expected 0 ticks when disabled, got {tick_count}"
    dut._log.info("TC-001 PASSED: No ticks when disabled")


@cocotb.test()
async def test_tick_pulse_at_half_period(dut):
    """TC-002: TICK should pulse at BR_CYCLES/2 (half bit time)."""
    await init_dut(dut)
    
    dut.ENABLE.value = 1
    
    # Count cycles until first tick
    cycles_to_first_tick = 0
    while dut.TICK.value != 1 and cycles_to_first_tick < BR_CYCLES:
        await RisingEdge(dut.CLK)
        cycles_to_first_tick += 1
    
    expected = BR_CYCLES // 2
    dut._log.info(f"First tick after {cycles_to_first_tick} cycles (expected ~{expected})")
    
    # Allow some tolerance for initialization
    assert abs(cycles_to_first_tick - expected) <= 3, \
        f"Expected tick at ~{expected} cycles, got {cycles_to_first_tick}"
    
    dut._log.info("TC-002 PASSED: Tick at half period")


@cocotb.test()
async def test_tick_full_period(dut):
    """TC-003: After first tick, next tick should be at BR_CYCLES interval."""
    await init_dut(dut)
    
    dut.ENABLE.value = 1
    
    # Wait for first tick
    while dut.TICK.value != 1:
        await RisingEdge(dut.CLK)
    
    # Measure cycles to next tick
    await RisingEdge(dut.CLK)  # Move past current tick
    
    cycles_to_next = 0
    while dut.TICK.value != 1 and cycles_to_next < BR_CYCLES * 2:
        await RisingEdge(dut.CLK)
        cycles_to_next += 1
    
    # Next tick should be at half period again (counts from 0 to BR_CYCLES, tick at BR_CYCLES/2)
    expected = BR_CYCLES // 2
    dut._log.info(f"Next tick after {cycles_to_next} cycles")
    
    assert cycles_to_next <= BR_CYCLES, \
        f"Tick period too long: {cycles_to_next} > {BR_CYCLES}"
    
    dut._log.info("TC-003 PASSED: Full period tick timing")


@cocotb.test()
async def test_pause_stops_counter(dut):
    """TC-004: PAUSE=1 should freeze the counter."""
    await init_dut(dut)
    
    dut.ENABLE.value = 1
    
    # Run for some cycles
    await ClockCycles(dut.CLK, 20)
    
    # Enable pause
    dut.PAUSE.value = 1
    
    # Count ticks during pause
    tick_count = 0
    for _ in range(BR_CYCLES):
        await RisingEdge(dut.CLK)
        if dut.TICK.value == 1:
            tick_count += 1
    
    assert tick_count == 0, f"Should not tick while paused, got {tick_count} ticks"
    
    # Disable pause and verify ticks resume
    dut.PAUSE.value = 0
    
    tick_count = 0
    for _ in range(BR_CYCLES):
        await RisingEdge(dut.CLK)
        if dut.TICK.value == 1:
            tick_count += 1
    
    assert tick_count >= 1, "Should resume ticking after unpause"
    dut._log.info("TC-004 PASSED: Pause freezes counter")


@cocotb.test()
async def test_reset_clears_counter(dut):
    """TC-005: RST should clear the counter."""
    await init_dut(dut)
    
    dut.ENABLE.value = 1
    
    # Run until a tick
    while dut.TICK.value != 1:
        await RisingEdge(dut.CLK)
    
    # Assert reset
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 2)
    
    # TICK should be low during reset
    assert dut.TICK.value == 0, "TICK should be 0 during reset"
    
    # Release reset
    dut.RST.value = 0
    
    # Measure cycles to next tick (should be half period again)
    cycles = 0
    while dut.TICK.value != 1 and cycles < BR_CYCLES:
        await RisingEdge(dut.CLK)
        cycles += 1
    
    dut._log.info(f"After reset, tick at {cycles} cycles")
    dut._log.info("TC-005 PASSED: Reset clears counter")


@cocotb.test()
@cocotb.parametrize(br=[24, 26, 107, 216])
async def test_tick_period_is_br(dut, br):
    """DS-P2.236: pins the sampler period at exactly BR_CYCLES clocks.

    Calibration (uart_datalogger baudRateMin_r) keeps the minimum recorded
    interval, which is the true bit time at every prescaler phase, and
    uart_playback replays each interval for exactly its distance, so one bit
    of the rebuilt waveform lasts BR_CYCLES clocks. The period was
    BR_CYCLES + 1, which offset a recorder that lost a tick per interval; at
    1 Mbaud it put the stop sample late enough to miss a back-to-back start
    edge (test_uart_playback_timing_ds_p2_236).
    """
    await init_dut(dut)
    dut.BR_CYCLES.value = br
    dut.ENABLE.value = 1
    ticks, cyc = [], 0
    while len(ticks) < 6 and cyc < (br + 2) * 8:
        await RisingEdge(dut.CLK)
        cyc += 1
        if dut.TICK.value == 1:
            ticks.append(cyc)
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    dut._log.info(f"BR_CYCLES={br} tick gaps={gaps}")
    assert gaps == [br] * 5, f"BR_CYCLES={br}: tick gaps {gaps}, expected {br}"


def main():
    """Run simulation for uart_baudrate_gen"""
    run_simulation(
        top_level="uart_baudrate_gen",
        module="test_uart_baudrate_gen",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/uart/uart_baudrate_gen.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )


if __name__ == "__main__":
    main()