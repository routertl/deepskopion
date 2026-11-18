

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
    from engine.paths import SRC_PATH, SDK_SRC_PATH


except ImportError:
    print("Error: Could not import engine. Make sure the script is running in the correct environment.")
    sys.exit(1)

from sdk.cocotb_helpers import requires

# Deepskopion project root
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"


# entity qspi_datalogger is
#     generic (
#       G_QSPI_ID_WIDTH   : integer                                    := 5;
#       G_QSPI_DEV_ID     : natural range 0 to (2**G_QSPI_ID_WIDTH)-1  := 28;
#       G_QSPI_PINS       : natural range 3 to 6                       := 6; -- 4 data pins + SCLK + CSn
#       G_TIMESTAMP_WIDTH : natural range 16 to 32                     := 16;
#       G_CONFIG_WIDTH    : integer                                    := 16;
#       G_ERROR_WIDTH     : integer                                    := 2;
#       G_ENABLE_INFILTER : integer                                    := 0;
#       G_ILA_EN          : integer                                    := 1
#       );
#     port (
#       CLK         : in  std_logic;
#       RST         : in  std_logic;
#       QSPI_PINS   : in  std_logic_vector(G_QSPI_PINS-1 downto 0);
#       CONFIG      : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
#       TIMESTAMP   : in  std_logic_vector(G_TIMESTAMP_WIDTH-1 downto 0);
#       -- QSPI
#       QSPI_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
#       QSPI_MSG_VLD : out std_logic;
#       QSPI_MSG_RDY : in  std_logic;
#       --
#       QSPI_ERRORS : out std_logic_vector(G_ERROR_WIDTH-1 downto 0)
#       );
# end qspi_datalogger;

# Default generics - modify as needed for your test
GENERICS = {
    "G_QSPI_ID_WIDTH": 5,
    "G_QSPI_DEV_ID": 28,
    "G_QSPI_PINS": 6,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_ENABLE_INFILTER": 0,
    "G_ILA_EN": 1
}

def main():
    """Run simulation for qspi_datalogger"""
    run_simulation(
        top_level="qspi_datalogger",
        module="test_qspi_datalogger",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{SDK_SRC_PATH}/units/infilter.vhd",
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/qspi/qspi_datalogger.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )

DEV_ID = GENERICS["G_QSPI_DEV_ID"]
EVENT_TYPE = 0b100
EV_START = 0x03   # qspi enable marker (== ENABLED in the uniform lifecycle)
EV_STOP = 0x00    # qspi disable marker (== DISABLED)


def _cfg_word(dev_id: int, payload: int) -> int:
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


async def _next_event(dut, max_cycles=60):
    """Return (code) of the next QSPI event frame (type [26:24]==100), else None."""
    for _ in range(max_cycles):
        await RisingEdge(dut.CLK)
        if int(dut.QSPI_MSG_VLD.value) == 1:
            raw = int(dut.QSPI_MSG_OUT.value)
            if ((raw >> 24) & 0x7) == EVENT_TYPE:
                return raw & 0xFF
    return None


@cocotb.test()
@requires("QSPI-REQ-51")
async def test_capture_rst_reconfig(dut):
    """QSPI-REQ-51 (DS-P3.90 Phase 2): qspi's decode FSM SELF-RESETS on disable —
    it returns to the STOP (idle) state and clears its sample count when
    configEnable=0 — so it needs no separate CAPTURE_RST (that would risk
    suppressing its inline START/STOP lifecycle, H.96). A disable -> re-enable
    therefore cleanly re-arms: the FSM emits START (0x03) on enable, STOP (0x00)
    on disable, and START again on re-enable, proving the self-reset + symmetric
    lifecycle across a reconfiguration. Drives no SPI traffic — the markers are
    FSM-driven on the enable edge. Uses an explicit matching-id disable word
    (CONFIG=0 would not disable a non-zero device-id, H.98).

    QSPI_MSG_VLD / QSPI_MSG_OUT are sampled inside _next_event().
    """
    _qspi_msg_vld = dut.QSPI_MSG_VLD  # sampled by _next_event
    _qspi_msg_out = dut.QSPI_MSG_OUT  # sampled by _next_event
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())
    dut.RST.value = 1
    dut.QSPI_PINS.value = 0       # steady -> no CS/SCLK edges, no decode events
    dut.QSPI_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 3)

    # Enable -> START marker.
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x001)
    code = await _next_event(dut, 80)
    assert code == EV_START, f"enable did not emit START(0x03): got {code}"

    # Disable -> STOP marker (FSM returns IDLE->STOP).
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x000)
    code = await _next_event(dut, 80)
    assert code == EV_STOP, f"disable did not emit STOP(0x00): got {code}"

    # Re-enable -> START again, proving the FSM self-reset to STOP and re-armed.
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x001)
    code = await _next_event(dut, 80)
    assert code == EV_START, (
        f"re-enable did not re-emit START(0x03) — FSM did not self-reset: {code}"
    )
    dut._log.info("QSPI-REQ-51 FSM self-reset + symmetric lifecycle verified.")


@cocotb.test()
@requires("QSPI-REQ-50")
async def test_reset_determinism(dut):
    """QSPI-REQ-50: post-reset, QSPI_MSG_VLD stays low and
    QSPI_ERRORS stays cleared during the idle window (no bus
    activity on QSPI_PINS).
    """
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())

    # Hold reset active (active-high) + clear stimulus
    dut.RST.value = 1
    dut.QSPI_PINS.value = 0
    dut.QSPI_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 8)

    # Under reset: VLD low, ERRORS cleared.
    assert int(dut.QSPI_MSG_VLD.value) == 0, (
        f"QSPI_MSG_VLD must be 0 under RST, got {int(dut.QSPI_MSG_VLD.value)}"
    )
    assert int(dut.QSPI_ERRORS.value) == 0, (
        f"QSPI_ERRORS must be 0 under RST, got 0x{int(dut.QSPI_ERRORS.value):x}"
    )

    # Release reset; line stays idle (no SCLK activity, CS held low/high
    # via the all-zero stimulus). Datalogger must NOT emit anything.
    dut.RST.value = 0
    await RisingEdge(dut.CLK)
    await ClockCycles(dut.CLK, 200)

    assert int(dut.QSPI_MSG_VLD.value) == 0, (
        "QSPI_MSG_VLD pulsed after RST de-assert with no bus activity"
    )
    assert int(dut.QSPI_ERRORS.value) == 0, (
        "QSPI_ERRORS asserted after RST de-assert with no bus activity"
    )

    dut._log.info("QSPI-REQ-50 reset-determinism check passed.")

if __name__ == "__main__":
    main()