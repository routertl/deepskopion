# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.317: every RW regbank register reads its declared reset value 0 after reset.

DE25 silicon (C3, C4, 2026-11-15): the pre-arm DATALOGGER_CONFIG readback returned the low 16 bits of BUILD_DATE
(0xE478, 0x8E28) instead of 0. project_regbank.yml declares no reset value for any RW register, so each resets to 0.

Code reading before this test (a mechanism to test, not yet observed): the generated system_regbank_pkg.vhd keys
C_INIT_REGBANK by C_INDEX_* constants, which are BYTE addresses (system_regbank_wrapper.vhd divides them by 4), while
system_regbank.vhd indexes the bank by WORD (address >> C_ADDR_LSB = 2) and loads reg_bank(i) <= C_INIT_REGBANK(i) at
reset. The BUILD_DATE init (byte 0x08) would then land in word 8 = byte 0x20 (DATALOGGER_CONFIG) and the
REGBANK_NUM_WORDS init (byte 0x10) in word 16 = byte 0x40 (SPI_INJ_CONTROL).

Bench: hw/sim/tb_default_mix_core.vhd (the real deepskopion_core), AXI-Lite reads only, no write before the reads.

Pre-stated verdict (before the first run):
  the 21 RW registers of project_regbank.yml (hard-coded below, checked against the yml) each read 0x00000000;
  on the current RTL this is RED at 0x020 (DATALOGGER_CONFIG = BUILD_DATE) and 0x040 (SPI_INJ_CONTROL =
  REGBANK_NUM_WORDS), and every other RW register reads 0. BUILD_DATE (0x008) and REGBANK_NUM_WORDS (0x010) are read
  and logged for the comparison, not judged.

Second test (added 2026-11-15 20:5x CET, before its first run): RO and ROM registers are write-protected over AXI.
Each of the RO/ROM registers of project_regbank.yml (read from the yml) is written 0xA5A5A5A5, then read; none may
read back 0xA5A5A5A5. Prediction on today's RTL: RED (the generated access vectors are all '0', so no word is
write-protected; a register not refreshed from REGS_IN keeps the written value). Expected GREEN once the schema
declares its byte address mode and is regenerated (RTL-P1.316).

