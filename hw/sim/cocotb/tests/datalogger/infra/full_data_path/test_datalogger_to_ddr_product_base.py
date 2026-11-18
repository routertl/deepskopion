"""DS-P2.110 — product-geometry write-path regression (acceptance gate for DS-T1.12).

The sibling `test_datalogger_to_ddr.py` exercises the full datalogger→DDR path, but
ONLY at base 0 / a 128-bit toy DDR width. That is the scaled-sim trap: green at
toy generics proves nothing at the geometry the DE25 product actually ships. The
DE25 top (`deepskopion_de25_top.vhd:716`) instantiates the core at

    ddr_base_addr = 0xA0000000   (upper half of the 32-bit DDR space)
    ddr_data_width = 256          (f2sdram is 256-bit)
    ddr_addr_width = 32

and it was precisely at THIS geometry that the DS-P3.153 rr_ddr_stream repin
(`d24b0cd`) regressed on silicon (DS-T1.12): the uploader stayed IDLE
(write_idle=1), never issued an AW beat, and the commit pointer wedged at 0 while
the enqueue counter climbed. The existing tb could not reproduce it because its
`G_DDR_BASE_ADDR` was an INTEGER and 0xA0000000 (2_684_354_560) overflows a
32-bit signed VHDL INTEGER — so the base silently degraded to 0 / 128-bit.

DS-P2.110 moved `G_DDR_BASE_ADDR` into the vector domain (mirroring the
`deepskopion_core` seam DS-T3.32 already fixed for RTL), which lets this test
elaborate the tb at the exact DE25 product generics and drive real UART traffic
through the write plane.

WHAT THIS ASSERTS (all at product geometry):
  1. The uploader ISSUES a write burst — M_AXI_AWVALID asserts at least once.
     (The DS-T1.12 silicon signature is that it never does.)
  2. Every emitted AWADDR carries the full 32-bit product base — its top nibble
     is 0xA — proving the base propagated intact through elaboration and was
     NOT truncated to a toy value.
  3. The commit pointer (`ddr_stream_axi.wr_commit_addr_o`, the DE25
     RING_COMMIT_PTR source) ADVANCES past 0 under sustained enqueue — proving
     the write actually completed its B-response, not just launched an address.
  4. write_idle is not stuck high across the whole run.

BASELINE / MUTATION power evidence is recorded in the DS-P2.110 commit message.

Run:  rr sim run test_datalogger_to_ddr_product_base   (from the deepskopion dir)
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: Could not import engine. Run via "
          "`rr sim run test_datalogger_to_ddr_product_base`.")
    sys.exit(1)

from cocotbext.axi import AxiBus, AxiRam
from rr_cocotb_tb.drivers.uart_master import UartSource

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT))
from sw.stream.zs_msg import parse_header, ZS_MAGIC, HEADER_SIZE  # noqa: E402

PERIOD_NS = 10

# ── EXACT DE25 product geometry (deepskopion_de25_top.vhd:716) ────────────────
PRODUCT_BASE = 0xA0000000
GENERICS = {
    "G_SYS_CLK":         100_000_000,
    "G_AXI_ID":          0,
    "G_FIFO_DEPTH":      9,
    "G_USER_DATA_WIDTH": 32,
    "G_DDR_DATA_WIDTH":  256,          # f2sdram width — NOT the 128-bit toy
    "G_DDR_ADDR_WIDTH":  32,
    "G_DDR_BASE_ADDR":   'x"A0000000"',  # upper half; overflows INTEGER — vector only
    "G_BUFFER_WIDTH":    28,           # DE25 G_DDR_WIN_WIDTH = 28 (256 MiB ring)
    "G_FIFO_IMPL":       "olo",
}

UART0_ID = 12

# AxiRam wraps writes mod its size (address % size), so a small sink transparently
# absorbs the 0xA0000000-based AXI addresses — the ring offset lands at the
# equivalent low address. A power-of-two ≥ a few frames' worth is all we need.
DDR_SIZE = 1 << 20   # 1 MiB


async def bring_up(dut):
    """Start the single CLK domain, attach the AxiRam sink, walk reset."""
    cocotb.start_soon(Clock(dut.CLK, PERIOD_NS, units="ns").start())

    dut.I2C0_SCL.value = 1
    dut.I2C0_SDA.value = 1
    dut.UART0_TX.value = 1
    dut.SPI0_SCLK.value = 0
    dut.SPI0_CS.value = 1
    dut.SPI0_MOSI.value = 0
    dut.SPI0_MISO.value = 0
    dut.EDGE0.value = 0
    dut.CONFIG.value = 0

    axi_ram = AxiRam(AxiBus.from_prefix(dut, "M_AXI"), dut.CLK, dut.RST, size=DDR_SIZE)

    dut.RST.value = 1
    await ClockCycles(dut.CLK, 16)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 16)
    return axi_ram


async def latch_config(dut, dev_id: int, cfg_bits: int):
    """Id-gated CONFIG write for one datalogger instance (see sibling tb)."""
    dut.CONFIG.value = (dev_id & 0x1F) << 11
    await ClockCycles(dut.CLK, 4)
    dut.CONFIG.value = ((dev_id & 0x1F) << 11) | (cfg_bits & 0x7FF)
    await ClockCycles(dut.CLK, 8)


async def drain_pipeline(dut, max_us: int = 800):
    """Poll WRITE_IDLE high for a sustained window ⇒ DMA flushed."""
    quiet = 0
    waited_us = 0
    while waited_us < max_us:
        await Timer(2, units="us")
        waited_us += 2
        if int(dut.WRITE_IDLE.value) == 1:
            quiet += 2
            if quiet >= 20:
                break
        else:
            quiet = 0
    await Timer(5, units="us")


@cocotb.test()
async def test_product_base_uploader_issues_aw_and_commit_advances(dut):
    """At the DE25 product geometry the write plane must actually run."""
    axi_ram = await bring_up(dut)

    # ── Passive AW-channel + write-idle monitors (never soften an assert) ──
    seen_awaddr: list[int] = []
    awvalid_ever = [False]
    write_idle_ever_low = [False]

    async def _monitor():
        while True:
            await RisingEdge(dut.CLK)
            try:
                awv = int(dut.M_AXI_AWVALID.value)
                awr = int(dut.M_AXI_AWREADY.value)
                wid = int(dut.WRITE_IDLE.value)
            except ValueError:
                continue   # X during reset settle
            if wid == 0:
                write_idle_ever_low[0] = True
            if awv == 1:
                awvalid_ever[0] = True
                if awr == 1:
                    seen_awaddr.append(int(dut.M_AXI_AWADDR.value))
    _mon = cocotb.start_soon(_monitor())

    # ── Sustained UART0 enqueue: baud-lock burst + payload run ──
    await latch_config(dut, UART0_ID, cfg_bits=0b011)   # Enable + AutoSync
    await ClockCycles(dut.CLK, 50)

    rng = random.Random(0xA0110)
    uart = UartSource(dut.UART0_TX, baud=460_800, bits=8, stop_bits=1, parity="none")
    await uart.write_bytes([rng.randint(0, 0xFF) for _ in range(150)])  # baud calibration
    await uart.write_bytes([rng.randint(0, 0xFF) for _ in range(64)])   # payload frames
    await uart.write_bytes([0x00, 0xFF, 0x00, 0xFF])                    # flush the pipe tail

    await drain_pipeline(dut)
    await ClockCycles(dut.CLK, 100)
    _mon.kill()

    commit = int(dut.WR_COMMIT_ADDR.value)
    dut._log.info(
        f"DS-P2.110 product geometry (base=0x{PRODUCT_BASE:08X}, ddr_width=256): "
        f"AWVALID ever={awvalid_ever[0]}, AW beats={len(seen_awaddr)}, "
        f"AWADDRs[:4]={[hex(a) for a in seen_awaddr[:4]]}, "
        f"WR_COMMIT_ADDR=0x{commit:08X}, write_idle_ever_low={write_idle_ever_low[0]}")

    # (1) The uploader must ISSUE a write burst — the DS-T1.12 silicon signature
    #     is that it never asserts AWVALID (write plane wedged IDLE).
    assert awvalid_ever[0], (
        "DS-T1.12 REPRODUCED: at the DE25 product geometry (base=0xA0000000, "
        "ddr_data_width=256) the write DMA NEVER asserted AWVALID under sustained "
        "datalogger enqueue — the uploader is wedged IDLE and issues no burst. This "
        "is the exact on-silicon symptom (write_idle=1, no AW, COMMIT stuck at 0). "
        "File T-tier.")
    assert seen_awaddr, (
        "AWVALID asserted but the sink never completed an AW handshake — the "
        "sink/monitor never observed a burst address. Cannot verify base "
        "propagation. File T-tier.")

    # (2) Every AWADDR must carry the full 32-bit product base (top nibble 0xA),
    #     proving the base was NOT truncated to a toy value during elaboration.
    bad = [hex(a) for a in seen_awaddr if (a >> 28) != 0xA]
    assert not bad, (
        f"AWADDR(s) {bad} do not carry the product base 0xA0000000 (top nibble "
        f"!= 0xA) — the 32-bit base was truncated/degraded through the generic "
        f"seam. This is the INTEGER-overflow scaled-sim trap DS-P2.110 fixes. "
        f"File T-tier.")

    # (3) The commit pointer must ADVANCE past 0 — proving the write completed
    #     its B-response, not merely launched an address. On silicon this stayed
    #     wedged at 0 (RING_COMMIT_PTR) while the enqueue counter climbed.
    assert commit != 0, (
        f"WR_COMMIT_ADDR wedged at 0 despite {len(seen_awaddr)} AW beat(s) — the "
        f"write plane launched addresses but no write ever B-completed to advance "
        f"the commit pointer. This is the DS-T1.12 COMMIT wedge. File T-tier.")

    # (4) write_idle must have dropped at least once — a plane that reports idle
    #     for the entire run never did any work.
    assert write_idle_ever_low[0], (
        "WRITE_IDLE stayed high for the ENTIRE run — the write plane never became "
        "busy, so no transfer was ever in flight (write_idle=1 silicon signature). "
        "File T-tier.")

    # End-to-end sanity: at least one structurally valid zs_msg frame landed in
    # DDR. extract via magic-resync (width-agnostic — 256-bit lanes just change
    # the padding stride the resync already tolerates).
    buf = bytes(axi_ram.read(0, DDR_SIZE))
    frames = 0
    off = 0
    while off + HEADER_SIZE <= len(buf):
        if int.from_bytes(buf[off:off + 2], "little") != ZS_MAGIC:
            off += 4
            continue
        try:
            hdr = parse_header(buf, off)
        except ValueError:
            off += 4
            continue
        if hdr.version == 1 and hdr.header_len == HEADER_SIZE // 4:
            frames += 1
            off += (((HEADER_SIZE + hdr.payload_len) + 3) // 4) * 4
        else:
            off += 4
    assert frames > 0, (
        "no structurally valid zs_msg frame landed in the DDR sink at product "
        "geometry despite AW issued + commit advanced — the 256-bit write path "
        "corrupted the framed stream. File T-tier.")
    dut._log.info(
        f"DS-P2.110: write plane ran at product geometry — {len(seen_awaddr)} AW "
        f"beat(s) at base 0xA*, commit=0x{commit:08X}, {frames} frame(s) in DDR.")


if __name__ == "__main__":
    run_simulation(
        top_level="tb_datalogger_to_ddr",
        module=Path(__file__).stem,
        generics=GENERICS,
        scope_to_dut="tb_datalogger_to_ddr",
        work_lib="tich",
        waves=True,
        verbose=True,
    )
