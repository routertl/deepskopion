"""
DS-T3.5 reproduction — uart_datalogger must NOT emit spurious parity_error (0x02)
status events on a clean 8N1 (no-parity) stream.

Confirmed on silicon (Zybo Z7-20, run_self_test REQ-5): driving the EMIO UART0
master with "DEEPSKOPION-DS-T9-UART-READBACK!" on a clean 8N1 link, the UART
datalogger captured every payload char IN ORDER but interleaved spurious 0x02
(C_UART_PARITY_ERROR, status type "100"=4) status messages between ~half the
bytes — breaking the contiguous-payload read-back. The DATA is bit-aligned
correct (no parity bit is consumed), so this is a false parity_error, not a
mis-decode.

This reproduces it in sim against the auto-calibration path (autoSync=1, the
REQ-5 config): send calibration traffic + the exact REQ-5 payload on 8N1, then
assert ZERO parity_error (0x02) status messages were emitted, AND the payload
decodes contiguously.

Message format: [31:27 ID][26:24 Type][23:8 TS][7:0 Data]. Type 2 = payload,
Type 4 = status; a parity error is type 4 with data == 0x02.

Run:  rr sim run test_uart_no_spurious_parity   (rm -rf hw/sim/work first)
"""
import sys
import os
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
    setup_dut, apply_config, GENERICS, CUSTOM_LIBRARIES, UartSource, FastSignalCollector
)

BAUD_RATE = 460_800
CAL_BYTES = 200
# The exact REQ-5 payload (DS-T9 UART read-back self-test).
PAYLOAD = list(b"DEEPSKOPION-DS-T9-UART-READBACK!")

STATUS_TYPE = 0b100          # C_UART_PACKET_STATUS_TYPE = "100" = 4
PARITY_ERROR = 0x02          # C_UART_PARITY_ERROR


# Full datalogger witness for UART-REQ-14 (8N1 half): asserts zero status frames
# of any code, zero parity_error, contiguous payload, and that the latched verdict
# stayed 'no parity' (parityConf_r bit 0 == 0).
@cocotb.test()
@requires("UART-REQ-14")
async def no_spurious_parity_on_clean_8n1(dut):
    import random
    random.seed(7)

    await setup_dut(dut)
    await apply_config(dut, GENERICS["G_UART_DEV_ID"])   # enable + autoSync=1
    await ClockCycles(dut.CLK, 100)

    collector = FastSignalCollector(dut.CLK, dut.UART_MSG_VLD, dut.UART_MSG_OUT)
    cocotb.start_soon(collector.start())

    src = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=8, stop_bits=1, parity="none")

    # Phase 1: calibration traffic (clean 8N1).
    await src.write_bytes([random.randint(0, 0xFF) for _ in range(CAL_BYTES)])
    for _ in range(200):
        if int(dut.calibrationDone_r.value) == 1:
            break
        await ClockCycles(dut.CLK, 100)
    assert int(dut.calibrationDone_r.value) == 1, "calibration never locked on clean 8N1"

    # Phase 2: the exact REQ-5 payload.
    await src.write_bytes(PAYLOAD)
    bit_ns = 1e9 / BAUD_RATE
    await Timer(int(bit_ns * 10 * (len(PAYLOAD) + 8)), units="ns")
    await Timer(1_000_000, units="ns")   # idle flush settle

    payloads, parity_errs, statuses = [], 0, []
    for item in collector._buf:
        val = item.value
        mtype = (val >> 24) & 0x7
        data = val & 0xFF
        if mtype == 0b010:                       # payload
            payloads.append(data)
        elif mtype == STATUS_TYPE:               # status
            statuses.append(data)
            if data == PARITY_ERROR:
                parity_errs += 1

    dut._log.info(f"captured {len(payloads)} payload bytes, {len(statuses)} status msgs, "
                  f"{parity_errs} parity_error(0x02)")
    dut._log.info(f"status codes seen: {sorted(set(statuses))}")
    dut._log.info(f"payload tail: {bytes(payloads[-len(PAYLOAD):]) if payloads else b''}")

    # The defect: spurious parity errors on a no-parity stream.
    assert parity_errs == 0, (
        f"DS-T3.5 reproduced: {parity_errs} spurious parity_error (0x02) status events "
        f"on a clean 8N1 stream — the UART datalogger flags parity errors with no parity present"
    )

    # UART-REQ-14: zero type-100 status frames of any code on clean 8N1.
    assert len(statuses) == 0, (
        f"expected 0 status frames on clean 8N1, saw {len(statuses)}: {sorted(set(statuses))}"
    )

    # Latched verdict must stay 'no parity' (bit 0 == 0)
    assert int(dut.parityConf_r.value) & 1 == 0, (
        f"expected parityConf_r(0) == 0 (no parity) on clean 8N1, saw {int(dut.parityConf_r.value)}"
    )

    # And the payload must decode contiguously (sanity — same as the HW check).
    n = len(PAYLOAD)
    found = any(payloads[i:i + n] == PAYLOAD for i in range(max(0, len(payloads) - n + 1)))
    assert found, f"payload not found contiguously in {bytes(payloads)!r}"


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_no_spurious_parity",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
