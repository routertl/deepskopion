

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
    from sdk.cocotb_helpers import requires


except ImportError:
    print("Error: Could not import engine. Make sure the script is running in the correct environment.")
    sys.exit(1)

# Deepskopion project root
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"


# entity level_datalogger is
#     generic (
#       G_LEVEL_ID_WIDTH  : integer                                    := 5;
#       G_LEVEL_DEV_ID    : natural range 0 to (2**G_LEVEL_ID_WIDTH)-1 := 24;
#       G_TIMESTAMP_WIDTH : natural range 16 to 32                     := 16;
#       G_CONFIG_WIDTH    : integer                                    := 16;
#       G_ERROR_WIDTH     : integer                                    := 2;
#       G_SIM_EN          : integer                                    := 0; -- 1 for simulation, 0 for synthesis
#       G_ILA_EN          : integer                                    := 1
#       );
#     port (
#       CLK         : in  std_logic;
#       RST         : in  std_logic;
#       LEVEL_PINS  : in  std_logic_vector(C_LEVEL_INJ_PINS-1 downto 0);
#       CONFIG      : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
#       TIMESTAMP   : in  std_logic_vector(G_TIMESTAMP_WIDTH-1 downto 0);
#       -- LEVEL
#       LEVEL_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
#       LEVEL_MSG_VLD : out std_logic;
#       LEVEL_MSG_RDY : in  std_logic;
#       --
#       LEVEL_ERRORS : out std_logic_vector(G_ERROR_WIDTH-1 downto 0)
#       );
# end level_datalogger;

# Default generics - modify as needed for your test
GENERICS = {
    "G_LEVEL_ID_WIDTH": 5,
    "G_LEVEL_DEV_ID": 24,
    "G_TIMESTAMP_WIDTH": 16,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_SIM_EN": 0
}

def main():
    """Run simulation for level_datalogger"""
    run_simulation(
        top_level="level_datalogger",
        module="test_level_datalogger",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/nonstd/level_datalogger/level_datalogger.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )

@cocotb.test()
@requires("LEVEL-REQ-50")
async def test_reset_determinism(dut):
    """LEVEL-REQ-50: post-reset, LEVEL_MSG_VLD stays low and
    LEVEL_ERRORS is zero (architecture hardcodes errors to 0).
    """
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())

    # Hold reset active (active-high) + clear stimulus
    dut.RST.value = 1
    dut.LEVEL_PINS.value = 0
    dut.LEVEL_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 8)

    assert int(dut.LEVEL_MSG_VLD.value) == 0, (
        f"LEVEL_MSG_VLD must be 0 under RST, got {int(dut.LEVEL_MSG_VLD.value)}"
    )
    assert int(dut.LEVEL_ERRORS.value) == 0, (
        f"LEVEL_ERRORS must be 0 under RST, got 0x{int(dut.LEVEL_ERRORS.value):x}"
    )

    # Release reset; LEVEL_PINS held steady — no level change.
    dut.RST.value = 0
    await RisingEdge(dut.CLK)
    await ClockCycles(dut.CLK, 200)

    assert int(dut.LEVEL_MSG_VLD.value) == 0, (
        "LEVEL_MSG_VLD pulsed post-reset with LEVEL_PINS steady"
    )
    assert int(dut.LEVEL_ERRORS.value) == 0, (
        "LEVEL_ERRORS asserted post-reset; the architecture hardcodes it to 0"
    )

    dut._log.info("LEVEL-REQ-50 reset-determinism check passed.")

DEV_ID = GENERICS["G_LEVEL_DEV_ID"]
EVENT_TYPE = 0b100
ENABLED = 0x03
DISABLED = 0x00


def _cfg_word(dev_id: int, payload: int) -> int:
    """DATALOGGER_CONFIG = [device_id(15:11) | payload(10:0)]."""
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


def _frame(code: int, ts: int = 0) -> int:
    return (DEV_ID << 27) | (EVENT_TYPE << 24) | ((ts & 0xFFFF) << 8) | (code & 0xFF)


async def _capture_next_vld(dut, max_cycles=20):
    """Return LEVEL_MSG_OUT on the next cycle LEVEL_MSG_VLD is high, else None."""
    for _ in range(max_cycles):
        await RisingEdge(dut.CLK)
        if int(dut.LEVEL_MSG_VLD.value) == 1:
            return int(dut.LEVEL_MSG_OUT.value)
    return None


async def _next_payload(dut, max_cycles=40):
    """Capture next LEVEL frame whose type field [26:24] == 0 (level payload)."""
    for _ in range(max_cycles):
        await RisingEdge(dut.CLK)
        if int(dut.LEVEL_MSG_VLD.value) == 1:
            raw = int(dut.LEVEL_MSG_OUT.value)
            if ((raw >> 24) & 0x7) == 0:   # level inject payload code "000"
                return raw
    return None


