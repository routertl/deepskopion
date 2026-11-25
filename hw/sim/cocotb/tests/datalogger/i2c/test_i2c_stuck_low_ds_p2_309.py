# rr unknown-llm (AI-generated)
# tests/datalogger/i2c/test_i2c_stuck_low_ds_p2_309.py
"""DS-P2.309: what the I2C datalogger reports when its input lines sit LOW.

DE25 C3 run 1 (2026-11-15) measured a 40.5/s I2C STRETCH ERROR storm with the
logger enabled and its EMIO mux bit clear, i.e. reading the GPIO_D header pins
with nothing attached and no pull resistor. When the mux then switched the
logger to the internal idle-high stimulus lines, the first event was a single
STOP_ERROR before the real START.

These tests drive ``i2c_datalogger`` directly, so the SCL-stuck-low timeout is
scaled by ``G_CLK_FREQ_HZ`` (C_SCL_TIMEOUT_MS = 25 ms of CLK cycles):
100 kHz -> 2500 cycles per timeout instead of 2.5 M.

Run with:
    SIM=nvc python test_i2c_stuck_low_ds_p2_309.py
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

_ds_root = Path(__file__).resolve().parents[6]
_vip_parent = _ds_root / "hw" / "sim"
if str(_vip_parent) not in sys.path:
    sys.path.insert(0, str(_vip_parent))

from sdk.cocotb_helpers import requires  # noqa: E402
from vip.datalogger.i2c import I2cMsgDecoder, I2cEvent, I2cDataloggerCfg, pack_config  # noqa: E402

DEV_ID = 28
CLK_FREQ_HZ = 100_000                    # scaled clock for the timeout only
TIMEOUT_CYCLES = (CLK_FREQ_HZ // 1000) * 25   # = 2500, the RTL's C_SCL_TIMEOUT_CYCLES

GENERICS = {
    "G_I2C_ID_WIDTH": 5,
    "G_I2C_DEV_ID": DEV_ID,
    "G_TIMESTAMP_WIDTH": 16,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_ILA_EN": 0,
    "G_CLK_FREQ_HZ": CLK_FREQ_HZ,
}

# I2C_PINS = SDA(1) & SCL(0)
IDLE_HIGH = 0b11
BOTH_LOW = 0b00
SCL_LOW_SDA_HIGH = 0b10


async def _start(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.I2C_MSG_RDY.value = 1
    dut.I2C_PINS.value = IDLE_HIGH
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 5)


def _monitor(dut, sink):
    decoder = I2cMsgDecoder(device_id=DEV_ID)

    async def run():
        while True:
            await RisingEdge(dut.CLK)
            if dut.I2C_MSG_VLD.value == 1 and dut.I2C_MSG_RDY.value == 1:
                msg = decoder.decode(int(dut.I2C_MSG_OUT.value))
                sink.append(msg.event_name if msg.is_event else f"BYTE_{msg.data:02X}")
    return cocotb.start_soon(run())


async def _enable_on_lines(dut, pins):
    """Put the lines in state *pins*, then enable the logger on them."""
    dut.I2C_PINS.value = pins
    await ClockCycles(dut.CLK, 10)
    dut.CONFIG.value = pack_config(I2cDataloggerCfg(device_id=DEV_ID, enable=True))


@cocotb.test()
@requires("I2C-REQ-14")
async def test_scl_low_gives_one_stretch_error_per_timeout(dut):
    """SCL held low with the logger enabled: one STRETCH_ERROR per 25 ms of
    SCL low, no START, no byte. 4 timeouts + margin -> exactly 4."""
    await _start(dut)
    events = []
    _monitor(dut, events)
    await _enable_on_lines(dut, BOTH_LOW)
    await ClockCycles(dut.CLK, 4 * TIMEOUT_CYCLES + TIMEOUT_CYCLES // 2)
    assert events[0] == "ENABLED", events
    rest = events[1:]
    assert rest == ["STRETCH_ERROR"] * 4, (
        f"expected exactly 4 STRETCH_ERROR (one per {TIMEOUT_CYCLES} cycles of SCL low), got {rest}")


@cocotb.test()
@requires("I2C-REQ-14")
async def test_idle_high_lines_give_no_event(dut):
    """Control: the same window with both lines idle high gives ENABLED only."""
    await _start(dut)
    events = []
    _monitor(dut, events)
    await _enable_on_lines(dut, IDLE_HIGH)
    await ClockCycles(dut.CLK, 4 * TIMEOUT_CYCLES + TIMEOUT_CYCLES // 2)
    assert events == ["ENABLED"], events


async def _switch_after_storm(dut, low_state):
    await _start(dut)
    events = []
    _monitor(dut, events)
    await _enable_on_lines(dut, low_state)
    await ClockCycles(dut.CLK, 2 * TIMEOUT_CYCLES + TIMEOUT_CYCLES // 2)
    n_storm = len(events)
    dut.I2C_PINS.value = IDLE_HIGH          # the mux switch: both lines at once
    await ClockCycles(dut.CLK, TIMEOUT_CYCLES)
    return events[:n_storm], events[n_storm:]


@cocotb.test()
@requires("I2C-REQ-15")
async def test_both_low_then_idle_high_gives_one_stop_error(dut):
    """SCL and SDA low, then both lines switched to idle high in one cycle:
    the storm stops and the switch is reported as exactly one STOP_ERROR."""
    before, after = await _switch_after_storm(dut, BOTH_LOW)
    assert before == ["ENABLED", "STRETCH_ERROR", "STRETCH_ERROR"], before
    assert after == ["STOP_ERROR"], after


@cocotb.test()
@requires("I2C-REQ-15")
async def test_scl_low_sda_high_then_idle_high_gives_no_event(dut):
    """SCL low with SDA already high: the switch raises only SCL, which is not
    a STOP condition, so the storm stops with no event."""
    before, after = await _switch_after_storm(dut, SCL_LOW_SDA_HIGH)
    assert before == ["ENABLED", "STRETCH_ERROR", "STRETCH_ERROR"], before
    assert after == [], after


if __name__ == "__main__":
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
    DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

    run_simulation(
        top_level="i2c_datalogger",
        module=Path(__file__).stem,
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries={
            "tich": [
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/i2c/i2c_datalogger.vhd",
            ],
        },
    )
