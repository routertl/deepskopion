"""
test_spi_injector — cocotb regression for hw/rtl/responder/spi_injector.vhd
(DS-P3.46; SPI side of DS-P2.51 Phase 3).

Wraps spi_injector in tb_spi_injector (push-pull bus, single driver per
line) and exercises the regbank-fed FIFO → slave-driven MISO contract.
Each test anchors on a single SPI-INJ-REQ-N from
hw/rtl/responder/requirements.yml.

Mirrors test_i2c_injector.py — same FIFO contract, different bus shape.

Run with:
    rr sim run test_spi_injector
"""
import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Edge, FallingEdge, RisingEdge, Timer
from cocotb.utils import get_sim_time

# Routertl SDK cocotb path (mirrors test_i2c_injector.py).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

# Deepskopion VIP path.
_ds_root = Path(__file__).resolve().parents[5]
_vip_parent = _ds_root / "hw" / "sim"
if str(_vip_parent) not in sys.path:
    sys.path.insert(0, str(_vip_parent))

from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv
from rr_cocotb_tb.drivers.spi_master import SpiBus, SpiConfig, SpiMaster


GENERICS = {
    "G_FIFO_DEPTH": 4,
}

CTRL_ENABLE     = 0x1
CTRL_FIFO_RESET = 1 << 9


def make_control(enable: bool = True, fifo_reset: bool = False) -> int:
    val = 0
    if enable:
        val |= CTRL_ENABLE
    if fifo_reset:
        val |= CTRL_FIFO_RESET
    return val


class SpiInjectorTb:
    """Testbench environment for tb_spi_injector."""

    def __init__(self, dut, strict_protocol: bool = True):
        self.dut = dut
        self.env = TbEnv(
            dut,
            clk="CLK",
            rst="RST",
            period_ns=10,  # 100 MHz clk_sys (matches deepskopion_top)
        )
        # SPI master at 1 MHz — plenty of margin over 100 MHz clk_sys
        # (100× ratio; the slave's 3-stage SCLK sync needs SCLK_period
        # > ~3× clk_sys_period).
        self.bus = SpiBus(
            sclk=dut.M_SCLK,
            mosi=dut.M_MOSI,
            miso=dut.M_MISO,
            cs=dut.M_CS_N,
        )
        self.cfg = SpiConfig(
            word_width=8,
            sclk_freq=1_000_000,    # 1 MHz
            cpol=False, cpha=False, # Mode 0 (matches spi_slave)
            msb_first=True,
            cs_active_low=True,
        )
        # DS-P2.224: the strict SPI protocol monitor (on by default) samples
        # SCLK on a free-running testbench clock; the DUT samples it on CLK.
        self.spi = SpiMaster(self.bus, self.cfg, sample_clk=dut.CLK, strict_protocol=strict_protocol)

    async def setup(self):
        await self.env.start_clock()
        # Pin defaults before reset (avoid X-prop on first cycle).
        self.dut.ENABLE.value     = 0
        self.dut.CONTROL.value    = 0
        self.dut.DATAIN.value     = 0
        self.dut.DATAIN_VLD.value = 0
        await self.env.reset(cycles=5)

    async def enable_slave(self):
        """Pin enable."""
        self.dut.CONTROL.value = make_control(enable=True)
        self.dut.ENABLE.value  = 1
        await ClockCycles(self.env.clk, 4)

    async def push_byte(self, byte: int):
        """Push one byte into the TX FIFO via DATAIN strobe, driven from a clock edge.

        Measured (DS-T2.102, probe job ids in the ticket): SpiMaster.write returns
        in the timestep of a CLK rising edge. A strobe set there and cleared after
        ClockCycles(1) rose and fell in that SAME timestep: VLD high for 0 ps, so
        the FIFO never saw it (the push was dropped on the old and the new slave
        alike). A clocked writer cannot produce that strobe. Waiting for a rising
        edge first gives the strobe its full cycle, and the width is asserted.
        """
        await RisingEdge(self.env.clk)
        t_set = get_sim_time("ps")
        self.dut.DATAIN.value = byte & 0xFF
        self.dut.DATAIN_VLD.value = 1
        await ClockCycles(self.env.clk, 1)
        assert get_sim_time("ps") > t_set, "DATAIN_VLD strobe had zero width: nothing exercised"
        self.dut.DATAIN_VLD.value = 0
        await ClockCycles(self.env.clk, 1)

    async def push_bytes(self, payload):
        for b in payload:
            await self.push_byte(b)

    async def reset_fifo(self):
        """Pulse CONTROL[9] (self-clearing edge) and wait for drain to settle.

        DS-T12: CONTROL[9] now enters a drain-mode FSM rather than asserting
        the XPM FIFO RST (mirror of DS-T3.2 fix for i2c_injector). Drain
        runs at one byte per cycle and the FSM has ~3 cycles of entry
        latency from the CONTROL[9] write, so a naive fixed-cycle wait
        could break on the pre-entry "drain_busy=0" state. Poll for both
        drain_busy=STATUS[15]=0 AND fifo_empty=STATUS[0]=1 — that's the
        post-drain settled state, robust to whatever the host had loaded.
        """
        prev = self.dut.CONTROL.value.to_unsigned()
        self.dut.CONTROL.value = prev | CTRL_FIFO_RESET
        await ClockCycles(self.env.clk, 2)
        self.dut.CONTROL.value = prev
        max_drain_cycles = 4 * (2 ** GENERICS["G_FIFO_DEPTH"])
        for _ in range(max_drain_cycles):
            s = self.dut.STATUS.value.to_unsigned()
            if ((s >> 15) & 0x1) == 0 and (s & 0x1) == 1:
                break
            await ClockCycles(self.env.clk, 1)
        else:
            raise AssertionError(
                f"drain did not settle (drain_busy + fifo_empty) within "
                f"{max_drain_cycles} cycles: "
                f"STATUS=0x{self.dut.STATUS.value.to_unsigned():08X}"
            )

    def status_fifo_empty(self) -> int:
        return self.dut.STATUS.value.to_unsigned() & 0x1

    def status_fifo_count(self) -> int:
        depth = GENERICS["G_FIFO_DEPTH"]
        mask = (1 << (depth + 1)) - 1
        return (self.dut.STATUS.value.to_unsigned() >> 3) & mask

    def miso_t(self) -> int:
        """Read the slave's MISO tri-state enable directly."""
        return int(self.dut.i_injector.u_slave.MISO_T.value)


