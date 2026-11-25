# rr unknown-llm (AI-generated)
"""
UART Datalogger Integration Test - 1Mbps 8N1
Verifies full integration: UART input -> Datalogger calibration -> FrameGen -> Reassembled payload.
"""

import sys
import random
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
    DEEPSKOPION_ROOT, DEEPSKOPION_RTL, GENERICS, CUSTOM_LIBRARIES,
    setup_dut, apply_config,
    FastSignalCollector, UartDataloggerMsgReassembler
)
from rr_cocotb_tb.drivers.uart_master import UartSource


# Test Parameters
BAUD_RATE = 1_000_000  # 1Mbps
DATA_BITS = 8
PARITY = "none"
STOP_BITS = 1

CAL_BYTES = 150
PAYLOAD_BYTES = 100


@cocotb.test()
async def test_integration_1M_8N1(dut):
    """
    Integration test at 1Mbps 8N1:
    1. Send calibration data to lock the datalogger.
    2. Send payload data.
    3. Capture and reassemble datalogger output.
    4. Verify payload matches.
    """
    # Setup
    await setup_dut(dut)
    dev_id = GENERICS["G_UART_DEV_ID"]
    await apply_config(dut, dev_id)
    await ClockCycles(dut.CLK, 100)

    dut._log.info(f"Integration Test: {BAUD_RATE}bps, {DATA_BITS}N1")

    # Reassembler and Collector
    reassembler = UartDataloggerMsgReassembler(DATA_BITS, dut._log)
    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT,
                                     value_fn=lambda v: reassembler.push(v))
    collector_coro = cocotb.start_soon(collector.start())

    # UART Source
    uart_source = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=DATA_BITS,
                             stop_bits=STOP_BITS, parity=PARITY)

    # Phase 1: Calibration
    dut._log.info(f"Phase 1: Sending {CAL_BYTES} calibration bytes...")
    cal_data = [random.randint(0, 255) for _ in range(CAL_BYTES)]
    await uart_source.write_bytes(cal_data)

    # Wait for calibration lock using calibrationDone_r (proven approach from smoke test)
    calibration_locked = False
    for _ in range(200):
        if dut.calibrationDone_r.value == 1:
            calibration_locked = True
            dut._log.info("Calibration LOCKED via calibrationDone_r.")
            break
        await ClockCycles(dut.CLK, 100)
    
    if not calibration_locked:
        dut._log.error("Calibration FAILED - calibrationDone_r never went high.")
        dut._log.info(f"Collector captured {collector.count} pulses so far.")
        assert False, "Calibration timeout"

    # Phase 2: Payload
    dut._log.info(f"Phase 2: Sending {PAYLOAD_BYTES} payload bytes...")
    payload_data = [random.randint(0, 255) for _ in range(PAYLOAD_BYTES)]
    await uart_source.write_bytes(payload_data)

    # Flush - wait for buffer timeout (~12ms) to ensure all data is output
    dut._log.info("Waiting for buffer timeout flush...")
    await Timer(15, units="ms")

    # Collect results
    collector_coro.kill()
    results = reassembler.get_results()

    dut._log.info(f"Collector captured {collector.count} total pulses.")
    dut._log.info(f"Reassembled {len(results)} bytes (expected {CAL_BYTES + PAYLOAD_BYTES}).")

    # Verification: Check the last PAYLOAD_BYTES match
    if len(results) >= PAYLOAD_BYTES:
        received_payload = results[-PAYLOAD_BYTES:]
        for i, (exp, got) in enumerate(zip(payload_data, received_payload)):
            assert exp == got, f"Payload mismatch at byte {i}: expected 0x{exp:02X}, got 0x{got:02X}"
        dut._log.info(f"✓ INTEGRATION TEST PASSED: All {PAYLOAD_BYTES} payload bytes verified.")
    else:
        assert False, f"Not enough data captured: {len(results)} < {PAYLOAD_BYTES}"


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_integration_1M_8N1",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
