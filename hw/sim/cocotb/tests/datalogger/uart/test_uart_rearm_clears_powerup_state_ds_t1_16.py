# rr unknown-llm (AI-generated)
"""
UART Datalogger — a re-arm must clear power-up calibration state (DS-T1.16)

On the PolarFire kit the UART datalogger (id 12) emitted nothing from any
source, while simulation of the same arm sequence decoded every byte. A
throwaway debug image (DS-T1.16, 2026-09-26) read the datalogger's internals
off silicon: baud measured right (108 ticks at div 434, 216 at 115200), edges
and FIFO writes identical to simulation, buffering finished, calibrationDone
set, and calibrationFail_r = 1 in explicit 8N1 mode, where the RTL never sets
it. startStreaming = calibrationDone and not calibrationFail held the frame
generator in IDLE, so there was no CONFIG and no bytes. The flop had no reset
and relied on its VHDL initial value; Synplify ignores initial values on
PolarFire (FX1172), so it powered up '1'. A disable/enable could not clear it,
because the per-datalogger reset did not reach it. uart_frame_gen.parityFail_r
has the same shape: only CHECK_PARITY writes it, yet GENERATE_MSG reads it with
parity off, so a power-up '1' adds a PARITY_ERROR to every byte.

Simulation honours initial values, so a plain run can never see this. This test
puts both flops into the state silicon left them in, then runs the kit's exact
arm (DATALOGGER_CONFIG disable, then enable 8N1 explicit, as
sw/stream/arm_datalogger_defaults.py writes it) and the DE-C0 contract payload
back-to-back at 115200, like MMUART1 sends it. The verdict is read only from
the datalogger's own message stream (UART_MSG_VLD/UART_MSG_OUT): a CONFIG
record, the ten payload bytes in order, and no PARITY_ERROR status.

Run via the sanctioned executor (ROUTERTL-001), or through its pytest gate
tools/tests/test_uart_rearm_powerup_state_ds_t1_16.py.
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge, Timer

test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")
sys.path.insert(0, str(test_dir.parents[5]))   # repo root: utils import hw.sim.*

import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    setup_dut, GENERICS, CUSTOM_LIBRARIES, UartSource
)

BAUD_RATE = 115_200
# DE-C0 contract payload (sw/stream/testbench/de_c0_stimulus_contract.py).
UART_PAYLOAD = [0xA3, 0x5C, 0x71, 0x0E, 0xFF, 0x42, 0x00, 0xFF, 0x00, 0xFF]
EXPLICIT_8N1 = 0x0D   # enable | autoSync=0 | dataBits=8 | no parity | 1 stop

TYPE_CONFIG, TYPE_PAYLOAD, TYPE_STATUS = 0b001, 0b010, 0b100
STATUS_PARITY_ERROR = 0x02


@cocotb.test()
async def rearm_clears_powerup_calibration_state(dut):
    """Power-up calibrationFail_r/parityFail_r = 1, kit re-arm, 8N1 bytes must decode."""
    dev_id = GENERICS["G_UART_DEV_ID"]
    await setup_dut(dut)

    # The state the kit's flops were in (calibrationFail_r read back as 1).
    dut.calibrationFail_r.value = 1
    dut.i_uart_frame_gen.parityFail_r.value = 1
    await ClockCycles(dut.CLK, 2)

    msgs = []

    async def monitor():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.UART_MSG_VLD.value) == 1 and int(dut.UART_MSG_RDY.value) == 1:
                msgs.append(int(dut.UART_MSG_OUT.value))

    cocotb.start_soon(monitor())

    # arm_datalogger_defaults: disable, then enable with the 8N1 framing.
    dut.CONFIG.value = (dev_id & 0x1F) << 11
    await ClockCycles(dut.CLK, 20)
    dut.CONFIG.value = ((dev_id & 0x1F) << 11) | EXPLICIT_8N1
    await ClockCycles(dut.CLK, 100)

    uart = UartSource(dut.UART_IN, baud=BAUD_RATE, bits=8, stop_bits=1, parity="none")
    await uart.write_bytes(UART_PAYLOAD)
    bit_ns = 1e9 / BAUD_RATE
    await Timer(int(bit_ns * 10 * (len(UART_PAYLOAD) + 4)), units="ns")
    await Timer(2_000_000, units="ns")

    kinds = [((m >> 24) & 0x7, m & 0xFF) for m in msgs]
    configs = [k for k in kinds if k[0] == TYPE_CONFIG]
    payload = [d for t, d in kinds if t == TYPE_PAYLOAD]
    parity_errors = [k for k in kinds if k == (TYPE_STATUS, STATUS_PARITY_ERROR)]
    dut._log.info(f"{len(msgs)} msgs: {[hex(m) for m in msgs]}")

    assert configs and payload == UART_PAYLOAD, (
        f"DS-T1.16: explicit 8N1 decoded {len(payload)} of {len(UART_PAYLOAD)} "
        f"payload bytes after a disable/enable re-arm with power-up "
        f"calibrationFail_r=1 (config records={len(configs)}, payload="
        f"{[hex(b) for b in payload]}) — the per-datalogger reset does not clear "
        f"the calibration-failure latch, so the frame generator never streams")
    assert not parity_errors, (
        f"DS-T1.16: {len(parity_errors)} PARITY_ERROR status record(s) on an 8N1 "
        f"(parity off) line after re-arm — uart_frame_gen.parityFail_r survives "
        f"the per-datalogger reset")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_rearm_clears_powerup_state_ds_t1_16",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
