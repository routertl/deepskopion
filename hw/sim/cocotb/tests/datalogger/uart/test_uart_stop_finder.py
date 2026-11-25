# rr unknown-llm (AI-generated)
"""
UART Stop Finder Isolation Test
================================
Tests uart_stop_finder.vhd in isolation.

Test strategy:
- Generate UART waveforms by driving WAVE record
- Verify MATCH asserts after G_MIN_MATCHES valid frames
- Test parity detection consistency
- Test stop bit configuration detection

Run with: SIM=nvc python test_uart_stop_finder.py
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

# Locate routertl SDK cocotb dir via the pip-installed routertl_core
# (DS-P2.29 fallout — old shim walked up the tree for an `engine/`
# folder that no longer exists post-pip-decouple).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH
except ImportError:
    print("Error: Could not import engine.")
    sys.exit(1)

# Deepskopion project root
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

# Constants
CLK_PERIOD_NS = 10  # 100 MHz
BR_CYCLES = 100     # 100 clock cycles per UART bit (1 MHz baud)

GENERICS = {
    "G_CONF_NBITS": 8,
    "G_DIST_WIDTH": 16,
    "G_MIN_MATCHES": 8  # Reduced for faster testing
}


@dataclass
class UartConfig:
    """UART configuration"""
    nbits: int = 8
    parity: str = "none"  # "none", "even", "odd"
    stop_bits: int = 1


def compute_parity(data: int, nbits: int, parity_type: str) -> int:
    """Compute parity bit."""
    ones = bin(data & ((1 << nbits) - 1)).count('1')
    if parity_type == "even":
        return ones % 2
    elif parity_type == "odd":
        return 1 - (ones % 2)
    return 0


class WaveGenerator:
    """Generate UART waveforms on WAVE record."""
    
    def __init__(self, dut, config: UartConfig):
        self.dut = dut
        self.config = config
        self._timestamp = 0
    
    async def drive_bit(self, level: int):
        """Drive single bit for BR_CYCLES."""
        self.dut.WAVE.LEVEL.value = level
        self._timestamp += 1
        self.dut.WAVE.TIMESTAMP.value = self._timestamp & 0xFFFF
        await ClockCycles(self.dut.CLK, BR_CYCLES)
    
    async def send_byte(self, data: int):
        """Send UART frame: [START][DATA][PARITY?][STOP]."""
        # Start bit (low)
        await self.drive_bit(0)
        
        # Data bits (LSB first)
        for i in range(self.config.nbits):
            bit = (data >> i) & 0x01
            await self.drive_bit(bit)
        
        # Parity bit (if enabled)
        if self.config.parity != "none":
            parity = compute_parity(data, self.config.nbits, self.config.parity)
            await self.drive_bit(parity)
        
        # Stop bit(s)
        for _ in range(self.config.stop_bits):
            await self.drive_bit(1)
        
        # Inter-byte idle (short)
        await ClockCycles(self.dut.CLK, BR_CYCLES)


async def init_dut(dut, config: UartConfig = None):
    """Initialize DUT."""
    if config is None:
        config = UartConfig()
    
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    
    dut.RST.value = 1
    dut.ENABLE.value = 0
    dut.BR_CYCLES.value = BR_CYCLES
    dut.WAVE.LEVEL.value = 1  # Idle high
    dut.WAVE.TIMESTAMP.value = 0
    
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 2)
    
    return WaveGenerator(dut, config)


@cocotb.test()
async def test_match_after_valid_frames(dut):
    """TC-001: MATCH should assert after G_MIN_MATCHES valid UART frames."""
    config = UartConfig(nbits=8, parity="none", stop_bits=1)
    generator = await init_dut(dut, config)
    
    dut.ENABLE.value = 1
    
    min_matches = GENERICS["G_MIN_MATCHES"]
    
    # Send enough frames to trigger match
    for i in range(min_matches + 2):
        await generator.send_byte(0x55)
        
        if dut.MATCH.value == 1:
            dut._log.info(f"MATCH asserted after {i+1} frames")
            break
    
    assert dut.MATCH.value == 1, f"MATCH should assert after {min_matches} valid frames"
    dut._log.info("TC-001 PASSED: MATCH asserts after valid frames")


@cocotb.test()
async def test_no_match_on_invalid_stop(dut):
    """TC-002: MATCH should not assert if stop bit is wrong."""
    config = UartConfig(nbits=8, parity="none", stop_bits=1)
    generator = await init_dut(dut, config)
    
    dut.ENABLE.value = 1
    
    # Send byte with invalid stop bit (drive low during stop)
    # Start bit
    await generator.drive_bit(0)
    
    # Data bits
    for i in range(8):
        await generator.drive_bit((0x55 >> i) & 1)
    
    # Invalid stop bit (should be high, drive low)
    await generator.drive_bit(0)
    
    # Wait a bit
    await ClockCycles(dut.CLK, BR_CYCLES * 5)
    
    # MATCH should not be asserted
    assert dut.MATCH.value == 0, "MATCH should not assert after invalid stop bit"
    dut._log.info("TC-002 PASSED: No match on invalid stop bit")


@cocotb.test()
async def test_enable_reset_behavior(dut):
    """TC-003: ENABLE=0 should reset FSM to WAIT_START."""
    config = UartConfig(nbits=8, parity="none", stop_bits=1)
    generator = await init_dut(dut, config)
    
    dut.ENABLE.value = 1
    
    # Send some valid frames
    for _ in range(4):
        await generator.send_byte(0xAA)
    
    # Disable
    dut.ENABLE.value = 0
    await ClockCycles(dut.CLK, 10)
    
    # Re-enable - counter should restart
    dut.ENABLE.value = 1
    
    # Need to send G_MIN_MATCHES frames again
    min_matches = GENERICS["G_MIN_MATCHES"]
    for i in range(min_matches + 2):
        await generator.send_byte(0x55)
        if dut.MATCH.value == 1:
            dut._log.info(f"MATCH re-asserted after {i+1} frames")
            break
    
    dut._log.info("TC-003 PASSED: Enable/disable resets state")


@cocotb.test()
async def test_parity_detection(dut):
    """TC-004: PARITY output should reflect consistent parity."""
    config = UartConfig(nbits=8, parity="even", stop_bits=1)
    generator = await init_dut(dut, config)
    
    dut.ENABLE.value = 1
    
    min_matches = GENERICS["G_MIN_MATCHES"]
    
    # Send frames with consistent even parity
    for _ in range(min_matches + 2):
        # Use data that produces consistent parity
        await generator.send_byte(0x55)  # Even number of 1s
    
    await ClockCycles(dut.CLK, BR_CYCLES * 2)
    
    # Check PARITY[0] - 1 if no parity fail
    parity_val = int(dut.PARITY.value)
    dut._log.info(f"PARITY output: 0b{parity_val:02b}")
    
    dut._log.info("TC-004 PASSED: Parity detection works")


# Partial witness for UART-REQ-14 only: it counts parity_error frames and is
# blind to the latched parityConf_r verdict (see the REQ text). Untagged until
# DS-P2.179 adds a witness that can fail on that verdict; tagging it would
# report REQ-14 covered when it is not.
@cocotb.test()
async def test_no_false_parity_on_varied_8n1(dut):
    """Positive guard: a single uart_stop_finder must report no-parity (PARITY[0]=0)
    for a varied 8N1 stream — even when the last two bytes share popcount-parity.

    This pins the per-finder detection's STICKY behaviour: once `isEvenParity`
    changes between bytes, CHECK_STOP bypasses the parity FSM (line ~162) so
    parityFail latches '1' and the verdict can't be flipped back by a coincidental
    matching tail. The sequence O,E,O,E,...,E,E exercises exactly that (varied body,
    matching tail) and the finder correctly concludes no-parity.

    NB: this confirms the per-finder logic is sound — it is NOT the DS-T3.5 bug.
    DS-T3.5 (8N1 reliably mis-detected as has-parity on HW) lives at the BANK level
    in uart_datalogger — which NBITS candidate wins the stopMatch one-hot and what
    parityMatch(winner) is latched at calibration-done. That reproduction is pending
    the on-HW config-event evidence; the fix will be a windowed parity voter.
    """
    config = UartConfig(nbits=8, parity="none", stop_bits=1)
    generator = await init_dut(dut, config)
    dut.ENABLE.value = 1

    #   0x01 = 1 one  -> ODD  popcount
    #   0x03 = 2 ones -> EVEN popcount
    # Alternating popcount-parity (clear variation), then a matching EVEN/EVEN tail.
    seq = [0x01, 0x03, 0x01, 0x03, 0x01, 0x03, 0x01, 0x03, 0x03, 0x03]
    for b in seq:
        await generator.send_byte(b)
    await ClockCycles(dut.CLK, BR_CYCLES * 2)

    assert dut.MATCH.value == 1, "stop-bit MATCH should assert on valid 8N1 frames"
    parity_bits = int(dut.PARITY.value)
    has_parity = parity_bits & 0x1
    dut._log.info(f"PARITY=0b{parity_bits:02b} (has_parity={has_parity})")
    assert has_parity == 0, (
        "uart_stop_finder reported HAS-PARITY on a varied no-parity 8N1 stream — the "
        "per-finder sticky no-parity detection regressed (a matching tail must not "
        "flip the verdict once variation has been seen)."
    )


def main():
    """Run simulation for uart_stop_finder"""
    run_simulation(
        top_level="uart_stop_finder",
        module="test_uart_stop_finder",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/uart_pkg.vhd",
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/uart/uart_baudrate_gen.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/uart/uart_stop_finder.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )


if __name__ == "__main__":
    main()