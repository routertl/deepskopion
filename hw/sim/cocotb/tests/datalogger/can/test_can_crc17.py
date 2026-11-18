"""can_crc17 unit test (DS-P2.64 step 2, CAN-REQ-19) — CAN-FD CRC-17 (<=16 B).

LAYER-0 GOLDEN ANCHOR (the circular-green keystone). The CRC-17 polynomial, bit
order and init are NOT taken on faith from our own code or a paraphrase — they are
pinned to an EXTERNAL authority: the reveng CRC catalogue entry CRC-17/CAN-FD
  width=17 poly=0x1685B init=0 refin=false refout=false xorout=0 check=0x04F03
where `check` is the CRC of the ASCII string "123456789". That single value
(0x04F03) is the external truth this test asserts against — if the RTL polynomial
were off by one bit (e.g. the x^3 term that the CiA web prose drops), this fails.

The remaining vectors were produced by an INDEPENDENT bit-serial Python oracle that
itself reproduces the reveng check exactly (so it is the correct CRC-17/CAN-FD
function by construction — a CRC is fully determined by poly+init+bitorder, and one
external check value validates all of it). Their expected values are HARD-CODED
literals here (ROUTERTL-002), never computed at runtime.

  Source of truth: reveng CRC catalogue, https://reveng.sourceforge.io/crc-catalogue/17plus.htm
  Polynomial: x^17+x^16+x^14+x^13+x^11+x^6+x^4+x^3+x+1 (ISO 11898-1:2015 / Bosch CAN FD v1.0)

Covers CAN-REQ-19 (the crc_error compare lands in the FSM, step 4).
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
RTL = DS_ROOT / "hw" / "rtl" / "datalogger" / "can" / "can_crc17.vhd"

POLY = 0x1685B   # documented; used only to express the leading-1 preset identity


def _bits(data: bytes):
    return [(byte >> i) & 1 for byte in data for i in range(7, -1, -1)]


# (name, input bits, EXPECTED crc-17) — all expected values are hard-coded literals.
# The "123456789" row is the EXTERNAL reveng anchor (0x04F03); the rest are oracle-
# generated (oracle anchored by that same row).
GOLDENS = [
    ("ascii_123456789 (reveng anchor)", _bits(b"123456789"),        0x04F03),
    ("empty",                           [],                          0x00000),
    ("single_1_bit (== poly preset)",   [1],                         POLY),     # 0x1685B
    ("byte_FF",                         _bits(b"\xFF"),              0x19052),
    ("deadbeef",                        _bits(b"\xDE\xAD\xBE\xEF"),  0x08BBB),
    ("zeros_8B",                        _bits(b"\x00" * 8),          0x00000),
    ("ones_8B",                         _bits(b"\xFF" * 8),          0x17EFB),
    ("pat_16B (<=16 CRC-17 domain)",    _bits(bytes(range(16))),     0x1443B),
    ("pat_20B",                         _bits(bytes(range(20))),     0x14584),
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
@requires("CAN-REQ-19")
async def crc17_golden_vectors(dut):
    corner_case = "CAN-FD.CRC17"
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
            f"{corner_case} can_crc17[{name}] over {len(bits)} bits = 0x{got:05X}, "
            f"golden 0x{want:05X}")
    anchor = next(w for n, _, w in GOLDENS if "reveng" in n)
    dut._log.info(f"can_crc17 PASS — {len(GOLDENS)} golden vectors exact; "
                  f"external reveng anchor check(\"123456789\")=0x{anchor:05X} confirmed")


def main():
    run_simulation(
        top_level="can_crc17",
        module="test_can_crc17",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [str(RTL)]},
    )


if __name__ == "__main__":
    main()
