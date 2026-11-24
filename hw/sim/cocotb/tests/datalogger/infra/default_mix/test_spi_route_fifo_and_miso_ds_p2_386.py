# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""
test_spi_route_fifo_and_miso_ds_p2_386: the production SPI arm queues ONE byte
per transfer, and the responder behaves correctly with no trailing pad.

DS-P2.386. sw/stream/stimulus_armer.py arm_spi used to queue [miso, 0x00] per
transfer, the 0x00 being "popped at the 8th SCLK fall so MISO returns low". The
pad is not needed: with an empty FIFO the slave reloads zeros at that edge.
This test replays the REAL armer over AXI (the same replay as
test_selftest_coverage, through the register file, the stimulus generator and
the SPI injector inside the core) and judges the wires:

  * each transfer returns its MISO byte: the byte the master would see is
    rebuilt from the MISO net at the SCLK rising edges, and must equal
    SPI_MISO_PAYLOAD in order (so a pad displacing a later payload byte shows);
  * from the 8th SCLK falling edge (plus the slave's synchroniser latency) to
    CS rising, the responder is still ENABLED (MISO_T = 0) and DRIVES 0
    (MISO_O = 0), and the net is 0: a responder that kept the last bit, or that
    released the line early and let a wrapper read it as 0, does not pass;
  * the FIFO is EMPTY and its count is exactly 0 from the first SCLK rising
    edge (plus synchroniser latency; the byte is consumed no later than that
    edge, whichever of the two pop timings the slave has) through the trailing
    edge and the end of the transfer, with no transient to 1, and at the end of
    the run; the count is never above 1 at any time;
  * every clock where the SLAVE's read strobe (SPI_TX_RDY) is high while the
    FIFO is empty is counted and logged (zero on the shipped slave): the count
    must stay 0 across such a read, so an underflow would show as a corrupted
    count (the drain FSM's own exit cycle after the armer's FIFO reset is not
    the slave and is not counted);
  * presence: four transfers with eight SCLK rising edges each were seen, else
    the test FAILS as "nothing exercised".

Run (from the deepskopion dir): rr sim run test_spi_route_fifo_and_miso_ds_p2_386
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))                               # test_selftest_coverage
_COCOTB = _HERE.parents[4]
sys.path.insert(0, str(_COCOTB / "tests" / "stimulus" / "core_seam"))  # run_core_seam

from test_selftest_coverage import (  # noqa: E402
    CLK_PERIOD_NS, SLEEP_CAP_US, SPI_MISO_BYTES, TOP, _armer_writes, _w,
)
from _core_seam_common import run_core_seam  # noqa: E402

SYNC_LATENCY = 6   # clk_sys cycles: the slave's 3-stage SCLK/CS synchroniser plus one register


@cocotb.test()
async def test_one_byte_per_transfer_arm_spi(dut):
    await _check(dut, None)


@cocotb.test()
async def test_one_byte_per_transfer_arm_spi_routed(dut):
    """The board's op=stim spi with route spi: the armer takes the edge route itself (DS-P2.343)."""
    await _check(dut, "spi")


