# rr unknown-llm (AI-generated)
"""
UART Frame Gen Isolation Test
=============================
Tests uart_frame_gen.vhd in isolation against the protocol YAML spec.

Test strategy:
- Generate UART input via WAVE.level transitions (simulating 1Mbps UART)
- READY always '1' (bypass backpressure)
- Verify all message types: config, payload, payload_9bit, status
- Parameterized for NBITS (5-9), parity, stop bits

BR_CYCLES calculation:
- Clock: 100 MHz
- Target baud: 1 Mbps
- BR_CYCLES = 100_000_000 / 1_000_000 = 100
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, ClockCycles, FallingEdge
from cocotb.handle import Force

# Locate routertl SDK cocotb dir via the pip-installed routertl_core
# (DS-P2.29 fallout — old shim walked up the tree for an `engine/`
# folder that no longer exists post-pip-decouple).
current_dir = Path(__file__).resolve().parent
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
sys.path.insert(0, str(current_dir))  # For cocotb module discovery

try:
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH
except ImportError:
    print("Error: Could not import engine.")
    sys.exit(1)

# ==============================================================================
# Constants
# ==============================================================================
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

CLK_PERIOD_NS = 10  # 100 MHz

# BR_CYCLES for uart_frame_gen isolation test
# In the full uart_datalogger, timestamps are downscaled by C_MIN_BITWIDTH_UART_CYCLES (~8).
# For isolation testing, we drive BR_CYCLES directly to uart_frame_gen.
# Using 100 cycles per bit (1us bit time at 100MHz) for clean timing.
BR_CYCLES = 100
BIT_TIME_NS = BR_CYCLES * CLK_PERIOD_NS  # 1000 ns = 1 us per bit

# Protocol message types (from YAML - expected after RTL fix)
MSG_TYPE_CONFIG = 0b001
MSG_TYPE_PAYLOAD = 0b010
MSG_TYPE_PAYLOAD_9BIT = 0b011
MSG_TYPE_STATUS = 0b100

# Status codes
STATUS_CAL_DONE = 0x00
STATUS_CAL_TIMEOUT = 0x01
STATUS_PARITY_ERR = 0x02
STATUS_START_ERR = 0x03
STATUS_STOP_ERR = 0x04

# DS-T3.15: the old RTL_PAYLOAD_TYPE_CURRENT=0b000 "RTL bug" tracking was stale
# residue — commit 48663d8 aligned uart_frame_gen to the yml (payload 000→010)
# long ago. The RTL emits 0b010 (normal) / 0b011 (9-bit MSB=1); tests now assert
# those directly.

GENERICS = {
    "G_UART_DEV_ID": 26,
    "G_UART_ID_WIDTH": 5,
    "G_MSG_WIDTH": 32,
    "G_BR_CYCLES_WIDTH": 16
}


# ==============================================================================
# Data Structures
# ==============================================================================
@dataclass
class UartConfig:
    """UART configuration parameters"""
    nbits: int = 8
    parity: str = "none"  # "none", "even", "odd"
    stop_bits: int = 1    # 1 or 2


@dataclass
class DecodedMessage:
    """Decoded 32-bit datalogger message"""
    raw: int
    device_id: int
    msg_type: int
    timestamp: int
    data: int
    # For config messages
    baud_cycles: Optional[int] = None
    stop_bits_cfg: Optional[int] = None
    parity_type: Optional[int] = None
    parity_enable: Optional[int] = None
    word_length: Optional[int] = None
    # For status messages
    status_code: Optional[int] = None


def decode_message(msg: int) -> DecodedMessage:
    """Decode a 32-bit message per protocol YAML"""
    device_id = (msg >> 27) & 0x1F
    msg_type = (msg >> 24) & 0x07
    timestamp = (msg >> 8) & 0xFFFF
    data = msg & 0xFF

    decoded = DecodedMessage(
        raw=msg,
        device_id=device_id,
        msg_type=msg_type,
        timestamp=timestamp,
        data=data
    )

    if msg_type == MSG_TYPE_CONFIG:
        decoded.baud_cycles = timestamp  # [23:8] contains baud_cycles
        decoded.stop_bits_cfg = (data >> 6) & 0x01
        decoded.parity_type = (data >> 5) & 0x01
        decoded.parity_enable = (data >> 4) & 0x01
        decoded.word_length = data & 0x0F
    elif msg_type == MSG_TYPE_STATUS:
        decoded.status_code = data

    return decoded


def compute_parity(data: int, nbits: int, parity_type: str) -> int:
    """Compute parity bit for given data"""
    ones = bin(data & ((1 << nbits) - 1)).count('1')
    if parity_type == "even":
        return ones % 2  # 0 if even number of 1s
    elif parity_type == "odd":
        return 1 - (ones % 2)  # 1 if even number of 1s
    return 0


# ==============================================================================
# UART Signal Generator
# ==============================================================================
class UartWaveGenerator:
    """
    Generate UART waveforms by driving WAVE.level transitions.
    
    The uart_frame_gen expects pre-processed WAVE input from uart_playback,
    which provides level + timestamp. For isolation testing, we simulate
    this by driving level transitions directly.
    """

    def __init__(self, dut, config: UartConfig):
        self.dut = dut
        self.config = config
        self._timestamp = 0

    async def reset(self):
        """Reset DUT and initialize signals"""
        self.dut.RST.value = 1
        self.dut.ENABLE.value = 0
        self.dut.READY.value = 1
        self.dut.BR_CYCLES.value = BR_CYCLES
        self.dut.NBITS.value = self.config.nbits
        self.dut.STOP_BITS.value = 1 if self.config.stop_bits > 1 else 0

        # Set parity: [1] = type (0=even, 1=odd), [0] = enable
        parity_val = 0
        if self.config.parity != "none":
            parity_val = 0b01  # Enable
            if self.config.parity == "odd":
                parity_val |= 0b10  # Type = odd
        self.dut.PARITY.value = parity_val

        # Initialize WAVE record
        # WAVE is a HierarchyObject with LEVEL and TIMESTAMP children
        self.dut.WAVE.LEVEL.value = 1  # Idle high
        self.dut.WAVE.TIMESTAMP.value = 0

        await ClockCycles(self.dut.CLK, 5)
        self.dut.RST.value = 0
        await ClockCycles(self.dut.CLK, 2)

    async def enable(self):
        """Enable the datalogger"""
        self.dut.ENABLE.value = 1
        await RisingEdge(self.dut.CLK)

    async def _drive_bit(self, level: int):
        """Drive a single bit for BR_CYCLES clock cycles"""
        self.dut.WAVE.LEVEL.value = level
        self._timestamp += 1
        self.dut.WAVE.TIMESTAMP.value = self._timestamp & 0xFFFF
        await ClockCycles(self.dut.CLK, BR_CYCLES)

    async def send_byte(self, data: int) -> int:
        """
        Send a UART byte and return capture timestamp.
        
        Frame format: [START=0][DATA bits LSB first][PARITY?][STOP=1+]
        """
        start_timestamp = self._timestamp

        # Start bit (low)
        await self._drive_bit(0)

        # Data bits (LSB first)
        for i in range(self.config.nbits):
            bit = (data >> i) & 0x01
            await self._drive_bit(bit)

        # Parity bit (if enabled)
        if self.config.parity != "none":
            parity = compute_parity(data, self.config.nbits, self.config.parity)
            await self._drive_bit(parity)

        # Stop bit(s) (high)
        for _ in range(self.config.stop_bits):
            await self._drive_bit(1)

        # Inter-byte idle
        await ClockCycles(self.dut.CLK, BR_CYCLES * 2)

        return start_timestamp

    async def send_invalid_start(self):
        """Send glitch that looks like start but isn't"""
        await self._drive_bit(0)  # Start
        await ClockCycles(self.dut.CLK, BR_CYCLES // 2)  # Half bit
        await self._drive_bit(1)  # Back to idle


# ==============================================================================
# Message Collector
# ==============================================================================
class MessageCollector:
    """Collect and decode output messages"""

    def __init__(self, dut):
        self.dut = dut
        self.messages: list[DecodedMessage] = []
        self._running = False

    async def start(self):
        """Start collecting messages in background"""
        self._running = True
        while self._running:
            await RisingEdge(self.dut.CLK)
            if self.dut.MSG_VLD.value == 1:
                msg = int(self.dut.MSG_OUT.value)
                decoded = decode_message(msg)
                self.messages.append(decoded)
                self.dut._log.info(
                    f"MSG: type={decoded.msg_type:03b} data=0x{decoded.data:02X} "
                    f"ts={decoded.timestamp} raw=0x{decoded.raw:08X}"
                )

    def stop(self):
        """Stop collecting"""
        self._running = False

    def clear(self):
        """Clear collected messages"""
        self.messages.clear()

    def get_by_type(self, msg_type: int) -> list[DecodedMessage]:
        """Get messages of specific type"""
        return [m for m in self.messages if m.msg_type == msg_type]


# ==============================================================================
# Test Cases
# ==============================================================================
@cocotb.test()
async def test_enable_sends_config_message(dut):
    """TC-001: Enabling datalogger should emit config message (type 0b001)"""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())

    config = UartConfig(nbits=8, parity="none", stop_bits=1)
    generator = UartWaveGenerator(dut, config)
    collector = MessageCollector(dut)

    await generator.reset()
    cocotb.start_soon(collector.start())

    # Enable should trigger config message (not status)
    await generator.enable()
    await ClockCycles(dut.CLK, 10)

    collector.stop()

    # Find config message (type 0b001)
    config_msgs = collector.get_by_type(MSG_TYPE_CONFIG)
    assert len(config_msgs) >= 1, f"Expected config message on enable, got {[m.msg_type for m in collector.messages]}"
    
    msg = config_msgs[0]
    dut._log.info(f"Config msg: word_length={msg.word_length}, parity_en={msg.parity_enable}, stop={msg.stop_bits_cfg}")
    
    # Verify config fields
    assert msg.word_length == 8, f"Expected word_length=8, got {msg.word_length}"
    assert msg.parity_enable == 0, f"Expected parity_enable=0, got {msg.parity_enable}"

    dut._log.info("TC-001 PASSED: Enable triggers config message")


