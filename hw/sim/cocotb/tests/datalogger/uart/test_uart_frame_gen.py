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
    from engine.paths import SRC_PATH, SDK_SRC_PATH


except ImportError:
    print("Error: Could not import engine. Make sure the script is running in the correct environment.")
    sys.exit(1)

# Deepskopion project root
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"


# entity uart_frame_gen is
#   generic (
#     G_UART_DEV_ID     : natural := 26;
#     G_UART_ID_WIDTH   : integer := 5;
#     G_MSG_WIDTH       : integer := 32;
#     G_BR_CYCLES_WIDTH : integer
#     );
#   port (
#     CLK       : in  std_logic;
#     RST       : in  std_logic;
#     ENABLE    : in  std_logic;
#     READY     : in  std_logic;
#     BR_CYCLES : in  std_logic_vector(G_BR_CYCLES_WIDTH-1 downto 0);
#     WAVE      : in  t_wave;
#     NBITS     : in  integer;
#     PARITY    : in  std_logic_vector(1 downto 0); -- Check partity if '1'
#     STOP_BITS : in  std_logic;  -- 1: one stop bit, 0: two stop bits (protocol stop_bit_encoding)
#     MSG_OUT   : out std_logic_vector(G_MSG_WIDTH-1 downto 0);
#     MSG_VLD   : out std_logic
#     );
# end uart_frame_gen;

# Default generics - modify as needed for your test
GENERICS = {
    "G_UART_DEV_ID": 26,
    "G_UART_ID_WIDTH": 5,
    "G_MSG_WIDTH": 32,
    "G_BR_CYCLES_WIDTH": 16
}

def main():
    """Run simulation for uart_frame_gen"""
    run_simulation(
        top_level="uart_frame_gen",
        module="test_uart_frame_gen",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/uart/uart_baudrate_gen.vhd",
                f"{DEEPSKOPION_RTL}/pkg/uart_pkg.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/uart/uart_frame_gen.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )

@cocotb.test()
async def test_basic_behavior(dut):
    """Basic test for uart_frame_gen"""
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())
    dut._log.info("Starting basic test for uart_frame_gen...")
    
    # Initialize reset if present
    if hasattr(dut, "RST"):
        dut.RST.value = 1
        await ClockCycles(dut.CLK, 3)
        dut.RST.value = 0
        await RisingEdge(dut.CLK)
    elif hasattr(dut, "R"):
        dut.R.value = 1
        await ClockCycles(dut.CLK, 3)
        dut.R.value = 0
        await RisingEdge(dut.CLK)
    else:
        dut._log.info("No reset port found, skipping reset.")

    # TODO: Add actual test logic here
    await Timer(500, units="ns")
    dut._log.info("Test completed successfully.")
    assert True

if __name__ == "__main__":
    main()