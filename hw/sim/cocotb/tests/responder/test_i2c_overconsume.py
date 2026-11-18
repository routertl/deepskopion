"""DS-T2.34 — reproduce the injector's FWFT empty-latency over-consume in sim.

On silicon the i2c_injector intermittently OVER-CONSUMES its TX FIFO: a
multi-byte master read comes back correct-prefix-then-0xFF (e.g. de ad be **ff**)
because the LAST byte's FIFO pop+read lands in the FWFT `empty`-flag latency
window. The slave gates "data available" on `I2C_TX_VLD = not fifo_empty`, but
the XPM FWFT `empty` flag lags the pop by a cycle — so for one clk phase the
slave reads stale/empty DOUT, drives the released line (FF) and bails to
WAIT_START. Cycle-accurately observed on silicon via the i2c_seam REA.

It's intermittent because the async SCL-vs-clk_sys phase decides whether the
read lands in that 1-cycle window. In sim everything is synchronous, so we
SWEEP the master SCL period + a sub-clk start phase to walk the alignment until
one lands in the window. The DUT uses the REAL xpm_fifo_sync (FWFT), so the
same `empty`/DOUT latency is present.

RED before the fix (some alignment over-consumes), GREEN after.

Run:  rr sim run test_i2c_overconsume
"""
import sys
from pathlib import Path

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
_ds_root = Path(__file__).resolve().parents[5]
if str(_ds_root / "hw" / "sim") not in sys.path:
    sys.path.insert(0, str(_ds_root / "hw" / "sim"))

import random

import cocotb
from cocotb.triggers import ClockCycles
from rr_cocotb_tb.drivers.i2c_master import I2cTiming

# Reuse the existing injector testbench harness.
from test_i2c_injector import I2cInjectorTb, SLAVE_ADDR, GENERICS, make_control, CTRL_FIFO_RESET


async def load_like_silicon(tb, payload):
    """Replicate sw load_i2c_response EXACTLY: CONTROL reset=1+en=1, then
    reset=0+en=1, then push DATAIN — WITHOUT waiting for the drain-mode FSM to
    finish (the silicon path does not poll drain_busy). This lets the reset's
    drain_mode (which forces fifo_rd_en=1) race the push/read."""
    clk = tb.env.clk
    tb.dut.ENABLE.value = 1
    tb.dut.CONTROL.value = make_control(enable=True, fifo_reset=True)   # edge → fifo_reset_r → drain_mode
    await ClockCycles(clk, 1)
    tb.dut.CONTROL.value = make_control(enable=True, fifo_reset=False)
    await ClockCycles(clk, 1)
    for b in payload:                                                   # push, no drain wait
        tb.dut.DATAIN.value = b & 0xFF
        tb.dut.DATAIN_VLD.value = 1
        await ClockCycles(clk, 1)
        tb.dut.DATAIN_VLD.value = 0
        await ClockCycles(clk, 1)

PAYLOAD = [0xD0, 0xD1, 0xD2, 0xD3]
BASE_PERIOD = 2500   # ~400 kHz, matching silicon


class JitteredTiming:
    """I2cTiming-compatible timing whose SCL half-period jitters per edge,
    modelling the real cdns-i2c master's irregular SCL under PS/AXI load.

    The driver reads ``half_period_ns`` once per SCL edge, so a randomized
    property gives genuine per-edge irregularity (unlike a clean fixed BFM).
    A seeded RNG keeps each run reproducible; the sweep walks (jitter, seed).
    """

    def __init__(self, period_ns, jitter_ns, rng, setup_ns=250, hold_ns=300):
        self.period_ns = period_ns
        self.setup_ns = setup_ns
        self.hold_ns = hold_ns
        self._jitter = jitter_ns
        self._rng = rng

    @property
    def half_period_ns(self) -> int:
        base = self.period_ns // 2
        return max(50, base + self._rng.randint(-self._jitter, self._jitter))


@cocotb.test()
async def overconsume_jitter_sweep(dut):
    tb = I2cInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    failures = []
    total = 0
    for jitter in (10, 30, 60, 120, 250):
        for seed in range(25):
            rng = random.Random(seed * 7919 + jitter)
            tb.i2c.timing = JitteredTiming(BASE_PERIOD, jitter, rng)

            await load_like_silicon(tb, PAYLOAD)   # silicon load: no drain wait
            await tb.i2c.start()
            ack = await tb.i2c.write_byte((SLAVE_ADDR << 1) | 1)
            total += 1
            if not ack:
                await tb.i2c.stop()
                failures.append((jitter, seed, "addr-NACK"))
                tb.env.log.info(f"** addr-NACK jitter={jitter} seed={seed}")
                continue
            got = [await tb.i2c.read_byte(send_ack=(i < 3)) for i in range(4)]
            await tb.i2c.stop()
            if got != PAYLOAD:
                failures.append((jitter, seed, [hex(b) for b in got]))
                tb.env.log.info(
                    f"** OVER-CONSUME jitter={jitter}ns seed={seed} -> "
                    f"{[hex(b) for b in got]} (want {[hex(b) for b in PAYLOAD]})"
                )

    tb.env.log.info(f"=== jitter sweep: {len(failures)}/{total} reads failed ===")
    assert not failures, (
        f"FWFT over-consume reproduced in {len(failures)}/{total} jittered reads "
        f"(first few: {failures[:8]})"
    )


def main():
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[5]
    DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"
    from hw.sim.portable_fifo_sources import olo_fifo_sources

    run_simulation(
        top_level="tb_i2c_injector",
        module="test_i2c_overconsume",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/i2c/i2c_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/i2c_injector.vhd",
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_i2c_injector.vhd",
            ],
        },
    )


if __name__ == "__main__":
    main()
