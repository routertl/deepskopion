

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


# entity timestamp_sync is
#   generic (
#     G_TS_ID_WIDTH      : integer                                     := 5;
#     G_TS_DEV_ID        : natural range 0 to (2 ** G_TS_ID_WIDTH) - 1 := 28;
#     G_TIMESTAMP_WIDTH  : natural range 16 to 32                      := 32;
#     G_CONFIG_WIDTH     : integer                                     := 16;
#     G_TIMESTAMP_BIT    : integer                                     := 14; -- 16mstick
#     G_TIMESTAMP_SLICE  : integer                                     := 18;
#     G_TS_ERROR_WIDTH   : integer                                     := 1
#   );
#   port (
#     CLK        : in std_logic;
#     RST        : in std_logic;
#     CONFIG     : in std_logic_vector(G_CONFIG_WIDTH - 1 downto 0);
#     TIMESTAMP  : in std_logic_vector(G_TIMESTAMP_WIDTH - 1 downto 0);
#     -- TS
#     TS_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0) := (others => '0');
#     TS_MSG_VLD : out std_logic;
#     TS_MSG_RDY : in std_logic;
#     TS_ERRORS  : out std_logic_vector(G_TS_ERROR_WIDTH-1 downto 0)
#     --
#   );
# end timestamp_sync;

# Default generics - modify as needed for your test
GENERICS = {
    "G_TS_ID_WIDTH": 5,
    "G_TS_DEV_ID": 28,
    "G_TIMESTAMP_WIDTH": 32,
    "G_CONFIG_WIDTH": 16,
    "G_TIMESTAMP_BIT": 14,
    "G_TS_ERROR_WIDTH": 1
}

def main():
    """Run simulation for timestamp_sync"""
    run_simulation(
        top_level="timestamp_sync",
        module="test_timestamp_sync",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/timestamp_sync/timestamp_sync.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )

@cocotb.test()
@requires("TS-REQ-50")
async def test_reset_determinism(dut):
    """TS-REQ-50: post-reset, TS_MSG_VLD stays low while TIMESTAMP
    is held steady (no slice-bit transitions).
    """
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())

    # Hold reset active (active-high)
    dut.RST.value = 1
    dut.TIMESTAMP.value = 0
    dut.CONFIG.value = 0
    dut.TS_MSG_RDY.value = 1
    await ClockCycles(dut.CLK, 8)

    assert int(dut.TS_MSG_VLD.value) == 0, (
        f"TS_MSG_VLD must be 0 under RST, got {int(dut.TS_MSG_VLD.value)}"
    )

    # Release reset; TIMESTAMP held steady — no slice transition.
    dut.RST.value = 0
    await RisingEdge(dut.CLK)
    await ClockCycles(dut.CLK, 200)

    assert int(dut.TS_MSG_VLD.value) == 0, (
        "TS_MSG_VLD pulsed post-reset with TIMESTAMP steady"
    )

    dut._log.info("TS-REQ-50 reset-determinism check passed.")

if __name__ == "__main__":
    main()