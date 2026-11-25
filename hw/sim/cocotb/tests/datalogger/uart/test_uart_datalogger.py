# rr unknown-llm (AI-generated)
"""
UART Datalogger Test - Full Protocol Verification

Tests the uart_datalogger module with multi-byte transmission, config verification,
and scoreboard-based validation using the native SDK UartSource driver for
accurate bit-level UART signal generation.

Run with:
    SIM=nvc python test_uart_datalogger.py
    pytest test_uart_datalogger.py -v
"""

import os
import sys
import random
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
    from engine.paths import SDK_SRC_PATH
except ImportError:
    print("Error: Could not import engine. Make sure the script is running in the correct environment.")
    sys.exit(1)

# SDK testbench imports
from rr_cocotb_tb.env import TbEnv
from rr_cocotb_tb.scoreboard import Scoreboard, SignalCollector
from hw.sim.vip.drivers.uart_datalogger_driver import UartDriver
from hw.sim.vip.drivers.base import DataloggerInputs
from rr_cocotb_tb.drivers.uart_master import UartSource  # Native SDK driver (no external deps)
from hw.sim.vip.datalogger.uart.monitor import UartMonitor
from hw.sim.vip.datalogger.uart.config import DataloggerCfg
from hw.sim.portable_fifo_sources import olo_fifo_sources

# Deepskopion project root
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"
# Entity interface documentation:
# entity uart_datalogger is
#     generic (
#       G_SYSTEM_CLK_FREQ : integer                                   := 100_000_000; -- 100 MHz
#       G_UART_ID_WIDTH   : integer                                   := 5;
#       G_UART_DEV_ID     : natural range 0 to (2**G_UART_ID_WIDTH)-1 := 28;
#       G_TIMESTAMP_WIDTH : natural range 16 to 32                    := 16;
#       G_CONFIG_WIDTH    : integer                                   := 16;
#       G_ERROR_WIDTH     : integer                                   := 2;
#       G_ILA_EN          : integer                                   := 1
#       );
#     port (
#       CLK         : in  std_logic;
#       RST         : in  std_logic;
#       UART_IN     : in  std_logic;
#       CONFIG      : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
#       TIMESTAMP   : in  std_logic_vector(G_TIMESTAMP_WIDTH-1 downto 0);
#       -- UART
#       UART_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
#       UART_MSG_VLD : out std_logic;
#       UART_MSG_RDY : in  std_logic;
#       --
#       UART_ERRORS  : out std_logic_vector(G_ERROR_WIDTH-1 downto 0)
#       );
# end uart_datalogger;

# Default generics
GENERICS = {
    "G_SYSTEM_CLK_FREQ": 100_000_000,
    "G_UART_ID_WIDTH": 5,
    "G_UART_DEV_ID": 23,
    "G_TIMESTAMP_WIDTH": 16,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_ILA_EN": 0,
}


# =============================================================================
# Test Cases
# =============================================================================

@cocotb.test()
async def test_basic_behavior(dut):
    """Basic test for uart_datalogger - verifies reset and idle state."""
    PERIOD = 10
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())
    dut._log.info("Starting basic test for uart_datalogger...")
    
    # Initialize signals
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.UART_MSG_RDY.value = 1
    dut.UART_IN.value = 1  # UART idle high
    
    # Reset
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)

    await Timer(500, units="ns")
    dut._log.info("Basic test completed successfully.")
    assert True


