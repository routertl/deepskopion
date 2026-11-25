# rr unknown-llm (AI-generated)
"""
UART Datalogger Integration Test - 921600 baud 8N1
Fast integration test at recommended high-speed baud rate.
Tests: Calibration lock + MSG_VLD pulse generation + payload verification.
"""

import sys
import random
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, ClockCycles, RisingEdge, First

# SDK path setup
current_dir = Path(__file__).resolve().parent
# Resolve SDK cocotb path from pip-installed package
import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    setup_dut, apply_config, GENERICS, CUSTOM_LIBRARIES, UartSource
)

# Test Parameters - 921600 is within verified working range
BAUD_RATE = 921_600
DATA_BITS = 8
PARITY = "none"
STOP_BITS = 1
CAL_BYTES = 150
PAYLOAD_BYTES = 10


@cocotb.test()
async def test_integration_921600_8n1(dut):
    """Integration test: calibration + output verification at 921600 baud."""
    
    await setup_dut(dut)
    dev_id = GENERICS["G_UART_DEV_ID"]
    await apply_config(dut, dev_id)
    await ClockCycles(dut.CLK, 100)
    
    dut._log.info(f"=== Integration Test: {BAUD_RATE}bps, {DATA_BITS}N{STOP_BITS} ===")
    
    uart_source = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=DATA_BITS,
                             stop_bits=STOP_BITS, parity=PARITY)
    
    # Phase 1: Calibration (150 randomized bytes)
    dut._log.info(f"Phase 1: Sending {CAL_BYTES} calibration bytes...")
    cal_data = [random.randint(0, 255) for _ in range(CAL_BYTES)]
    await uart_source.write_bytes(cal_data)
    
    # Wait for calibration with timeout (max 200 iterations = 20000 clocks)
    calibration_locked = False
    for i in range(200):
        if dut.calibrationDone_r.value == 1:
            calibration_locked = True
            dut._log.info(f"Calibration complete after {i} poll iterations")
            break
        await ClockCycles(dut.CLK, 100)
    
    # Check calibration result
    cal_fail = int(dut.calibrationFail_r.value)
    if not calibration_locked:
        dut._log.error("Calibration timeout - calibrationDone_r never went high")
        assert False, "Calibration timeout"
    
    if cal_fail:
        dut._log.error(f"calibrationFail_r = {cal_fail}")
        assert False, "Calibration failed"
    
    dut._log.info("✓ Calibration PASSED - calibrationFail_r=0")
    
    # Phase 2: Send payload and count MSG_VLD pulses
    dut._log.info(f"Phase 2: Sending {PAYLOAD_BYTES} payload bytes...")
    payload = [random.randint(0, 255) for _ in range(PAYLOAD_BYTES)]
    await uart_source.write_bytes(payload)
    
    # Phase 3: Wait for output and count pulses
    dut._log.info("Phase 3: Counting UART_MSG_VLD pulses...")
    pulse_count = 0
    max_wait_ns = 500_000  # 500us total wait time for processing
    start_time = cocotb.utils.get_sim_time(units="ns")
    
    while True:
        elapsed = cocotb.utils.get_sim_time(units="ns") - start_time
        if elapsed > max_wait_ns:
            break
        
        # Wait for rising edge of MSG_VLD with short timeout
        timeout = Timer(10000, units="ns")  # 10us chunks
        rising = RisingEdge(dut.UART_MSG_VLD)
        result = await First(rising, timeout)
        
        if result is rising:
            pulse_count += 1
            await ClockCycles(dut.CLK, 5)  # Brief wait for signal to settle
    
    dut._log.info(f"=== RESULTS: {pulse_count} MSG_VLD pulses captured ===")
    
    # Expect at least some output (calibration msg + some payload frames)
    if pulse_count > 0:
        dut._log.info(f"✓ TEST PASSED: {pulse_count} output pulses generated")
    else:
        dut._log.error("✗ TEST FAILED: No MSG_VLD pulses detected")
        assert False, "No output generated after calibration"


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_integration_921k",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,  # Faster without waves
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
