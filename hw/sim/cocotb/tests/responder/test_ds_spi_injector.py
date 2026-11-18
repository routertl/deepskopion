"""
test_spi_injector — cocotb regression for hw/rtl/responder/spi_injector.vhd
(DS-P3.46; SPI side of DS-P2.51 Phase 3).

Wraps spi_injector in tb_spi_injector (push-pull bus, single driver per
line) and exercises the regbank-fed FIFO → slave-driven MISO contract.
Each test anchors on a single SPI-INJ-REQ-N from
hw/rtl/responder/requirements.yml.

Mirrors test_i2c_injector.py — same FIFO contract, different bus shape.

Run with:
    rr sim run test_spi_injector
"""
import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Timer

# Routertl SDK cocotb path (mirrors test_i2c_injector.py).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

# Deepskopion VIP path.
_ds_root = Path(__file__).resolve().parents[5]
_vip_parent = _ds_root / "hw" / "sim"
if str(_vip_parent) not in sys.path:
    sys.path.insert(0, str(_vip_parent))

from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv
from rr_cocotb_tb.drivers.spi_master import SpiBus, SpiConfig, SpiMaster


GENERICS = {
    "G_FIFO_DEPTH": 4,
}

CTRL_ENABLE     = 0x1
CTRL_FIFO_RESET = 1 << 9


def make_control(enable: bool = True, fifo_reset: bool = False) -> int:
    val = 0
    if enable:
        val |= CTRL_ENABLE
    if fifo_reset:
        val |= CTRL_FIFO_RESET
    return val


class SpiInjectorTb:
    """Testbench environment for tb_spi_injector."""

    def __init__(self, dut):
        self.dut = dut
        self.env = TbEnv(
            dut,
            clk="CLK",
            rst="RST",
            period_ns=10,  # 100 MHz clk_sys (matches deepskopion_top)
        )
        # SPI master at 1 MHz — plenty of margin over 100 MHz clk_sys
        # (100× ratio; the slave's 3-stage SCLK sync needs SCLK_period
        # > ~3× clk_sys_period).
        self.bus = SpiBus(
            sclk=dut.M_SCLK,
            mosi=dut.M_MOSI,
            miso=dut.M_MISO,
            cs=dut.M_CS_N,
        )
        self.cfg = SpiConfig(
            word_width=8,
            sclk_freq=1_000_000,    # 1 MHz
            cpol=False, cpha=False, # Mode 0 (matches spi_slave)
            msb_first=True,
            cs_active_low=True,
        )
        self.spi = SpiMaster(self.bus, self.cfg)

    async def setup(self):
        await self.env.start_clock()
        # Pin defaults before reset (avoid X-prop on first cycle).
        self.dut.ENABLE.value     = 0
        self.dut.CONTROL.value    = 0
        self.dut.DATAIN.value     = 0
        self.dut.DATAIN_VLD.value = 0
        await self.env.reset(cycles=5)

    async def enable_slave(self):
        """Pin enable."""
        self.dut.CONTROL.value = make_control(enable=True)
        self.dut.ENABLE.value  = 1
        await ClockCycles(self.env.clk, 4)

    async def push_byte(self, byte: int):
        """Push one byte into the TX FIFO via DATAIN strobe."""
        self.dut.DATAIN.value = byte & 0xFF
        self.dut.DATAIN_VLD.value = 1
        await ClockCycles(self.env.clk, 1)
        self.dut.DATAIN_VLD.value = 0
        await ClockCycles(self.env.clk, 1)

    async def push_bytes(self, payload):
        for b in payload:
            await self.push_byte(b)

    async def reset_fifo(self):
        """Pulse CONTROL[9] (self-clearing edge) and wait for drain to settle.

        DS-T12: CONTROL[9] now enters a drain-mode FSM rather than asserting
        the XPM FIFO RST (mirror of DS-T3.2 fix for i2c_injector). Drain
        runs at one byte per cycle and the FSM has ~3 cycles of entry
        latency from the CONTROL[9] write, so a naive fixed-cycle wait
        could break on the pre-entry "drain_busy=0" state. Poll for both
        drain_busy=STATUS[15]=0 AND fifo_empty=STATUS[0]=1 — that's the
        post-drain settled state, robust to whatever the host had loaded.
        """
        prev = self.dut.CONTROL.value.to_unsigned()
        self.dut.CONTROL.value = prev | CTRL_FIFO_RESET
        await ClockCycles(self.env.clk, 2)
        self.dut.CONTROL.value = prev
        max_drain_cycles = 4 * (2 ** GENERICS["G_FIFO_DEPTH"])
        for _ in range(max_drain_cycles):
            s = self.dut.STATUS.value.to_unsigned()
            if ((s >> 15) & 0x1) == 0 and (s & 0x1) == 1:
                break
            await ClockCycles(self.env.clk, 1)
        else:
            raise AssertionError(
                f"drain did not settle (drain_busy + fifo_empty) within "
                f"{max_drain_cycles} cycles: "
                f"STATUS=0x{self.dut.STATUS.value.to_unsigned():08X}"
            )

    def status_fifo_empty(self) -> int:
        return self.dut.STATUS.value.to_unsigned() & 0x1

    def status_fifo_count(self) -> int:
        depth = GENERICS["G_FIFO_DEPTH"]
        mask = (1 << (depth + 1)) - 1
        return (self.dut.STATUS.value.to_unsigned() >> 3) & mask

    def miso_t(self) -> int:
        """Read the slave's MISO tri-state enable directly."""
        return int(self.dut.i_injector.u_slave.MISO_T.value)