# ─────────────────────────────────────────────────────────────────────────
#  Functional tests
# ─────────────────────────────────────────────────────────────────────────

@cocotb.test()
@requires("SPI-INJ-REQ-4")
async def test_datain_push_increments_count(dut):
    """DATAIN_VLD pulses push bytes; STATUS count advances; empty clears."""
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    assert tb.status_fifo_empty() == 1, \
        f"FIFO not empty post-reset: STATUS=0x{dut.STATUS.value.to_unsigned():08X}"
    assert tb.status_fifo_count() == 0

    await tb.push_byte(0x5A)
    await ClockCycles(tb.env.clk, 4)

    assert tb.status_fifo_empty() == 0, "FIFO still empty after push"
    assert tb.status_fifo_count() == 1, \
        f"FIFO count after 1 push = {tb.status_fifo_count()}, want 1"
    tb.env.log.info("✓ DATAIN_VLD push increments count + clears empty")


@cocotb.test()
@requires("SPI-INJ-REQ-3")
async def test_miso_hi_z_when_idle(dut):
    """CS_N='1' (idle) holds MISO_T='1' (Hi-Z)."""
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    # No CS asserted yet — slave should be silent.
    await ClockCycles(tb.env.clk, 8)
    assert tb.miso_t() == 1, \
        f"MISO_T = {tb.miso_t()} at idle, expected 1 (Hi-Z)"
    tb.env.log.info("✓ MISO released at idle (T=1)")


@cocotb.test()
@requires("SPI-INJ-REQ-1")
async def test_single_byte_read_returns_first_fifo_byte(dut):
    """8 SCLK cycles return the first FIFO byte on MISO, MSB-first."""
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    # DS-P3.331: 0xA5 is a bit-palindrome, so on its own it passes whether the
    # slave shifts MSB-first or LSB-first. 0x91 (0b10010001) reverses to 0x89,
    # which no byte this file pushes equals, so it fails an LSB-first slave on
    # its own. Both are kept, each in its own one-byte transaction: 0xA5 keeps
    # the end-bit checks it always had (a bit 5 cleared in the CS-falling
    # preload reads 0x85), 0x91 adds the bit-order check.
    for expected in (0xA5, 0x91):
        await tb.push_byte(expected)
        await ClockCycles(tb.env.clk, 4)

        # Master writes a 0x00 dummy byte (full-duplex); we don't care what
        # the slave receives — only what it sends back on MISO.
        rx = await tb.spi.write([0x00])
        assert rx[0] == expected, \
            f"master read 0x{rx[0]:02X} on MISO, expected 0x{expected:02X}"
        tb.env.log.info(f"✓ single-byte read returned 0x{rx[0]:02X}")


