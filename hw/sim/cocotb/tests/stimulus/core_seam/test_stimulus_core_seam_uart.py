# rr unknown-llm (AI-generated)
"""
test_stimulus_core_seam_uart — DS-P2.100 landing-review Finding B:
C_INDEX_STIM_UART_* unpack AND the stim_uart_tx_o -> emio_uart_tx_i
DE25-loopback, end to end through the real regbank, proven against the
REAL tich.deepskopion_core (tb_stimulus_core_seam.vhd).

Observes PROBE_CAPTURE_UART36 (core's capture_pin(36), fed unconditionally from
ds_emio_mux.SRC_UART_TX, itself gated by EMIO_MUX_SEL(1)) rather than
OBS_STIM_UART_TX (this file's own loopback wire, upstream of core's
emio_uart_tx_i port) — that tb-local wire would stay "correct" even if the
loopback into core were reverted to the DS-P2.95 tied-off constants, since
it taps BEFORE the loopback. PROBE_CAPTURE_UART36 is downstream of both the
C_INDEX_STIM_UART_* unpack and the loopback, so mutating either turns this
test RED.

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_uart
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, Timer, with_timeout

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    CLK_PERIOD_NS, SEAM_RED_TIMEOUT_NS, SLOT_EMIO_MUX_SEL, SLOT_STIM_UART_BAUD_DIV,
    SLOT_STIM_UART_CONTROL, SLOT_STIM_UART_DATA, requires, run_core_seam, start,
)

SEL_BIT_UART = 1 << 1  # ds_emio_mux.vhd: EMIO_MUX_SEL(1) selects the EMIO UART source


@cocotb.test()
@requires("STIM-SEAM-REQ-2")
async def test_core_seam_uart_write_reaches_capture_pin_36(dut):
    """A real AXI write to EMIO_MUX_SEL (bit[1]=1, select the EMIO UART
    source) + STIM_UART_CONTROL/BAUD_DIV/DATA must produce a correct 8N1
    frame on core's own internal capture_pin(36) — proving BOTH the
    C_INDEX_STIM_UART_* unpack inside deepskopion_core.vhd AND the
    stim_uart_tx_o -> emio_uart_tx_i loopback wired at this bench's
    DE25-top-mirroring level, not just ds_stimulus_gen in isolation."""
    axi = await start(dut)

    await axi.write(SLOT_EMIO_MUX_SEL, SEL_BIT_UART.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)

    baud_div = 20
    bit_time_ns = CLK_PERIOD_NS * baud_div
    await axi.write(SLOT_STIM_UART_CONTROL, (1).to_bytes(4, "little"))
    await axi.write(SLOT_STIM_UART_BAUD_DIV, baud_div.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 4)

    payload = 0x5A

    async def capture():
        # DS-P2.100 landing-review Finding 6: a broken C_INDEX_STIM_UART_*
        # unpack (or a reverted loopback) means PROBE_CAPTURE_UART36 never
        # toggles — the bare `await FallingEdge(...)` this replaced hung
        # unbounded (reproduced: >30 min before manual cancellation),
        # wedging the shared sim queue. Bound it so a broken unpack fails
        # in seconds with a clear TestFailure instead.
        await with_timeout(FallingEdge(dut.PROBE_CAPTURE_UART36), SEAM_RED_TIMEOUT_NS, "ns")
        await Timer(bit_time_ns // 2, unit="ns")
        assert int(dut.PROBE_CAPTURE_UART36.value) == 0, "false start bit"
        data = 0
        for i in range(8):
            await Timer(bit_time_ns, unit="ns")
            data |= int(dut.PROBE_CAPTURE_UART36.value) << i
        await Timer(bit_time_ns, unit="ns")
        return data, int(dut.PROBE_CAPTURE_UART36.value)

    cap_task = cocotb.start_soon(capture())
    await axi.write(SLOT_STIM_UART_DATA, payload.to_bytes(4, "little"))

    data, stop_bit = await cap_task.join()
    assert data == payload, f"core.capture_pin(36) decoded {data:#x}, expected {payload:#x}"
    assert stop_bit == 1, "core.capture_pin(36) frame had a bad stop bit"


if __name__ == "__main__":
    run_core_seam("test_stimulus_core_seam_uart")