async def _check(dut, arm_route):
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "S_AXI"), dut.CLK, dut.RST)

    core = dut.u_core
    inj = core.u_spi_injector
    samples = []   # (sclk, cs_n, miso_net, miso_o, miso_t, empty, count, slave_rd) per clk

    async def monitor():
        while True:
            await RisingEdge(dut.CLK)
            samples.append((
                int(core.spi0_bus_sclk_w.value), int(core.spi0_cs_gpio_w.value),
                int(core.spi0_bus_miso_w.value),
                int(core.spi_inj_miso_o_w.value), int(core.spi_inj_miso_t_w.value),
                int(inj.fifo_empty.value), int(inj.fifo_data_cnt.value),
                int(inj.fifo_rd_en_slave.value)))

    task = cocotb.start_soon(monitor())

    for off, val in _armer_writes("arm_spi", False, None, arm_route):
        if off is None:
            await Timer(min(val * 1e6, SLEEP_CAP_US), unit="us")
            continue
        await axi.write(off, _w(val))
        await ClockCycles(dut.CLK, 4)
    await Timer(50, unit="us")
    task.kill()

    # Split the record into transfers: CS low from its falling to its rising sample.
    transfers = []
    starts_abs = []
    start = None
    for i in range(1, len(samples)):
        if samples[i - 1][1] == 1 and samples[i][1] == 0:
            start = i
        elif samples[i - 1][1] == 0 and samples[i][1] == 1 and start is not None:
            transfers.append(samples[start:i + 1])
            starts_abs.append(start)
            start = None
    assert start is None and samples[-1][1] == 1, "the record ends inside an open CS-low transfer"
    assert max(s[6] for s in samples) <= 1, \
        f"FIFO count above 1 somewhere in the run: {sorted({s[6] for s in samples})}"
    dut._log.info(f"arm_spi: {len(transfers)} transfers, expected MISO bytes "
                  f"{[hex(b) for b in SPI_MISO_BYTES]}")
    assert len(transfers) == len(SPI_MISO_BYTES), (
        f"{len(transfers)} CS-low transfers seen, expected {len(SPI_MISO_BYTES)}: "
        f"nothing (or the wrong thing) was exercised")

    returned = []
    for k, tr in enumerate(transfers):
        rises = [i for i in range(1, len(tr)) if tr[i - 1][0] == 0 and tr[i][0] == 1]
        falls = [i for i in range(1, len(tr)) if tr[i - 1][0] == 1 and tr[i][0] == 0]
        assert len(rises) == 8 and len(falls) == 8, \
            f"transfer {k}: {len(rises)} SCLK rising / {len(falls)} falling edges, expected 8 / 8"
        # The byte the master sees: the MISO net at each rising edge, MSB first.
        byte = 0
        for i in rises:
            byte = (byte << 1) | tr[i - 1][2]
        returned.append(byte)

        # The FIFO never holds more than the one preloaded byte, and from the first rising
        # edge on (the byte is consumed by then) it is empty with count exactly 0.
        assert max(s[6] for s in tr) <= 1, \
            f"transfer {k}: FIFO count above 1: {sorted({s[6] for s in tr})}"
        body = tr[rises[0] + SYNC_LATENCY:]
        assert all(s[5] == 1 and s[6] == 0 for s in body), (
            f"transfer {k}: FIFO not empty / count not 0 from the first SCLK edge on: "
            f"(empty, count) {sorted({(s[5], s[6]) for s in body})}")

        # After the 8th falling edge reaches the slave, until CS rises.
        tail = tr[falls[-1] + SYNC_LATENCY:]
        assert tail, f"transfer {k}: CS rose before the responder could react: nothing exercised"
        assert all(s[4] == 0 for s in tail), \
            f"transfer {k}: the responder released MISO (MISO_T=1) before CS rose"
        assert all(s[3] == 0 and s[2] == 0 for s in tail), (
            f"transfer {k}: MISO not driven low after the byte: "
            f"MISO_O {[s[3] for s in tail]} net {[s[2] for s in tail]}")

    empty_reads = sum(1 for s in samples if s[7] == 1 and s[5] == 1)
    dut._log.info(f"arm_spi: {empty_reads} clock(s) with the slave read strobe high while the FIFO is empty")
    # Witness tied to the mutation: DS_P2_386_EXPECT_EMPTY_READS is 0 for the shipped slave (the default) and is set
    # to the exact count only when the forced-read mutant is run, so the same checker cannot accept both 0 and N.
    expect = int(os.environ.get("DS_P2_386_EXPECT_EMPTY_READS", "0"))
    assert empty_reads == expect, f"{empty_reads} empty read(s) seen, expected {expect}"
    # Across every such read the FIFO must stay empty with count 0 on the next clock (no underflow, no corruption).
    empty_idx = [i for i, s in enumerate(samples) if s[7] == 1 and s[5] == 1]
    assert all(i + 1 < len(samples) for i in empty_idx), "an empty read in the last sample has no following sample"
    if expect:
        # One empty read in each transfer's response window (from its 8th SCLK falling edge to 12 clocks later),
        # none anywhere else: the forced reads are the trailing-edge ones, not idle or clustered strobes.
        windows = []
        for st, tr in zip(starts_abs, transfers):
            f8 = [i for i in range(1, len(tr)) if tr[i - 1][0] == 1 and tr[i][0] == 0][-1]
            windows.append((st + f8, st + f8 + 12))
        for k, (lo, hi) in enumerate(windows):
            n = sum(1 for i in empty_idx if lo <= i <= hi)
            assert n == 1, f"transfer {k}: {n} empty read(s) in its 8th-falling-edge window, expected exactly 1"
        assert all(any(lo <= i <= hi for lo, hi in windows) for i in empty_idx), \
            "an empty read occurred outside every 8th-falling-edge window"
    for i in empty_idx:
        nxt = samples[i + 1]
        assert nxt[5] == 1 and nxt[6] == 0, \
            f"read of the empty FIFO at sample {i} left (empty, count) = ({nxt[5]}, {nxt[6]})"

    assert returned == SPI_MISO_BYTES, (
        f"transfers returned {[hex(b) for b in returned]}, "
        f"expected {[hex(b) for b in SPI_MISO_BYTES]}")
    last = samples[-1]
    assert last[5] == 1 and last[6] == 0, f"FIFO not empty at the end: empty={last[5]} count={last[6]}"


if __name__ == "__main__":
    run_core_seam("test_spi_route_fifo_and_miso_ds_p2_386", top_level=TOP)
