# rr unknown-llm (AI-generated)
"""
test_stimulus_core_seam_zybo_uart_arm_ds_p2_245 — DS-P2.245.

Silicon (zybo-z7-20, 2026-11-09, images 520fdbf4 and fd2c116c): every arm of
the UART datalogger produced a STATUS "calibration_done" and a STATUS
"start_error" before any byte, also on an arm that sent no byte.

Observed in this bench: they are the datalogger_lifecycle DISABLED and ENABLED
frames. uart_datalogger.vhd gave them STATUS type 100 with codes 0x00 and 0x03,
the codes uart_frame_gen uses for CALIBRATION_DONE and START_ERROR. The UART
lifecycle codes are now enabled = 0x05 and disabled = 0x06.

This replays the board's arm sequence (ds236_board_arm.py, emio_inject
SelfTestBench) through the REAL deepskopion_core on the Zynq wiring
(tb_stimulus_core_seam_zybo.vhd, G_STIM_DRIVES_EMIO true), register by
register over core's own AXI-Lite port, with the PS UART TX leg held idle
high and NO byte sent, twice:

  1. release_stimulus_generator: STIM_UART/I2C/SPI_CONTROL <= 0
  2. set_emio_mux(uart=True):    EMIO_MUX_SEL <= 0x2
  3. reset_and_enable_datalogger(12, extra=6):
       DATALOGGER_CONFIG <= 0x6000 (ENABLE=0), then 0x600D (8N1, ENABLE=1)

The first arm starts from the power-up state (disabled), the second re-arms
the enabled datalogger, as the board's second and later arms do. The board's
sleeps (50 ms after the mux, 1 ms after the disable) are shortened to 50 us
and 20 us.

Verdict (the DS-P2.245 witness rule): per arm, exactly the lifecycle frames
in order — uart_enabled for the first arm, uart_disabled then uart_enabled
for the re-arm — and no other STATUS code.

Run (from the deepskopion dir):  rr sim run test_stimulus_core_seam_zybo_uart_arm_ds_p2_245
"""
from __future__ import annotations

import cocotb
import cocotb.utils
from cocotb.triggers import RisingEdge, Timer

# DS-P2.269: this test lives in the UART suite (it asserts UART-REQ-18); its core-seam
# helpers stay in tests/stimulus/core_seam, imported by path.
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "stimulus" / "core_seam"))

from _core_seam_common import (  # noqa: E402 (sets up sys.path for sdk.* too)
    SEL_BIT_UART, SLOT_EMIO_MUX_SEL, SLOT_STIM_I2C_CONTROL,
    SLOT_STIM_SPI_CONTROL, SLOT_STIM_UART_CONTROL, requires, start,
)
from _zybo_seam import run

SLOT_DATALOGGER_CONFIG = 0x20
UART_ID = 12
CFG_DISABLE = UART_ID << 11              # 0x6000
CFG_ENABLE_8N1 = (UART_ID << 11) | 0x0D  # 0x600D: enable, explicit 8N1
MSG_TYPE_STATUS = 0b100
# uart_datalogger_protocol.yml status_codes, hard-coded (ROUTERTL-002).
STATUS_NAMES = {0x00: "calibration_done", 0x01: "calibration_timeout",
                0x02: "parity_error", 0x03: "start_error", 0x04: "stop_error",
                0x05: "enabled", 0x06: "disabled"}
# Window after each enable. The lifecycle frame follows the config write by a
# few cycles; 2 ms also covers the buffer-stage timeout of one stored edge
# (16 x the saturated distance counter, ~1.3 ms at 100 MHz) if one occurred.
WINDOW_MS = 2


def _now() -> int:
    return int(cocotb.utils.get_sim_time(unit="ns"))


def _w(v: int) -> bytes:
    return (v & 0xFFFFFFFF).to_bytes(4, "little")


async def _log_changes(sig, name: str, log: list) -> None:
    while True:
        await sig.value_change
        log.append((_now(), name, str(sig.value)))


async def _log_messages(logger, log: list) -> None:
    while True:
        await RisingEdge(logger.CLK)
        if str(logger.UART_MSG_VLD.value) == "1" and str(logger.UART_MSG_RDY.value) == "1":
            log.append((_now(), "msg", int(logger.UART_MSG_OUT.value)))


@cocotb.test()
@requires("UART-REQ-18")
async def test_zybo_uart_arm_emits_only_the_lifecycle_frames(dut):
    dut.PS_UART_TX.value = 1
    axi = await start(dut)
    logger = (dut.u_core.u_datalogger_gather.GEN_UART_BLOCK
              .GEN_UART_DATALOGGERS[0].i_uart_datalogger)

    events: list = []
    for sig, name in ((logger.UART_IN, "UART_IN"), (logger.uartPin_r, "uartPin_r"),
                      (logger.fifoUartWrEn_r, "fifo_wr"),
                      (logger.calibrationDone_r, "calibrationDone_r"),
                      (logger.dataloggerRst, "dataloggerRst"),
                      (dut.PROBE_CAPTURE_UART36, "capture_pin36")):
        cocotb.start_soon(_log_changes(sig, name, events))
    msgs: list = []
    cocotb.start_soon(_log_messages(logger, msgs))

    async def arm(tag: str) -> int:
        for slot in (SLOT_STIM_UART_CONTROL, SLOT_STIM_I2C_CONTROL,
                     SLOT_STIM_SPI_CONTROL):
            await axi.write(slot, _w(0))
        await axi.write(SLOT_EMIO_MUX_SEL, _w(SEL_BIT_UART))
        await Timer(50, unit="us")
        t_arm = _now()
        await axi.write(SLOT_DATALOGGER_CONFIG, _w(CFG_DISABLE))
        await Timer(20, unit="us")
        t_enable = _now()
        await axi.write(SLOT_DATALOGGER_CONFIG, _w(CFG_ENABLE_8N1))
        await Timer(WINDOW_MS, unit="ms")
        dut._log.info(f"{tag}: disable write t={t_arm} ns, enable write t={t_enable} ns")
        return t_arm

    t_first = await arm("arm 1 (from disabled)")
    t_second = await arm("arm 2 (re-arm from enabled)")

    for t, name, val in events:
        dut._log.info(f"edge t={t} ns {name}={val}")
    per_arm = {1: [], 2: []}
    for t, _, word in msgs:
        mtype = (word >> 24) & 0x7
        code = word & 0xFF
        dut._log.info(f"msg  t={t} ns word=0x{word:08X} type={mtype:03b} code=0x{code:02X}")
        if mtype == MSG_TYPE_STATUS:
            arm_no = 2 if t >= t_second else 1 if t >= t_first else 0
            per_arm.setdefault(arm_no, []).append(
                STATUS_NAMES.get(code, f"0x{code:02X}"))

    assert per_arm == {1: ["enabled"], 2: ["disabled", "enabled"]}, (
        f"DS-P2.245: STATUS frames per arm {per_arm}; expected exactly "
        f"{{1: ['enabled'], 2: ['disabled', 'enabled']}} (the lifecycle frames, "
        f"in order) and no other STATUS code. The edges datalogger 12 saw are "
        f"logged above")


if __name__ == "__main__":
    run("test_stimulus_core_seam_zybo_uart_arm_ds_p2_245")
