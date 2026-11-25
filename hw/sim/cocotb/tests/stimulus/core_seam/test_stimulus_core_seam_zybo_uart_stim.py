# rr unknown-llm (AI-generated)
"""
test_stimulus_core_seam_zybo_uart_stim — DS-P1.90, STIM-SEAM-REQ-5 (UART).

Zynq wiring (tb_stimulus_core_seam_zybo.vhd). The PS UART TX leg is held LOW
for the whole test, a level that would break any frame. With
STIM_UART_CONTROL bit[0] set, the generator's 8N1 frame must still arrive
whole on capture_pin(36): the core takes the leg from the generator, not from
the PS. With G_STIM_DRIVES_EMIO false (the pre-DS-P1.90 Zynq wiring) the pin
stays low and the start-bit wait times out.

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_zybo_uart_stim
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    CLK_PERIOD_NS, SEL_BIT_UART, SLOT_EMIO_MUX_SEL, SLOT_STIM_UART_BAUD_DIV,
    SLOT_STIM_UART_CONTROL, SLOT_STIM_UART_DATA, requires, start,
)
from _zybo_seam import decode_uart_8n1, run


@cocotb.test()
@requires("STIM-SEAM-REQ-5")
async def test_zybo_seam_uart_generator_wins_over_ps_leg(dut):
    dut.PS_UART_TX.value = 0
    axi = await start(dut)
    await axi.write(SLOT_EMIO_MUX_SEL, SEL_BIT_UART.to_bytes(4, "little"))
    baud_div = 20
    await axi.write(SLOT_STIM_UART_BAUD_DIV, baud_div.to_bytes(4, "little"))
    await axi.write(SLOT_STIM_UART_CONTROL, (1).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 8)
    assert int(dut.PROBE_CAPTURE_UART36.value) == 1, (
        "capture_pin(36) is not idle-high with the generator enabled: the PS "
        "leg (held low) still reaches the UART capture pin")

    cap = cocotb.start_soon(decode_uart_8n1(dut.PROBE_CAPTURE_UART36,
                                            CLK_PERIOD_NS * baud_div))
    await axi.write(SLOT_STIM_UART_DATA, (0x5A).to_bytes(4, "little"))
    data, stop = await cap.join()
    assert (data, stop) == (0x5A, 1), f"capture_pin(36) decoded {data:#04x} stop={stop}"


if __name__ == "__main__":
    run("test_stimulus_core_seam_zybo_uart_stim")