@cocotb.test()
@requires("LEVEL-REQ-52")
async def test_capture_rst_reconfig(dut):
    """LEVEL-REQ-52 (DS-P3.90 Phase 2): disabling the datalogger (ENABLE=0) flushes
    the decode regs via config_control.CAPTURE_RST, so a re-enable with UNCHANGED
    pins re-emits the current level (levelPins_r was cleared). Proves the
    per-datalogger reset reaches the decode core and is SW-reachable. WITHOUT the
    flush, levelPins_r would still hold 0xAA and the unchanged-pins re-enable
    would emit nothing.

    LEVEL_MSG_OUT / LEVEL_MSG_VLD are sampled inside _next_payload(); the locals
    below make the references explicit for the @requires signal-reference check.
    """
    _level_msg_out = dut.LEVEL_MSG_OUT  # sampled by _next_payload
    _level_msg_vld = dut.LEVEL_MSG_VLD  # sampled by _next_payload
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())
    dut.RST.value = 1
    dut.LEVEL_PINS.value = 0xAA
    dut.LEVEL_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 3)

    # Enable -> first payload reports the current level (0xAA).
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x001)
    raw = await _next_payload(dut, 30)
    assert raw is not None and (raw & 0xFF) == 0xAA, (
        f"first payload not 0xAA: {None if raw is None else hex(raw)}"
    )

    # Disable -> CAPTURE_RST flushes levelPins_r / pinChange_r / msgVld_r.
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x000)
    await ClockCycles(dut.CLK, 10)

    # Re-enable with pins UNCHANGED (still 0xAA). Because levelPins_r was
    # cleared by CAPTURE_RST, the 0 != 0xAA comparison fires and the current
    # level re-emits — the observable proof the decode reset reached the core.
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x001)
    raw = await _next_payload(dut, 30)
    assert raw is not None and (raw & 0xFF) == 0xAA, (
        "re-enable did not re-emit the current level — CAPTURE_RST did not flush "
        f"levelPins_r: {None if raw is None else hex(raw)}"
    )
    dut._log.info("LEVEL-REQ-52 CAPTURE_RST reconfig flush verified.")


@cocotb.test()
@requires("LEVEL-REQ-51")
async def test_lifecycle_enable_disable(dut):
    """LEVEL-REQ-51 (DS-P3.90 Phase 1): the uniform datalogger_lifecycle emits a
    symmetric ENABLED frame on the enable edge and DISABLED on the disable edge,
    with the level datalogger's frame layout (dev | type=100 | ts | code).

    LEVEL_MSG_OUT / LEVEL_MSG_VLD are sampled inside _capture_next_vld(); the
    locals below make the references explicit for the @requires signal-reference
    check (coverage-map layer-1.5).
    """
    _level_msg_out = dut.LEVEL_MSG_OUT  # sampled by _capture_next_vld
    _level_msg_vld = dut.LEVEL_MSG_VLD  # sampled by _capture_next_vld
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())
    dut.RST.value = 1
    dut.LEVEL_PINS.value = 0          # steady -> no payload interference
    dut.LEVEL_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 3)

    # Enable device 24 -> exactly one ENABLED lifecycle frame.
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x001)
    got = await _capture_next_vld(dut, 12)
    assert got == _frame(ENABLED), (
        f"ENABLED frame wrong: {got:#010x} != {_frame(ENABLED):#010x}"
    )
    dut._log.info(f"ENABLED frame OK: {got:#010x}")

    # ...and silence while enabled + pins steady.
    quiet = await _capture_next_vld(dut, 8)
    assert quiet is None, f"spurious frame while enabled+steady: {quiet:#010x}"

    # Disable device 24 (explicit matching-id word, payload bit0=0).
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x000)
    got = await _capture_next_vld(dut, 12)
    assert got == _frame(DISABLED), (
        f"DISABLED frame wrong: {got:#010x} != {_frame(DISABLED):#010x}"
    )
    dut._log.info(f"DISABLED frame OK: {got:#010x}")
    dut._log.info("LEVEL-REQ-51 lifecycle enable/disable check passed.")


@cocotb.test()
@requires("LEVEL-REQ-10")
async def test_timestamp_is_change_time_not_delivery(dut):
    """LEVEL-REQ-10 (DS-T3.11): the level payload must stamp WHEN the level
    changed, not when the arbiter finally accepted it. Hold TIMESTAMP=V, change a
    pin under backpressure (LEVEL_MSG_RDY=0), advance TIMESTAMP=W during the
    stall, then release RDY — the frame must carry V (and the changed pins), not
    the post-change W. Before the fix, msg was built from live TIMESTAMP/LEVEL_PINS
    sampled at delivery, so it carried W.
    """
    _level_msg_out = dut.LEVEL_MSG_OUT  # sampled by _next_payload
    _level_msg_vld = dut.LEVEL_MSG_VLD  # sampled by _next_payload
    V, W = 0x1111, 0x2222
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())
    dut.RST.value = 1
    dut.LEVEL_PINS.value = 0
    dut.LEVEL_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = V
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 3)

    # enable (pins=0 → no payload) and let the ENABLED lifecycle frame drain
    dut.CONFIG.value = _cfg_word(DEV_ID, 0x001)
    await ClockCycles(dut.CLK, 12)

    # backpressure ON, then change a pin while TIMESTAMP=V → capture (V, 0x5A)
    dut.LEVEL_MSG_RDY.value = 0
    await ClockCycles(dut.CLK, 2)
    dut.LEVEL_PINS.value = 0x5A
    await ClockCycles(dut.CLK, 3)
    dut.TIMESTAMP.value = W                 # advance time during the stall
    await ClockCycles(dut.CLK, 15)
    dut.LEVEL_MSG_RDY.value = 1             # release the arbiter

    raw = await _next_payload(dut, 30)
    assert raw is not None, "no level payload after releasing backpressure"
    assert (raw & 0xFF) == 0x5A, f"pins got 0x{raw & 0xFF:02x} != 0x5A (raw={raw:#010x})"
    ts = (raw >> 8) & 0xFFFF
    assert ts == V, (
        f"level timestamp is DELIVERY-time, not CHANGE-time: 0x{ts:04x} != 0x{V:04x} "
        f"(reflects post-change 0x{W:04x}) — DS-T3.11 (raw={raw:#010x})")
    dut._log.info(f"LEVEL change-time stamp OK raw={raw:#010x}")


if __name__ == "__main__":
    main()