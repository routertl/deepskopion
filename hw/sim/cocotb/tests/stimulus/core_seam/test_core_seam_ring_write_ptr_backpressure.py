# rr unknown-llm (AI-generated)
"""
test_core_seam_ring_write_ptr_backpressure: RING_WRITE_PTR must not advance on
a cycle where the framer's OUT_READY (ddr_s_axis_ready) is low.

Audit finding 17 (docs vs RTL, 99ecebd9): FDD/regbank say RING_WRITE_PTR is the
"number of 32-bit messages written into the ring", but deepskopion_core.vhd:1846
increments ring_write_cnt_r on framer_out_valid alone, without ddr_s_axis_ready.
zs_msg_framer holds OUT_VALID while OUT_READY=0, so under back-pressure the
counter would include stalled cycles and run ahead of the words actually handed
to the DDR stream. This test only observes; it changes no RTL.

Method: the REAL tich.deepskopion_core (tb_stimulus_core_seam.vhd, DDR AXI side
tied idle). One lifecycle pair is provoked through the core's own AXI-Lite port,
then, every clk_sys edge, the pre-edge values of framer_out_valid,
ddr_s_axis_ready and ring_write_cnt_r are sampled.

Verdicts (each a separate assertion, so a red names its cause):
  * presence: the test must SEE presented words and at least MIN_STALLED_CYCLES
    stalled cycles (valid=1, ready=0), otherwise nothing was exercised and the
    test FAILS as "stall not exercised", never as a pass.
  * no-advance-on-stall: counter(k+1) == counter(k) whenever valid(k)=1 and
    ready(k)=0. RED if the pointer advances while OUT_READY=0.
  * count == accepted words: the internal counter and the RING_WRITE_PTR
    register both equal the number of cycles with valid=1 and ready=1.

Run (from the deepskopion dir):  rr sim run test_core_seam_ring_write_ptr_backpressure
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

from _core_seam_common import run_core_seam, start

SLOT_DATALOGGER_CONFIG = 0x20
SLOT_RING_WRITE_PTR = 336
UART_ID = 12
OBSERVE_CYCLES = 100_000  # 1 ms at 100 MHz
FLOOD_PAIRS = 200
MIN_STALLED_CYCLES = 1000   # a real stall, not one stray cycle


def _w(v: int) -> bytes:
    return (v & 0xFFFFFFFF).to_bytes(4, "little")


@cocotb.test()
async def test_ring_write_ptr_does_not_advance_while_out_ready_low(dut):
    axi = await start(dut)
    core = dut.u_core

    samples = []   # (valid, ready, cnt) as seen just before each rising edge

    async def sampler():
        for _ in range(OBSERVE_CYCLES):
            await RisingEdge(dut.CLK)
            samples.append((
                int(core.framer_out_valid.value),
                int(core.ddr_s_axis_ready.value),
                int(core.ring_write_cnt_r.value),
            ))

    task = cocotb.start_soon(sampler())

    # Each disable/enable pair on a datalogger makes the framer present a few
    # frames. The DDR AXI side of the seam bench is tied idle, so enough pairs
    # fill the stream's input FIFO and the framer meets a low OUT_READY with no
    # forcing of any internal signal. A run that never stalls FAILS (see
    # the presence assertion below).
    for _ in range(FLOOD_PAIRS):
        await axi.write(SLOT_DATALOGGER_CONFIG, _w(UART_ID << 11))
        await ClockCycles(dut.CLK, 40)
        await axi.write(SLOT_DATALOGGER_CONFIG, _w((UART_ID << 11) | 0x0D))
        await ClockCycles(dut.CLK, 40)
    await task

    reg = int.from_bytes((await axi.read(SLOT_RING_WRITE_PTR, 4)).data, "little")

    presented = sum(1 for v, _r, _c in samples if v)
    accepted = sum(1 for v, r, _c in samples if v and r)
    stalled = [k for k, (v, r, _c) in enumerate(samples) if v and not r]
    dut._log.info(
        f"framer presented={presented} cycles, accepted={accepted}, "
        f"stalled={len(stalled)}, final ring_write_cnt_r={samples[-1][2]}, "
        f"RING_WRITE_PTR register={reg}")

    # Presence of the instrument first: a run that never presented a word, or
    # never met a low OUT_READY, exercised nothing and must not read as a pass.
    assert presented > 0, (
        "no frame was ever presented by the framer: the test exercised nothing "
        "(not a pass)")
    assert len(stalled) >= MIN_STALLED_CYCLES, (
        f"the stall was not exercised: only {len(stalled)} cycles with "
        f"framer_out_valid=1 and ddr_s_axis_ready=0 (need >= {MIN_STALLED_CYCLES}). "
        f"Raise FLOOD_PAIRS; a green without a stall says nothing about the "
        f"back-pressure branch")

    advanced_on_stall = [
        k for k in stalled
        if k + 1 < len(samples) and samples[k + 1][2] != samples[k][2]
    ]
    assert not advanced_on_stall, (
        f"ring_write_cnt_r advanced on {len(advanced_on_stall)} of "
        f"{len(stalled)} stalled cycles (framer_out_valid=1, ddr_s_axis_ready=0); "
        f"first at sample {advanced_on_stall[0]}: "
        f"{samples[advanced_on_stall[0]][2]} -> "
        f"{samples[advanced_on_stall[0] + 1][2]}. RING_WRITE_PTR runs ahead of "
        f"the words the DDR stream accepted")

    assert reg == accepted, (
        f"RING_WRITE_PTR register reads {reg} but the DDR stream accepted "
        f"{accepted} words")

    assert samples[-1][2] == accepted, (
        f"ring_write_cnt_r={samples[-1][2]} but the DDR stream accepted "
        f"{accepted} words (valid&ready cycles)")


if __name__ == "__main__":
    run_core_seam("test_core_seam_ring_write_ptr_backpressure")