@cocotb.test()
@requires("SPI-INJ-REQ-7")
async def test_separate_one_byte_reads_return_queued_bytes_in_order(dut):
    """Bytes queued together are returned by separate one-byte transactions.

    DS-T2.102: the slave used to pop the next queued byte at the SCLK falling
    edge that ends a transaction, so with two bytes queued a one-byte read
    returned the first and discarded the second (count 2 -> 0, next read 0x00).
    A byte is now consumed at its first SCLK rising edge, so each one-byte
    transaction takes exactly one byte. The values are not bit-palindromes.
    """
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    queued = [0x91, 0xD2, 0x27]
    for byte in queued:
        await tb.push_byte(byte)
    await ClockCycles(tb.env.clk, 4)
    assert tb.status_fifo_count() == len(queued), \
        f"FIFO count {tb.status_fifo_count()} after {len(queued)} pushes"

    received = []
    for done, expected in enumerate(queued, start=1):
        rx = await tb.spi.write([0x00])
        received.append(rx[0])
        await ClockCycles(tb.env.clk, 4)
        remaining = len(queued) - done
        assert tb.status_fifo_count() == remaining, (
            f"after one-byte read {done} the FIFO holds {tb.status_fifo_count()} "
            f"bytes, expected {remaining} (one byte consumed per transaction)")
    assert received == queued, (
        f"separate one-byte reads returned {[f'0x{b:02X}' for b in received]}, "
        f"expected {[f'0x{b:02X}' for b in queued]}")


@cocotb.test()
@requires("SPI-INJ-REQ-7")
async def test_cs_select_without_clock_does_not_consume_byte(dut):
    """Selecting the slave with no SCLK edge leaves the queued byte in place.

    DS-T2.102: the CS-fall preload used to pop the head byte at once, so a
    select that is released without a single clock lost it. The byte is
    consumed only when its first SCLK rising edge arrives.
    """
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    expected = 0x91
    await tb.push_byte(expected)
    await ClockCycles(tb.env.clk, 4)
    assert tb.status_fifo_count() == 1

    # CS low and high again with SCLK held low: no clock edge at all.
    dut.M_CS_N.value = 0
    await ClockCycles(tb.env.clk, 30)
    assert tb.status_fifo_count() == 1, (
        f"FIFO count {tb.status_fifo_count()} while selected with no clock; "
        f"the byte was consumed before its first SCLK edge")
    dut.M_CS_N.value = 1
    await ClockCycles(tb.env.clk, 10)
    assert tb.status_fifo_count() == 1, \
        "the byte was lost when the select was released without a clock"

    rx = await tb.spi.write([0x00])
    assert rx[0] == expected, \
        f"master read 0x{rx[0]:02X} after an aborted select, expected 0x{expected:02X}"


@cocotb.test()
@requires("SPI-INJ-REQ-7")
async def test_byte_is_consumed_at_its_first_rising_edge(dut):
    """The head byte leaves the FIFO a few clocks after the first SCLK rising edge.

    DS-T2.102: the pop happens at the byte's first SCLK rising edge: not at CS
    fall (before any clock), not at the 8th edge, not at the first falling edge
    (half an SCLK period later), and not at the end of the transaction. A
    transaction aborted after that edge therefore loses the byte, by design.
    The count is read 8 clk_sys cycles after the first rising edge, which is
    inside the first half-period (50 cycles) and past the 3-stage synchroniser.
    """
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    await tb.push_byte(0x91)
    await tb.push_byte(0xD2)
    await ClockCycles(tb.env.clk, 4)
    assert tb.status_fifo_count() == 2

    xfer = cocotb.start_soon(tb.spi.write([0x00]))
    await FallingEdge(dut.M_CS_N)
    await ClockCycles(tb.env.clk, 10)
    assert tb.status_fifo_count() == 2, \
        f"count {tb.status_fifo_count()} 10 cycles after CS fall, before any SCLK edge: popped at CS fall"
    await RisingEdge(dut.M_SCLK)
    assert tb.status_fifo_count() == 2, (
        f"count {tb.status_fifo_count()} at the first SCLK rising edge on the wire, expected 2: "
        f"the byte was consumed before the edge")
    await ClockCycles(tb.env.clk, 8)
    assert tb.status_fifo_count() == 1, (
        f"count {tb.status_fifo_count()} 8 cycles after the first SCLK rising edge, expected 1: "
        f"the byte is not consumed at its first rising edge")
    rx = await xfer
    assert rx[0] == 0x91, f"master read 0x{rx[0]:02X}, expected 0x91"
    await ClockCycles(tb.env.clk, 8)
    assert tb.status_fifo_count() == 1, "the second byte was consumed by the first transaction"