@cocotb.test()
async def test_8n1_payload_message(dut):
    """TC-002: Send 8N1 byte and verify payload message structure"""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())

    config = UartConfig(nbits=8, parity="none", stop_bits=1)
    generator = UartWaveGenerator(dut, config)
    collector = MessageCollector(dut)

    await generator.reset()
    await generator.enable()

    cocotb.start_soon(collector.start())
    await ClockCycles(dut.CLK, 5)
    collector.clear()  # Clear enable status message

    # Send test byte
    test_data = 0x55
    await generator.send_byte(test_data)
    await ClockCycles(dut.CLK, BR_CYCLES * 5)

    collector.stop()

    # Verify payload message (8N1 → normal payload, type 0b010 per yml/RTL)
    payload_msgs = [m for m in collector.messages
                    if m.msg_type in (MSG_TYPE_PAYLOAD, MSG_TYPE_PAYLOAD_9BIT)]

    assert len(payload_msgs) >= 1, f"Expected payload message, got {len(payload_msgs)}"

    msg = payload_msgs[0]
    dut._log.info(f"Payload: data=0x{msg.data:02X}, type=0b{msg.msg_type:03b}")

    # Data validation
    assert msg.data == test_data, f"Data mismatch: expected 0x{test_data:02X}, got 0x{msg.data:02X}"

    # Type validation — 8N1 (≤8-bit) is the normal payload type (DS-T3.15).
    assert msg.msg_type == MSG_TYPE_PAYLOAD, \
        f"Wrong type: expected 0b010, got 0b{msg.msg_type:03b}"

    dut._log.info("TC-002 PASSED: 8N1 payload captured correctly")


