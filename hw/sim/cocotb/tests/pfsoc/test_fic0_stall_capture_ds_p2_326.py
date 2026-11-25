# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""
test_fic0_stall_capture_ds_p2_326 — DS-P2.326: what the PolarFire FIC0 read analyzer would store, through the real
FIC0 front end.

tb_pfsoc_fic0_stall_capture drives tb_pfsoc_fic0_regbank_read (lane shim, lwh2f_axilite_bridge, axil_demux4,
regbank, wired as deepskopion_pfsoc_fabric wires them) with routertl's shipped Axi4Master, packs the FIC0 read
channel into the fabric's probe word, and runs the bench top's fic0_stall_trigger and fic0_rea_sampler on it. The test
keeps every sample whose event bit (36) is set: exactly what the analyzer stores with its one storage qualifier.

Expected values are hard-coded (ROUTERTL-002): N = 64 stall cycles (the tb generic), REGBANK_NUM_WORDS at +0x10 and
REGBANK_DATA at +0x14 (low address byte 0x10 / 0x14), RRESP OKAY = 0.

Run:  rr sim run test_fic0_stall_capture_ds_p2_326
"""
from __future__ import annotations

import random
import re
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

import routertl_core as _rrc

sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rr_cocotb_tb.drivers.axi4 import Axi4Master, AxiResp  # noqa: E402
from rr_cocotb_tb.env import TbEnv  # noqa: E402
from test_pfsoc_fic0_regbank_read_ds_p2_305 import (  # noqa: E402
    CONFIG_HEX, DEEPSKOPION_ROOT, DEEPSKOPION_RTL, REGBANK_HEX, SIZE_4_BYTES, FIC_BASE,
)

N = 64
SLOT_NUM, SLOT_DATA = 0x10, 0x14
TB = DEEPSKOPION_ROOT / "hw" / "sim" / "tb_pfsoc_fic0_stall_capture.vhd"
FABRIC = DEEPSKOPION_RTL / "pfsoc" / "deepskopion_pfsoc_fabric.vhd"
GENERICS = {"G_REGBANK_HEX_FILE": str(REGBANK_HEX), "G_CONFIG_HEX_FILE": str(CONFIG_HEX), "G_STALL_CYCLES": N}


def decode(v: int) -> dict:
    return {"arvalid": v & 1, "arready": (v >> 1) & 1, "rvalid": (v >> 2) & 1, "rready": (v >> 3) & 1,
            "rresp": (v >> 4) & 3, "araddr": (v >> 6) & 0xFF, "arid": (v >> 14) & 0xFF, "rid": (v >> 22) & 0xFF,
            "rlast": (v >> 30) & 1, "stall": (v >> 32) & 1, "outstanding": (v >> 33) & 1, "trig": (v >> 34) & 1,
            "heartbeat": (v >> 35) & 1, "event": (v >> 36) & 1}


class Store:
    """The analyzer's qualified storage: every clock's sample with event = 1."""

    def __init__(self, dut):
        self.dut = dut
        self.samples: list[dict] = []

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            s = decode(int(self.dut.sample_o.value))
            if s["event"]:
                self.samples.append(s)


class HoldR:
    """RREADY cadence for Axi4Master: low for the first `hold` clocks of each read it is consulted on, then high."""

    def __init__(self, hold: int):
        self.hold = hold
        self.n = 0

    def __call__(self) -> bool:
        self.n += 1
        return self.n > self.hold


async def start(dut, rb=None):
    tb = TbEnv(dut, clk="clk", rst="rst", period_ns=10, auto_attach_from_env=False)
    for name in ("awid", "awaddr", "awprot", "awlen", "awsize", "awburst", "awcache", "awqos", "awvalid", "wdata",
                 "wstrb", "wlast", "wvalid", "bready", "arid", "araddr", "arprot", "arlen", "arsize", "arburst",
                 "arcache", "arqos", "arvalid", "rready"):
        getattr(dut, f"s_axi_{name}").value = 0
    await tb.start_clock()
    await tb.reset(cycles=8, active_low=False)
    await ClockCycles(dut.clk, 4)
    axi = Axi4Master(tb, prefix="s_axi", raise_on_resp=False, rb_backpressure=rb, strict_protocol=True)
    store = Store(dut)
    cocotb.start_soon(store.run())
    return tb, axi, store


async def devmem(axi, offset: int, arid: int, timeout_ns: int = 20000):
    data, resp = await axi.read(FIC_BASE + offset, beats=1, id=arid, size=SIZE_4_BYTES, timeout_ns=timeout_ns)
    return resp


def _norm(text: str) -> str:
    return " ".join(text.split())


@cocotb.test()
async def test_the_bench_probe_packing_is_the_fabrics(dut):
    """The tb packs fic0_rd_probe_o with the fabric's exact expression, so the capture decoded here is the one the
    bench top produces on silicon."""
    rx = r"fic0_rd_probe_o\s*<=\s*([^;]+);"
    tb = re.search(rx, TB.read_text(encoding="utf-8"))
    fab = re.search(rx, FABRIC.read_text(encoding="utf-8"))
    assert tb and fab, "probe assignment not found"
    assert _norm(tb.group(1)) == _norm(fab.group(1)), (tb.group(1), fab.group(1))


@cocotb.test()
async def test_devmem_reads_store_one_ar_and_one_r_event_each_and_never_trigger(dut):
    """NUM_WORDS then 200 REGBANK_DATA reads, ids changing, 0-6 idle clocks: the store holds exactly 2 events per
    read (AR handshake, then its R handshake), in order, with the read's address byte and id, RRESP OKAY; no
    trigger."""
    _, axi, store = await start(dut)
    rng = random.Random(3261)
    reads = [(SLOT_NUM, rng.randrange(256))] + [(SLOT_DATA, rng.randrange(256)) for _ in range(200)]
    for off, arid in reads:
        await ClockCycles(dut.clk, rng.randrange(7))
        assert await devmem(axi, off, arid) == AxiResp.OKAY
    await ClockCycles(dut.clk, 4)
    s = store.samples
    assert len(s) == 2 * len(reads), f"{len(s)} stored events for {len(reads)} reads"
    for i, (off, arid) in enumerate(reads):
        ar, r = s[2 * i], s[2 * i + 1]
        assert ar["arvalid"] and ar["arready"] and ar["araddr"] == off and ar["arid"] == arid, (i, ar)
        assert r["rvalid"] and r["rready"] and r["rid"] == arid and r["rresp"] == 0 and r["rlast"] == 1, (i, r)
    assert all(x["trig"] == 0 for x in s)
    assert int(dut.trig_o.value) == 0


@cocotb.test()
async def test_an_r_held_back_past_n_clocks_triggers_with_the_read_still_outstanding(dut):
    """One REGBANK_DATA read whose RREADY is held low for N+20 clocks: stored events are the AR handshake, then the
    trigger with that read outstanding (its id and address in the same sample), then the R handshake; trig_o stays
    set afterwards."""
    _, axi, store = await start(dut, rb=HoldR(N + 20))
    assert await devmem(axi, SLOT_DATA, 0x5A) == AxiResp.OKAY
    await ClockCycles(dut.clk, 4)
    s = store.samples
    ar = [x for x in s if x["arvalid"] and x["arready"]]
    trig = [x for x in s if x["trig"] == 1]
    r = [x for x in s if x["rvalid"] and x["rready"]]
    assert len(ar) == 1 and ar[0]["arid"] == 0x5A and ar[0]["araddr"] == SLOT_DATA, s
    assert trig, f"no trigger in {len(s)} stored events"
    t0 = trig[0]
    assert t0["outstanding"] == 1 and t0["stall"] == 1 and t0["rready"] == 0, t0
    assert s.index(ar[0]) < s.index(t0) < s.index(r[0]), "order AR, trigger, R"
    assert r[0]["rid"] == 0x5A and r[0]["trig"] == 1, r[0]
    assert int(dut.trig_o.value) == 1


@cocotb.test()
async def test_an_r_held_back_less_than_n_clocks_never_triggers(dut):
    """The same read with RREADY low for N-8 clocks: exactly two stored events (AR, R), no trigger."""
    _, axi, store = await start(dut, rb=HoldR(N - 8))
    assert await devmem(axi, SLOT_DATA, 0x21) == AxiResp.OKAY
    await ClockCycles(dut.clk, 4)
    assert len(store.samples) == 2 and all(x["trig"] == 0 for x in store.samples), store.samples
    assert int(dut.trig_o.value) == 0


if __name__ == "__main__":
    from engine.simulation import run_simulation

    run_simulation(
        top_level="tb_pfsoc_fic0_stall_capture",
        module="test_fic0_stall_capture_ds_p2_326",
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
                f"{DEEPSKOPION_RTL}/pfsoc/fic0_stall_trigger.vhd",
                f"{DEEPSKOPION_RTL}/pfsoc/fic0_rea_sampler.vhd",
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_pfsoc_fic0_stall_capture.vhd",
            ],
        },
    )
