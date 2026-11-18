"""can_crc21 unit test (DS-P2.64 step 2, CAN-REQ-20) — CAN-FD CRC-21 (>16 B).

LAYER-0 GOLDEN ANCHOR (the circular-green keystone). Same discipline as
test_can_crc17: the CRC-21 polynomial/bit-order/init are pinned to the EXTERNAL
reveng CRC catalogue entry CRC-21/CAN-FD
  width=21 poly=0x102899 init=0 refin=false refout=false xorout=0 check=0x0ED841
where `check` is the CRC of the ASCII string "123456789" — the external truth
this test asserts against (0x0ED841). The remaining vectors are hard-coded
literals (ROUTERTL-002) from an independent oracle that reproduces that anchor.

  Source of truth: reveng CRC catalogue, https://reveng.sourceforge.io/crc-catalogue/17plus.htm
  Polynomial: x^21+x^20+x^13+x^11+x^7+x^4+x^3+1 (ISO 11898-1:2015 / Bosch CAN FD v1.0)

Covers CAN-REQ-20 (the crc_error compare lands in the FSM, step 4).
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402
from sdk.cocotb_helpers import requires  # noqa: E402

DS_ROOT = Path(__file__).resolve().parents[6]
RTL = DS_ROOT / "hw" / "rtl" / "datalogger" / "can" / "can_crc21.vhd"

POLY = 0x102899   # documented; used only to express the leading-1 preset identity


def _bits(data: bytes):
    return [(byte >> i) & 1 for byte in data for i in range(7, -1, -1)]


# (name, input bits, EXPECTED crc-21) — all expected values are hard-coded literals.
# The "123456789" row is the EXTERNAL reveng anchor (0x0ED841); the rest are oracle-
# generated (oracle anchored by that same row).
GOLDENS = [
    ("ascii_123456789 (reveng anchor)", _bits(b"123456789"),        0x0ED841),
    ("empty",                           [],                          0x000000),
    ("single_1_bit (== poly preset)",   [1],                         POLY),      # 0x102899
    ("byte_FF",                         _bits(b"\xFF"),              0x104A5A),
    ("deadbeef",                        _bits(b"\xDE\xAD\xBE\xEF"),  0x1B60A6),
    ("zeros_8B",                        _bits(b"\x00" * 8),          0x000000),
    ("ones_8B",                         _bits(b"\xFF" * 8),          0x1FC962),
    ("pat_16B",                         _bits(bytes(range(16))),     0x11B0A7),
    ("pat_20B (>16 CRC-21 domain)",     _bits(bytes(range(20))),     0x13A817),
]


async def crc_of(dut, bits):
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
    await RisingEdge(dut.CLK)
    return int(dut.CRC.value)


@cocotb.test()
@requires("CAN-REQ-20")
async def crc21_golden_vectors(dut):
    corner_case = "CAN-FD.CRC21"
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.RST.value = 1
    dut.CLR.value = 0
    dut.EN.value = 0
    dut.DIN.value = 0
    await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)

    for name, bits, want in GOLDENS:
        got = await crc_of(dut, bits)
        assert got == want, (
            f"{corner_case} can_crc21[{name}] over {len(bits)} bits = 0x{got:06X}, "
            f"golden 0x{want:06X}")
    anchor = next(w for n, _, w in GOLDENS if "reveng" in n)
    dut._log.info(f"can_crc21 PASS — {len(GOLDENS)} golden vectors exact; "
                  f"external reveng anchor check(\"123456789\")=0x{anchor:06X} confirmed")


def main():
    run_simulation(
        top_level="can_crc21",
        module="test_can_crc21",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [str(RTL)]},
    )


if __name__ == "__main__":
    main()