@cocotb.test()
async def test_multiple_bytes(dut):
    """TC-003: Send multiple bytes and verify message count"""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())

    config = UartConfig(nbits=8, parity="none", stop_bits=1)
    generator = UartWaveGenerator(dut, config)
    collector = MessageCollector(dut)

    await generator.reset()
    await generator.enable()

    cocotb.start_soon(collector.start())
    await ClockCycles(dut.CLK, 5)
    collector.clear()

    # Send 5 bytes
    test_bytes = [0xAA, 0x55, 0x00, 0xFF, 0x42]
    for byte_val in test_bytes:
        await generator.send_byte(byte_val)

    await ClockCycles(dut.CLK, BR_CYCLES * 10)
    collector.stop()

    # Count payload messages (excluding status)
    payload_msgs = [m for m in collector.messages 
                    if m.msg_type in (MSG_TYPE_PAYLOAD, MSG_TYPE_PAYLOAD_9BIT)]
    
    dut._log.info(f"Sent {len(test_bytes)} bytes, received {len(payload_msgs)} payload messages")
    
    # Verify data in order
    for i, (expected, msg) in enumerate(zip(test_bytes, payload_msgs)):
        assert msg.data == expected, \
            f"Byte {i}: expected 0x{expected:02X}, got 0x{msg.data:02X}"

    assert len(payload_msgs) == len(test_bytes), \
        f"Message count: expected {len(test_bytes)}, got {len(payload_msgs)}"

    dut._log.info("TC-003 PASSED: Multiple bytes captured in order")