@cocotb.test()
@requires("SPI-INJ-REQ-7")
async def test_drain_while_selected_does_not_pop_the_next_byte(dut):
    """An entry queued after a drain is not consumed by the drained entry's load.

    DS-T2.102 (T1 review): the slave loads A at CS fall. A drain through
    CONTROL[9] then empties the FIFO and B is queued while CS_N is still low.
    The first rising edge must not pop B: the load that was pending belonged to
    A. The transaction (a full 8 clocks, driven by hand because CS_N is already
    low) leaves B queued, and the next transaction returns B.
    """
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    await tb.push_byte(0xA5)
    await ClockCycles(tb.env.clk, 4)
    assert tb.status_fifo_count() == 1

    dut.M_CS_N.value = 0
    await ClockCycles(tb.env.clk, 10)          # past the CS synchroniser: A is loaded
    dut.CONTROL.value = make_control(enable=True, fifo_reset=True)
    await ClockCycles(tb.env.clk, 4)
    dut.CONTROL.value = make_control(enable=True)
    await ClockCycles(tb.env.clk, 40)          # drain completes (empty again)
    assert tb.status_fifo_count() == 0, "the drain did not empty the FIFO"
    await tb.push_byte(0x91)
    await ClockCycles(tb.env.clk, 4)
    assert tb.status_fifo_count() == 1

    got = 0
    for _ in range(8):                         # one full byte, mode 0, 1 MHz
        got = (got << 1) | int(dut.M_MISO.value)   # the master samples at the rising edge: read just before it
        dut.M_SCLK.value = 1
        await Timer(500, unit="ns")
        dut.M_SCLK.value = 0
        await Timer(500, unit="ns")
    await ClockCycles(tb.env.clk, 8)
    dut.M_CS_N.value = 1
    await ClockCycles(tb.env.clk, 20)
    assert tb.status_fifo_count() == 1, (
        f"count {tb.status_fifo_count()} after the transaction: the stale load of the "
        f"drained byte popped the byte queued after the drain")

    assert got == 0x00, (f"the transaction after the drain returned 0x{got:02X}: "
                         f"the drained byte (0xA5) was still on MISO, expected zeros")
    rx = await tb.spi.write([0x00])
    assert rx[0] == 0x91, f"master read 0x{rx[0]:02X} in the next transaction, expected 0x91"


@cocotb.test()
@requires("SPI-INJ-REQ-7")
async def test_push_straight_after_a_transaction_is_kept(dut):
    """The tightest gap tested: push_byte called the moment SpiMaster.write returns.

    DS-T2.102 (T1 review): measured, the strobe starts in the timestep of the
    return (SpiMaster.write returns in the timestep of a CLK edge, and the
    helper's RisingEdge fires on that edge), is exactly one clock wide and
    reaches the FIFO write enable. The gap (0 to 10 ns) and the width (10 ns)
    are asserted from the simulator times, and the FIFO count rises.
    """
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    wr_rise, wr_fall = [], []

    async def watch_wr():
        sig = dut.i_injector.fifo_wr_en
        while True:
            await Edge(sig)
            (wr_rise if int(sig.value) == 1 else wr_fall).append(get_sim_time("ps"))

    cocotb.start_soon(watch_wr())
    await tb.spi.write([0x00])
    t_return = get_sim_time("ps")
    await tb.push_byte(0x91)
    await ClockCycles(tb.env.clk, 4)
    assert len(wr_rise) == 1 and len(wr_fall) == 1, \
        f"FIFO write enable rose {len(wr_rise)}x and fell {len(wr_fall)}x, expected one pulse"
    assert 0 <= wr_rise[0] - t_return <= 10_000, \
        f"strobe reached the FIFO {wr_rise[0] - t_return} ps after the transaction returned, expected 0..10000"
    assert wr_fall[0] - wr_rise[0] == 10_000, \
        f"FIFO write enable was {wr_fall[0] - wr_rise[0]} ps wide, expected one clock (10000)"
    assert tb.status_fifo_count() == 1, \
        f"count {tb.status_fifo_count()} after the push, expected 1"
    rx = await tb.spi.write([0x00])
    assert rx[0] == 0x91, f"master read 0x{rx[0]:02X}, expected 0x91"


