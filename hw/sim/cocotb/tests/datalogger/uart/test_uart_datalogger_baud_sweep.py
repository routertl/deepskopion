# rr unknown-llm (AI-generated)
"""
UART Datalogger Baud Rate Sweep Test
Focuses on baud rates [115200 : 3M].
Data format fixed at 8N1.
"""

import sys
import os
from pathlib import Path
import cocotb

# Locate routertl SDK cocotb dir via the pip-installed routertl_core
# (DS-P2.29 fallout — old shim walked up the tree for an `engine/`
# folder that no longer exists post-pip-decouple).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    GENERICS, CUSTOM_LIBRARIES, run_parameterized_test
)

@cocotb.test()
@cocotb.parametrize(
    baud=[115200, 460800, 921600, 1000000, 1843200, 3000000],
    bits=[8],
    parity=["none"],
    stop_bits=[1]
)
async def test_capture_baud(dut, baud, bits, parity, stop_bits):
    await run_parameterized_test(dut, baud, bits, parity, stop_bits)

def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_datalogger_baud_sweep",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )

if __name__ == "__main__":
    main()
