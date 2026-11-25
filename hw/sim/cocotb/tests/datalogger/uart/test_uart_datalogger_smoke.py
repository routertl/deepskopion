# rr unknown-llm (AI-generated)
"""
UART Datalogger Smoke Test
Single fast configuration to verify basic functionality.
"""

import sys
import os
from pathlib import Path
import cocotb
from cocotb.triggers import Timer, ClockCycles

# Locate routertl SDK cocotb dir via the pip-installed routertl_core
# (DS-P2.29 fallout — old shim walked up the tree for an `engine/`
# folder that no longer exists post-pip-decouple).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from engine.simulation import run_simulation
from sdk.cocotb_helpers import requires
from uart_datalogger_test_utils import (
    setup_dut, apply_config, G_SYSTEM_CLK_FREQ, GENERICS,
    CUSTOM_LIBRARIES, UartSource
)

@cocotb.test()
@requires("UART-REQ-1", "UART-REQ-10")
async def test_smoke(dut):
    """Basic smoke test: 115200, 8N1.

    Tags:
      UART-REQ-1  — baud detection (asserted via dut.calibrationDone_r==1)
      UART-REQ-10 — PAYLOAD emission for 8-bit data (test data captured
                    end-to-end, though the smoke test currently only
                    asserts calibration; full payload-match coverage
                    lives in test_uart_precommit).
    """
    baud = 115200
    bits = 8
    parity = "none"
    stop_bits = 1
    
    await setup_dut(dut)
    dev_id = GENERICS["G_UART_DEV_ID"]
    await apply_config(dut, dev_id)
    await ClockCycles(dut.CLK, 100)
    
    dut._log.info(f"Smoke Test: Baud={baud}, Bits={bits}, Parity={parity}")
    
    uart_source = UartSource(dut.UART_IN, baud=baud, bits=bits, stop_bits=stop_bits, parity=parity)
    
    # Send calibration traffic (RANDOMIZED data is essential for lock)
    import random
    max_val = (1 << bits) - 1
    cal_data = [random.randint(0, max_val) for _ in range(150)]
    await uart_source.write_bytes(cal_data)
    
    # Wait for lock
    for _ in range(200):
        if dut.calibrationDone_r.value == 1:
            dut._log.info("Calibration Done!")
            break
        await ClockCycles(dut.CLK, 100)
        
    assert dut.calibrationDone_r.value == 1, "Calibration timeout"
    
    # Send test data
    test_data = [0xA5, 0x5A, 0x12, 0x34]
    await uart_source.write_bytes(test_data)
    
    # Wait for processing
    await Timer(100, units="us")
    dut._log.info("Smoke Test Completed Successfully")

def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_datalogger_smoke",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )

if __name__ == "__main__":
    main()
