# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.318: arm_uart_breaks with the UART header input held low, against its contract record list.

``stimulus_armer.arm_uart_breaks`` resets and enables UART datalogger 12 (autoSync) and switches EMIO_MUX_SEL after,
the order DS-P2.315 found wrong for arm_uart: enabled on the header pin held low, the UART logger calibrated on the
header-to-stimulus step. For the break pattern the calibration fails by design (DS-P2.257: 13 low bit-times fit no UART
format), so the old order may give the same records; this test records which.

Bench: hw/sim/tb_default_mix_core.vhd (the real deepskopion_core), as test_arm_order_uart_i2c_spi_ds_p2_315. The
production writes are recorded from arm_uart_breaks with its defaults (13 low bit-times, 80 breaks, UART_BAUD_DIV 434)
and replayed over AXI-Lite; its logger reset sleep is replayed as 20 us. Each case starts from reset with the logger
disabled and holds jb(0) (UART#12 TX) at one level for the whole case.

Pre-stated verdict (before the first run), per case:
  stimulus presence first: the generator's UART TX toggles at least 160 times after the arm (80 breaks), else VOID;
  UART datalogger 12 records after the arm are exactly enabled (STATUS 0x05), then calibration_timeout (STATUS 0x01)
  (uart_datalogger_protocol.yml status codes; the DS-P2.257 witness list), and no other record.
  Prediction on today's order: header high GREEN; header low not predicted (recorded RED or GREEN).

First run (rr queue job 8fdfcc9b7217, today's order): both cases GREEN on the list above, but header low gave the
calibration_timeout at 127 us (after about 2 breaks), header high at 4.91 ms. With the header low the calibration
fails on the header-to-stimulus step, not on the break pattern the armer exists to drive. Rule added after that run,
before any other run: the calibration_timeout record comes after the 140th UART TX toggle, i.e. after the first 70
breaks, the size of the calibration buffer the 80 breaks exceed (DS-P2.257). Prediction on today's order: header low
RED on this rule (measured above), header high GREEN.

Run (from the deepskopion dir): rr sim run test_arm_uart_breaks_order_ds_p2_318
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Edge, RisingEdge, Timer
from cocotb.utils import get_sim_time
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

_HERE = Path(__file__).resolve()
_COCOTB = _HERE.parents[4]                          # hw/sim/cocotb
_ROOT = _HERE.parents[7]                            # deepskopion
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_COCOTB / "tests" / "stimulus" / "core_seam"))
from _core_seam_common import run_core_seam  # noqa: E402

TOP = "tb_default_mix_core"
CLK_PERIOD_NS = 10
IDLE = {
    "JA_0": 1, "JA_1": 1, "JA_2": 1, "JA_3": 1,
    "JA_4": 1, "JA_5": 0, "JA_6": 0, "JA_7": 0,
    "JB_0": 1, "JB_1": 1, "JB_2": 0, "JB_3": 0, "JB_4": 0,
    "JB_5": 1, "JB_6": 0, "JB_7": 0,
}
UART_ID = 12
UART_STATUS = 0x104
EXPECTED = [(UART_STATUS, 0x05), (UART_STATUS, 0x01)]      # enabled, calibration_timeout
MIN_TOGGLES = 160
TOGGLES_BEFORE_TIMEOUT = 140                               # 70 breaks: the calibration buffer the 80 breaks exceed
SLEEP_RESET_NS = 20_000
# 80 breaks x (13 + 1) bit-times x 434 cycles x 10 ns = 4.86 ms, plus the calibration timeout margin.
WINDOW_MS = 8


def _armer_ops() -> list:
    from sw.stream import stimulus_armer

    ops: list = []

    class _Recorder:
        def write_u32(self, off, val):
            ops.append((int(off), int(val)))

    real_sleep = stimulus_armer.time.sleep
    stimulus_armer.time.sleep = lambda _s: ops.append(("sleep", SLEEP_RESET_NS))
    try:
        stimulus_armer.arm_uart_breaks(_Recorder())
    finally:
        stimulus_armer.time.sleep = real_sleep
    return ops


async def _case(dut, level: int) -> None:
    levels = dict(IDLE, JB_0=level)
    for name, lv in levels.items():
        getattr(dut, name).value = lv
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, unit="ns").start())
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)

    records: list = []

    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.PROBE_DOUT_VALID.value) == 1 and int(dut.PROBE_DOUT_REQ.value) == 1:
                records.append((get_sim_time("ns"), int(dut.PROBE_DOUT_DATALOGGER_ID.value),
                                int(dut.PROBE_DOUT_MSG_TYPE.value), int(dut.PROBE_DOUT_PAYLOAD.value)))

    toggles: list = []

    async def watch():
        sig = dut.u_core.stim_uart_tx_w
        while True:
            await Edge(sig)
            toggles.append(get_sim_time("ns"))

    cocotb.start_soon(collect())
    cocotb.start_soon(watch())
    t_arm = get_sim_time("ns")
    ops = _armer_ops()
    dut._log.info(f"header={level}: armer writes {[op if op[0] == 'sleep' else (hex(op[0]), hex(op[1])) for op in ops]}")
    for op in ops:
        if op[0] == "sleep":
            await Timer(op[1], unit="ns")
        else:
            await axi.write(op[0], op[1].to_bytes(4, "little"))
    await Timer(WINDOW_MS, unit="ms")

    mine = [(t, mt, p) for t, i, mt, p in records if i == UART_ID and t >= t_arm]
    got = [(mt & 0xFFF, p & 0xFF) for _, mt, p in mine]
    dut._log.info(f"header={level}: stimulus toggles {len(toggles)}; records {[(t, hex(mt), hex(p)) for t, mt, p in mine]}")
    assert len(toggles) >= MIN_TOGGLES, f"VOID: {len(toggles)} UART TX toggles after the arm, expected >= {MIN_TOGGLES}"
    assert got == EXPECTED, (f"header={level}: records {[(hex(a), hex(b)) for a, b in got]}, "
                             f"expected {[(hex(a), hex(b)) for a, b in EXPECTED]}")
    t_timeout = mine[1][0]
    before = sum(1 for t in toggles if t < t_timeout)
    assert before >= TOGGLES_BEFORE_TIMEOUT, (
        f"header={level}: calibration_timeout at {t_timeout} ns after {before} UART TX toggles, expected >= "
        f"{TOGGLES_BEFORE_TIMEOUT} (the timeout must come from the breaks, not from the mux switch)")


@cocotb.test()
async def test_arm_uart_breaks_header_low(dut):
    await _case(dut, 0)


@cocotb.test()
async def test_arm_uart_breaks_header_high(dut):
    await _case(dut, 1)


if __name__ == "__main__":
    run_core_seam("test_arm_uart_breaks_order_ds_p2_318", top_level=TOP)