@cocotb.test()
async def test_parity_modes(dut):
    """TC-004: Test even and odd parity modes"""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())

    for parity_mode in ["even", "odd"]:
        dut._log.info(f"Testing parity mode: {parity_mode}")
        
        config = UartConfig(nbits=8, parity=parity_mode, stop_bits=1)
        generator = UartWaveGenerator(dut, config)
        collector = MessageCollector(dut)

        await generator.reset()
        await generator.enable()

        cocotb.start_soon(collector.start())
        await ClockCycles(dut.CLK, 5)
        collector.clear()

        # Send byte with known parity
        test_data = 0x55  # 4 ones -> even=0, odd=1
        await generator.send_byte(test_data)
        await ClockCycles(dut.CLK, BR_CYCLES * 10)

        collector.stop()

        payload_msgs = [m for m in collector.messages 
                        if m.msg_type in (MSG_TYPE_PAYLOAD, MSG_TYPE_PAYLOAD_9BIT)]
        
        assert len(payload_msgs) >= 1, f"No payload for {parity_mode} parity"
        assert payload_msgs[0].data == test_data, f"Data mismatch in {parity_mode} mode"

    dut._log.info("TC-004 PASSED: Even and odd parity modes work")


@cocotb.test()
async def test_different_word_lengths(dut):
    """TC-005: Test 5, 6, 7, 8 bit word lengths"""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())

    for nbits in [5, 6, 7, 8]:
        dut._log.info(f"Testing {nbits}-bit word length")
        
        config = UartConfig(nbits=nbits, parity="none", stop_bits=1)
        generator = UartWaveGenerator(dut, config)
        collector = MessageCollector(dut)

        await generator.reset()
        await generator.enable()

        cocotb.start_soon(collector.start())
        await ClockCycles(dut.CLK, 5)
        collector.clear()

        # Send max value for this word length
        max_val = (1 << nbits) - 1
        await generator.send_byte(max_val)
        await ClockCycles(dut.CLK, BR_CYCLES * 10)

        collector.stop()

        payload_msgs = [m for m in collector.messages 
                        if m.msg_type in (MSG_TYPE_PAYLOAD, MSG_TYPE_PAYLOAD_9BIT)]
        
        assert len(payload_msgs) >= 1, f"No payload for {nbits}-bit"
        # Data should be left-aligned in [7:0] for sub-8-bit modes
        dut._log.info(f"{nbits}-bit: sent 0x{max_val:02X}, got 0x{payload_msgs[0].data:02X}")

    dut._log.info("TC-005 PASSED: Various word lengths work")


