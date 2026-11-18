"""
UART Datalogger Test Utilities
Common setup and check functions for UART datalogger tests.
"""

import sys
from pathlib import Path
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, ClockCycles, RisingEdge
from rr_cocotb_tb.drivers.uart_master import UartSource
from rr_cocotb_tb.scoreboard import SignalCollector, Item, _to_int_resolving_xz
from rr_cocotb_tb.env import TbEnv as Tb


# Project paths
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

from engine.paths import SDK_SRC_PATH
from hw.sim.portable_fifo_sources import olo_fifo_sources

# Global Constants
G_SYSTEM_CLK_FREQ = 100_000_000
PERIOD_NS = 10  # 100 MHz
GENERICS = {
    "G_SYSTEM_CLK_FREQ": G_SYSTEM_CLK_FREQ,
    "G_UART_DEV_ID": 23,
}

# Protocol constants
MSG_TYPE_CONFIG = 0b001
MSG_TYPE_PAYLOAD = 0b010
MSG_TYPE_PAYLOAD_9BIT = 0b011

# =============================================================================
# FastSignalCollector: Optimized collector for UART_MSG_VLD pulses
# =============================================================================
class FastSignalCollector(SignalCollector):
    """
    Optimized collector that waits for RisingEdge(vld) instead of polling.
    Provides captured raw values to a reassembler via value_fn.
    """
    def __init__(self, clk, valid_sig, data_sig, value_fn=None, *, history=None):
        super().__init__(clk, valid_sig, data_sig, value_fn, history=history)
        self._log = cocotb.log  # Use cocotb's global logger

    async def start(self):
        self.count = 0
        while True:
            await RisingEdge(self.valid_sig)
            self.count += 1
            raw_val = _to_int_resolving_xz(self.data_sig.value)
            self._log.info(f"FastSignalCollector: Captured raw pulse: 0x{raw_val:08x}")
            if self._value_fn is not None:
                val = self._value_fn(raw_val)
            else:
                val = raw_val
            item = Item(value=val)
            t = cocotb.utils.get_sim_time(units="ns")
            self.q.put_nowait((item, t))
            self._buf.append(item)
            self._event.set()


# =============================================================================
# UartDataloggerMsgReassembler: Reassembles fragmented UART datalogger messages
# =============================================================================
class UartDataloggerMsgReassembler:
    """
    Reassembles fragmented UART datalogger messages for >8 bit data widths.
    Format: [31:27 ID] [26:24 Type] [23:8 Timestamp] [7:0 Data]
    """
    def __init__(self, nbits, log):
        self.nbits = nbits
        self.log = log
        self.messages = {}  # timestamp -> { 'A': val, 'B': val, ... }
        self.results = []
        self.calibrated = False

    def push(self, raw_val):
        msg_type = (raw_val >> 24) & 0x7
        timestamp = (raw_val >> 8) & 0xFFFF
        data = raw_val & 0xFF

        # Protocol message types: PAYLOAD=2 (0b010), PAYLOAD_9BIT=3 (0b011), CONFIG=1 (0b001)
        if msg_type == MSG_TYPE_PAYLOAD or msg_type == MSG_TYPE_PAYLOAD_9BIT:
            if self.nbits <= 8:
                # For 8-bit mode, data is in bits [7:0] directly - no reassembly needed
                self.results.append(data)
            else:
                # For >8-bit mode, use timestamp-based grouping for multi-fragment reassembly
                if timestamp not in self.messages:
                    self.messages[timestamp] = {}
                entry = self.messages[timestamp]
                
                if 'A' not in entry:
                    entry['A'] = data
                else:
                    entry['B'] = data
                    val = (entry['A'] << (self.nbits - 8)) | ((entry['B'] >> (4 - (self.nbits - 8))) & ((1 << (self.nbits - 8)) - 1))
                    self.results.append(val)
                    del self.messages[timestamp]
        elif msg_type == MSG_TYPE_CONFIG:  # CONFIG
            self.calibrated = True
        # STATUS (type 4) is ignored for simplicity

    def get_results(self):
        return self.results


