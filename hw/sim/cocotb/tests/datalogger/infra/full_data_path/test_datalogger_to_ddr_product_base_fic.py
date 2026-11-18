"""PF-H0 (DS-P2.124) — PolarFire SoC FIC product-geometry write-path guard.

This is the DS-P2.110 product-geometry gate extended to the PolarFire SoC MSS-FIC
backend. The DE25 sibling (test_datalogger_to_ddr_product_base) proved the write
plane at the DE25 f2sdram geometry (256-bit, base 0xA0000000) through
ddr_stream_axi; this one proves it at the PFSoC FIC geometry the
`deepskopion_pfsoc_top` ships — 64-bit native FIC width, 64-beat burst cap,
base 0xA0000000 — through **ddr_stream_fic** (DDR-P2.15), selected by the tb's
G_DDR_BACKEND="fic" arm (the same arm deepskopion_core.vhd's G_DDR_BACKEND adds).

Why a separate PFSoC guard rather than trusting the DE25 one: the PFSoC top is
the ONLY leg that elaborates the fic body, at a DIFFERENT width (64 not 256) and
a DIFFERENT burst cap (64 not 16). A width truncation, a broken fic-arm port map,
or a base that failed to propagate through the fic wrapper would all pass the
axi-only DE25 guard and still ship a wedged PolarFire ring. This test elaborates
the fic body at the exact product generics and asserts the write plane runs.

WHAT THIS ASSERTS (all at PFSoC FIC geometry, through ddr_stream_fic):
  1. The uploader ISSUES a write burst — M_AXI_AWVALID asserts at least once.
  2. Every emitted AWADDR carries the full 32-bit product base (top nibble 0xA)
     — proving the base propagated intact through the fic wrapper's elaboration.
  3. WR_COMMIT_ADDR advances past 0 — the write B-completed, not just launched.
  4. WRITE_IDLE dropped at least once — the plane actually did work.
  5. At least one structurally valid zs_msg frame landed in the DDR sink.

BASELINE / MUTATION power evidence is recorded in the DS-P2.124 commit message.

Run:  rr sim run test_datalogger_to_ddr_product_base_fic   (from the deepskopion dir)
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
          "`rr sim run test_datalogger_to_ddr_product_base_fic`.")
    sys.exit(1)

from cocotbext.axi import AxiBus, AxiRam
from rr_cocotb_tb.drivers.uart_master import UartSource

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT))
from sw.stream.zs_msg import parse_header, ZS_MAGIC, HEADER_SIZE  # noqa: E402

PERIOD_NS = 10

# ── EXACT PFSoC FIC product geometry (deepskopion_pfsoc_top + PF-P0 §3.3/§3.4) ──
PRODUCT_BASE = 0xA0000000
GENERICS = {
    "G_SYS_CLK":         100_000_000,   # clk_sys stays 100 MHz (PF-P0 §2)
    "G_AXI_ID":          0,
    "G_FIFO_DEPTH":      9,
    "G_USER_DATA_WIDTH": 32,
    "G_DDR_DATA_WIDTH":  64,             # FIC native width — NOT the 256-bit DE25 f2sdram
    "G_DDR_ADDR_WIDTH":  32,
    "G_DDR_BASE_ADDR":   'x"A0000000"',  # upper half; overflows INTEGER — vector only
    "G_BUFFER_WIDTH":    28,             # PFSoC G_DDR_WIN_WIDTH = 28 (256 MiB ring)
    "G_FIFO_IMPL":       "olo",
    "G_DDR_BACKEND":     "fic",          # elaborate the PolarFire SoC MSS-FIC body
}

UART0_ID = 12
DDR_SIZE = 1 << 20   # 1 MiB — AxiRam wraps 0xA0000000-based addresses mod size


async def bring_up(dut):
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
    dut.CONFIG.value = (dev_id & 0x1F) << 11
    await ClockCycles(dut.CLK, 4)
    dut.CONFIG.value = ((dev_id & 0x1F) << 11) | (cfg_bits & 0x7FF)
    await ClockCycles(dut.CLK, 8)


async def drain_pipeline(dut, max_us: int = 800):
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
async def test_fic_product_base_uploader_issues_aw_and_commit_advances(dut):
    """At the PFSoC FIC product geometry the write plane must actually run."""
    axi_ram = await bring_up(dut)

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

    await latch_config(dut, UART0_ID, cfg_bits=0b011)   # Enable + AutoSync
    await ClockCycles(dut.CLK, 50)

    rng = random.Random(0xA0F1C)
    uart = UartSource(dut.UART0_TX, baud=460_800, bits=8, stop_bits=1, parity="none")
    await uart.write_bytes([rng.randint(0, 0xFF) for _ in range(150)])  # baud calibration
    await uart.write_bytes([rng.randint(0, 0xFF) for _ in range(64)])   # payload frames
    await uart.write_bytes([0x00, 0xFF, 0x00, 0xFF])                    # flush the pipe tail

    await drain_pipeline(dut)
    await ClockCycles(dut.CLK, 100)
    _mon.kill()

    commit = int(dut.WR_COMMIT_ADDR.value)
    dut._log.info(
        f"DS-P2.124 PFSoC FIC geometry (base=0x{PRODUCT_BASE:08X}, ddr_width=64): "
        f"AWVALID ever={awvalid_ever[0]}, AW beats={len(seen_awaddr)}, "
        f"AWADDRs[:4]={[hex(a) for a in seen_awaddr[:4]]}, "
        f"WR_COMMIT_ADDR=0x{commit:08X}, write_idle_ever_low={write_idle_ever_low[0]}")

    assert awvalid_ever[0], (
        "PFSoC FIC write DMA NEVER asserted AWVALID under sustained datalogger "
        "enqueue at product geometry (base=0xA0000000, ddr_data_width=64) — the "
        "uploader is wedged IDLE and issues no burst (the DS-T1.12 wedge shape, "
        "now on the fic backend). File T-tier.")
    assert seen_awaddr, (
        "AWVALID asserted but the sink never completed an AW handshake — cannot "
        "verify base propagation through the fic wrapper. File T-tier.")

    bad = [hex(a) for a in seen_awaddr if (a >> 28) != 0xA]
    assert not bad, (
        f"AWADDR(s) {bad} do not carry the product base 0xA0000000 (top nibble "
        f"!= 0xA) — the 32-bit base was truncated/degraded through the fic "
        f"wrapper's elaboration. File T-tier.")

    assert commit != 0, (
        f"WR_COMMIT_ADDR wedged at 0 despite {len(seen_awaddr)} AW beat(s) — the "
        f"fic write plane launched addresses but no write B-completed to advance "
        f"the commit pointer. File T-tier.")

    assert write_idle_ever_low[0], (
        "WRITE_IDLE stayed high for the ENTIRE run — the fic write plane never "
        "became busy, so no transfer was ever in flight. File T-tier.")

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
        "no structurally valid zs_msg frame landed in the DDR sink at PFSoC FIC "
        "product geometry despite AW issued + commit advanced — the 64-bit fic "
        "write path corrupted the framed stream. File T-tier.")
    dut._log.info(
        f"DS-P2.124: fic write plane ran at PFSoC product geometry — "
        f"{len(seen_awaddr)} AW beat(s) at base 0xA*, commit=0x{commit:08X}, "
        f"{frames} frame(s) in DDR.")


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
