# rr unknown-llm (AI-generated)
"""
UART Datalogger Baud Rate Sweep Test
Finds the working baud rate upper limit.
"""

import sys
import random
import os
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, ClockCycles

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

# Get baud rate from environment, default to 115200
BAUD_RATE = int(os.environ.get("TEST_BAUD", "115200"))

@cocotb.test()
async def test_baud_calibration(dut):
    """Test calibration at specified baud rate."""
    baud = BAUD_RATE
    bits = 8
    parity = "none"
    stop_bits = 1
    
    await setup_dut(dut)
    dev_id = GENERICS["G_UART_DEV_ID"]
    await apply_config(dut, dev_id)
    await ClockCycles(dut.CLK, 100)
    
    dut._log.info(f"=== Testing Baud: {baud} ===")
    
    uart_source = UartSource(dut.UART_IN, baud=baud, bits=bits, 
                             stop_bits=stop_bits, parity=parity)
    
    # Send calibration traffic
    cal_data = [random.randint(0, 255) for _ in range(150)]
    await uart_source.write_bytes(cal_data)
    
    # Wait for lock
    for i in range(200):
        if dut.calibrationDone_r.value == 1:
            break
        await ClockCycles(dut.CLK, 100)
    
    cal_done = int(dut.calibrationDone_r.value) == 1
    cal_fail = int(dut.calibrationFail_r.value) == 1
    
    dut._log.info(f"Baud {baud}: calibrationDone={cal_done}, calibrationFail={cal_fail}")
    
    if cal_done and not cal_fail:
        dut._log.info(f"✓ BAUD {baud}: PASS")
    else:
        dut._log.error(f"✗ BAUD {baud}: FAIL")
        assert False, f"Calibration failed at {baud} baud"


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_baud_sweep",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,  # Faster without waves
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
