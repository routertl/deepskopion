# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""
test_pfsoc_fic0_rom_stream_rdloop_ds_p2_305 — DS-P2.305 done-when 3, desk/sim step: the silicon recipe that stops
the PolarFire kit, replayed through the PolarFire FIC0 front end in simulation.

Silicon (PREREG 305-A1, fabric a831f737, 2026-11-16): `rdloop.sh` = 3 rounds of [one read of REGBANK_NUM_WORDS
(<FIC_BASE>+0x10, resets the ROM stream pointer), then 7357 reads of REGBANK_DATA (+0x14)] stopped the kit in 3 of 3
runs (after about 8357, 9357 and 4000 reads); the same script reading GIT_HASH (+0x00) completed 3 of 3.

tb_pfsoc_fic0_regbank_read (the FIC0 lane shim, lwh2f_axilite_bridge, axil_demux4 and system_regbank_wrapper, wired as
deepskopion_pfsoc_top wires them, all on one clock as in the top) is driven by routertl's shipped Axi4Master, one
single-beat 4-byte read per devmem call (helpers from test_pfsoc_fic0_regbank_read_ds_p2_305). Every read must
complete within the BFM timeout; a hang is an AxiTimeoutError, i.e. a red test.

Pre-stated prediction (before the first run): GREEN for all five tests. The fabric RTL of this path already passes a
single drain (test_pfsoc_fic0_regbank_read_ds_p2_305); nothing in the RTL is known to depend on the round count or the
read phase. A red test is a finding (a hang or a protocol violation on the ROM stream port), filed with MC.

Not modelled here (the follow-up owning layer if this stays green): the MSS FIC0 hard interface and its clock
crossing, the exact AXI shape a Linux devmem load presents on silicon, and the Libero-inferred ROM primitive behind
regbank_rom.

Expected values: the regbank words are the ROM's own source file; GIT_HASH is the bench's tied REGS_IN value 0
(the core, not this bench, drives C_GIT_HASH) (ROUTERTL-002).

Run:  rr sim run test_pfsoc_fic0_rom_stream_rdloop_ds_p2_305
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_pfsoc_fic0_regbank_read_ds_p2_305 import (  # noqa: E402
    DEEPSKOPION_ROOT, DEEPSKOPION_RTL, GENERICS, REGBANK_HEX, SLOT_REGBANK_DATA,
    SLOT_REGBANK_NUM_WORDS, AxiResp, _check_r_channel, _first_mismatch, _hexwords, devmem, drain, start,
)

SLOT_GIT_HASH = 0x00
ROUNDS = 3          # rdloop.sh R
N_PER_ROUND = 7357  # rdloop.sh N (the a831f737 ROM length); the stream here is the current hex


async def _round(dut, axi, rid, seed: int, max_gap: int, want: list[int], ids: list[int]) -> None:
    n = min(N_PER_ROUND, len(want))
    count, got, rids = await drain(dut, axi, rid, SLOT_REGBANK_NUM_WORDS, SLOT_REGBANK_DATA,
                                   seed=seed, max_gap=max_gap, n=n)
    ids.extend(rids)
    assert count == len(want), f"REGBANK_NUM_WORDS read {count}, want {len(want)}"
    assert got == want[:n], _first_mismatch(got, want[:n])


@cocotb.test()
async def test_rdloop_three_rounds_back_to_back(dut):
    """The silicon recipe: 3 x [NUM_WORDS, then N DATA reads], RREADY high, no idle clocks."""
    want = _hexwords(REGBANK_HEX)
    _, axi, rid = await start(dut, seed=None)
    ids: list[int] = []
    for k in range(ROUNDS):
        await _round(dut, axi, rid, seed=3050 + k, max_gap=0, want=want, ids=ids)
    _check_r_channel(rid, ids)
    assert not axi.protocol_violations, axi.protocol_violations


@cocotb.test()
async def test_rdloop_three_rounds_with_backpressure_and_gaps(dut):
    """The same 3 rounds under random R backpressure and 0-3 idle clocks between reads."""
    want = _hexwords(REGBANK_HEX)
    _, axi, rid = await start(dut, seed=3051)
    ids: list[int] = []
    for k in range(ROUNDS):
        await _round(dut, axi, rid, seed=3060 + k, max_gap=3, want=want, ids=ids)
    _check_r_channel(rid, ids)
    assert not axi.protocol_violations, axi.protocol_violations