@cocotb.test()
@requires("SPI-INJ-REQ-7")
async def test_abort_after_one_rising_edge_consumes_that_byte_only(dut):
    """A transaction cut off after one SCLK rising edge consumes one byte, once.

    DS-T2.102 (T1 review, MC GO): the abort is the subject, so this test alone
    uses a non-strict SpiMaster (the strict monitor raises SPI_SHORT_XFER on any
    CS release before 8 clocks, as the BFM's own KAT test also works around).
    The stimulus proves itself from the bus: exactly one SCLK rising edge while
    CS_N was low. The next transaction returns the SECOND queued byte, so the
    half-consumed byte is neither lost twice nor replayed.
    """
    tb = SpiInjectorTb(dut, strict_protocol=False)
    await tb.setup()
    await tb.enable_slave()

    await tb.push_byte(0x91)
    await tb.push_byte(0xD2)
    await ClockCycles(tb.env.clk, 4)
    assert tb.status_fifo_count() == 2

    rises = []

    async def watch_bus():
        prev = int(dut.M_SCLK.value)
        while True:
            await RisingEdge(dut.CLK)
            cur = int(dut.M_SCLK.value)
            if prev == 0 and cur == 1 and int(dut.M_CS_N.value) == 0:
                rises.append(get_sim_time("ps"))     # a real low-to-high transition while selected
            prev = cur

    cocotb.start_soon(watch_bus())
    await tb.spi.inject_short_transfer(bits=1)
    await ClockCycles(tb.env.clk, 12)
    assert len(rises) == 1, f"stimulus: {len(rises)} SCLK rising edge(s) while CS_N was low, expected exactly 1"
    assert int(dut.M_CS_N.value) == 1, "stimulus: CS_N was not released"
    assert tb.status_fifo_count() == 1, (
        f"count {tb.status_fifo_count()} after the aborted transaction, expected 1: "
        f"the first byte is consumed by its first rising edge, the second is untouched")

    rx = await tb.spi.write([0x00])
    assert rx[0] == 0xD2, \
        f"master read 0x{rx[0]:02X} in the next transaction, expected 0xD2 (0x91 replayed or lost twice)"
    assert tb.status_fifo_count() == 0


@cocotb.test()
@requires("SPI-INJ-REQ-7")
async def test_second_byte_is_consumed_at_its_own_first_rising_edge(dut):
    """Within one CS assertion each byte is popped at ITS first rising edge.

    DS-T2.102 (T1 review): three entries are queued and a two-byte transaction
    reads two of them. The count is 2 after the first byte's first edge, still 2
    just before the ninth rising edge (the second byte's first), 1 a few cycles
    after it, and 1 after CS release: the surplus entry survives and is the
    next transaction's byte.
    """
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    for b in (0x91, 0xD2, 0x27):
        await tb.push_byte(b)
    await ClockCycles(tb.env.clk, 4)
    assert tb.status_fifo_count() == 3

    xfer = cocotb.start_soon(tb.spi.write([0x00, 0x00]))
    for n in range(1, 9):
        await RisingEdge(dut.M_SCLK)
        if n == 1:
            await ClockCycles(tb.env.clk, 8)
            assert tb.status_fifo_count() == 2, f"count {tb.status_fifo_count()} after rising edge 1, expected 2"
    await FallingEdge(dut.M_SCLK)                  # the 8th falling edge: the byte boundary
    await ClockCycles(tb.env.clk, 8)
    assert tb.status_fifo_count() == 2, (
        f"count {tb.status_fifo_count()} at the byte boundary, before the 9th rising edge, expected 2: "
        f"the second byte was consumed before its own first rising edge")
    await RisingEdge(dut.M_SCLK)                   # the 9th rising edge
    assert tb.status_fifo_count() == 2, (
        f"count {tb.status_fifo_count()} at the 9th rising edge on the wire, expected 2: "
        f"the second byte was consumed before its own first edge")
    await ClockCycles(tb.env.clk, 8)
    assert tb.status_fifo_count() == 1, \
        f"count {tb.status_fifo_count()} 8 cycles after the 9th rising edge, expected 1"
    rx = await xfer
    assert rx == [0x91, 0xD2], f"transaction returned {[hex(b) for b in rx]}, expected 0x91, 0xD2"
    await ClockCycles(tb.env.clk, 12)
    assert tb.status_fifo_count() == 1, "the surplus entry did not survive CS release"
    rx = await tb.spi.write([0x00])
    assert rx[0] == 0x27, f"next transaction read 0x{rx[0]:02X}, expected the surplus 0x27"


