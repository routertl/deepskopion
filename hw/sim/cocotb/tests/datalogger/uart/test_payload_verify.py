# rr unknown-llm (AI-generated)
"""
UART Datalogger Payload Verification Sweep
Verifies correct payload capture for all UART configurations at 460.8k baud.
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

# Fix seed for reproducibility
random.seed(100)

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
    setup_dut, apply_config, GENERICS, CUSTOM_LIBRARIES, UartSource, FastSignalCollector
)

# Test Parameters
BAUD_RATE = 460_800
CAL_BYTES = 500
PAYLOAD_LEN = 8

@cocotb.test()
@cocotb.parametrize(
    data_bits=[5, 6, 7, 8, 9],
    parity=["none", "odd", "even"],
    stop_bits=[1]
)
async def run_payload_test(dut, data_bits, parity, stop_bits):
    """
    Parameterized test function for UART Payload Verification.
    """
    # Skip unsupported combinations
    if data_bits == 9 and parity != "none":
        dut._log.info(f"SKIPPING: 9-bit mode + Parity is not supported/verified.")
        return

    test_name = f"verify_{data_bits}bits_{parity}_{stop_bits}stop"
    dut._log.info(f"=== STARTING TEST: {test_name} (Baud={BAUD_RATE}) ===")
    
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
    
    # 4. Phase 1: Calibration (Random Data)
    # Ensure random data fits within data_bits width
    mask = (1 << data_bits) - 1
    dut._log.info(f"Phase 1: Sending {CAL_BYTES} calibration bytes...")
    cal_data = [random.randint(0, mask) for _ in range(CAL_BYTES)]
    await uart_source.write_bytes(cal_data)
    
    # Wait for calibration lock
    for _ in range(200):
        if dut.calibrationDone_r.value == 1:
            break
        await ClockCycles(dut.CLK, 100)
    
    # Check for calibration failure
    if dut.calibrationDone_r.value != 1:
        dut._log.error(f"Calibration Timeout! FailReg={dut.calibrationFail_r.value}")
        if int(dut.calibrationFail_r.value) == 1:
            assert False, "Calibration FAILED (Fail asserted)"
    
    # 5. Phase 2: Payload Generation
    # Generate random payload for this run
    payload = [random.randint(0, mask) for _ in range(PAYLOAD_LEN)]
    dut._log.info(f"Phase 2: Sending payload: {[hex(x) for x in payload]}")
    await uart_source.write_bytes(payload)
    
    # 6. Wait for Transmission
    frame_bits = 1 + data_bits + (1 if parity != "none" else 0) + stop_bits
    bit_time_ns = 1e9 / BAUD_RATE
    total_wait_ns = bit_time_ns * frame_bits * (len(payload) + 10) # +10 buffer
    
    await Timer(int(total_wait_ns), units="ns")
    await Timer(500_000, units="ns") # Fixed buffer
    
    # 7. Analysis & Verification
    dut._log.info(f"=== RESULTS: {len(collector._buf)} messages captured ===")
    
    captured_payloads = []
    
    for item in collector._buf:
        val = item.value
        # Decode: [31:27 ID] [26:24 Type] [23:8 Timestamp] [7:0 Data]
        # Type 2 = Payload (8-bit or 9-bit MSB=0)
        # Type 3 = Payload (9-bit MSB=1)
        msg_type = (val >> 24) & 0x7
        data_byte = val & 0xFF
        
        if msg_type == 2:
            captured_payloads.append(data_byte)
        elif msg_type == 3:
            # 9-bit mode with MSB=1
            captured_payloads.append(data_byte | 0x100)
            
    # Search for payload in captured stream
    match_index = -1
    seq_len = len(payload)
    
    # Sliding window search
    if len(captured_payloads) >= seq_len:
        for i in range(len(captured_payloads) - seq_len + 1):
            chunk = captured_payloads[i : i + seq_len]
            if chunk == payload:
                match_index = i
                break
                
    if match_index >= 0:
        dut._log.info(f"✓ TEST PASSED: Payload found at index {match_index}")
    else:
        dut._log.error(f"✗ TEST FAILED: Payload not found.")
        dut._log.info(f"  Expected: {[hex(x) for x in payload]}")
        tail = captured_payloads[-16:] if len(captured_payloads) > 16 else captured_payloads
        dut._log.info(f"  Captured Tail: {[hex(x) for x in tail]}")
        assert False, f"Payload verify failed for Config: {data_bits} bits, {parity}"


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_payload_verify",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )

if __name__ == "__main__":
    main()
