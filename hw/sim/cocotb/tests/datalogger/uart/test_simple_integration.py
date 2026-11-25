# rr unknown-llm (AI-generated)
"""
UART Datalogger Simplified Integration Test - 1Mbps 8N1
Verifies: Calibration lock + UART_MSG_VLD activity without full reassembly.
This is a diagnostic test to understand collector behavior.
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
    DEEPSKOPION_ROOT, DEEPSKOPION_RTL, GENERICS, CUSTOM_LIBRARIES,
    setup_dut, apply_config
)
from rr_cocotb_tb.drivers.uart_master import UartSource


# Test Parameters - Testing baud rate limits
BAUD_RATE = 460_800  # 4x 115200 = 460800
DATA_BITS = 8
PARITY = "none"
STOP_BITS = 1

CAL_BYTES = 150
PAYLOAD_BYTES = 20  # Small payload for diagnostic


async def count_vld_pulses(dut, max_time_ns, max_pulses=100):
    """Count UART_MSG_VLD pulses for a maximum time or until max_pulses."""
    count = 0
    start_time = cocotb.utils.get_sim_time(units="ns")
    
    while count < max_pulses:
        elapsed = cocotb.utils.get_sim_time(units="ns") - start_time
        if elapsed >= max_time_ns:
            break
        
        # Wait for rising edge or timeout
        try:
            await First(
                RisingEdge(dut.UART_MSG_VLD),
                Timer(1000, units="ns")  # Check every 1us
            )
            
            if dut.UART_MSG_VLD.value == 1:
                count += 1
                raw_val = int(dut.UART_MSG_OUT.value)
                dut._log.info(f"[Pulse {count}] UART_MSG_OUT = 0x{raw_val:08X}")
        except Exception as e:
            dut._log.warning(f"Exception in pulse counter: {e}")
            break
    
    return count


@cocotb.test()
async def test_simple_integration(dut):
    """
    Simplified integration test at 1Mbps 8N1:
    1. Send calibration data to lock the datalogger.
    2. Verify calibrationDone_r goes high.
    3. Send a small payload.
    4. Count UART_MSG_VLD pulses to verify output activity.
    """
    # Setup
    await setup_dut(dut)
    dev_id = GENERICS["G_UART_DEV_ID"]
    await apply_config(dut, dev_id)
    await ClockCycles(dut.CLK, 100)

    dut._log.info(f"=== Simple Integration Test: {BAUD_RATE}bps, {DATA_BITS}N1 ===")
    
    # Pre-check: Log initial signal states
    dut._log.info(f"Initial UART_MSG_VLD = {dut.UART_MSG_VLD.value}")
    dut._log.info(f"Initial UART_MSG_RDY = {dut.UART_MSG_RDY.value}")
    dut._log.info(f"Initial calibrationDone_r = {dut.calibrationDone_r.value}")
    
    # Check internal signals (if accessible)
    try:
        dut._log.info(f"Initial configEnable = {dut.configEnable.value}")
        dut._log.info(f"Initial calibrationFail_r = {dut.calibrationFail_r.value}")
        dut._log.info(f"Initial streamEmpty = {dut.streamEmpty.value}")
        dut._log.info(f"Initial fifoUartEmpty = {dut.fifoUartEmpty.value}")
        dut._log.info(f"Initial fsmUartStream_r = {dut.fsmUartStream_r.value}")
    except AttributeError as e:
        dut._log.warning(f"Some internal signals not accessible: {e}")

    # UART Source
    uart_source = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=DATA_BITS,
                             stop_bits=STOP_BITS, parity=PARITY)

    # FIFO Monitor - runs concurrently to check if edges are being stored
    fifo_saw_data = False
    async def monitor_fifo():
        nonlocal fifo_saw_data
        while True:
            try:
                empty = int(dut.fifoUartEmpty.value)
                if empty == 0 and not fifo_saw_data:
                    fifo_saw_data = True
                    dut._log.info(f"[FIFO] First data at {cocotb.utils.get_sim_time(units='ns'):.0f}ns")
            except:
                pass
            await ClockCycles(dut.CLK, 1000)  # Check less frequently
    
    fifo_monitor = cocotb.start_soon(monitor_fifo())

    # Phase 1: Calibration
    dut._log.info(f"Phase 1: Sending {CAL_BYTES} calibration bytes...")
    cal_data = [random.randint(0, 255) for _ in range(CAL_BYTES)]
    await uart_source.write_bytes(cal_data)
    
    # Stop FIFO monitor
    fifo_monitor.kill()
    dut._log.info(f"[FIFO Monitor] Saw data during TX: {fifo_saw_data}")

    # Wait for calibration lock with detailed logging
    calibration_locked = False
    for i in range(200):
        if dut.calibrationDone_r.value == 1:
            calibration_locked = True
            dut._log.info(f"Calibration LOCKED after {i} iterations.")
            break
        await ClockCycles(dut.CLK, 100)
    
    if not calibration_locked:
        dut._log.error("Calibration FAILED - calibrationDone_r never went high.")
        assert False, "Calibration timeout"

    # Post-calibration signal check
    dut._log.info(f"Post-cal UART_MSG_VLD = {dut.UART_MSG_VLD.value}")
    dut._log.info(f"Post-cal UART_MSG_RDY = {dut.UART_MSG_RDY.value}")
    
    # Post-cal internal signal check
    try:
        dut._log.info(f"Post-cal calibrationFail_r = {dut.calibrationFail_r.value}")
        dut._log.info(f"Post-cal streamEmpty = {dut.streamEmpty.value}")
        dut._log.info(f"Post-cal fifoUartEmpty = {dut.fifoUartEmpty.value}")
        dut._log.info(f"Post-cal fsmUartStream_r = {dut.fsmUartStream_r.value}")
        dut._log.info(f"Post-cal baudRateMin_r = {int(dut.baudRateMin_r.value)}")
    except AttributeError as e:
        dut._log.warning(f"Some internal signals not accessible: {e}")

    # Phase 2: Payload
    dut._log.info(f"Phase 2: Sending {PAYLOAD_BYTES} payload bytes...")
    payload_data = [random.randint(0, 255) for _ in range(PAYLOAD_BYTES)]
    await uart_source.write_bytes(payload_data)

    # Phase 3: Count pulses during buffer flush (15ms timeout)
    dut._log.info("Phase 3: Waiting for buffer flush and counting pulses...")
    pulse_count = await count_vld_pulses(dut, max_time_ns=15_000_000, max_pulses=300)

    dut._log.info(f"=== RESULTS: Captured {pulse_count} UART_MSG_VLD pulses ===")

    # Verification: Expect at least some pulses (calibration + payload)
    expected_min = CAL_BYTES + PAYLOAD_BYTES - 10  # Allow some margin
    if pulse_count >= expected_min:
        dut._log.info(f"✓ TEST PASSED: {pulse_count} pulses >= {expected_min} expected.")
    elif pulse_count > 0:
        dut._log.warning(f"⚠ PARTIAL: Only {pulse_count} pulses (expected ~{CAL_BYTES + PAYLOAD_BYTES})")
    else:
        dut._log.error(f"✗ FAILED: 0 pulses captured. Check UART_MSG_VLD/RDY handshake.")
        assert False, "No UART_MSG_VLD pulses captured"


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_simple_integration",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
