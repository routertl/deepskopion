"""can_crc15 unit test (DS-P3.100, Block 4).

Validates the CAN CRC-15 serial engine (generator 0x4599) two ways:

  1. HAND-VERIFIED golden vectors — short inputs whose CRC was worked out by hand
     from the Bosch algorithm (NOT from any code loop). These pin the LFSR math
     against literals (ROUTERTL-002):
       []      -> 0x0000   (empty / all-recessive prefix)
       [1]     -> 0x4599   (one dominant: crcnext=1 -> crc ^= 0x4599)
       [1,0]   -> 0x4EAB   ((0x4599<<1)&0x7FFF=0x0B32, crcnext=1 -> ^0x4599)
       [1,1]   -> 0x0B32   (0x0B32, crcnext=0 -> no xor)
       [0,1]   -> 0x4599   (leading recessive bits don't change a zero register)

  2. An INDEPENDENT Python oracle (a second implementation of the same spec),
     itself anchored by the hand goldens above, extended over a full standard
     frame's covered bits (SOF..end-of-data) — guards length/width handling that
     the short vectors can't reach.

Covers CAN-REQ-7 (CRC-15 computation; the crc_error compare lands in the FSM).
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

DS_ROOT = Path(__file__).resolve().parents[6]   # deepskopion/
RTL = DS_ROOT / "hw" / "rtl" / "datalogger" / "can" / "can_crc15.vhd"

POLY = 0x4599


def crc15_oracle(bits):
    """Independent Bosch CRC-15 reference (anchored by the hand goldens)."""
    crc = 0
    for b in bits:
        crcnext = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if crcnext:
            crc ^= POLY
    return crc


def bits_msb(value, width):
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


# Hand-verified (input bits -> expected CRC) — literals, not loop-derived.
HAND_GOLDENS = [
    ([],            0x0000),
    ([1],           0x4599),
    ([1, 0],        0x4EAB),
    ([1, 1],        0x0B32),
    ([0, 1],        0x4599),
    ([0, 0, 0, 0],  0x0000),
]

# Full standard frame's covered bits: SOF + ID(0x123) + RTR0 + IDE0 + r0 + DLC2
# + data[0xAB,0xCD] (de-stuffed; CRC spans SOF..end-of-data).
FRAME_COVERED = (
    [0] + bits_msb(0x123, 11) + [0, 0, 0] + bits_msb(0x2, 4)
    + bits_msb(0xAB, 8) + bits_msb(0xCD, 8)
)


async def crc_of(dut, bits):
    """Clear, stream `bits` with EN, return the registered CRC."""
    dut.CLR.value = 1
    dut.EN.value = 0
    dut.DIN.value = 0
    await RisingEdge(dut.CLK)
    dut.CLR.value = 0
    for b in bits:
        dut.DIN.value = b
        dut.EN.value = 1
        await RisingEdge(dut.CLK)
    dut.EN.value = 0
    await RisingEdge(dut.CLK)        # settle the last registered update
    return int(dut.CRC.value)


@cocotb.test()
async def crc15_hand_goldens(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.RST.value = 1
    dut.CLR.value = 0
    dut.EN.value = 0
    dut.DIN.value = 0
    await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)

    for bits, want in HAND_GOLDENS:
        got = await crc_of(dut, bits)
        assert got == want, f"CRC({bits}) = {got:#06x}, hand-expected {want:#06x}"
    dut._log.info(f"can_crc15 PASS — {len(HAND_GOLDENS)} hand-verified goldens exact")


@cocotb.test()
async def crc15_full_frame_vs_oracle(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.RST.value = 1
    dut.CLR.value = 0
    dut.EN.value = 0
    dut.DIN.value = 0
    await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)

    want = crc15_oracle(FRAME_COVERED)
    got = await crc_of(dut, FRAME_COVERED)
    assert got == want, (
        f"CRC over {len(FRAME_COVERED)} frame bits = {got:#06x}, oracle {want:#06x}")
    # second frame back-to-back (CLR re-init proven)
    bits2 = [0] + bits_msb(0x7A0, 11) + [1, 0, 0] + bits_msb(0x0, 4)
    want2 = crc15_oracle(bits2)
    got2 = await crc_of(dut, bits2)
    assert got2 == want2, f"second-frame CRC {got2:#06x}, oracle {want2:#06x}"
    dut._log.info(
        f"can_crc15 PASS — full-frame CRC {got:#06x} matches oracle ({len(FRAME_COVERED)} "
        f"bits); back-to-back re-init CRC {got2:#06x} ok")


def main():
    run_simulation(
        top_level="can_crc15",
        module="test_can_crc15",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [str(RTL)]},
    )


if __name__ == "__main__":
    main()
