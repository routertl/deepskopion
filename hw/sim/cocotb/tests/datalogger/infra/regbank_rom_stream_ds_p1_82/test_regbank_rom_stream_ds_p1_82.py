"""
test_regbank_rom_stream_ds_p1_82 — DS-P1.82: the regbank streaming ROM ports
deliver every word, in order, to a compliant AXI-Lite master.

DS-P1.79 run 1 read the ROMs back on DE25 silicon (boot 33). Read i of
REGBANK_DATA returned regbank_schema.hex word (2i) mod 1024 for all 7275 reads,
CONFIG_DATA returned config words 0, 2, ..., 10 and then 0xBAD00BAD, and
CONFIG_NUM_WORDS read 0. The RTL facts behind it:

  * the wrapper advanced a pointer on every clock with ARVALID high and the DATA
    index, while the inner regbank registers ARREADY one clock after ARVALID, so
    every compliant read moved the pointer at least twice;
  * the ROMs and pointers were 10 bits (1024 words) against a 7275-word ROM, and
    regbank_rom dropped hex lines past its depth without a message;
  * nothing drove CONFIG_NUM_WORDS (and only deepskopion_core drove
    REGBANK_NUM_WORDS, so the eye tops read 0 for both).

The only earlier seam (test_regbank_wrapper_rom_seam) reads the first word only,
which every one of those defects returns correctly.

These tests drive the REAL system_regbank_wrapper (through
tb_regbank_wrapper_rom_seam, TELEMETRY_IN tied to zero as on the eye tops) with
cocotbext-axi's AxiLiteMaster, which holds ARVALID until ARREADY as AXI requires.

Expected values: the 11 config words are hard-coded (ROUTERTL-002). The 7275
regbank words are the ROM's own source file, and their count is cross-checked
against C_REGBANK_WORDS in rom_info_pkg.vhd, which is generated independently of
the simulation, so a short or wrapped stream cannot match.

Run:  rr sim run test_regbank_rom_stream_ds_p1_82
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

# Routertl SDK cocotb path (matches every sibling deepskopion test).
import routertl_core as _rrc

sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"
REGBANK_HEX = DEEPSKOPION_RTL / "regbank" / "regbank_schema.hex"
CONFIG_HEX = DEEPSKOPION_RTL / "regbank" / "config_schema.hex"
ROM_INFO_PKG = DEEPSKOPION_RTL / "pkg" / "rom_info_pkg.vhd"

# Byte offsets (== C_INDEX_* in system_regbank_pkg.vhd).
SLOT_REGBANK_NUM_WORDS = 0x10
SLOT_REGBANK_DATA = 0x14
SLOT_CONFIG_NUM_WORDS = 0x18
SLOT_CONFIG_DATA = 0x1C

#: config_schema.hex, hard-coded (11 words: the DS-P1.104 default mix, bb0a0f50).
CONFIG_WORDS = [
    0xC0A1DE02, 0x00000009, 0x0A140100, 0x06010100, 0x06020100, 0x06030100,
    0x02040200, 0x02050200, 0x03080400, 0x010C0100, 0x010D0100,
]

CLK_PERIOD_NS = 10  # 100 MHz clk_sys

GENERICS = {
    "G_REGBANK_HEX_FILE": str(REGBANK_HEX),
    "G_CONFIG_HEX_FILE": str(CONFIG_HEX),
}


def _hexwords(path: Path) -> list[int]:
    return [int(l.strip(), 16) for l in path.read_text(encoding="ascii").splitlines() if l.strip()]


def _pkg_words(name: str) -> int:
    m = re.search(rf'{name}\s*:.*:= x"([0-9a-fA-F]+)"', ROM_INFO_PKG.read_text(encoding="utf-8"))
    assert m, f"{name} not found in rom_info_pkg.vhd"
    return int(m.group(1), 16)


async def start(dut) -> AxiLiteMaster:
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 8)
    return axi


async def rd(axi: AxiLiteMaster, addr: int) -> int:
    return int.from_bytes(await axi.read(addr, 4), "little")


async def stream(dut, axi: AxiLiteMaster, num_slot: int, data_slot: int, n: int,
                 gap: int = 0) -> tuple[int, list[int]]:
    """NUM_WORDS (resets the pointer), then n DATA reads, `gap` idle clocks apart."""
    count = await rd(axi, num_slot)
    words = []
    for _ in range(n):
        if gap:
            await ClockCycles(dut.CLK, gap)
        words.append(await rd(axi, data_slot))
    return count, words


def _first_mismatch(got: list[int], want: list[int]) -> str:
    i = next((k for k, (a, b) in enumerate(zip(got, want)) if a != b), None)
    if i is None:
        return f"lengths {len(got)} vs {len(want)}"
    return f"first mismatch at word {i}: read 0x{got[i]:08X}, expected 0x{want[i]:08X}"


@cocotb.test()
async def test_whole_regbank_rom_streams_in_order(dut):
    """REGBANK_NUM_WORDS reads 7275, then 7275 DATA reads return the whole ROM in order."""
    want = _hexwords(REGBANK_HEX)
    assert len(want) == _pkg_words("C_REGBANK_WORDS"), (
        f"regbank_schema.hex has {len(want)} words, rom_info_pkg says "
        f"{_pkg_words('C_REGBANK_WORDS')}: the two generated files disagree")
    axi = await start(dut)
    count, got = await stream(dut, axi, SLOT_REGBANK_NUM_WORDS, SLOT_REGBANK_DATA, len(want))
    eq = sum(1 for a, b in zip(got, want) if a == b)
    assert got == want, f"REGBANK ROM: {eq} of {len(want)} words equal; {_first_mismatch(got, want)}"
    assert count == len(want), f"REGBANK_NUM_WORDS read {count}, expected {len(want)}"


@cocotb.test()
async def test_whole_config_rom_streams_in_order(dut):
    """CONFIG_NUM_WORDS reads 11, then 11 DATA reads return the hard-coded config words."""
    assert _hexwords(CONFIG_HEX) == CONFIG_WORDS, "config_schema.hex changed: update CONFIG_WORDS"
    axi = await start(dut)
    count, got = await stream(dut, axi, SLOT_CONFIG_NUM_WORDS, SLOT_CONFIG_DATA, len(CONFIG_WORDS))
    assert got == CONFIG_WORDS, f"CONFIG ROM: {_first_mismatch(got, CONFIG_WORDS)}"
    assert count == 11, f"CONFIG_NUM_WORDS read {count}, expected 11"


@cocotb.test()
async def test_streams_with_idle_gaps_between_reads(dut):
    """Silicon cadence: a CPU devmem read is far slower than the bus. With 40 idle
    clocks between reads the first 64 regbank words and all 11 config words still
    stream in order."""
    want = _hexwords(REGBANK_HEX)[:64]
    axi = await start(dut)
    _, got = await stream(dut, axi, SLOT_REGBANK_NUM_WORDS, SLOT_REGBANK_DATA, 64, gap=40)
    assert got == want, f"REGBANK ROM with gaps: {_first_mismatch(got, want)}"
    _, cfg = await stream(dut, axi, SLOT_CONFIG_NUM_WORDS, SLOT_CONFIG_DATA, 11, gap=40)
    assert cfg == CONFIG_WORDS, f"CONFIG ROM with gaps: {_first_mismatch(cfg, CONFIG_WORDS)}"


@cocotb.test()
async def test_num_words_read_resets_the_pointer_mid_stream(dut):
    """A NUM_WORDS read mid-stream restarts that ROM at word 0 and leaves the other ROM alone."""
    rom = _hexwords(REGBANK_HEX)
    axi = await start(dut)
    _, first = await stream(dut, axi, SLOT_REGBANK_NUM_WORDS, SLOT_REGBANK_DATA, 5)
    assert first == rom[:5], f"REGBANK first 5: {_first_mismatch(first, rom[:5])}"
    _, cfg = await stream(dut, axi, SLOT_CONFIG_NUM_WORDS, SLOT_CONFIG_DATA, 3)
    assert cfg == CONFIG_WORDS[:3]
    nxt = await rd(axi, SLOT_REGBANK_DATA)
    assert nxt == rom[5], f"REGBANK word 5 after a CONFIG read: 0x{nxt:08X} != 0x{rom[5]:08X}"
    _, again = await stream(dut, axi, SLOT_REGBANK_NUM_WORDS, SLOT_REGBANK_DATA, 3)
    assert again == rom[:3], f"REGBANK after reset: {_first_mismatch(again, rom[:3])}"


# ─────────────────────────────────────────────────────────────────────────
#  Standalone Execution (ROUTERTL-001 — engine.simulation.run_simulation only)
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from engine.simulation import run_simulation

    run_simulation(
        top_level="tb_regbank_wrapper_rom_seam",
        module="test_regbank_rom_stream_ds_p1_82",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=False,
        work_lib="tich",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/rom_info_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/system_regbank_pkg.vhd",
                f"{DEEPSKOPION_RTL}/regbank/system_regbank.vhd",
                f"{DEEPSKOPION_RTL}/regbank/regbank_rom.vhd",
                f"{DEEPSKOPION_RTL}/regbank/system_regbank_wrapper.vhd",
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_regbank_wrapper_rom_seam.vhd",
            ],
        },
    )
