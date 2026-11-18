"""
test_stimulus_core_seam_uart_logger — DS-T1.16: the PolarFire kit's UART arm,
end to end through the REAL deepskopion_core at clk_sys = 100 MHz.

Silicon (DS-T1.16, 2026-09-26, kit image 0f9ae830 + a throwaway debug tap):
after the boot arm and every later re-arm, UART_PKT_CNT only ever counted the
ENABLED/DISABLED lifecycle frames. The datalogger measured the right baud and
buffered every edge, but calibrationFail_r read 1 — a flop with no reset whose
VHDL initial value Synplify drops on PolarFire — so the frame generator never
left IDLE. Simulation starts that flop at 0, which is why the plain sequence
always passed here.

This test runs the kit's register-level sequence through core's own AXI-Lite
port (the surface MmioRegbank drives): EMIO_MUX_SEL 0x202 at boot, the
DATALOGGER_CONFIG disable/enable 8N1 pair, then the DE-C0 stimulus control
(EMIO_MUX_SEL 0x2, STIM_UART_BAUD_DIV 434, STIM_UART_DATA per byte). Before the
arm it puts datalogger 12's calibrationFail_r into the state silicon had it in.
The verdict is the UART_PKT_CNT register read back over AXI, which is what the
kit reads: ENABLED + CONFIG + ten payload frames, not just the lifecycle frames.

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_uart_logger
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, Timer

from _core_seam_common import (  # noqa: E402
    SLOT_EMIO_MUX_SEL, SLOT_STIM_UART_BAUD_DIV, SLOT_STIM_UART_CONTROL,
    SLOT_STIM_UART_DATA, requires, run_core_seam, start,
)

SLOT_DATALOGGER_CONFIG = 0x20
SLOT_PKT_CNT_UART = 0xD8
UART_ID = 12
UART_PAYLOAD = [0xA3, 0x5C, 0x71, 0x0E, 0xFF, 0x42, 0x00, 0xFF, 0x00, 0xFF]
BOOT_MUX = 0x202        # arm_datalogger_defaults on a ds_mss_fabric=1 boot
STIM_MUX = 0x2          # bit 9 clear: ds_stimulus_gen drives the logger
STIM_BAUD_DIV = 434


def _w(v: int) -> bytes:
    return (v & 0xFFFFFFFF).to_bytes(4, "little")


@cocotb.test()
@requires("STIM-SEAM-REQ-2")
async def test_kit_uart_arm_emits_payload_frames(dut):
    axi = await start(dut)

    async def pkt_cnt() -> int:
        return int.from_bytes((await axi.read(SLOT_PKT_CNT_UART, 4)).data, "little")

    # Datalogger 12 (UART0) as the kit left it: calibrationFail_r = 1.
    logger = dut.u_core.u_datalogger_gather.GEN_UART_BLOCK.GEN_UART_DATALOGGERS[0].i_uart_datalogger
    logger.calibrationFail_r.value = 1
    await ClockCycles(dut.CLK, 2)

    await axi.write(SLOT_EMIO_MUX_SEL, _w(BOOT_MUX))
    await axi.write(SLOT_DATALOGGER_CONFIG, _w(UART_ID << 11))
    await Timer(20, unit="us")
    await axi.write(SLOT_DATALOGGER_CONFIG, _w((UART_ID << 11) | 0x0D))
    await Timer(20, unit="us")
    armed = await pkt_cnt()

    await axi.write(SLOT_EMIO_MUX_SEL, _w(STIM_MUX))
    await axi.write(SLOT_STIM_UART_BAUD_DIV, _w(STIM_BAUD_DIV))
    await axi.write(SLOT_STIM_UART_CONTROL, _w(1))
    for b in UART_PAYLOAD:
        await axi.write(SLOT_STIM_UART_DATA, _w(b))
        await Timer(200, unit="us")
    await Timer(1000, unit="us")
    done = await pkt_cnt()

    dut._log.info(f"UART_PKT_CNT after arm={armed} after payload={done}")
    assert done - armed >= 1 + len(UART_PAYLOAD), (
        f"DS-T1.16: UART_PKT_CNT rose by {done - armed} after the kit's stimulus "
        f"control ({armed} -> {done}); expected CONFIG + {len(UART_PAYLOAD)} payload "
        f"frames — datalogger 12 stays silent once calibrationFail_r is set")


if __name__ == "__main__":
    run_core_seam("test_stimulus_core_seam_uart_logger")