async def setup_dut(dut):
    """Common setup: clock, reset, signal init."""
    # Use SDK Tb for robust clock generation
    tb = Tb(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await tb.start_clock()
    
    # Initialize signals
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.UART_MSG_RDY.value = 1
    dut.UART_IN.value = 1  # UART idle high
    
    # Reset
    await tb.reset()

    # Start independent timestamp driver
    async def timestamp_driver():
        while True:
            await Timer(1000, units="ns")
            dut.TIMESTAMP.value = (int(dut.TIMESTAMP.value) + 1) % (2**16)
    
    cocotb.start_soon(timestamp_driver())

async def apply_config(dut, dev_id: int):
    """
    Enable the datalogger.
    
    IMPORTANT: configEnable (bit 0) comes from latched config. The latch is
    triggered by ID match + config change. We keep the full config word active
    to ensure configEnable stays 1.
    """
    # Step 1: Set dev_id only (prime the ID match)
    dut.CONFIG.value = (dev_id & 0x1F) << 11
    await ClockCycles(dut.CLK, 3)
    
    # Step 2: Set full config word (Enable=1, AutoSync=1)
    # This is latched by config_control on ID match
    config_word = ((dev_id & 0x1F) << 11) | 0x03
    dut.CONFIG.value = config_word
    await ClockCycles(dut.CLK, 5)  # Extra cycles to ensure latch
    
    # KEEP config_word active - don't change it!
    # The latch has stored the value, but we leave it unchanged for safety




async def run_parameterized_test(dut, baud, bits, parity, stop_bits):
    """Core parameterized test logic."""
    # Skip invalid combinations: 9-bit mode does not support parity
    if bits == 9 and parity != "none":
        dut._log.info(f"SKIPPING: 9-bit mode does not support parity (parity={parity})")
        return
    
    # 1. Setup
    await setup_dut(dut)
    dev_id = GENERICS["G_UART_DEV_ID"]
    await apply_config(dut, dev_id)
    await ClockCycles(dut.CLK, 100)
    
    dut._log.info(f"Test Configuration: Baud={baud}, Bits={bits}, Parity={parity}, StopBits={stop_bits}")
    
    # 2. Traffic Generation
    uart_source = UartSource(dut.UART_IN, baud=baud, bits=bits, stop_bits=stop_bits, parity=parity)
    
    import random
    max_val = (1 << bits) - 1
    data = [random.randint(0, max_val) for _ in range(150)]
    
    dut._log.info(f"Sending 150 bytes of traffic...")
    await uart_source.write_bytes(data)
    
    # 3. Wait for Lock
    bit_time_ns = 1_000_000_000 / baud
    byte_time_ns = bit_time_ns * (1 + bits + (1 if parity != "none" else 0) + stop_bits)
    total_time_ns = byte_time_ns * 300 
    
    start_time = cocotb.utils.get_sim_time(units="ns")
    calibration_locked = False
    
    while (cocotb.utils.get_sim_time(units="ns") - start_time) < total_time_ns:
        if dut.calibrationDone_r.value == 1:
            calibration_locked = True
            break
        if dut.calibrationFail_r.value == 1:
            assert False, "Calibration Failed"
        await ClockCycles(dut.CLK, 100)
        
    assert calibration_locked, "Timeout waiting for calibrationDone_r"
    dut._log.info(f"✓ CALIBRATION PASSED: Baud={baud}, Bits={bits}, Parity={parity}")

CUSTOM_LIBRARIES = {
    "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
    "tich": [
        f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
        f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
        f"{DEEPSKOPION_RTL}/pkg/uart_pkg.vhd",
        f"{DEEPSKOPION_ROOT}/libs/routertl/edge_detector/edge_detector.vhd",
        f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
        f"{DEEPSKOPION_ROOT}/libs/routertl/ram_dual_port/ram_dual_port.vhd",
        f"{DEEPSKOPION_RTL}/datalogger/uart/uart_baudrate_gen.vhd",
        f"{DEEPSKOPION_RTL}/datalogger/uart/uart_playback.vhd",
        f"{DEEPSKOPION_RTL}/datalogger/uart/uart_stop_finder.vhd",
        f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
        f"{DEEPSKOPION_RTL}/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
        f"{DEEPSKOPION_RTL}/datalogger/uart/uart_frame_gen.vhd",
        f"{DEEPSKOPION_RTL}/datalogger/uart/uart_datalogger.vhd",
    ]
}
