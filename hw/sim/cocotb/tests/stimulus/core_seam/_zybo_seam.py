"""_zybo_seam — shared helpers for the DS-P1.90 Zynq-wiring seam tests.

The bench is hw/sim/tb_stimulus_core_seam_zybo.vhd: the REAL deepskopion_core
with G_STIM_DRIVES_EMIO true, the emio_* legs driven by the test through the
PS_* ports (standing in for the Zynq PS EMIO), and stim_*_o left open, as in
deepskopion_top.vhd. Leading underscore: not a test module of its own.
"""
from __future__ import annotations

from cocotb.triggers import FallingEdge, Timer, with_timeout

from _core_seam_common import SEAM_RED_TIMEOUT_NS, run_core_seam

TOP = "tb_stimulus_core_seam_zybo"


async def decode_uart_8n1(signal, bit_ns: int) -> tuple[int, int]:
    """Wait (bounded) for a start bit on ``signal``; return (byte, stop bit)."""
    await with_timeout(FallingEdge(signal), SEAM_RED_TIMEOUT_NS, "ns")
    await Timer(bit_ns // 2, unit="ns")
    assert int(signal.value) == 0, "false start bit"
    data = 0
    for i in range(8):
        await Timer(bit_ns, unit="ns")
        data |= int(signal.value) << i
    await Timer(bit_ns, unit="ns")
    return data, int(signal.value)


async def drive_uart_8n1(signal, byte: int, bit_ns: int) -> None:
    """Bit-bang one 8N1 frame onto ``signal`` (a PS_* port), LSB first."""
    for level in [0] + [(byte >> i) & 1 for i in range(8)] + [1]:
        signal.value = level
        await Timer(bit_ns, unit="ns")


def run(module: str) -> None:
    run_core_seam(module, top_level=TOP)