@cocotb.test()
@requires("SPI-INJ-REQ-7")
async def test_transaction_outlasting_the_queue_returns_zeros_after_it(dut):
    """One entry queued, two bytes clocked: [entry, 0x00], count 0, no stale last bit.

    DS-T2.102 (T1 review): 0x91 ends in a 1, so an empty reload that left the
    preceding byte's final bit on MISO would return 0x01 for the second byte.
    """
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    await tb.push_byte(0x91)
    await ClockCycles(tb.env.clk, 4)
    assert tb.status_fifo_count() == 1
    rx = await tb.spi.write([0x00, 0x00])
    assert rx == [0x91, 0x00], f"transaction returned {[hex(b) for b in rx]}, expected 0x91, 0x00"
    await ClockCycles(tb.env.clk, 8)
    assert tb.status_fifo_count() == 0, f"count {tb.status_fifo_count()} after the transaction, expected 0"


async def _clock_byte(dut):
    """Drive one mode-0 byte by hand (1 MHz); return what a master samples at the rising edges."""
    got = 0
    for _ in range(8):
        got = (got << 1) | int(dut.M_MISO.value)   # sampled just before the rising edge
        dut.M_SCLK.value = 1
        await Timer(500, unit="ns")
        dut.M_SCLK.value = 0
        await Timer(500, unit="ns")
    return got


@cocotb.test()
@requires("SPI-INJ-REQ-7")
async def test_entry_arriving_after_an_empty_cs_preload_is_not_that_bytes_data(dut):
    """An entry queued after an empty preload at CS fall stays queued for a later transaction.

    DS-T2.102 (T1 review): the slave is selected with the FIFO empty and the
    preload finds no eligible head; 0x91 is queued before the first rising
    edge. The byte returns zeros, the count stays 1 through the byte, and the
    the next byte under the same selection (a later eligible load) returns 0x91.
    """
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    dut.M_CS_N.value = 0
    await ClockCycles(tb.env.clk, 10)           # past the CS synchroniser: the empty preload is done
    await tb.push_byte(0x91)
    await ClockCycles(tb.env.clk, 4)
    assert tb.status_fifo_count() == 1
    got = await _clock_byte(dut)
    assert got == 0x00, f"the byte returned 0x{got:02X}, expected zeros (the entry arrived after the preload)"
    await ClockCycles(tb.env.clk, 8)
    assert tb.status_fifo_count() == 1, \
        f"count {tb.status_fifo_count()} after the byte, expected 1: the late entry was consumed"
    # Same selection, next byte: its load finds the entry, so it is returned and consumed.
    got = await _clock_byte(dut)
    assert got == 0x91, f"the next byte under the same CS returned 0x{got:02X}, expected the queued 0x91"
    await ClockCycles(tb.env.clk, 8)
    assert tb.status_fifo_count() == 0, \
        f"count {tb.status_fifo_count()} after the second byte, expected 0 (exactly one entry consumed)"
    dut.M_CS_N.value = 1
    await ClockCycles(tb.env.clk, 20)
    assert tb.status_fifo_count() == 0


