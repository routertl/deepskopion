"""
_stim_common — shared helpers for the DS-P2.100 / DS-D.38 stimulus-gen test
suite: test_stimulus_uart_frame.py, test_stimulus_uart_idle.py,
test_stimulus_uart_drop_write.py, test_stimulus_i2c_ack.py,
test_stimulus_i2c_nack.py, test_stimulus_spi.py, test_stimulus_edge_period.py,
test_stimulus_edge_disabled.py, test_stimulus_edge_muxsel.py.

Leading underscore so `rr sim run <module>` / cocotb test discovery never
picks this up as a test module in its own right (it has no @cocotb.test()
functions) — see the DS-P2.100 revision commit message for why this file
exists: Finding A of the landing review found that running all 9 original
@cocotb.test()s in ONE nvc/cocotb session (the old single-file
test_stimulus_gen_loop.py) reproducibly hung nvc after the first test. A
FIRST split attempt regrouped by protocol (one file per protocol, 1-3 tests
each) — that still hangs: reproduced directly via `rr sim run
test_stimulus_i2c` (a 2-test file), which ran test 1 to completion and then
hung nvc at 100% CPU on test 2, exactly like the 9-test monolith. The hang
is keyed to "more than one @cocotb.test() sharing an nvc session", not to
file/protocol grouping, so the suite is now ONE test per file — each
compiled+run as its own `rr sim run <module>` invocation (its own nvc
session) — this module holds the pieces every one of those files needs so
none of them drift.

All of this still targets the SAME real fabric chain
(tich.ds_stimulus_gen -> tich.ds_emio_mux -> {tich.i2c_injector,
tich.spi_injector}) via tb_stimulus_gen_loop.vhd — unchanged by the split.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Event, FallingEdge, Timer

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

_ds_root = Path(__file__).resolve().parents[5]
_vip_parent = _ds_root / "hw" / "sim"
if str(_vip_parent) not in sys.path:
    sys.path.insert(0, str(_vip_parent))

GENERICS = {"G_FIFO_DEPTH": 4}

CLK_PERIOD_NS = 10  # 100 MHz clk_sys in this standalone loop (real DE25 clk_sys is 50 MHz;
                    # every timing here is regbank-divider-driven, not clock-frequency-derived)

# EMIO_MUX_SEL bit layout (ds_emio_mux.vhd + deepskopion_core.vhd headers).
SEL_I2C  = 1 << 0
SEL_UART = 1 << 1
SEL_SPI  = 1 << 2

# i2c_injector / spi_injector CONTROL layout (their .vhd headers).
INJ_CTRL_ENABLE = 0x1


def make_i2c_inj_control(addr: int, enable: bool = True) -> int:
    val = (addr & 0x7F) << 1
    if enable:
        val |= INJ_CTRL_ENABLE
    return val


async def start_clock(dut) -> None:
    # Each split test file runs in its OWN nvc/cocotb session (its own
    # `rr sim run <module>` invocation), so — unlike the old combined
    # file — there is no cross-test global-state hazard here. Still guard
    # against stacking two Clock() drivers if a file ever grows a second
    # @cocotb.test() sharing one module-level import.
    if not getattr(start_clock, "_started", False):
        cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, unit="ns").start())
        start_clock._started = True


async def reset_dut(dut) -> None:
    dut.RST.value = 1
    dut.TIMESTAMP.value = 0
    dut.EMIO_MUX_SEL.value = SEL_I2C | SEL_UART | SEL_SPI  # always route EMIO -> datalogger sources
    dut.STIM_UART_CONTROL.value = 0
    dut.STIM_UART_BAUD_DIV.value = 0
    dut.STIM_UART_DATA.value = 0
    dut.STIM_UART_DATA_VLD.value = 0
    dut.STIM_I2C_CONTROL.value = 0
    dut.STIM_I2C_CLK_DIV.value = 0
    dut.STIM_I2C_DATA.value = 0
    dut.STIM_I2C_DATA_VLD.value = 0
    dut.STIM_SPI_CONTROL.value = 0
    dut.STIM_SPI_CLK_DIV.value = 0
    dut.STIM_SPI_DATA.value = 0
    dut.STIM_SPI_DATA_VLD.value = 0
    dut.STIM_EDGE_CONTROL.value = 0
    dut.STIM_EDGE_SEL.value = 0
    dut.PMOD_EDGE_FALLBACK.value = 0
    dut.I2C_INJ_CONTROL.value = 0
    dut.I2C_INJ_DATAIN.value = 0
    dut.I2C_INJ_DATAIN_VLD.value = 0
    dut.SPI_INJ_CONTROL.value = 0
    dut.SPI_INJ_DATAIN.value = 0
    dut.SPI_INJ_DATAIN_VLD.value = 0
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 5)


async def pulse_write(dut, data_signal, vld_signal, value: int, cycles: int = 3) -> None:
    """Model a regbank per-slot write: DATA lands, VALID pulses for a few
    cycles (DS-T3.2/DS-T2.14 — the real regbank holds VALID high for
    several clk_sys cycles per single AXI write; the DUT edge-detects it)."""
    data_signal.value = value
    vld_signal.value = 1
    await ClockCycles(dut.CLK, cycles)
    vld_signal.value = 0
    await ClockCycles(dut.CLK, 1)


class UartCapture:
    """Manual 8N1 LSB-first decode, one shot. Signals completion through an
    Event (rather than the caller awaiting the cocotb.start_soon() Task
    directly) — see the DS-P2.100 revision commit message: a direct
    Task-join reproducibly hung the NEXT @cocotb.test() when all 9 tests
    shared one nvc session. Each split file now runs its own session, but
    the Event-based shape is kept as the proven-safe idiom."""

    def __init__(self, signal, bit_time_ns: int):
        self.signal = signal
        self.bit_time_ns = bit_time_ns
        self.data: int | None = None
        self.stop_bit: int | None = None
        self.done = Event()

    async def run(self) -> None:
        await FallingEdge(self.signal)  # start bit
        await Timer(self.bit_time_ns // 2, unit="ns")
        assert int(self.signal.value) == 0, "false start bit"
        data = 0
        for i in range(8):
            await Timer(self.bit_time_ns, unit="ns")
            data |= int(self.signal.value) << i
        await Timer(self.bit_time_ns, unit="ns")
        self.data = data
        self.stop_bit = int(self.signal.value)
        self.done.set()


async def signal_event(coro, ev: Event) -> None:
    """Run `coro` in the background and set `ev` on completion instead of
    the caller awaiting the cocotb.start_soon() Task directly — see
    UartCapture's docstring for why a direct Task-join hung the next
    @cocotb.test() in the old shared-session file."""
    await coro
    ev.set()


async def sample_i2c_bit(dut) -> int:
    from cocotb.triggers import RisingEdge
    await RisingEdge(dut.I2C0_BUS_SCL)
    await Timer(1, unit="ns")  # let combinational bus resolution settle
    return int(dut.I2C0_BUS_SDA.value)


def run_stimulus_gen_loop(module: str) -> None:
    """Shared `if __name__ == "__main__"` body — every split test file
    calls this with its own module name so `python test_stimulus_*.py`
    (and rr's underlying invocation) points at the same real
    tb_stimulus_gen_loop.vhd DUT the pre-split file used."""
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[5]
    DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"
    from hw.sim.portable_fifo_sources import olo_fifo_sources

    run_simulation(
        top_level="tb_stimulus_gen_loop",
        module=module,
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
                # FIFO primitive (injector dependency)
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                # I2C responder chain
                f"{DEEPSKOPION_RTL}/datalogger/i2c/i2c_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/i2c_injector.vhd",
                # SPI responder chain
                f"{DEEPSKOPION_RTL}/responder/spi_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/spi_injector.vhd",
                # Real fabric mux
                f"{DEEPSKOPION_RTL}/emio/ds_emio_mux.vhd",
                # DUT under test (DS-P2.100)
                f"{DEEPSKOPION_RTL}/stimulus/ds_stimulus_gen.vhd",
                # Sim wrapper
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_stimulus_gen_loop.vhd",
            ],
        },
    )
