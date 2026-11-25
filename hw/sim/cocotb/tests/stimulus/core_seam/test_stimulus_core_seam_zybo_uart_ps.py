# rr unknown-llm (AI-generated)
"""
test_stimulus_core_seam_zybo_uart_ps — DS-P1.90, STIM-SEAM-REQ-6 (UART).

Zynq wiring (tb_stimulus_core_seam_zybo.vhd). With STIM_UART_CONTROL bit[0]
clear, a frame bit-banged on the PS UART TX leg must arrive on capture_pin(36)
(the PS EMIO path the DS-X5 self-test uses). The generator is then enabled,
sends one frame, and is disabled again; the next PS frame must again arrive
whole, so the hand-back to the PS is proven, not only the power-up state.

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_zybo_uart_ps
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    CLK_PERIOD_NS, SEL_BIT_UART, SLOT_EMIO_MUX_SEL, SLOT_STIM_UART_BAUD_DIV,
    SLOT_STIM_UART_CONTROL, SLOT_STIM_UART_DATA, requires, start,
)
from _zybo_seam import decode_uart_8n1, drive_uart_8n1, run

BIT_NS = CLK_PERIOD_NS * 20


async def _ps_frame(dut, byte: int) -> None:
    cap = cocotb.start_soon(decode_uart_8n1(dut.PROBE_CAPTURE_UART36, BIT_NS))
    await drive_uart_8n1(dut.PS_UART_TX, byte, BIT_NS)
    data, stop = await cap.join()
    assert (data, stop) == (byte, 1), (
        f"PS frame {byte:#04x} reached capture_pin(36) as {data:#04x} stop={stop}")


@cocotb.test()
@requires("STIM-SEAM-REQ-6")
async def test_zybo_seam_uart_ps_leg_reaches_pin_before_and_after_generator(dut):
    dut.PS_UART_TX.value = 1
    axi = await start(dut)
    await axi.write(SLOT_EMIO_MUX_SEL, SEL_BIT_UART.to_bytes(4, "little"))
    await axi.write(SLOT_STIM_UART_BAUD_DIV, (20).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 8)
    await _ps_frame(dut, 0xA5)

    await axi.write(SLOT_STIM_UART_CONTROL, (1).to_bytes(4, "little"))
    cap = cocotb.start_soon(decode_uart_8n1(dut.PROBE_CAPTURE_UART36, BIT_NS))
    await axi.write(SLOT_STIM_UART_DATA, (0x3C).to_bytes(4, "little"))
    assert await cap.join() == (0x3C, 1), "generator frame did not arrive"
    await ClockCycles(dut.CLK, 8)
    await axi.write(SLOT_STIM_UART_CONTROL, (0).to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 8)

    await _ps_frame(dut, 0x96)


if __name__ == "__main__":
    run("test_stimulus_core_seam_zybo_uart_ps")
