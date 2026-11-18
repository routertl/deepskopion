# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""
test_pfsoc_fic0_regbank_read_ds_p2_305 — DS-P2.305 desk step (b): the regbank,
read through the PolarFire FIC0 path the way Linux `busybox devmem` reads it.

On the PolarFire SoC kit (fabric a831f737, DS-P1.88 C runs 1-3) the board
stopped answering console and ping during repeated devmem reads of the regbank
ROM slots. The ROM streaming itself is covered by test_regbank_rom_stream_ds_p1_82,
but through an AXI-Lite master straight on the wrapper. Nothing drove the
PolarFire front end: the 64-bit FIC0 lane shim, lwh2f_axilite_bridge (AXI4 full
to Lite, ID echo latched on the address handshake, RLAST tied high) and
axil_demux4.

tb_pfsoc_fic0_regbank_read wires that chain exactly as deepskopion_pfsoc_top
does. These tests drive it with routertl's native Axi4Master (64-bit data, 8-bit
id, 38-bit address), one single-beat 4-byte read per devmem call:
REGBANK_NUM_WORDS at <FIC_BASE>+0x10, then REGBANK_DATA at <FIC_BASE>+0x14 for
every word. Each read is checked for its data lane, RRESP, RLAST and that RID
echoes its own ARID, under R-channel backpressure, idle gaps and changing ids.

Expected values: the 12 config words are hard-coded (ROUTERTL-002). The regbank
words are the ROM's own source file, and their count is cross-checked against
C_REGBANK_WORDS in rom_info_pkg.vhd, which is generated independently of the
simulation, so a short or wrapped stream cannot match.

