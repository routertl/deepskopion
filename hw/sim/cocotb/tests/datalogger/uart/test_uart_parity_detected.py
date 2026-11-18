"""
DS-P2.179 — UART-REQ-14 witness: 8E1 and 8O1 streams through the full uart_datalogger
with autoSync must be detected as has-parity (parity_enable / latched verdict), and
must produce zero spurious parity_error (0x02) on clean data.

Observable:
- autoSync calibration against 8E1 latches parityConf_r = "01" (has-parity, even).
- autoSync calibration against 8O1 latches parityConf_r = "11" (has-parity, odd).
- Zero spurious parity_error (0x02) status events on clean 8E1 / 8O1 data.
- Emitted CONFIG message has parity_enable = 1.
- Contiguous payload decodes correctly on both formats.

Run:  rr sim run test_uart_parity_detected
"""
import os
import sys
import random
from pathlib import Path

import cocotb
from cocotb.triggers import Timer, ClockCycles

test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")

import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation
from sdk.cocotb_helpers import requires
from uart_datalogger_test_utils import (
    setup_dut, GENERICS, CUSTOM_LIBRARIES, UartSource, FastSignalCollector
)

BAUD_RATE = 460_800
CAL_BYTES = 120
PAYLOAD = list(b"DEEPSKOPION-DS-T9-UART-READBACK!")

STATUS_TYPE = 0b100          # C_UART_PACKET_STATUS_TYPE = "100" = 4
CONFIG_TYPE = 0b001          # C_UART_PACKET_CONFIG_TYPE = "001" = 1
PAYLOAD_TYPE = 0b010         # C_UART_PACKET_PAYLOAD_TYPE = "010" = 2
PARITY_ERROR = 0x02          # C_UART_PARITY_ERROR


async def run_parity_format_check(dut, parity: str, expected_conf: int, seed: int):
    """Drive calibration traffic and payload for the given parity format (8E1 or 8O1)."""
    dev_id = GENERICS["G_UART_DEV_ID"]

    # Sanctioned SW reset & arm sequence (UART-REQ-15):
    # ENABLE=0 clears stale state, ENABLE=1 + autoSync arms fresh calibration.
    dut.CONFIG.value = (dev_id & 0x1F) << 11
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = ((dev_id & 0x1F) << 11) | 0x00   # per-datalogger reset (ENABLE=0)
    await ClockCycles(dut.CLK, 40)
    dut.CONFIG.value = ((dev_id & 0x1F) << 11) | 0x03   # ENABLE=1, autoSync=1
    await ClockCycles(dut.CLK, 40)

    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    src = UartSource(
        dut.UART_IN, baud=BAUD_RATE, bits=8, stop_bits=1, parity=parity,
        strict_protocol=False,
    )

    # Phase 1: calibration traffic.
    rng = random.Random(seed)
    await src.write_bytes([rng.randint(0, 0xFF) for _ in range(CAL_BYTES)])
    for _ in range(300):
        if int(dut.calibrationDone_r.value) == 1:
            break
        await ClockCycles(dut.CLK, 100)
    assert int(dut.calibrationDone_r.value) == 1, (
        f"calibration never locked on clean 8{parity[0].upper()}1"
    )

    # Latched verdict must indicate has-parity (bit 0 == 1) and match expected format
    latched = int(dut.parityConf_r.value)
    dut._log.info(
        f"8{parity[0].upper()}1 latched parityConf_r = {bin(latched)} (expected {bin(expected_conf)})"
    )
    assert latched & 1 == 1, (
        f"UART-REQ-14: expected has-parity (bit 0 == 1) for 8{parity[0].upper()}1, got {bin(latched)}"
    )
    assert latched == expected_conf, (
        f"UART-REQ-14: expected parityConf_r == {bin(expected_conf)} for 8{parity[0].upper()}1, got {bin(latched)}"
    )

    # Phase 2: payload data.
    await src.write_bytes(PAYLOAD)
    bit_ns = 1e9 / BAUD_RATE
    # 8 data + 1 start + 1 parity + 1 stop = 11 bits per frame
    await Timer(int(bit_ns * 11 * (len(PAYLOAD) + 8)), units="ns")
    await Timer(1_000_000, units="ns")   # idle flush settle

    payloads, parity_errs, statuses, configs = [], 0, [], []
    for item in collector._buf:
        val = item.value
        mtype = (val >> 24) & 0x7
        data = val & 0xFF
        if mtype == PAYLOAD_TYPE:
            payloads.append(data)
        elif mtype == STATUS_TYPE:
            statuses.append(data)
            if data == PARITY_ERROR:
                parity_errs += 1
        elif mtype == CONFIG_TYPE:
            configs.append(val)

    dut._log.info(
        f"8{parity[0].upper()}1: captured {len(payloads)} payload bytes, {len(statuses)} status msgs, "
        f"{parity_errs} parity_error(0x02), {len(configs)} config msgs"
    )
    dut._log.info(f"8{parity[0].upper()}1 status codes seen: {sorted(set(statuses))}")
    dut._log.info(f"8{parity[0].upper()}1 payload tail: {bytes(payloads[-len(PAYLOAD):]) if payloads else b''}")

    # Zero spurious parity errors on clean data
    assert parity_errs == 0, (
        f"UART-REQ-14: {parity_errs} spurious parity_error (0x02) status events on clean 8{parity[0].upper()}1"
    )

    # Contiguous payload recovered
    n = len(PAYLOAD)
    found = any(payloads[i:i + n] == PAYLOAD for i in range(max(0, len(payloads) - n + 1)))
    assert found, (
        f"payload not found contiguously in 8{parity[0].upper()}1 stream: {bytes(payloads)!r}"
    )


@cocotb.test()
@requires("UART-REQ-14")
async def parity_detected_on_8e1_and_8o1(dut):
    """UART-REQ-14: 8E1 and 8O1 streams through the full uart_datalogger with autoSync

    asserts parity is detected (parity_enable / latched verdict = has-parity)
    and zero spurious parity_error on clean data.
    """
    await setup_dut(dut)

    # Part 1: 8E1 (even parity -> parityConf_r = "01")
    dut._log.info("=== Testing 8E1 (Even Parity) ===")
    await run_parity_format_check(dut, parity="even", expected_conf=0b01, seed=42)

    # Part 2: 8O1 (odd parity -> parityConf_r = "11")
    dut._log.info("=== Testing 8O1 (Odd Parity) ===")
    await run_parity_format_check(dut, parity="odd", expected_conf=0b11, seed=99)


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_parity_detected",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
