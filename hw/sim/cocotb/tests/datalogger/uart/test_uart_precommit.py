# rr unknown-llm (AI-generated)
"""
UART Datalogger Pre-Commit Regression
Expanded regression at 460.8K baud covering all word lengths (5-9)
with parity variants and 2-stop-bit anchors — 12 configs in ~4 min.
"""

import sys
import os
import random
from pathlib import Path

import cocotb
from cocotb.triggers import Timer, ClockCycles

# Set specific wave directory relative to this test
test_dir = Path(__file__).resolve().parent
waves_dir_target = test_dir.parents[3] / "waves"
os.environ["ROUTERTL_WAVES_DIR"] = str(waves_dir_target)

# Locate routertl SDK cocotb dir via the pip-installed routertl_core
# (DS-P2.29 fallout — old shim walked up the tree for an `engine/`
# folder that no longer exists post-pip-decouple).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from engine.simulation import run_simulation
from sdk.cocotb_helpers import requires
from uart_datalogger_test_utils import (
    setup_dut, apply_config, GENERICS, CUSTOM_LIBRARIES, UartSource, FastSignalCollector
)

# Test Parameters
BAUD_RATE = 460_800
CAL_BYTES = 100
PAYLOAD_LEN = 16

@cocotb.test()
@cocotb.parametrize(
    config=[
        {"bits": 5, "parity": "none", "stop": 1},
        {"bits": 5, "parity": "even", "stop": 1},
        {"bits": 6, "parity": "none", "stop": 1},
        {"bits": 6, "parity": "odd",  "stop": 1},
        {"bits": 7, "parity": "none", "stop": 1},
        {"bits": 7, "parity": "even", "stop": 1},
        {"bits": 8, "parity": "none", "stop": 1},
        {"bits": 8, "parity": "odd",  "stop": 1},
        {"bits": 8, "parity": "even", "stop": 1},
        {"bits": 9, "parity": "none", "stop": 1},
        {"bits": 7, "parity": "none", "stop": 2},
        {"bits": 8, "parity": "none", "stop": 2},
    ]
)
@requires(
    "UART-REQ-1",   # baud detection (calibrationDone asserts per config)
    "UART-REQ-2",   # data-bit width 5..9 (this matrix covers all 5)
    "UART-REQ-3",   # parity none/even/odd (this matrix covers all 3)
    "UART-REQ-4",   # stop bits 1/2 (this matrix covers both)
    "UART-REQ-10",  # PAYLOAD emission + match (captured == sent)
    "UART-REQ-11",  # PAYLOAD_9BIT for 9-bit data with bit8=1
    "UART-REQ-12",  # data field carries decoded byte (subsequence match)
)
async def run_regression_test(dut, config):
    data_bits = config["bits"]
    parity = config["parity"]
    stop_bits = config["stop"]
    
    test_name = f"regr_{data_bits}bits_{parity}"
    dut._log.info(f"=== STARTING REGRESSION TEST: {test_name} ===")
    
    # 1. Setup
    await setup_dut(dut)
    dev_id = GENERICS["G_UART_DEV_ID"]
    await apply_config(dut, dev_id)
    await ClockCycles(dut.CLK, 100)
    
    # 2. Driver Setup
    uart_source = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=data_bits,
                             stop_bits=stop_bits, parity=parity)

    # 3. Monitor Setup
    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())
    
    # 4. Phase 1: Calibration
    mask = (1 << data_bits) - 1
    # Optimization: 100 bytes is sufficient for both 8-bit and 9-bit detection at this baud/clock ratio
    num_cal_bytes = CAL_BYTES
    
    # Use fixed seed for regression stability
    random.seed(0xCAFE) 
    cal_data = [random.randint(0, mask) for _ in range(num_cal_bytes)]
    await uart_source.write_bytes(cal_data)
    
    # Wait for calibration
    for _ in range(200):
        if dut.calibrationDone_r.value == 1:
            break
        await ClockCycles(dut.CLK, 100)
        
    if dut.calibrationDone_r.value != 1:
        assert False, f"Calibration FAILED for {test_name}"
    
    # 5. Phase 2: Payload Verification
    payload = [random.randint(0, mask) for _ in range(PAYLOAD_LEN)]
    await uart_source.write_bytes(payload)
    
    # Wait for transmission
    frame_bits = 1 + data_bits + (1 if parity != "none" else 0) + stop_bits
    bit_time_ns = 1e9 / BAUD_RATE
    total_wait_ns = bit_time_ns * frame_bits * (len(payload) + 10)
    await Timer(int(total_wait_ns), units="ns")
    await Timer(200_000, units="ns") 
    
    # 6. Check Results
    captured_payloads = []
    for item in collector._buf:
        val = item.value
        msg_type = (val >> 24) & 0x7
        data_byte = val & 0xFF
        if msg_type == 2:
            captured_payloads.append(data_byte)
        elif msg_type == 3:
            captured_payloads.append(data_byte | 0x100)
            
    # Search for subsequence
    match_found = False
    seq_len = len(payload)
    if len(captured_payloads) >= seq_len:
        for i in range(len(captured_payloads) - seq_len + 1):
            if captured_payloads[i : i + seq_len] == payload:
                match_found = True
                break
                
    if match_found:
        dut._log.info(f"✓ {test_name} PASSED")
    else:
        dut._log.error(f"✗ {test_name} FAILED. Expected: {payload}")
        assert False, f"Payload verification failed for {test_name}"

def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_precommit",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
        verbose=True,
    )

if __name__ == "__main__":
    main()