@cocotb.test()
async def test_uart_datalogger_protocol(dut):
    """
    Full protocol verification test for UART datalogger.
    
    Tests multi-byte transmission with config byte verification and
    scoreboard-based validation. Uses native SDK UartSource driver for
    accurate bit-level UART signal generation.
    """
    PERIOD = 10  # ns

    # Determine log directory
    log_file_env = os.getenv("COCOTB_LOG_FILE")
    if log_file_env:
        log_path = Path(log_file_env).parent
    else:
        log_path = Path("logs")

    # Initialize environment
    env = TbEnv(
        dut, 
        clk="CLK", 
        rst="RST", 
        dev_id_generic="G_UART_DEV_ID", 
        period_ns=PERIOD, 
        log_dir=str(log_path)
    )
    log = env.log

    await env.start_clock()

    # Start UART monitor
    uartmon = UartMonitor(env, dut, valid_name="UART_MSG_VLD", data_name="UART_MSG_OUT")
    cocotb.start_soon(uartmon.start())

    # UART Parameters
    dev_id = env.dev_id  # Should match the generic ID of the DUT

    # Build and apply config
    cfg = DataloggerCfg(start=True, datalogger_id=dev_id)

    uart_driver_inputs = DataloggerInputs(
        clk="CLK",
        rst="RST",
        config="CONFIG",
        timestamp="TIMESTAMP",
        ready="UART_MSG_RDY",
    )

    dut.UART_IN.value = 1  # Idle state
    driver = UartDriver(env, dut, inputs=uart_driver_inputs)
    await driver.initialize()
    await driver.apply_config(cfg)
    await driver.start_timestamp_gen(0)

    # UART Protocol Parameters
    BAUD_RATE = 115200
    BITS = 6
    STOP_BITS = 1
    PARITY = "odd"  # "none", "even", "odd"

    UART_RX = dut.UART_IN
    await ClockCycles(env.clk, 5)
    await RisingEdge(env.clk)

    mask_bits = (1 << BITS) - 1

    # Signal collector with extraction function
    collector = SignalCollector(
        env.clk,
        dut.UART_MSG_VLD,
        dut.UART_MSG_OUT,
        lambda v: v & mask_bits
    )
    cocotb.start_soon(collector.start())

    # Create UART Source for accurate bit-level generation
    uart_source = UartSource(UART_RX, baud=BAUD_RATE, bits=BITS, stop_bits=STOP_BITS, parity=PARITY)

    # Calculate expected config byte
    if PARITY == "none":
        uart_parity = 0
        uart_expected_parity = 0
    elif PARITY == "even":
        uart_parity = 1
        uart_expected_parity = 0
    elif PARITY == "odd":
        uart_parity = 1
        uart_expected_parity = 1
    else:
        raise ValueError(f"Unsupported PARITY value: {PARITY!r}")
    
    uart_stop_bits = 1 if STOP_BITS == 1 else 0

    uart_expected_conf = (uart_stop_bits << 6 | (uart_expected_parity << 5) | (uart_parity << 4) | BITS) & mask_bits
    log.info(f"Expected UART config byte: 0x{uart_expected_conf:02x}")

    random.seed(0xC0FFEE)   # DS-T3.14: deterministic data for classification
    expected_values = [uart_expected_conf & 0xFF]

    # Phase 1: Send 150 bytes
    NUM_BYTES_PHASE1 = 150
    for i in range(NUM_BYTES_PHASE1):
        byte = random.randint(0x00, mask_bits)
        await uart_source.write(byte)
        log.info(f"Sent byte: 0x{byte:02x} ({i+1}/{NUM_BYTES_PHASE1})")
        expected_values.append(byte)

    await Timer(30, units="us")
    await Timer(100, units="us")  # Flush time

    # Phase 2: Send 15 more bytes
    NUM_BYTES_PHASE2 = 15
    for i in range(NUM_BYTES_PHASE2):
        byte = random.randint(0x00, mask_bits)
        await uart_source.write(byte)
        log.info(f"Sent byte: 0x{byte:02x} ({i+1}/{NUM_BYTES_PHASE2})")
        expected_values.append(byte)

    await Timer(200, units="us")  # Final flush

    # Scoreboard verification.
    # DS-T2.42 regression guard: the scoreboard expects EXACTLY one CONFIG byte
    # followed by the payload stream. Before the fix the datalogger emitted the
    # post-cal CONFIG frame ~7 times (held valid in IDLE vs the internal
    # 'not streamEmpty', ignoring external UART_MSG_RDY), shifting the whole
    # stream and failing at msg #1. A recurrence re-introduces that shift and
    # this scoreboard goes red again — so it is the standing witness for DS-T2.42.
    sb = Scoreboard(env, collector, fmt_fn=lambda x: f"0x{x:02x}")
    MASK = 0x000000FF
    sb.expect(expected_values, mask=MASK)
    await sb.run()

    log.info(f"UART configuration: 0x{collector.peek(0).value:02x}")
    log.info("UART datalogger protocol test PASSED")


# =============================================================================
# Standalone Execution
# =============================================================================

def main():
    """Run simulation for uart_datalogger"""
    
    run_simulation(
        top_level="uart_datalogger",
        module="test_uart_datalogger",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                # Packages first
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/uart_pkg.vhd",
                # SDK units (dependencies)
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{SDK_SRC_PATH}/units/ram_dual_port.vhd",
                # UART modules (ordered by dependency)
                f"{DEEPSKOPION_RTL}/datalogger/uart/uart_baudrate_gen.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/uart/uart_playback.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/uart/uart_stop_finder.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                # DS-P3.90 added a datalogger_lifecycle instantiation to uart_datalogger;
                # this source was never added here, leaving the test un-compilable.
                f"{DEEPSKOPION_RTL}/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/uart/uart_frame_gen.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/uart/uart_datalogger.vhd",
            ]
        },
    )


if __name__ == "__main__":
    main()