Third test (added 2026-11-15 22:1x CET, before its first run): the two manifest count registers read their ROM's word
count. REGBANK_NUM_WORDS (0x010) must read the number of words in hw/rtl/regbank/regbank_schema.hex, CONFIG_NUM_WORDS
(0x018) the number in hw/rtl/regbank/config_schema.hex (11 = 0xB at this commit). Prediction: RED with the schema's
`address_mode: bytes` and CONFIG_NUM_WORDS declared ROM (its ROM init has no external constant and is x"00000000", and
ROM blocks the wrapper's REGS_IN: 0x0 measured in job 209be61d835a); GREEN with CONFIG_NUM_WORDS declared RO.

Run (from the deepskopion dir): rr sim run test_regbank_reset_values_ds_p2_317
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
import yaml
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

_HERE = Path(__file__).resolve()
_COCOTB = _HERE.parents[4]                          # hw/sim/cocotb
_ROOT = _HERE.parents[7]                            # deepskopion
sys.path.insert(0, str(_COCOTB / "tests" / "stimulus" / "core_seam"))
from _core_seam_common import run_core_seam  # noqa: E402

TOP = "tb_default_mix_core"
CLK_PERIOD_NS = 10

# project_regbank.yml, access RW (name, byte address); each resets to 0 (no reset value declared).
RW_REGS = (
    ("DATALOGGER_CONFIG", 0x020), ("EMIO_MUX_SEL", 0x024), ("I2C_INJ_CONTROL", 0x030), ("I2C_INJ_DATAIN", 0x034),
    ("SPI_INJ_CONTROL", 0x040), ("SPI_INJ_DATAIN", 0x044), ("TELEMETRY_CTRL", 0x140),
    ("TELEMETRY_SNAPSHOT_TRIGGER", 0x144), ("RING_READ_PTR", 0x154), ("EYE_CONTROL", 0x200),
    ("EYE_BIT_TARGET", 0x204), ("STIM_UART_CONTROL", 0x220), ("STIM_UART_BAUD_DIV", 0x224),
    ("STIM_UART_DATA", 0x228), ("STIM_I2C_CONTROL", 0x22C), ("STIM_I2C_CLK_DIV", 0x230), ("STIM_I2C_DATA", 0x234),
    ("STIM_SPI_CONTROL", 0x238), ("STIM_SPI_CLK_DIV", 0x23C), ("STIM_SPI_DATA", 0x240), ("STIM_EDGE_CONTROL", 0x244),
)
LOGGED = (("BUILD_DATE", 0x008), ("REGBANK_NUM_WORDS", 0x010))


PATTERN = 0xA5A5A5A5


def _yml_ro() -> list:
    d = yaml.safe_load((_ROOT / "project_regbank.yml").read_text(encoding="utf-8"))
    return [(r["name"], int(r["address"])) for r in d["registers"] if r.get("access") in ("RO", "ROM")]


def _yml_rw() -> set:
    d = yaml.safe_load((_ROOT / "project_regbank.yml").read_text(encoding="utf-8"))
    return {(r["name"], int(r["address"])) for r in d["registers"] if r.get("access") == "RW"}


@cocotb.test()
async def test_rw_registers_read_their_reset_value_after_reset(dut):
    assert set(RW_REGS) == _yml_rw(), f"RW_REGS drifted from project_regbank.yml: {set(RW_REGS) ^ _yml_rw()}"
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, unit="ns").start())
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)

    async def rd(addr: int) -> int:
        return int.from_bytes((await axi.read(addr, 4)).data, "little")

    logged = {name: await rd(addr) for name, addr in LOGGED}
    got = {name: await rd(addr) for name, addr in RW_REGS}
    dut._log.info(f"logged: {[(n, hex(v)) for n, v in logged.items()]}")
    dut._log.info(f"RW after reset: {[(n, hex(v)) for n, v in got.items()]}")
    nonzero = {n: hex(v) for n, v in got.items() if v != 0}
    assert not nonzero, f"RW registers not at their reset value 0 after reset: {nonzero}; logged {logged}"


@cocotb.test()
async def test_ro_and_rom_registers_ignore_axi_writes(dut):
    ro = _yml_ro()
    assert len(ro) >= 50, f"expected the yml's RO/ROM registers, got {len(ro)}"
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, unit="ns").start())
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)
    for _, addr in ro:
        await axi.write(addr, PATTERN.to_bytes(4, "little"))
    await ClockCycles(dut.CLK, 16)
    back = {name: int.from_bytes((await axi.read(addr, 4)).data, "little") for name, addr in ro}
    took = sorted(n for n, v in back.items() if v == PATTERN)
    dut._log.info(f"RO/ROM after a {PATTERN:#x} write: {[(n, hex(v)) for n, v in back.items()]}")
    assert not took, f"{len(took)} of {len(ro)} RO/ROM registers kept an AXI write of {PATTERN:#x}: {took}"


def _hex_word_count(name: str) -> int:
    path = _ROOT / "hw" / "rtl" / "regbank" / name
    return sum(1 for line in path.read_text(encoding="ascii").splitlines() if line.strip())


@cocotb.test()
async def test_manifest_count_registers_read_their_rom_word_counts(dut):
    want = {"REGBANK_NUM_WORDS": (0x010, _hex_word_count("regbank_schema.hex")),
            "CONFIG_NUM_WORDS": (0x018, _hex_word_count("config_schema.hex"))}
    assert want["CONFIG_NUM_WORDS"][1] == 0xB, f"config_schema.hex word count {want['CONFIG_NUM_WORDS'][1]}, stated 11"
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, unit="ns").start())
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)
    got = {n: int.from_bytes((await axi.read(a, 4)).data, "little") for n, (a, _) in want.items()}
    dut._log.info(f"manifest counts: {[(n, hex(v), hex(want[n][1])) for n, v in got.items()]}")
    bad = {n: (hex(v), hex(want[n][1])) for n, v in got.items() if v != want[n][1]}
    assert not bad, f"manifest count registers (read, expected): {bad}"


if __name__ == "__main__":
    run_core_seam("test_regbank_reset_values_ds_p2_317", top_level=TOP)