@cocotb.test()
async def test_rom_data_reads_at_every_gap_phase(dut):
    """For every idle gap 0..31 clocks: NUM_WORDS, then 48 DATA reads with exactly that gap. Sweeps the phase of a
    DATA read against the ROM's read and pointer-advance cycles."""
    want = _hexwords(REGBANK_HEX)
    _, axi, rid = await start(dut, seed=None)
    ids: list[int] = []
    for gap in range(32):
        count, resp = await devmem(axi, SLOT_REGBANK_NUM_WORDS, gap)
        ids.append(gap)
        assert resp == AxiResp.OKAY and count == len(want), f"gap {gap}: NUM_WORDS {count} {resp.name}"
        got = []
        for i in range(48):
            await ClockCycles(dut.clk, gap)
            w, resp = await devmem(axi, SLOT_REGBANK_DATA, (gap + i) & 0xFF)
            ids.append((gap + i) & 0xFF)
            assert resp == AxiResp.OKAY, f"gap {gap} read {i}: RRESP {resp.name}"
            got.append(w)
        assert got == want[:48], f"gap {gap}: " + _first_mismatch(got, want[:48])
    _check_r_channel(rid, ids)
    assert not axi.protocol_violations, axi.protocol_violations


@cocotb.test()
async def test_reads_past_the_end_of_the_stream_still_complete(dut):
    """After a full drain, 64 more DATA reads without a pointer reset. Prohibition: no read may hang or return a
    protocol error. The data value past the end is not specified and is recorded, not judged."""
    want = _hexwords(REGBANK_HEX)
    _, axi, rid = await start(dut, seed=None)
    count, got, ids = await drain(dut, axi, rid, SLOT_REGBANK_NUM_WORDS, SLOT_REGBANK_DATA, seed=3070, max_gap=0)
    assert got == want, _first_mismatch(got, want)
    past = []
    for i in range(64):
        w, resp = await devmem(axi, SLOT_REGBANK_DATA, i)
        ids.append(i)
        assert resp == AxiResp.OKAY, f"read {i} past the end: RRESP {resp.name}"
        past.append(w)
    dut._log.info(f"64 reads past the end: distinct values {sorted({hex(v) for v in past})[:8]}")
    _check_r_channel(rid, ids)
    assert not axi.protocol_violations, axi.protocol_violations


@cocotb.test()
async def test_git_hash_reads_between_stream_reads_do_not_move_the_pointer(dut):
    """NUM_WORDS, then 600 x [DATA, GIT_HASH]: the DATA stream equals the hex prefix (arm B's register read between
    arm A's reads does not move the stream pointer). GIT_HASH reads the wrapper's REGS_IN value: on silicon
    deepskopion_core drives it with C_GIT_HASH (deepskopion_core.vhd:2417); this bench ties every REGS_IN word to 0
    (tb_pfsoc_fic0_regbank_read.vhd:102), so the expected value here is 0 (first run measured 0x0)."""
    want = _hexwords(REGBANK_HEX)
    git = 0x00000000
    _, axi, rid = await start(dut, seed=None)
    count, resp = await devmem(axi, SLOT_REGBANK_NUM_WORDS, 1)
    ids = [1]
    assert count == len(want) and resp == AxiResp.OKAY
    rng = random.Random(3080)
    got, hashes = [], []
    for i in range(600):
        a, b = rng.randrange(256), rng.randrange(256)
        w, r1 = await devmem(axi, SLOT_REGBANK_DATA, a)
        h, r2 = await devmem(axi, SLOT_GIT_HASH, b)
        ids += [a, b]
        assert r1 == AxiResp.OKAY and r2 == AxiResp.OKAY, f"step {i}: RRESP {r1.name}/{r2.name}"
        got.append(w)
        hashes.append(h)
    assert got == want[:600], _first_mismatch(got, want[:600])
    bad = [(i, hex(h)) for i, h in enumerate(hashes) if h != git]
    assert not bad, f"GIT_HASH reads differ from the bench's tied value 0x{git:08X}: {bad[:5]}"
    _check_r_channel(rid, ids)
    assert not axi.protocol_violations, axi.protocol_violations


if __name__ == "__main__":
    from engine.simulation import run_simulation

    run_simulation(
        top_level="tb_pfsoc_fic0_regbank_read",
        module="test_pfsoc_fic0_rom_stream_rdloop_ds_p2_305",
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
