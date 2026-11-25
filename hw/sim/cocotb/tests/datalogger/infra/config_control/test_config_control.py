# rr unknown-llm (AI-generated)


import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, ClockCycles

# Locate routertl SDK cocotb dir via the pip-installed routertl_core
# (DS-P2.29 fallout — old shim walked up the tree for an `engine/`
# folder that no longer exists post-pip-decouple).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
    from sdk.cocotb_helpers import requires
    from engine.paths import SRC_PATH, SDK_SRC_PATH


except ImportError:
    print("Error: Could not import engine. Make sure the script is running in the correct environment.")
    sys.exit(1)

# Deepskopion project root
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"


# entity config_control is
#     generic (
#       G_DEV_ID_WIDTH : integer                                  := 5;
#       G_DEV_ID       : natural range 0 to (2**G_DEV_ID_WIDTH)-1 := 24;
#       G_CONFIG_WIDTH : integer                                  := 16;
#       G_SIM_EN       : integer                                  := 0; -- 1 for simulation, 0 for synthesis
#       G_ILA_EN       : integer                                  := 1
#       );
#     port (
#       CLK         : in  std_logic;
#       RST         : in  std_logic;
#       CONFIG      : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
#       -- LEVEL
#       CONFIG_OUT  : out std_logic_vector(G_CONFIG_WIDTH-G_DEV_ID_WIDTH-1 downto 0)
#       );
# end config_control;

# Default generics - modify as needed for your test
GENERICS = {
    "G_DEV_ID_WIDTH": 5,
    "G_DEV_ID": 24,
    "G_CONFIG_WIDTH": 16,
    "G_SIM_EN": 0
}

def main():
    """Run simulation for config_control"""
    run_simulation(
        top_level="config_control",
        module="test_config_control",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
        # DS-P2.138: the three files above ARE the whole closure (ieee + tich
        # datalogger packages only). Declaring it isolated lets `rr sim run`
        # skip the ROOT-project closure preflight and package-generation gate
        # (sdk/cli/commands/sim.py _module_declares_isolated_hdl), so a fresh
        # tree's unrelated gaps — the ungenerated REA rr_rea_build_id_pkg, the
        # unsynced olo libs — can no longer block this unit test.
        include_project_packages=False,
    )

@cocotb.test()
@requires("CC-REQ-50")
async def test_reset_determinism(dut):
    """CC-REQ-50: post-reset, CONFIG_OUT stays all-zero while
    CONFIG is held at zero (no matching device-id write).
    """
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())

    # Hold reset active (active-high)
    dut.RST.value = 1
    dut.CONFIG.value = 0
    await ClockCycles(dut.CLK, 8)

    assert int(dut.CONFIG_OUT.value) == 0, (
        f"CONFIG_OUT must be 0 under RST, got 0x{int(dut.CONFIG_OUT.value):x}"
    )

    # Release reset; CONFIG held at zero — no matching device-id write.
    dut.RST.value = 0
    await RisingEdge(dut.CLK)
    await ClockCycles(dut.CLK, 200)

    assert int(dut.CONFIG_OUT.value) == 0, (
        f"CONFIG_OUT changed post-reset with CONFIG=0, got 0x{int(dut.CONFIG_OUT.value):x}"
    )

    dut._log.info("CC-REQ-50 reset-determinism check passed.")


DEV_ID = GENERICS["G_DEV_ID"]   # 24


def _cfg(dev, payload):
    return ((dev & 0x1F) << 11) | (payload & 0x7FF)


@cocotb.test()
@requires("CC-REQ-51")
async def test_enable_latches_regardless_of_config_timing(dut):
    """CC-REQ-51 (DS-P3.96): a matching-device CONFIG write with ENABLE=1 MUST
    latch CONFIG_OUT(0)=1; ENABLE=0 must un-latch; a different device's CONFIG
    must not disturb this device. New enable/disable/isolation coverage (the
    block previously had only the CC-REQ-50 reset test).

    NB: DS-P3.96's underlying defect was a SIM-ONLY delta skew (newConfEvent_r
    from direct CONFIG vs deviceIdMatch_r from a delta-lagged concurrent signal)
    that only manifests under the racy multi-coroutine scheduling of an
    INTEGRATION bench — it does NOT reproduce with clean unit-test CONFIG writes,
    so this test passes on the pre-fix RTL too. The skew regression lives in
    test_datalogger_gather_i2c.py (fails pre-fix, passes post-fix). This test guards
    the latch PROPERTY the fix makes hold by construction. Hardware was always
    correct (timing-closed logic has no delta race)."""
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())
    dut.RST.value = 1
    dut.CONFIG.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)

    # Enable this device — step CONFIG WITHOUT a preceding edge-sync (the skew-
    # prone pattern from DS-P3.32). The latch must still fire.
    dut.CONFIG.value = _cfg(DEV_ID, 0x001)        # ENABLE=1
    await ClockCycles(dut.CLK, 10)
    assert int(dut.CONFIG_OUT.value) & 1 == 1, (
        "CC-REQ-51 regression: ENABLE did not latch (config_control delta-skew). "
        f"CONFIG_OUT=0x{int(dut.CONFIG_OUT.value):x}")
    assert int(dut.CAPTURE_RST.value) == 0, "CAPTURE_RST must release when enabled"

    # A different device's CONFIG must NOT disturb this device's latched state.
    dut.CONFIG.value = _cfg((DEV_ID + 1) & 0x1F, 0x000)
    await ClockCycles(dut.CLK, 10)
    assert int(dut.CONFIG_OUT.value) & 1 == 1, (
        "CC-REQ-51 isolation: another device's CONFIG cleared our ENABLE")

    # Disable this device — ENABLE=0 must latch and re-assert CAPTURE_RST.
    dut.CONFIG.value = _cfg(DEV_ID, 0x000)
    await ClockCycles(dut.CLK, 10)
    assert int(dut.CONFIG_OUT.value) & 1 == 0, "ENABLE=0 did not latch (disable)"
    assert int(dut.CAPTURE_RST.value) == 1, "CAPTURE_RST must assert when disabled"

    dut._log.info("CC-REQ-51 enable-latch (timing-robust) + isolation + disable: PASS")


@cocotb.test()
@requires("CC-REQ-51")
async def test_enable_latches_when_config_steps_on_clock_edge(dut):
    """CC-REQ-51: drive the ENABLE CONFIG step from a coroutine fired ON the
    rising edge and assert it still latches (a second CONFIG-timing variant of
    the latch property; see the note on the integration-level skew repro above)."""
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())
    dut.RST.value = 1
    dut.CONFIG.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0

    async def _step_on_edge():
        await RisingEdge(dut.CLK)
        dut.CONFIG.value = _cfg(DEV_ID, 0x001)
    cocotb.start_soon(_step_on_edge())
    await ClockCycles(dut.CLK, 12)

    assert int(dut.CONFIG_OUT.value) & 1 == 1, (
        "CC-REQ-51 (edge-stepped): ENABLE did not latch — "
        f"CONFIG_OUT=0x{int(dut.CONFIG_OUT.value):x}")
    dut._log.info("CC-REQ-51 edge-stepped enable-latch: PASS")


if __name__ == "__main__":
    main()