# ─────────────────────────────────────────────────────────────────────────
#  Functional tests
# ─────────────────────────────────────────────────────────────────────────

@cocotb.test()
@requires("SPI-INJ-REQ-4")
async def test_datain_push_increments_count(dut):
    """DATAIN_VLD pulses push bytes; STATUS count advances; empty clears."""
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    assert tb.status_fifo_empty() == 1, \
        f"FIFO not empty post-reset: STATUS=0x{dut.STATUS.value.to_unsigned():08X}"
    assert tb.status_fifo_count() == 0

    await tb.push_byte(0x5A)
    await ClockCycles(tb.env.clk, 4)

    assert tb.status_fifo_empty() == 0, "FIFO still empty after push"
    assert tb.status_fifo_count() == 1, \
        f"FIFO count after 1 push = {tb.status_fifo_count()}, want 1"
    tb.env.log.info("✓ DATAIN_VLD push increments count + clears empty")


@cocotb.test()
@requires("SPI-INJ-REQ-3")
async def test_miso_hi_z_when_idle(dut):
    """CS_N='1' (idle) holds MISO_T='1' (Hi-Z)."""
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    # No CS asserted yet — slave should be silent.
    await ClockCycles(tb.env.clk, 8)
    assert tb.miso_t() == 1, \
        f"MISO_T = {tb.miso_t()} at idle, expected 1 (Hi-Z)"
    tb.env.log.info("✓ MISO released at idle (T=1)")


@cocotb.test()
@requires("SPI-INJ-REQ-1")
async def test_single_byte_read_returns_first_fifo_byte(dut):
    """8 SCLK cycles return the first FIFO byte on MISO, MSB-first."""
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    expected = 0xA5
    await tb.push_byte(expected)
    await ClockCycles(tb.env.clk, 4)

    # Master writes a 0x00 dummy byte (full-duplex); we don't care what
    # the slave receives — only what it sends back on MISO.
    rx = await tb.spi.write([0x00])
    assert rx[0] == expected, \
        f"master read 0x{rx[0]:02X} on MISO, expected 0x{expected:02X}"
    tb.env.log.info(f"✓ single-byte read returned 0x{rx[0]:02X}")


@cocotb.test()
@requires("SPI-INJ-REQ-2")
async def test_multi_byte_read_in_fifo_order(dut):
    """N×8 SCLK returns the first N FIFO entries on MISO in order."""
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    payload = [0xDE, 0xAD, 0xBE, 0xEF]
    await tb.push_bytes(payload)
    await ClockCycles(tb.env.clk, 4)

    rx = await tb.spi.write([0x00] * len(payload))
    assert rx == payload, \
        f"FIFO-order mismatch: got {[hex(b) for b in rx]}, want {[hex(b) for b in payload]}"
    tb.env.log.info(f"✓ multi-byte read returned {[hex(b) for b in rx]} in order")


@cocotb.test()
@requires("SPI-INJ-REQ-5")
async def test_fifo_empty_returns_zero(dut):
    """With the FIFO empty, master reads zeros on MISO."""
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    assert tb.status_fifo_empty() == 1, "test pre-condition: FIFO must be empty"
    rx = await tb.spi.write([0x00, 0x00])
    assert rx == [0x00, 0x00], \
        f"empty-FIFO read got {[hex(b) for b in rx]}, expected zeros"
    tb.env.log.info("✓ empty FIFO returns zeros on MISO")


# ─────────────────────────────────────────────────────────────────────────
#  Standalone Execution
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from engine.simulation import run_simulation
    from engine.paths import SRC_PATH, SDK_SRC_PATH

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[5]
    DEEPSKOPION_RTL  = DEEPSKOPION_ROOT / "hw" / "rtl"
    from hw.sim.portable_fifo_sources import olo_fifo_sources

    run_simulation(
        top_level="tb_spi_injector",
        module="test_ds_spi_injector",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                # SDK shared units
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                # Deepskopion packages
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                # Responder modules (DUT chain)
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{DEEPSKOPION_RTL}/responder/spi_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/spi_injector.vhd",
                # Sim wrapper
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_spi_injector.vhd",
            ],
        },
    )