Run:  rr sim run test_pfsoc_fic0_regbank_read_ds_p2_305
"""
from __future__ import annotations

import random
import re
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import routertl_core as _rrc

sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from rr_cocotb_tb.drivers.axi4 import Axi4Master, AxiResp  # noqa: E402
from rr_cocotb_tb.env import TbEnv  # noqa: E402

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[5]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"
REGBANK_HEX = DEEPSKOPION_RTL / "regbank" / "regbank_schema.hex"
CONFIG_HEX = DEEPSKOPION_RTL / "regbank" / "config_schema.hex"
ROM_INFO_PKG = DEEPSKOPION_RTL / "pkg" / "rom_info_pkg.vhd"
PFSOC_TOP = DEEPSKOPION_RTL / "pfsoc" / "deepskopion_pfsoc_fabric.vhd"
TB = DEEPSKOPION_ROOT / "hw" / "sim" / "tb_pfsoc_fic0_regbank_read.vhd"

#: FIC0 base of the regbank in the MSS memory map (bootargs ds_mmio_base).
FIC_BASE = 0x6000_0000
SLOT_REGBANK_NUM_WORDS = 0x10
SLOT_REGBANK_DATA = 0x14
SLOT_CONFIG_NUM_WORDS = 0x18
SLOT_CONFIG_DATA = 0x1C
SIZE_4_BYTES = 2  # ARSIZE for a 32-bit devmem read

#: config_schema.hex, hard-coded (11 words: the DS-P1.104 default mix, bb0a0f50).
CONFIG_WORDS = [
    0xC0A1DE02, 0x00000009, 0x0A140100, 0x06010100, 0x06020100, 0x06030100,
    0x02040200, 0x02050200, 0x03080400, 0x010C0100, 0x010D0100,
]

GENERICS = {
    "G_REGBANK_HEX_FILE": str(REGBANK_HEX),
    "G_CONFIG_HEX_FILE": str(CONFIG_HEX),
}

#: The three lane-shim assignments the bench copies from the top.
SHIM_LINES = (
    "fic0_wdata <= fic0_wdata64(63 downto 32) when fic0_wstrb8(7 downto 4) /= \"0000\"",
    "else fic0_wdata64(31 downto 0);",
    "fic0_wstrb <= fic0_wstrb8(7 downto 4) or fic0_wstrb8(3 downto 0);",
    "fic0_rdata64 <= fic0_rdata & fic0_rdata;",
)


def _hexwords(path: Path) -> list[int]:
    return [int(l.strip(), 16) for l in path.read_text(encoding="ascii").splitlines() if l.strip()]


def _pkg_words(name: str) -> int:
    m = re.search(rf'{name}\s*:.*:= x"([0-9a-fA-F]+)"', ROM_INFO_PKG.read_text(encoding="utf-8"))
    assert m, f"{name} not found in rom_info_pkg.vhd"
    return int(m.group(1), 16)


class RidChecker:
    """Records the RID, RLAST and RRESP of every R handshake on the FIC0 port."""

    def __init__(self, dut):
        self.dut = dut
        self.beats: list[tuple[int, int, int]] = []

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            if int(self.dut.s_axi_rvalid.value) and int(self.dut.s_axi_rready.value):
                self.beats.append((int(self.dut.s_axi_rid.value),
                                   int(self.dut.s_axi_rlast.value),
                                   int(self.dut.s_axi_rresp.value)))


async def start(dut, *, seed: int | None = None):
    tb = TbEnv(dut, clk="clk", rst="rst", period_ns=10, auto_attach_from_env=False)
    for name in ("awid", "awaddr", "awprot", "awlen", "awsize", "awburst", "awcache",
                 "awqos", "awvalid", "wdata", "wstrb", "wlast", "wvalid", "bready",
                 "arid", "araddr", "arprot", "arlen", "arsize", "arburst", "arcache",
                 "arqos", "arvalid", "rready"):
        getattr(dut, f"s_axi_{name}").value = 0
    await tb.start_clock()
    await tb.reset(cycles=8, active_low=False)
    await ClockCycles(dut.clk, 4)
    bp = None if seed is None else Axi4Master.random_backpressure(seed=seed, p_ready=0.5)
    axi = Axi4Master(tb, prefix="s_axi", raise_on_resp=False, rb_backpressure=bp,
                     strict_protocol=True)
    axi.enable_protocol_monitor()
    rid = RidChecker(dut)
    cocotb.start_soon(rid.run())
    return tb, axi, rid


async def devmem(axi: Axi4Master, offset: int, arid: int) -> tuple[int, AxiResp]:
    """One `busybox devmem <FIC_BASE+offset> 32`: a single-beat 4-byte read.
    The 32-bit word rides the 64-bit lane its address selects (addr[2])."""
    addr = FIC_BASE + offset
    data, resp = await axi.read(addr, beats=1, id=arid, size=SIZE_4_BYTES, timeout_ns=20000)
    assert len(data) == 1, f"read @0x{addr:08X} returned {len(data)} beats"
    lane = (data[0] >> 32) & 0xFFFFFFFF if addr & 0x4 else data[0] & 0xFFFFFFFF
    return lane, resp


async def drain(dut, axi, rid: RidChecker, num_slot: int, data_slot: int, *,
                seed: int, max_gap: int, n: int | None = None) -> tuple[int, list[int], list[int]]:
    rng = random.Random(seed)
    ids: list[int] = []
    first_id = rng.randrange(256)
    count, resp = await devmem(axi, num_slot, first_id)
    ids.append(first_id)
    assert resp == AxiResp.OKAY, f"NUM_WORDS read RRESP {resp.name}"
    words = []
    for _ in range(count if n is None else n):
        if max_gap:
            await ClockCycles(dut.clk, rng.randrange(max_gap + 1))
        arid = rng.randrange(256)
        w, resp = await devmem(axi, data_slot, arid)
        assert resp == AxiResp.OKAY, f"DATA read {len(words)} RRESP {resp.name}"
        ids.append(arid)
        words.append(w)
    return count, words, ids


def _check_r_channel(rid: RidChecker, ids: list[int]) -> None:
    assert len(rid.beats) == len(ids), (
        f"{len(rid.beats)} R handshakes for {len(ids)} reads")
    for i, ((got_id, last, rresp), want_id) in enumerate(zip(rid.beats, ids)):
        assert got_id == want_id, f"read {i}: RID 0x{got_id:02X}, ARID was 0x{want_id:02X}"
        assert last == 1, f"read {i}: RLAST {last}"
        assert rresp == 0, f"read {i}: RRESP {rresp}"


def _first_mismatch(got: list[int], want: list[int]) -> str:
    for i, (g, w) in enumerate(zip(got, want)):
        if g != w:
            return f"first mismatch at word {i}: got 0x{g:08X}, want 0x{w:08X}"
    return f"length {len(got)} vs {len(want)}"


@cocotb.test()
async def test_devmem_pattern_drains_the_whole_regbank_rom(dut):
    """NUM_WORDS then every REGBANK_DATA word through FIC0, ids changing per read,
    random R backpressure and 0-3 idle clocks between reads."""
    want = _hexwords(REGBANK_HEX)
    assert len(want) == _pkg_words("C_REGBANK_WORDS"), (
        f"regbank_schema.hex has {len(want)} words, rom_info_pkg says "
        f"{_pkg_words('C_REGBANK_WORDS')}: the two generated files disagree")
    _, axi, rid = await start(dut, seed=305)
    count, got, ids = await drain(dut, axi, rid, SLOT_REGBANK_NUM_WORDS, SLOT_REGBANK_DATA,
                                  seed=305, max_gap=3)
    assert count == len(want), f"REGBANK_NUM_WORDS read {count}, want {len(want)}"
    assert got == want, _first_mismatch(got, want)
    _check_r_channel(rid, ids)
    assert not axi.protocol_violations, axi.protocol_violations


@cocotb.test()
async def test_devmem_pattern_back_to_back_without_backpressure(dut):
    """The same drain with RREADY held high and no idle clocks: the fastest
    pattern the MSS could present."""
    want = _hexwords(REGBANK_HEX)
    _, axi, rid = await start(dut, seed=None)
    count, got, ids = await drain(dut, axi, rid, SLOT_REGBANK_NUM_WORDS, SLOT_REGBANK_DATA,
                                  seed=7, max_gap=0)
    assert count == len(want)
    assert got == want, _first_mismatch(got, want)
    _check_r_channel(rid, ids)
    assert not axi.protocol_violations, axi.protocol_violations


@cocotb.test()
async def test_devmem_pattern_with_long_gaps(dut):
    """The first 300 words with 0-400 idle clocks between reads (devmem calls
    are milliseconds apart on silicon; this checks no state ages out)."""
    want = _hexwords(REGBANK_HEX)[:300]
    _, axi, rid = await start(dut, seed=11)
    _, got, ids = await drain(dut, axi, rid, SLOT_REGBANK_NUM_WORDS, SLOT_REGBANK_DATA,
                              seed=11, max_gap=400, n=300)
    assert got == want, _first_mismatch(got, want)
    _check_r_channel(rid, ids)


@cocotb.test()
async def test_devmem_pattern_drains_the_config_rom(dut):
    _, axi, rid = await start(dut, seed=13)
    count, got, ids = await drain(dut, axi, rid, SLOT_CONFIG_NUM_WORDS, SLOT_CONFIG_DATA,
                                  seed=13, max_gap=3)
    assert count == len(CONFIG_WORDS), f"CONFIG_NUM_WORDS read {count}"
    assert got == CONFIG_WORDS, _first_mismatch(got, CONFIG_WORDS)
    _check_r_channel(rid, ids)


@cocotb.test()
async def test_the_bench_shim_is_the_tops_shim(dut):
    """The lane shim is copied into the bench; refuse a copy that drifted."""
    top = " ".join(PFSOC_TOP.read_text(encoding="utf-8").split())
    tb = " ".join(TB.read_text(encoding="utf-8").split())
    for line in SHIM_LINES:
        norm = " ".join(line.split())
        assert norm in top, f"the top no longer has: {line}"
        assert norm in tb, f"the bench no longer has: {line}"


# ─────────────────────────────────────────────────────────────────────────
#  Standalone Execution (ROUTERTL-001 — engine.simulation.run_simulation only)
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from engine.simulation import run_simulation

    run_simulation(
        top_level="tb_pfsoc_fic0_regbank_read",
        module="test_pfsoc_fic0_regbank_read_ds_p2_305",
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
                f"{DEEPSKOPION_RTL}/de25/lwh2f_axilite_bridge.vhd",
                f"{DEEPSKOPION_RTL}/pfsoc/axil_demux4.vhd",
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_pfsoc_fic0_regbank_read.vhd",
            ],
        },
    )