@cocotb.test()
@requires("SPI-INJ-REQ-7")
async def test_entry_arriving_after_an_empty_byte_boundary_reload_is_not_that_bytes_data(dut):
    """The same rule at the byte boundary: the 2nd byte's reload found the FIFO empty.

    DS-T2.102 (T1 review): one entry is queued and a two-byte transaction is
    clocked by hand. 0xD2 is queued after the 8th falling edge reloaded zeros
    and before the 9th rising edge. The second byte returns zeros, 0xD2 stays
    queued, and a third byte under the same selection returns it.
    """
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    await tb.push_byte(0x91)
    await ClockCycles(tb.env.clk, 4)
    dut.M_CS_N.value = 0
    await ClockCycles(tb.env.clk, 10)
    # First byte, stopping after the 8th falling edge by hand so 0xD2 lands in the gap.
    got1 = 0
    for _ in range(8):
        got1 = (got1 << 1) | int(dut.M_MISO.value)
        dut.M_SCLK.value = 1
        await Timer(500, unit="ns")
        dut.M_SCLK.value = 0
        await Timer(500, unit="ns")
    assert got1 == 0x91, f"first byte returned 0x{got1:02X}, expected 0x91"
    await ClockCycles(tb.env.clk, 12)           # the byte-boundary reload (empty) is done; 9th rising edge not yet
    await tb.push_byte(0xD2)
    await ClockCycles(tb.env.clk, 4)
    assert tb.status_fifo_count() == 1
    got2 = await _clock_byte(dut)
    assert got2 == 0x00, f"the second byte returned 0x{got2:02X}, expected zeros (0xD2 arrived after the reload)"
    await ClockCycles(tb.env.clk, 8)
    assert tb.status_fifo_count() == 1, \
        f"count {tb.status_fifo_count()} after the second byte, expected 1: the late entry was consumed"
    # Same selection, third byte: its load finds 0xD2, so it is returned and consumed.
    got3 = await _clock_byte(dut)
    assert got3 == 0xD2, f"the third byte returned 0x{got3:02X}, expected the queued 0xD2"
    await ClockCycles(tb.env.clk, 8)
    assert tb.status_fifo_count() == 0, \
        f"count {tb.status_fifo_count()} after the third byte, expected 0 (exactly one entry consumed)"
    dut.M_CS_N.value = 1
    await ClockCycles(tb.env.clk, 20)
    assert tb.status_fifo_count() == 0


@cocotb.test()
@requires("SPI-INJ-REQ-2")
async def test_multi_byte_read_in_fifo_order(dut):
    """N×8 SCLK returns the first N FIFO entries on MISO in order."""
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    payload = [0xDE, 0xAD, 0xBE, 0xEF]
    await tb.push_bytes(payload)
    await ClockCycles(tb.env.clk, 4)

    rx = await tb.spi.write([0x00] * len(payload))
    assert rx == payload, \
        f"FIFO-order mismatch: got {[hex(b) for b in rx]}, want {[hex(b) for b in payload]}"
    tb.env.log.info(f"✓ multi-byte read returned {[hex(b) for b in rx]} in order")


@cocotb.test()
@requires("SPI-INJ-REQ-5")
async def test_fifo_empty_returns_zero(dut):
    """With the FIFO empty, master reads zeros on MISO."""
    tb = SpiInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    assert tb.status_fifo_empty() == 1, "test pre-condition: FIFO must be empty"
    rx = await tb.spi.write([0x00, 0x00])
    assert rx == [0x00, 0x00], \
        f"empty-FIFO read got {[hex(b) for b in rx]}, expected zeros"
    tb.env.log.info("✓ empty FIFO returns zeros on MISO")


# ─────────────────────────────────────────────────────────────────────────
#  Standalone Execution
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from engine.simulation import run_simulation
    from engine.paths import SRC_PATH, SDK_SRC_PATH

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[5]
    DEEPSKOPION_RTL  = DEEPSKOPION_ROOT / "hw" / "rtl"
    from hw.sim.portable_fifo_sources import olo_fifo_sources

    run_simulation(
        top_level="tb_spi_injector",
        module="test_ds_spi_injector",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                # SDK shared units
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                # Deepskopion packages
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                # Responder modules (DUT chain)
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{DEEPSKOPION_RTL}/responder/spi_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/spi_injector.vhd",
                # Sim wrapper
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_spi_injector.vhd",
            ],
        },
    )
