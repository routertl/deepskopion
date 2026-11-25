# rr unknown-llm (AI-generated)
"""DS-P3.90 Phase 1 — UART uniform lifecycle (UART-REQ-18).

The UART datalogger emits a symmetric pair of status frames via the shared
datalogger_lifecycle component, in the ALWAYS-ON (global RST) domain so the markers
survive the per-datalogger dataloggerRst (the whole point — H.96): exactly one ENABLED
frame (event type "100", code 0x05) on the per-device ENABLE rising edge and one
DISABLED frame (code 0x06) on the falling edge, on UART_MSG_OUT with the UART
frame layout (dev | type | timestamp | code). Distinct from the UART payload
types (0b010 / 0b011), so it never masks or is masked by payload capture.

Run:  rm -rf hw/sim/work && rr sim run test_uart_lifecycle
"""
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import RisingEdge, ClockCycles

current_dir = Path(__file__).resolve().parent
import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation
from sdk.cocotb_helpers import requires
from uart_datalogger_test_utils import GENERICS, CUSTOM_LIBRARIES, setup_dut

DEV_ID = GENERICS["G_UART_DEV_ID"]   # 23
EVENT_TYPE = 0b100
# DS-P2.245: 0x03/0x00 were the frame generator's START_ERROR/CALIBRATION_DONE.
ENABLED = 0x05
DISABLED = 0x06


def _cfg_word(dev_id: int, payload: int) -> int:
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


def _fields(raw: int):
    """(dev_id, type, code) — timestamp [23:8] masked out (it free-runs)."""
    return (raw >> 27) & 0x1F, (raw >> 24) & 0x7, raw & 0xFF


async def _next_vld(dut, max_cycles=40):
    for _ in range(max_cycles):
        await RisingEdge(dut.CLK)
        if int(dut.UART_MSG_VLD.value) == 1:
            return int(dut.UART_MSG_OUT.value)
    return None


@cocotb.test()
@requires("UART-REQ-18")
async def lifecycle_enable_disable(dut):
    # UART_MSG_OUT / UART_MSG_VLD are sampled inside _next_vld(); the locals
    # below make the references explicit for the @requires signal-reference
    # check (coverage-map layer-1.5).
    _uart_msg_out = dut.UART_MSG_OUT  # sampled by _next_vld
    _uart_msg_vld = dut.UART_MSG_VLD  # sampled by _next_vld
    await setup_dut(dut)
    dut.UART_IN.value = 1            # idle high -> no payload traffic
    await ClockCycles(dut.CLK, 5)

    # Enable device -> exactly one ENABLED lifecycle frame.
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x001)
    raw = await _next_vld(dut, 40)
    assert raw is not None, "no frame after enable"
    dev, typ, code = _fields(raw)
    assert (dev, typ, code) == (DEV_ID, EVENT_TYPE, ENABLED), (
        f"ENABLED frame wrong: dev={dev} type={typ:#05b} code={code:#04x} "
        f"(raw {raw:#010x}); want dev={DEV_ID} type=0b100 code=0x05"
    )
    dut._log.info(f"ENABLED frame OK: {raw:#010x}")

    # Silence while enabled + line idle.
    quiet = await _next_vld(dut, 20)
    assert quiet is None, f"spurious frame while enabled+idle: {quiet:#010x}"

    # Disable device (explicit matching-id word, payload bit0=0).
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x000)
    raw = await _next_vld(dut, 40)
    assert raw is not None, "no frame after disable"
    dev, typ, code = _fields(raw)
    assert (dev, typ, code) == (DEV_ID, EVENT_TYPE, DISABLED), (
        f"DISABLED frame wrong: dev={dev} type={typ:#05b} code={code:#04x} "
        f"(raw {raw:#010x}); want dev={DEV_ID} type=0b100 code=0x06"
    )
    dut._log.info(f"DISABLED frame OK: {raw:#010x}")
    dut._log.info("UART-REQ-18 lifecycle enable/disable check passed.")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_lifecycle",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