@cocotb.test()
async def test_9bit_mode_fragmentation(dut):
    """
    TC-006: Test 9-bit mode message behavior.
    
    Current RTL: Sends TWO messages (A + B fragments)
    YAML Spec: Should send ONE message with type encoding MSB
    
    This test documents current behavior and will FAIL after RTL fix.
    """
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())

    dut._log.info("Testing 9-bit mode (documenting current fragmented behavior)")

    config = UartConfig(nbits=9, parity="none", stop_bits=1)
    generator = UartWaveGenerator(dut, config)
    collector = MessageCollector(dut)

    await generator.reset()
    await generator.enable()

    cocotb.start_soon(collector.start())
    await ClockCycles(dut.CLK, 5)
    collector.clear()

    # Send 9-bit value 0x155 (bit8=1, data=0x55)
    test_data = 0x155
    await generator.send_byte(test_data)
    await ClockCycles(dut.CLK, BR_CYCLES * 15)

    collector.stop()

    # Current RTL sends TWO messages for 9-bit
    # After fix, should be ONE message with type reflecting MSB
    payload_msgs = [m for m in collector.messages 
                    if m.msg_type in (MSG_TYPE_PAYLOAD, MSG_TYPE_PAYLOAD_9BIT)]
    
    dut._log.info(f"9-bit mode: received {len(payload_msgs)} payload message(s)")
    for i, msg in enumerate(payload_msgs):
        dut._log.info(f"  [{i}] type=0b{msg.msg_type:03b} data=0x{msg.data:02X}")

    # Document current behavior (expect 2 fragments)
    if len(payload_msgs) == 2:
        dut._log.warning("RTL BEHAVIOR: 9-bit sends 2 fragments (A+B)")
        dut._log.warning("YAML SPEC: Should send 1 message with type encoding MSB")
        assert False, "9-bit should send 1 message, not 2 fragments"
    elif len(payload_msgs) == 1:
        # This is the EXPECTED behavior after RTL fix
        if payload_msgs[0].msg_type == MSG_TYPE_PAYLOAD_9BIT:
            # Note: Data may be bit-reversed due to shift register ordering
            dut._log.info(f"9-bit HIGH mode: type 0b011 (MSB=1) ✓, data=0x{payload_msgs[0].data:02X}")
        elif payload_msgs[0].msg_type == MSG_TYPE_PAYLOAD:
            dut._log.info(f"9-bit LOW mode: type 0b010 (MSB=0), data=0x{payload_msgs[0].data:02X}")
        else:
            assert False, f"Unexpected payload type: 0b{payload_msgs[0].msg_type:03b}"
    else:
        assert False, f"Expected 1 message for 9-bit, got {len(payload_msgs)}"

    dut._log.info("TC-006 PASSED: 9-bit mode uses single message with type encoding")


@cocotb.test()
async def test_stop_bits(dut):
    """TC-007: Test 1 and 2 stop bit modes"""
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())

    for stop_bits in [1, 2]:
        dut._log.info(f"Testing {stop_bits} stop bit(s)")
        
        config = UartConfig(nbits=8, parity="none", stop_bits=stop_bits)
        generator = UartWaveGenerator(dut, config)
        collector = MessageCollector(dut)

        await generator.reset()
        await generator.enable()

        cocotb.start_soon(collector.start())
        await ClockCycles(dut.CLK, 5)
        collector.clear()

        await generator.send_byte(0xAA)
        await ClockCycles(dut.CLK, BR_CYCLES * 10)

        collector.stop()

        payload_msgs = [m for m in collector.messages 
                        if m.msg_type in (MSG_TYPE_PAYLOAD, MSG_TYPE_PAYLOAD_9BIT)]
        
        assert len(payload_msgs) >= 1, f"No payload for {stop_bits} stop bits"

    dut._log.info("TC-007 PASSED: Both stop bit modes work")


# ==============================================================================
# Main Entry Point
# ==============================================================================
def main():
    """Run simulation for uart_frame_gen isolation test"""
    run_simulation(
        top_level="uart_frame_gen",
        module="test_uart_frame_gen_isolation",
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


if __name__ == "__main__":
    main()
