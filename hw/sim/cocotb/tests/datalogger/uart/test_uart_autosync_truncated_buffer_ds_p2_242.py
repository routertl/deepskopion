# rr unknown-llm (AI-generated)
"""DS-P2.242: autoSync locks the line's word length when the calibration buffer
ends inside a frame.

The calibration buffer closes when the edge FIFO holds 2**(C_FIFO_DEPTH-3)-1 =
127 edges (uart_datalogger.vhd, WRITE_BUFFER), so a calibration burst longer
than that is cut inside a frame. The format decision waits for the replay to
drain. A stop-finder sample taken after the playback FIFO went empty judges the
cut frame on the level the playback holds, which is not recorded data, and
must not refute a format the recorded frames support.

Observed 2026-11-09 at seed 1794211055 of test_integration_1M_8N1: the 8-bit
instance, at G_MIN_MATCHES, was refuted 17 cycles after the playback FIFO went
empty, inside the two-bit settle, so no instance was left and calibration
failed (0 of 250 bytes). The burst below is that run's 150 calibration bytes,
sent the same way (rr_cocotb_tb UartSource, 1 Mbaud 8N1, autoSync on, after
setup_dut + apply_config + 100 cycles), so it is hard-coded rather than drawn
from a seed.

DS-T2.93: the same cut also cleared the stop-bit result. After the drain the
8-bit instance mismatches on the playback hold (JUDGE = '0', so MATCH stays),
and NOT_A_MATCH cleared stopBitsConf_r, so the line was reported with two stop
bits (CONFIG frame byte 0x08). The line is 8N1: stopBitsConf_r = '1' (one stop
bit, uart_datalogger_protocol.yml stop_bit_encoding) and the CONFIG frame's
low byte is 0x48 (stop_bits 1, no parity, word_length 8).
"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

import sim.cocotb  # noqa: F401  (SDK path setup, as the other UART tests)

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    GENERICS, CUSTOM_LIBRARIES, setup_dut, apply_config,
)
from rr_cocotb_tb.drivers.uart_master import UartSource

CAL_BYTES = [
    0x34, 0xE9, 0x59, 0x5B, 0x11, 0x83, 0xD9, 0x2A, 0xBC, 0x19, 0x35, 0x85,
    0x8A, 0xBE, 0x1A, 0x7B, 0xEE, 0xB6, 0x70, 0x4F, 0x29, 0xCF, 0xB5, 0xB9,
    0x9C, 0x42, 0x7A, 0x81, 0x94, 0xA2, 0xA5, 0x1F, 0x8D, 0xA5, 0xC4, 0x6C,
    0x53, 0x1F, 0x76, 0x92, 0x61, 0x06, 0x9B, 0x27, 0xB6, 0xFA, 0xD7, 0xAD,
    0x86, 0x93, 0xB8, 0x01, 0x7F, 0xEE, 0xA7, 0x7D, 0xA7, 0xE5, 0x88, 0x31,
    0x26, 0x56, 0xFB, 0x75, 0x10, 0xA9, 0x95, 0x02, 0x1D, 0x0E, 0xA4, 0xB1,
    0x63, 0x4E, 0x58, 0xD1, 0xD3, 0x0C, 0x94, 0xFD, 0x46, 0x81, 0x96, 0xEE,
    0xDD, 0xA0, 0x48, 0x82, 0xC2, 0x62, 0x39, 0x17, 0x4E, 0x71, 0xD5, 0x5E,
    0xD8, 0xB1, 0x94, 0xB4, 0xCC, 0x0D, 0xF1, 0xDC, 0x75, 0xA3, 0xC1, 0x12,
    0x0C, 0xB4, 0x7D, 0x94, 0x40, 0x5C, 0x56, 0x1A, 0x04, 0x32, 0xBB, 0x61,
    0x2C, 0x36, 0x12, 0x0B, 0xA9, 0xC9, 0x8B, 0x5E, 0x0E, 0x06, 0xE3, 0xF8,
    0xC3, 0xE4, 0x4A, 0x6D, 0x1E, 0x7C, 0x0A, 0xCE, 0xD6, 0xA3, 0xAC, 0x29,
    0x85, 0xE2, 0x10, 0x58, 0x2C, 0x4E,
]


CONFIG_TYPE = 0b001


async def _record_config(dut, out):
    """Low byte of every accepted CONFIG transfer (VLD and RDY at a CLK edge)."""
    while True:
        await RisingEdge(dut.CLK)
        await ReadOnly()
        if int(dut.UART_MSG_VLD.value) == 1 and int(dut.UART_MSG_RDY.value) == 1:
            raw = int(dut.UART_MSG_OUT.value)
            if (raw >> 24) & 0x7 == CONFIG_TYPE:
                out.append(raw & 0xFF)


@cocotb.test()
async def autosync_locks_8n1_when_the_buffer_ends_mid_frame(dut):
    await setup_dut(dut)
    configs: list = []
    recorder = cocotb.start_soon(_record_config(dut, configs))
    await apply_config(dut, GENERICS["G_UART_DEV_ID"])
    await ClockCycles(dut.CLK, 100)
    uart = UartSource(dut.UART_IN, baud=1_000_000, bits=8, stop_bits=1, parity="none")
    await uart.write_bytes(CAL_BYTES)
    for _ in range(200):
        if int(dut.calibrationDone_r.value) == 1:
            break
        await ClockCycles(dut.CLK, 100)
    await ClockCycles(dut.CLK, 4)
    result = (int(dut.calibrationDone_r.value), int(dut.calibrationFail_r.value),
              int(dut.nbitsConf_r.value))
    dut._log.info(f"(done, fail, nbits) = {result}")
    assert result == (1, 0, 8), (
        f"DS-P2.242: calibration buffer cut inside a frame: (done, fail, nbits) "
        f"= {result}, expected (1, 0, 8)")
    await ClockCycles(dut.CLK, 200)
    recorder.kill()
    stop_bits = int(dut.stopBitsConf_r.value)
    dut._log.info(f"stopBitsConf_r = {stop_bits}, CONFIG low bytes = "
                  f"{[hex(c) for c in configs]}")
    assert stop_bits == 1, (
        f"DS-T2.93: 8N1 line reported stopBitsConf_r = {stop_bits} (0 = two stop bits)")
    assert configs == [0x48], (
        f"DS-T2.93: CONFIG frame low bytes {[hex(c) for c in configs]}, expected [0x48]")


def main():
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_autosync_truncated_buffer_ds_p2_242",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        verbose=True,
        work_lib="tich",
        custom_libraries=CUSTOM_LIBRARIES,
    )


if __name__ == "__main__":
    main()
