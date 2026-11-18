"""DS-P3.117 — edge_datalogger max-frequency CONFIRMATION gate (EDGE-REQ-64).

The glitch floor (EDGE-REQ-54) rejects an interval shorter than
G_MAXFREQ_MIN_INTERVAL. A runt pulse that is wider than the floor (and so also
survives the gather's glitch_filter) still splits one half-period into three
short intervals, and a single-interval tracker latches the shortest of them as
the line's max frequency until the host re-arms. G_MAXFREQ_CONFIRM = N offers
the LARGEST of the last N consecutive intervals instead, so with N = 4 every
window holds a full half-period and the runt cannot raise the max.

This module elaborates the DUT with G_MAXFREQ_CONFIRM = 4. The EDGE-REQ-53..58
benches in test_edge_datalogger.py keep N = 1 (the single-interval tracker the
gather instantiates today), so both settings are simulated.
"""

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: Could not import engine. Make sure the script is running in the correct environment.")
    sys.exit(1)

from sdk.cocotb_helpers import requires
from hw.sim.portable_fifo_sources import olo_fifo_sources

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

GENERICS = {
    "G_ED_ID_WIDTH": 5,
    "G_ED_DEV_ID": 28,
    "G_TIMESTAMP_WIDTH": 16,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_ENABLE_INFILTER": 0,
    "G_ILA_EN": 0,
    "G_MAXFREQ_WIDTH": 24,
    "G_MAXFREQ_MIN_INTERVAL": 4,
    "G_MAXFREQ_CONFIRM": 4,
}

# Hard-coded mirrors of the generics (ROUTERTL-002).
MAXFREQ_MIN_INTERVAL = 4
MAXFREQ_CONFIRM = 4
MAXFREQ_ALLONES = (1 << 24) - 1

# device_id=28 in CONFIG[15:11], control_code "01" (enable edge capturing).
_CFG_ENABLE = (28 << 11) | 0b01


def main():
    run_simulation(
        top_level="edge_datalogger",
        module="test_edge_datalogger_maxfreq_confirm",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_ROOT}/libs/routertl/edge_detector/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/nonstd/edge_datalogger/edge_datalogger.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )


async def _reset_and_enable(dut):
    """Reset, wait out the FIFO reset-busy window, enable edge capturing."""
    dut.RST.value = 1
    dut.EDGE_IN.value = 0
    dut.ED_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 64)
    dut.CONFIG.value = _CFG_ENABLE
    await ClockCycles(dut.CLK, 4)


async def _drive_intervals(dut, intervals):
    """Toggle EDGE_IN, then wait each interval in turn before the next toggle.
    The first toggle opens the window; each following one closes an interval of
    exactly the clocks waited. Returns every interval ED_MAX_NEW latched."""
    latched = []
    for clocks in intervals:
        dut.EDGE_IN.value = 1 - int(dut.EDGE_IN.value)
        for _ in range(clocks):
            await RisingEdge(dut.CLK)
            if int(dut.ED_MAX_NEW.value) == 1:
                latched.append(int(dut.ED_MAX_INTERVAL.value))
    return latched


@cocotb.test()
@requires("EDGE-REQ-64")
async def test_maxfreq_runt_above_floor_cannot_raise_max(dut):
    """EDGE-REQ-64: a runt wider than the floor, in the middle of a half-period,
    must not raise the max. A genuinely faster line that lasts N intervals must."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await _reset_and_enable(dut)
    assert int(dut.ED_MAX_INTERVAL.value) == MAXFREQ_ALLONES

    # A slow line, H = 40 clocks: 6 toggles close 5 intervals (>= N).
    H_SLOW = 40
    latched = await _drive_intervals(dut, [H_SLOW] * 6)
    assert latched == [H_SLOW], f"setup: slow line latched {latched}, want [40]"
    assert int(dut.ED_MAX_INTERVAL.value) == H_SLOW

    # One runt pulse inside the next half-period: 18 + 6 + 16 = 40. Every piece
    # is at or above the floor (4), so the floor does not reject any of them; a
    # single-interval tracker latches 6 here (a 6.7x faster false max).
    RUNT_PIECES = [18, 6, 16]
    assert min(RUNT_PIECES) >= MAXFREQ_MIN_INTERVAL
    latched = await _drive_intervals(dut, RUNT_PIECES + [H_SLOW] * 5)
    after = int(dut.ED_MAX_INTERVAL.value)
    assert after == H_SLOW, (
        f"a runt above the floor raised the max: ED_MAX_INTERVAL={after}, "
        f"must stay {H_SLOW}")
    assert latched == [], f"ED_MAX_NEW pulsed for the runt, latched {latched}"

    # Genuine faster activity of only N-1 = 3 intervals is not yet confirmed...
    H_FAST = 10
    latched = await _drive_intervals(dut, [H_FAST] * (MAXFREQ_CONFIRM - 1) + [H_SLOW])
    assert latched == [], (
        f"{MAXFREQ_CONFIRM - 1} fast intervals latched {latched}; the window "
        f"needs {MAXFREQ_CONFIRM}")
    assert int(dut.ED_MAX_INTERVAL.value) == H_SLOW

    # ...and the N-th consecutive one confirms it: the line really is faster.
    latched = await _drive_intervals(dut, [H_FAST] * (MAXFREQ_CONFIRM + 2))
    assert latched == [H_FAST], (
        f"a line at H={H_FAST} for {MAXFREQ_CONFIRM + 2} intervals latched "
        f"{latched}, want [{H_FAST}]")
    assert int(dut.ED_MAX_INTERVAL.value) == H_FAST

    # Host re-arm (rising edge of CONFIG[2]) clears the latch and reopens the
    # window. After it, N-1 = 3 closed intervals confirm nothing: the gate counts
    # intervals since the window opened, never stale ones from before the re-arm.
    dut.CONFIG.value = _CFG_ENABLE | (1 << 2)
    await ClockCycles(dut.CLK, 6)
    assert int(dut.ED_MAX_INTERVAL.value) == MAXFREQ_ALLONES, "re-arm did not clear"
    dut.CONFIG.value = _CFG_ENABLE
    await ClockCycles(dut.CLK, 3)
    latched = await _drive_intervals(dut, [H_SLOW] * MAXFREQ_CONFIRM)
    assert latched == [] and int(dut.ED_MAX_INTERVAL.value) == MAXFREQ_ALLONES, (
        f"{MAXFREQ_CONFIRM - 1} intervals after re-arm latched {latched} / "
        f"{int(dut.ED_MAX_INTERVAL.value):#x}; the gate needs {MAXFREQ_CONFIRM}")
    latched = await _drive_intervals(dut, [H_SLOW] * 2)
    assert latched == [H_SLOW], f"after re-arm the slow line latched {latched}, want [40]"
    dut._log.info("EDGE-REQ-64: runt above floor ignored; confirmed fast line latched.")


if __name__ == "__main__":
    main()
