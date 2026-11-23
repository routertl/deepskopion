"""Full datalogger data-path integration test — I2C / UART / SPI → DDR.

Drives real protocol traffic onto the scalar datalogger pins of
`tb_datalogger_to_ddr` (datalogger_gather → zs_msg_framer → rr.ddr_stream_axi)
and verifies the framed zs_msg frames land — correctly decoded — in a
cocotbext.axi.AxiRam DDR sink attached to the flat M_AXI_* master.

ADVERSARIAL verification (§"Definition of done — sim/verification"
+ ROUTERTL-002): every expected value below is HARD-CODED from the spec
(zs_msg.py domains, datalogger_config_pkg device-ids, the framer's 6-word
layout) — never derived from DUT output at runtime. Magic, domain,
datalogger_id and the decoded payload byte are all asserted against constants.

Frame geometry (zs_msg_framer.vhd, G_PAYLOAD_WORDS=1):
    6 × 32-bit words = 24 bytes per frame.
      w0: magic(LE 0x5A53) | version=1 | header_len=5
      w1: datalogger_id | msg_class | msg_type(sev|dom|evt)
      w2: timestamp[31:0]
      w3: timestamp[63:32]
      w4: payload_len | flags
      w5: payload (the raw 32-bit datalogger message)

DDR packing (uploader / olo_base_wconv_n2xn, confirmed by rr-ddr-stream's
cleaver dump): the 32-bit word stream packs LSB-first into each 128-bit
DDR word, so the flat AxiRam byte buffer IS the contiguous LE zs_msg byte
stream — parse_header(buf, offset) walks it directly at offset = i*24.

Run:  rr sim run test_datalogger_to_ddr   (from the deepskopion dir)

The strict BFM monitors need the clock they oversample on — `_EnvShim.clk` and
`sample_clk=dut.CLK` below. Do not drop them, and do not reach for
strict_protocol=False: this testbench is the first with a live clock to let the
SDK's SpiMonitor actually run, and it immediately caught a real BFM defect
(RTL-T2.245, fixed in routertl 04619cf3). 6/6 as of that fix.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, ClockCycles

# Locate the routertl SDK cocotb dir via the pip-installed routertl_core
# (DS-P2.29 fallout — old shim walked up the tree for an `engine/` folder
# that no longer exists post-pip-decouple). Same idiom as every other
# deepskopion datalogger test.
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: Could not import engine. Run via `rr sim run test_datalogger_to_ddr`.")
    sys.exit(1)

from cocotbext.axi import AxiBus, AxiRam

# Native SDK BFMs (zero external deps, on path under `rr sim`).
from rr_cocotb_tb.env import TbEnv
from rr_cocotb_tb.drivers.uart_master import UartSource
from rr_cocotb_tb.drivers.i2c_master import I2cMasterDriver, I2cTiming
from rr_cocotb_tb.drivers.spi_master import SpiBus, SpiConfig, SpiMaster

# Decode oracle — the PS-side zs_msg parser, used here as the test oracle.
DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT))
from sw.stream.zs_msg import (
    parse_header, ZS_MAGIC, HEADER_SIZE,
    DOMAIN_UART, DOMAIN_I2C, DOMAIN_SPI, DOMAIN_EDGE, EVT_EDGE_BATCH,
    parse_edge_batch,
    CLASS_DATA,
)

PERIOD_NS = 10

GENERICS = {
    "G_SYS_CLK":         100_000_000,
    "G_AXI_ID":          0,
    "G_FIFO_DEPTH":      9,
    "G_USER_DATA_WIDTH": 32,
    "G_DDR_DATA_WIDTH":  128,
    "G_DDR_ADDR_WIDTH":  32,
    # DS-P2.110: G_DDR_BASE_ADDR is now std_logic_vector(31 downto 0) (mirrors
    # the deepskopion_core seam). Pass a VHDL bit-string literal, not a Python
    # int — NVC's -g needs a vector literal for a vector generic. base 0 here.
    "G_DDR_BASE_ADDR":   'x"00000000"',
    "G_BUFFER_WIDTH":    20,
    "G_FIFO_IMPL":       "olo",
}

# ── Profile and package resolution (DS-P2.166) ──────────────────────────────
MIX_DIR = DEEPSKOPION_ROOT / "tools" / "tests" / "datalogger_mix"


def _resolve_mix_paths() -> tuple[str, Path, Path]:
    profile_name = os.environ.get("RR_SIM_PROFILE", "default")
    if profile_name == "default":
        pkg = DEEPSKOPION_ROOT / "hw" / "rtl" / "pkg" / "datalogger_config_pkg.vhd"
        gather = DEEPSKOPION_ROOT / "hw" / "rtl" / "datalogger" / "infra" / "datalogger_gather" / "datalogger_gather.vhd"
    elif profile_name == "zero_edge":
        pkg = MIX_DIR / "fixtures" / "zero_edge" / "datalogger_config_pkg.vhd"
        gather = MIX_DIR / "fixtures" / "zero_edge" / "datalogger_gather.vhd"
    elif profile_name == "edge_only":
        pkg = MIX_DIR / "fixtures" / "edge_only" / "datalogger_config_pkg.vhd"
        gather = MIX_DIR / "fixtures" / "edge_only" / "datalogger_gather.vhd"
    else:
        sys.path.insert(0, str(DEEPSKOPION_ROOT / "hw" / "sim" / "cocotb" / "tests" / "datalogger" / "infra" / "datalogger_gather"))
        import test_datalogger_mix_sweep as sw
        profile = sw.load_profile(profile_name)
        out_dir = sw.mix_out_dir(profile_name)
        pkg, gather = sw.mix_sources(profile_name, profile, out_dir)
    return profile_name, pkg, gather


def _read_pkg_constants(pkg_path: Path) -> dict[str, int]:
    text = pkg_path.read_text(encoding="utf-8")
    consts = {}
    for m in re.finditer(r"constant\s+(C_[A-Za-z0-9_]+)\s*:\s*(?:natural|integer|positive)\s*:=\s*([0-9]+)\s*;", text):
        consts[m.group(1)] = int(m.group(2))
    return consts


PROFILE_NAME, PKG_PATH, GATHER_PATH = _resolve_mix_paths()
PKG_CONSTS = _read_pkg_constants(PKG_PATH)

EDGE_DATALOGGERS = PKG_CONSTS.get("C_EDGE_DATALOGGERS", 0)
UART_DATALOGGERS = PKG_CONSTS.get("C_UART_DATALOGGERS", 0)
I2C_DATALOGGERS  = PKG_CONSTS.get("C_I2C_DATALOGGERS", 0)
SPI_DATALOGGERS  = PKG_CONSTS.get("C_SPI_DATALOGGERS", 0)

# ── HARD-CODED expectations (spec from config package, never DUT-derived) ───
UART0_ID = PKG_CONSTS.get("C_UART0_ID", 12)
I2C0_ID  = PKG_CONSTS.get("C_I2C0_ID", 4)
SPI0_ID  = PKG_CONSTS.get("C_SPI0_ID", 8)
EDGE0_ID = PKG_CONSTS.get("C_EDGE0_ID", 1)

# Shared with capture_correctness.  Literal parsing in
# test_capture_correctness.py makes drift on either side fail closed.
EDGE_TRANSITIONS = [(10_000, 1), (10_200, 0), (10_520, 1), (10_800, 0)]

# DS-T2.84: the edge lifecycle frame's own msg_type event id (zs_msg_pkg.vhd
# C_ZS_EVT_EDGE_LIFECYCLE) and the ENABLED code in its payload byte 0
# (edge_datalogger.vhd C_ED_EVENT_ENABLED). Literals, per ROUTERTL-002.
EVT_EDGE_LIFECYCLE = 0x08
EDGE_LIFECYCLE_ENABLED = 0x03

# Frame geometry.
FRAME_WORDS = 6
FRAME_BYTES = FRAME_WORDS * 4   # 24
PAYLOAD_WORD_OFFSET = 5 * 4     # byte offset of word5 within a frame

# Free-running TS bit-width fed to the framer (full 64) and gather (low 32).
DDR_SIZE = 1 << 16   # 64 KiB AxiRam (addr width is 32 but ring stays tiny here)


# ── Pipeline / DDR helpers ───────────────────────────────────────────────────

async def bring_up(dut):
    """Start the single CLK domain, attach AxiRam, walk active-high reset."""
    cocotb.start_soon(Clock(dut.CLK, PERIOD_NS, units="ns").start())

    # Idle all datalogger pins (open-drain buses idle high; SPI clk/data idle low,
    # CS idle high). These mirror the wrapper's port defaults.
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
    """Write a CONFIG word for one datalogger instance.

    config_control latches on (deviceIdMatch && CONFIG-value-changed). We
    prime the device-id (creating a value transition), then write the full
    word with the enable/config bits. The bus is shared across all dataloggers
    so the id-gate keeps the write private to `dev_id`.
    """
    dut.CONFIG.value = (dev_id & 0x1F) << 11
    await ClockCycles(dut.CLK, 4)
    dut.CONFIG.value = ((dev_id & 0x1F) << 11) | (cfg_bits & 0x7FF)
    await ClockCycles(dut.CLK, 8)


async def drain_pipeline(dut, max_us: int = 400):
    """Let the gather→framer→DDR chain quiesce: poll WRITE_IDLE high for a
    sustained window. No settle-sleeps to dodge races — this is a genuine
    "DMA has flushed" poll plus a modest fixed slack for the AXI B-response.
    """
    # First give the slow protocol stimulus + framer time to push into the
    # DMA FIFO, then wait for the write plane to report idle.
    quiet = 0
    waited_us = 0
    while waited_us < max_us:
        await Timer(2, units="us")
        waited_us += 2
        if int(dut.WRITE_IDLE.value) == 1:
            quiet += 2
            if quiet >= 20:   # 20 µs sustained idle ⇒ DMA flushed
                break
        else:
            quiet = 0
    # Small fixed slack for the final AXI B handshakes to settle in AxiRam.
    await Timer(5, units="us")


def extract_frames(axi_ram, n_bytes: int) -> list:
    """Read the AxiRam byte buffer from base 0 and walk it the way the real
    PS-side consumer (DdrRingDataSource) does: magic-resync on 32-bit-word
    boundaries.

    The DMA write plane flushes each framed event into whole 128-bit DDR
    words; a 6-word (24 B) frame that ends mid-DDR-word is zero-padded out to
    the next 16-byte boundary (observed: 32-byte stride, last 2 lanes zero).
    A fixed-stride walk therefore desyncs on the padding — so we scan every
    4-byte word for magic 0x5A53, parse a header there, validate it, and
    resync forward by the frame's own header+payload length (word-rounded).

    Returns a list of (ZsHeader, payload_word:int) tuples — payload_word is
    the raw 32-bit word5 read LE from the first payload word.
    """
    buf = bytes(axi_ram.read(0, n_bytes))
    frames = []
    off = 0
    limit = len(buf)
    while off + HEADER_SIZE <= limit:
        if int.from_bytes(buf[off:off + 2], "little") != ZS_MAGIC:
            off += 4   # resync: try the next 32-bit word boundary
            continue
        try:
            hdr = parse_header(buf, off)
        except ValueError:
            off += 4
            continue
        # Structural sanity before accepting (a stray 0x5A53 in payload data
        # would otherwise be mis-parsed). version=1, header_len=5 words.
        if hdr.version != 1 or hdr.header_len != HEADER_SIZE // 4:
            off += 4
            continue
        po = off + PAYLOAD_WORD_OFFSET
        payload_word = int.from_bytes(buf[po:po + 4], "little") if po + 4 <= limit else 0
        frames.append((hdr, payload_word))
        # Advance past this frame's header+payload, then round UP to the next
        # 32-bit word (resync scan handles any DDR-word zero padding after).
        adv = HEADER_SIZE + hdr.payload_len
        adv = ((adv + 3) // 4) * 4
        off += max(adv, 4)
    return frames


def extract_wire_frames(axi_ram, n_bytes: int) -> list[bytes]:
    """Return complete, structurally valid zs_msg frames from padded DDR."""
    buf = bytes(axi_ram.read(0, n_bytes))
    out, off = [], 0
    while off + HEADER_SIZE <= len(buf):
        if int.from_bytes(buf[off:off + 2], "little") != ZS_MAGIC:
            off += 4
            continue
        try:
            hdr = parse_header(buf, off)
        except ValueError:
            off += 4
            continue
        size = HEADER_SIZE + hdr.payload_len
        if hdr.version == 1 and hdr.header_len == HEADER_SIZE // 4 and off + size <= len(buf):
            out.append(buf[off:off + size])
            off += ((size + 3) // 4) * 4
        else:
            off += 4
    return out


def _gather_overflow_cnt(dut) -> int:
    """Read the gather's saturating per-input burst-FIFO overflow counter
    (t_fifo_csr.overflowCnt, exposed via the TB signal g_fifo_csr). A non-zero
    value means a datalogger out-ran its arbiter input FIFO and frames were dropped
    at the datalogger→arbiter boundary, before the burst output. Fails LOUDLY if the
    field cannot be read — the overflow guard must never silently no-op."""
    csr = dut.g_fifo_csr
    for field in ("overflowCnt", "overflowcnt", "OVERFLOWCNT"):
        sub = getattr(csr, field, None)
        if sub is not None:
            return int(sub.value)
    raise AssertionError(
        "could not read g_fifo_csr.overflowCnt — overflow guard cannot be "
        "evaluated; refusing to treat that as a pass.")


def assert_frame_invariants(frames, *, expected_domain, expected_datalogger_id,
                            label):
    """Common HARD-CODED structural checks on every captured frame.

    These never read the DUT to decide the expected value — domain &
    datalogger_id come from zs_msg.py / datalogger_config_pkg constants.
    """
    assert frames, (
        f"{label}: ZERO zs_msg frames landed in the DDR sink. The full "
        f"data path (gather→framer→ddr_stream_axi→AxiRam) delivered nothing. "
        f"File T-tier."
    )
    for i, (hdr, _pw) in enumerate(frames):
        assert hdr.magic == ZS_MAGIC, (
            f"{label} frame {i}: bad magic 0x{hdr.magic:04x} (want "
            f"0x{ZS_MAGIC:04x}). Framer/DDR word packing corrupted the "
            f"header. File T-tier."
        )
        assert hdr.version == 1, (
            f"{label} frame {i}: version={hdr.version}, want 1. File T-tier."
        )
        assert hdr.header_len == HEADER_SIZE // 4, (
            f"{label} frame {i}: header_len={hdr.header_len} words, want "
            f"{HEADER_SIZE // 4}. File T-tier."
        )
        assert hdr.msg_class == CLASS_DATA, (
            f"{label} frame {i}: msg_class={hdr.msg_class}, want "
            f"{CLASS_DATA} (DATA). File T-tier."
        )
        assert hdr.domain == expected_domain, (
            f"{label} frame {i}: domain={hdr.domain} (0x{hdr.msg_type:04x}), "
            f"want {expected_domain}. The gather is stamping the wrong "
            f"protocol domain. File T-tier."
        )
        assert hdr.datalogger_id == expected_datalogger_id, (
            f"{label} frame {i}: datalogger_id={hdr.datalogger_id}, want "
            f"{expected_datalogger_id}. Wrong instance routed through the "
            f"chain. File T-tier."
        )


# ── Tests ────────────────────────────────────────────────────────────────────

@cocotb.test()
async def test_edge_to_ddr(dut):
    """Finite canonical EDGE vector → EDGE_BATCH in DDR, exact intervals.

    DS-T2.84: CONFIG 01 enables capture without a read, so no batch entry may
    be an S=1 level readback. The ENABLED lifecycle frame lands as its own
    DOMAIN_EDGE/EVT_EDGE_LIFECYCLE frame. This test used to require an S=1
    batch entry and called it the "initial state-readback": with no read
    requested, that entry was the lifecycle marker, which every EDGE_EVENTS
    consumer reads as "line is low"."""
    axi_ram = await bring_up(dut)
    if EDGE_DATALOGGERS == 0:
        # Zero-edge case (DS-P2.166): stimulate EDGE0 and verify NO EDGE record reaches DDR
        dut.EDGE0.value = 0
        await ClockCycles(dut.CLK, 50)
        previous_ns = 0
        for timestamp_ns, level in EDGE_TRANSITIONS:
            await Timer(timestamp_ns - previous_ns, unit="ns")
            dut.EDGE0.value = level
            previous_ns = timestamp_ns

        await drain_pipeline(dut)
        all_edge_events = []
        for frame in extract_wire_frames(axi_ram, DDR_SIZE):
            hdr = parse_header(frame)
            if hdr.domain == DOMAIN_EDGE:
                all_edge_events.append(hdr)

        assert len(all_edge_events) == 0, (
            f"zero_edge: expected ZERO EDGE records in DDR, got {len(all_edge_events)}: {all_edge_events!r}"
        )
        dut._log.info("zero_edge: verified NO EDGE record reached DDR under stimulation.")
        return

    await latch_config(dut, EDGE0_ID, cfg_bits=0b001)
    await ClockCycles(dut.CLK, 50)

    previous_ns = 0
    for timestamp_ns, level in EDGE_TRANSITIONS:
        await Timer(timestamp_ns - previous_ns, unit="ns")
        dut.EDGE0.value = level
        previous_ns = timestamp_ns

    await drain_pipeline(dut)
    all_edge_events = []
    lifecycle_frames = []
    for frame in extract_wire_frames(axi_ram, DDR_SIZE):
        hdr = parse_header(frame)
        if hdr.domain == DOMAIN_EDGE and hdr.event_id == EVT_EDGE_BATCH:
            _hdr, events = parse_edge_batch(frame)
            all_edge_events.extend(events)
        elif hdr.domain == DOMAIN_EDGE and hdr.event_id == EVT_EDGE_LIFECYCLE:
            lifecycle_frames.append((hdr, frame[HEADER_SIZE:]))

    readbacks = [event for event in all_edge_events if event.s]
    assert not readbacks, (
        "EDGE: lifecycle marker rode EDGE_BATCH as an S=1 level readback — no "
        f"read was requested, yet {len(readbacks)} batch entr(y/ies) decode as "
        f"'line is {[int(e.e) for e in readbacks]}': {readbacks!r}")
    assert len(lifecycle_frames) == 1, (
        f"EDGE: expected one ENABLED lifecycle frame (msg_type 0x06{EVT_EDGE_LIFECYCLE:02x}), "
        f"got {len(lifecycle_frames)}")
    lc_hdr, lc_payload = lifecycle_frames[0]
    assert lc_hdr.datalogger_id == EDGE0_ID and lc_hdr.payload_len == 4, (
        f"EDGE: lifecycle frame header {lc_hdr!r}")
    assert lc_payload[0] == EDGE_LIFECYCLE_ENABLED, (
        f"EDGE: lifecycle code {lc_payload[0]:#04x}, want ENABLED "
        f"{EDGE_LIFECYCLE_ENABLED:#04x}")
    edge_events = [event for event in all_edge_events if not event.s]
    assert len(edge_events) == 4, (
        f"EDGE: expected exactly four DDR-delivered transitions, got {len(edge_events)}; "
        f"all records were {all_edge_events!r}")
    first_tick = edge_events[0].ts_64b
    observed = [
        (10_000 + (event.ts_64b - first_tick) * PERIOD_NS, 1 - int(event.e))
        for event in edge_events
    ]
    assert observed == EDGE_TRANSITIONS, (
        f"EDGE: DDR vector {observed!r} != canonical {EDGE_TRANSITIONS!r}")

# DS-P2.16: a Stream-RLE edge word's own msg_type event id (zs_msg_pkg.vhd
# C_ZS_EVT_EDGE_RLE) and the RLE word types (edge_datalogger_protocol.yml).
EVT_EDGE_RLE = 0x09
RLE_SYNC_LO, RLE_PACK = 0b000, 0b101
# CONFIG[4:3] = "10" Stream-RLE, [1:0] = "01" enable.
EDGE_CFG_RLE = (0b10 << 3) | 0b01
# Tick intervals between the six driven transitions: two full PACK words.
RLE_INTERVALS = [20, 33, 47, 21, 60]


@cocotb.test()
async def test_edge_rle_to_ddr(dut):
    """DS-P2.16: a Stream-RLE capture reaches DDR as one DOMAIN_EDGE/EVT_EDGE_RLE
    (0x0609) frame per RLE word, carrying the word unchanged: SYNC_LO, then
    PACK(d0, 20, 33), PACK(47, 21, 60). PROHIBITION: no RLE word rides an
    EDGE_BATCH, where its [26:24] word type would be read as S/F/E flags."""
    if EDGE_DATALOGGERS == 0:
        return
    axi_ram = await bring_up(dut)
    await latch_config(dut, EDGE0_ID, cfg_bits=EDGE_CFG_RLE)
    await ClockCycles(dut.CLK, 30)

    level = 0
    for gap in [1] + RLE_INTERVALS:
        await ClockCycles(dut.CLK, gap)
        level ^= 1
        dut.EDGE0.value = level

    await drain_pipeline(dut)
    rle_words = []
    batch_events = []
    for frame in extract_wire_frames(axi_ram, DDR_SIZE):
        hdr = parse_header(frame)
        if hdr.domain != DOMAIN_EDGE:
            continue
        if hdr.event_id == EVT_EDGE_BATCH:
            batch_events.extend(parse_edge_batch(frame)[1])
        elif hdr.event_id == EVT_EDGE_RLE:
            assert hdr.datalogger_id == EDGE0_ID and hdr.payload_len == 4, (
                f"EDGE RLE frame header {hdr!r}")
            rle_words.append(int.from_bytes(frame[HEADER_SIZE:HEADER_SIZE + 4], "little"))

    assert not batch_events, (
        f"EDGE: {len(batch_events)} Stream-RLE word(s) rode EDGE_BATCH as S/F/E "
        f"entries: {batch_events!r}")
    assert len(rle_words) == 3, (
        f"EDGE: want 3 EVT_EDGE_RLE frames (SYNC + 2 PACK), got "
        f"{[hex(w) for w in rle_words]}")
    assert all(w >> 27 == EDGE0_ID for w in rle_words), (
        f"EDGE: RLE word device ids {[w >> 27 for w in rle_words]}, want {EDGE0_ID}")
    assert [(w >> 24) & 0x7 for w in rle_words] == [RLE_SYNC_LO, RLE_PACK, RLE_PACK], (
        f"EDGE: RLE word types {[hex(w) for w in rle_words]}")
    assert (rle_words[1] >> 8) & 0xFFFF == (33 << 8) | 20, (
        f"EDGE: first PACK {rle_words[1]:#010x}, want deltas d0, 20, 33")
    assert rle_words[2] & 0xFFFFFF == (60 << 16) | (21 << 8) | 47, (
        f"EDGE: second PACK {rle_words[2]:#010x}, want deltas 47, 21, 60")
    assert 1 <= rle_words[1] & 0xFF <= 255


@cocotb.test()
async def test_uart_to_ddr(dut):
    """UART0 traffic → framed zs_msg frames in DDR.

    The uart_datalogger auto-calibrates baud over a burst of bytes, then emits
    one DATA message per received byte (MSG_OUT = dev_id|type|ts|byte, so the
    byte lands in payload[7:0]). We send a calibration burst, then a KNOWN
    marker payload, drain to DDR, and assert that the marker bytes appear in
    frames carrying DOMAIN_UART + datalogger_id UART0 (=12) with the exact byte
    in payload[7:0].
    """
    if UART_DATALOGGERS == 0:
        dut._log.info("UART datalogger absent in active mix; test skipped.")
        return

    axi_ram = await bring_up(dut)

    # Free-running 64-bit timestamp into the framer is provided by the
    # wrapper's internal ts counter; the gather's TIMESTAMP comes from the
    # same counter (low 32). No cocotb timestamp drive needed.

    # DIAGNOSTIC: count frames the gather actually emits so we can split "DUT
    # produced few frames" from "DMA/sink dropped frames". A passive monitor —
    # never softens an assert. DS-T1.4: the AUTHORITATIVE frame count is
    # `gather_handshakes` (valid AND ready). `gather_pulses` (valid-high cycles)
    # OVER-counts: the burst arbiter holds DOUT_VALID until DOUT_REQ, so a
    # frame waiting on a busy framer is counted once per held cycle. Keep all
    # three so a future mismatch is self-diagnosing.
    gather_pulses = 0       # valid-high CYCLES (over-counts held valid — diag only)
    gather_handshakes = 0   # valid AND ready — the TRUE frame count (assert on this)
    gather_edges = 0        # rising edges of valid (merges back-to-back — diag only)

    async def _count_gather():
        nonlocal gather_pulses, gather_handshakes, gather_edges
        prev_valid = 0
        while True:
            await RisingEdge(dut.CLK)
            try:
                v = int(dut.GATHER_BURST_VALID.value)
                r = int(dut.g_dout_req.value)
            except (ValueError, AttributeError):
                continue
            if v == 1:
                gather_pulses += 1            # every valid-high cycle (over-counts held valid)
                if r == 1:
                    gather_handshakes += 1    # actual valid&ready handshake
                if prev_valid == 0:
                    gather_edges += 1          # rising edge of valid
            prev_valid = v
    _gtask = cocotb.start_soon(_count_gather())

    # Enable UART0: bit0=Enable, bit1=AutoSync (per uart_datalogger config map).
    await latch_config(dut, UART0_ID, cfg_bits=0b011)
    await ClockCycles(dut.CLK, 50)

    import random
    rng = random.Random(0xDEAD)
    BAUD = 460_800   # matches test_payload_verify's value-checked 8N1 case
    uart = UartSource(dut.UART0_TX, baud=BAUD, bits=8, stop_bits=1, parity="none")

    # Calibration burst — the auto-sync datalogger locks its baud estimate from
    # the edge transitions in a run of bytes. It MUST be RANDOM data: a
    # constant pattern (e.g. all-0x55) biases the period estimator and locks
    # to a wrong baud, which then systematically mis-decodes every byte (we
    # diagnosed exactly that 0x55→0x15 corruption before switching to random
    # — see test_payload_verify which uses random cal data for the same
    # reason).
    CAL = [rng.randint(0, 0xFF) for _ in range(150)]
    await uart.write_bytes(CAL)

    # KNOWN marker payload (post-calibration), HARD-CODED (ROUTERTL-002).
    MARKER = [0xA3, 0x5C, 0x71, 0x0E, 0xFF, 0x42]
    await uart.write_bytes(MARKER)
    # The uart_datalogger flushes a captured byte through its internal playback
    # FIFO + frame_gen with a few bytes of latency — the LAST byte of any
    # burst stays in the pipe until subsequent traffic pushes it out. Send a
    # short trailing burst so the full marker (incl. its last byte) is
    # emitted before we drain. This is genuine UART pipeline behaviour, not a
    # settle-hack: the bytes really do need to clock through the FIFO.
    await uart.write_bytes([0x00, 0xFF, 0x00, 0xFF])

    await drain_pipeline(dut)
    _gtask.kill()

    frames = extract_frames(axi_ram, DDR_SIZE)
    overflow_cnt = _gather_overflow_cnt(dut)
    dut._log.info(f"UART: GATHER_BURST valid_cycles={gather_pulses}, "
                  f"handshakes(valid&ready)={gather_handshakes}, "
                  f"valid_rising_edges={gather_edges}, frames_in_ddr={len(frames)}, "
                  f"input_overflowCnt={overflow_cnt}")

    # End-to-end integrity (HARD): every frame the gather emitted must have
    # landed in DDR — no DMA drops, no duplication. This is the strongest
    # full-path invariant and must NOT be softened.
    #
    # DS-T1.4 (2026-06-02): a "frame emitted by the gather" is a valid&READY
    # HANDSHAKE, not a valid-high CYCLE. The gather's burst arbiter (a FIFO
    # arbiter) HOLDS DOUT_VALID until DOUT_REQ, so while the framer is
    # mid-emit the same frame's valid stays high for several cycles. Counting
    # valid-cycles (the old `gather_pulses`) over-counts those held cycles as
    # phantom frames (188 cycles for 167 real frames here) — a MONITOR bug, not
    # a DMA drop. The correct count is `gather_handshakes`; with it the path is
    # provably lossless (167 == 167). gather_pulses/edges stay as diagnostics.
    assert len(frames) == gather_handshakes, (
        f"UART: gather emitted {gather_handshakes} frames (valid&ready "
        f"handshakes) but {len(frames)} landed in DDR. The "
        f"framer→ddr_stream_axi→AxiRam path "
        f"{'dropped' if len(frames) < gather_handshakes else 'duplicated'} "
        f"frames. File T-tier."
    )
    # Strengthening (DS-T1.4): also assert NO frames were dropped at the gather's
    # per-input burst FIFO. overflowCnt saturates on any lost word, so a non-zero
    # value means the datalogger out-ran the FIFO and 167 would itself be lossy.
    assert overflow_cnt == 0, (
        f"UART: gather per-input burst FIFO overflowed {overflow_cnt} time(s) — "
        f"frames were dropped at the datalogger→arbiter boundary before reaching "
        f"DDR. File T-tier."
    )

    uart_frames = [(h, pw) for (h, pw) in frames if h.domain == DOMAIN_UART]
    assert len(uart_frames) == len(frames), (
        f"UART: {len(frames) - len(uart_frames)} non-UART-domain frame(s) "
        f"appeared with only UART0 enabled — cross-talk. File T-tier."
    )

    assert_frame_invariants(uart_frames, expected_domain=DOMAIN_UART,
                            expected_datalogger_id=UART0_ID, label="UART")

    # Timestamps must be monotonically non-decreasing across the frame stream
    # (the wrapper's free-running counter; reordering would corrupt this).
    ts_seq = [h.timestamp for (h, _pw) in uart_frames]
    assert all(b >= a for a, b in zip(ts_seq, ts_seq[1:])), (
        f"UART: frame timestamps are not monotonic — frames reordered "
        f"through the chain. First non-monotonic around "
        f"{next((i for i, (a, b) in enumerate(zip(ts_seq, ts_seq[1:])) if b < a), None)}. "
        f"File T-tier."
    )

    # Filter to the 8-bit DATA payload frames. The gather stamps msg_type
    # event_id = payload[26:24]; the uart_datalogger's 8-bit payload type is
    # 0b010 (C_UART_PACKET_PAYLOAD_TYPE), so event_id == 2 selects payload
    # frames and excludes config (1) / status frames. The byte rides in
    # payload[7:0].
    UART_PAYLOAD_EVENT = 2
    seen_bytes = [pw & 0xFF for (h, pw) in uart_frames
                  if h.event_id == UART_PAYLOAD_EVENT]
    dut._log.info(f"UART: {len(uart_frames)} UART frames "
                  f"({len(seen_bytes)} payload frames); "
                  f"payload bytes={[hex(b) for b in seen_bytes]}")

    def contains_subseq(haystack, needle):
        n = len(needle)
        for s in range(0, len(haystack) - n + 1):
            if haystack[s:s + n] == needle:
                return s
        return -1

    # Assert the marker AND its trailing flush bytes appear as one contiguous
    # run — proves exact byte ordering end-to-end, not mere membership.
    FLUSH = [0x00, 0xFF, 0x00, 0xFF]
    idx = contains_subseq(seen_bytes, MARKER + FLUSH)
    assert idx >= 0, (
        f"UART: marker+flush {[hex(b) for b in MARKER + FLUSH]} not found as "
        f"a contiguous run in the DDR-delivered UART byte stream "
        f"{[hex(b) for b in seen_bytes]}. Bytes were dropped, reordered, or "
        f"the payload[7:0] field is mis-packed through the framer→DDR path. "
        f"File T-tier."
    )
    dut._log.info(f"UART: marker+flush found contiguous at payload-frame "
                  f"index {idx}; domain/datalogger_id verified on every UART "
                  f"frame; timestamps monotonic; {len(frames)} frames "
                  f"delivered with no DMA drop.")


@cocotb.test()
async def test_i2c_to_ddr(dut):
    """I2C0 traffic → framed zs_msg frames in DDR.

    Drive a complete I2C write transaction (START, addr byte, data bytes,
    STOP). The i2c_datalogger emits event + payload DATA messages; we assert
    every DDR frame carries DOMAIN_I2C + datalogger_id I2C0 (=4) and that at
    least one frame appears (the bus activity produced output).
    """
    if I2C_DATALOGGERS == 0:
        dut._log.info("I2C datalogger absent in active mix; test skipped.")
        return

    axi_ram = await bring_up(dut)

    # Enable I2C0: bit0 = Enable.
    await latch_config(dut, I2C0_ID, cfg_bits=0b001)
    await ClockCycles(dut.CLK, 50)

    # I2C master on the two scalar pins. sda_in = sda (single-wire model in
    # the wrapper — no separate sense line; the BFM only reads sda for ACK).
    i2c = I2cMasterDriver(
        dut_env_shim(dut),
        sda=dut.I2C0_SDA, scl=dut.I2C0_SCL, sda_in=dut.I2C0_SDA,
        timing=I2cTiming(period_ns=10_000),   # 100 kHz
    )
    await i2c.initialize()

    # KNOWN transaction: write [0xAB, 0xCD, 0x3E] to slave 0x50.
    DATA = [0xAB, 0xCD, 0x3E]
    await i2c.write_transaction(0x50, DATA, read=False)

    await drain_pipeline(dut)

    frames = extract_frames(axi_ram, DDR_SIZE)
    i2c_frames = [(h, pw) for (h, pw) in frames if h.domain == DOMAIN_I2C]

    assert_frame_invariants(i2c_frames, expected_domain=DOMAIN_I2C,
                            expected_datalogger_id=I2C0_ID, label="I2C")

    # i2c_datalogger encodes event vs payload in payload[26:24] → gather event_id:
    # events use type "100" (event_id == 4); byte payloads use type "00" with
    # the 3rd bit = ACK/NACK, so payload frames have event_id ∈ {0,1}. Byte
    # rides in payload[7:0]. Filter payload frames and assert the written
    # DATA bytes appear as a CONTIGUOUS in-order run (after the addr byte).
    I2C_EVENT = 4
    payload_bytes = [pw & 0xFF for (h, pw) in i2c_frames if h.event_id != I2C_EVENT]
    event_ids    = [h.event_id for (h, _pw) in i2c_frames if h.event_id == I2C_EVENT]
    dut._log.info(f"I2C: {len(i2c_frames)} frames "
                  f"({len(payload_bytes)} byte-payloads, {len(event_ids)} events); "
                  f"payload bytes={[hex(b) for b in payload_bytes]}")

    def contains_subseq(haystack, needle):
        n = len(needle)
        for s in range(0, len(haystack) - n + 1):
            if haystack[s:s + n] == needle:
                return s
        return -1

    idx = contains_subseq(payload_bytes, DATA)
    assert idx >= 0, (
        f"I2C: written data {[hex(b) for b in DATA]} not found as a "
        f"contiguous in-order run in the DDR-delivered I2C byte payloads "
        f"{[hex(b) for b in payload_bytes]}. Bytes dropped, reordered, or "
        f"mis-packed through the framer→DDR path. File T-tier."
    )
    # The address byte (0x50<<1 | W = 0xA0) must precede the data in the
    # payload stream — prove the whole known transaction, byte-exact and in
    # order.  The old ``0xA0 in prefix or idx > 0`` condition was vacuous when
    # the data happened to start after index zero: any unrelated byte made it
    # pass even if the address was corrupted.  Pinning the adjacent byte is the
    # regression guard for DS-X3's exact-payload requirement.
    assert idx > 0 and payload_bytes[idx - 1] == 0xA0, (
        f"I2C: exact address byte 0xA0 missing immediately before data run at "
        f"index {idx}; payload stream was {[hex(b) for b in payload_bytes]}. "
        f"File T-tier."
    )
    # At least START + STOP events must have framed through.
    assert len(event_ids) >= 2, (
        f"I2C: only {len(event_ids)} event frames in DDR; expected ≥2 "
        f"(START + STOP) for a complete transaction. File T-tier."
    )
    dut._log.info(f"I2C: data bytes contiguous + in-order at index {idx}; "
                  f"{len(event_ids)} event frames; domain/datalogger_id verified "
                  f"on every I2C frame.")


@cocotb.test()
async def test_spi_to_ddr(dut):
    """SPI0 traffic → framed zs_msg frames in DDR.

    SPI0 is a qspi_datalogger in 4-pin mode. In 2-data-pin (SPI) mode it emits
    a type-A payload per byte with MOSI in payload[7:0] and a type-B payload
    with MISO. We drive KNOWN MOSI bytes (MISO idle = 0), assert every DDR
    frame carries DOMAIN_SPI + datalogger_id SPI0 (=8), and that each MOSI byte
    shows up in the DDR payload stream.
    """
    if SPI_DATALOGGERS == 0:
        dut._log.info("SPI datalogger absent in active mix; test skipped.")
        return

    axi_ram = await bring_up(dut)

    # Enable SPI0: bit0 = Enable (configEnable = deviceLoadConfig(0)).
    await latch_config(dut, SPI0_ID, cfg_bits=0b001)
    await ClockCycles(dut.CLK, 50)

    bus = SpiBus(sclk=dut.SPI0_SCLK, mosi=dut.SPI0_MOSI,
                 miso=dut.SPI0_MISO, cs=dut.SPI0_CS)
    cfg = SpiConfig(word_width=8, sclk_freq=1e6, cpol=False, cpha=False,
                    msb_first=True, cs_active_low=True)
    spi = SpiMaster(bus, cfg, sample_clk=dut.CLK)

    # KNOWN MOSI bytes.
    MOSI = [0xDE, 0xAD, 0xBE, 0xEF]
    await spi.write(MOSI)

    await drain_pipeline(dut)

    frames = extract_frames(axi_ram, DDR_SIZE)
    spi_frames = [(h, pw) for (h, pw) in frames if h.domain == DOMAIN_SPI]

    assert_frame_invariants(spi_frames, expected_domain=DOMAIN_SPI,
                            expected_datalogger_id=SPI0_ID, label="SPI")

    # qspi_datalogger (4-pin SPI mode) emits per byte: a type-A payload carrying
    # MOSI in payload[7:0] (event_id 1 = C_SPI_PACKET_PAYLOAD_TYPE_A) and a
    # type-B payload carrying MISO (event_id 2). Filter the MOSI (type-A)
    # frames and assert they equal the driven sequence EXACTLY and in order.
    SPI_MOSI_EVENT = 1   # C_SPI_PACKET_PAYLOAD_TYPE_A
    SPI_MISO_EVENT = 2   # C_SPI_PACKET_PAYLOAD_TYPE_B
    mosi_bytes = [pw & 0xFF for (h, pw) in spi_frames if h.event_id == SPI_MOSI_EVENT]
    miso_bytes = [pw & 0xFF for (h, pw) in spi_frames if h.event_id == SPI_MISO_EVENT]
    dut._log.info(f"SPI: {len(spi_frames)} frames; MOSI(type-A)="
                  f"{[hex(b) for b in mosi_bytes]} MISO(type-B)="
                  f"{[hex(b) for b in miso_bytes]}")

    assert mosi_bytes == MOSI, (
        f"SPI: type-A (MOSI) DDR payload bytes {[hex(b) for b in mosi_bytes]} "
        f"!= driven MOSI {[hex(b) for b in MOSI]} (exact, in-order). Bytes "
        f"dropped, reordered, or mis-packed through the framer→DDR path. "
        f"File T-tier."
    )
    # MISO was held idle (0) — every type-B byte must be 0x00.
    assert all(b == 0x00 for b in miso_bytes), (
        f"SPI: type-B (MISO) bytes {[hex(b) for b in miso_bytes]} not all 0 "
        f"despite MISO held idle-low. File T-tier."
    )
    dut._log.info("SPI: MOSI sequence exact + in-order in DDR; MISO all-zero; "
                  "domain/datalogger_id verified on every SPI frame.")


@cocotb.test()
async def test_combined_i2c_uart_spi_to_ddr(dut):
    """All three protocols enabled + driven in one run → interleaved frames
    in DDR. Verifies the burst arbiter + framer + DMA carry all three
    domains without cross-talk: every captured frame's (domain, datalogger_id)
    pair must be one of the three known protocol identities, and all three
    protocols must be represented.
    """
    if min(I2C_DATALOGGERS, UART_DATALOGGERS, SPI_DATALOGGERS) == 0:
        dut._log.info("Combined test requires I2C, UART, and SPI; skipped in this mix.")
        return

    axi_ram = await bring_up(dut)

    # Enable all three (id-gated CONFIG writes, sequenced).
    await latch_config(dut, UART0_ID, cfg_bits=0b011)
    await latch_config(dut, I2C0_ID,  cfg_bits=0b001)
    await latch_config(dut, SPI0_ID,  cfg_bits=0b001)
    await ClockCycles(dut.CLK, 50)

    # UART calibration + marker.
    uart = UartSource(dut.UART0_TX, baud=460_800, bits=8, stop_bits=1, parity="none")
    i2c = I2cMasterDriver(dut_env_shim(dut), sda=dut.I2C0_SDA, scl=dut.I2C0_SCL,
                          sda_in=dut.I2C0_SDA, timing=I2cTiming(period_ns=10_000))
    bus = SpiBus(sclk=dut.SPI0_SCLK, mosi=dut.SPI0_MOSI, miso=dut.SPI0_MISO, cs=dut.SPI0_CS)
    spi = SpiMaster(bus, SpiConfig(word_width=8, sclk_freq=1e6, cpol=False, cpha=False),
                    sample_clk=dut.CLK)
    await i2c.initialize()

    import random
    _rng = random.Random(0xBEEF)
    await uart.write_bytes([_rng.randint(0, 0xFF) for _ in range(150)])  # baud-lock
    await uart.write_bytes([0xA3, 0x5C])        # UART marker
    await i2c.write_transaction(0x50, [0xAB, 0xCD], read=False)
    await spi.write([0xDE, 0xAD])

    await drain_pipeline(dut, max_us=600)

    frames = extract_frames(axi_ram, DDR_SIZE)
    assert frames, (
        "Combined: ZERO zs_msg frames in DDR with all three protocols "
        "driven. File T-tier."
    )

    KNOWN = {
        (DOMAIN_UART, UART0_ID),
        (DOMAIN_I2C,  I2C0_ID),
        (DOMAIN_SPI,  SPI0_ID),
    }
    seen = set()
    aliens = []
    for i, (hdr, _pw) in enumerate(frames):
        assert hdr.magic == ZS_MAGIC, (
            f"Combined frame {i}: bad magic 0x{hdr.magic:04x}. File T-tier."
        )
        ident = (hdr.domain, hdr.datalogger_id)
        if ident in KNOWN:
            seen.add(ident)
        else:
            aliens.append((i, ident, hex(hdr.msg_type)))

    assert not aliens, (
        f"Combined: {len(aliens)} frame(s) with an unexpected "
        f"(domain, datalogger_id) pair: {aliens[:5]}. Cross-talk or arbiter "
        f"mux corruption in the multi-protocol path. File T-tier."
    )
    missing = KNOWN - seen
    assert not missing, (
        f"Combined: protocols {sorted(missing)} produced ZERO frames in DDR "
        f"despite being enabled + driven. One protocol's path to DDR is "
        f"broken or starved by the arbiter. File T-tier."
    )
    dut._log.info(f"Combined: {len(frames)} frames; all three identities "
                  f"present {sorted(seen)}; no alien (domain,datalogger_id) pairs.")


@cocotb.test()
async def test_uart_i2c_pair_to_ddr(dut):
    """DS-T2.24 HW-mirror: exactly TWO dataloggers (UART + I2C) enabled + driven,
    SPI deliberately held OUT. Isolates the 2-datalogger interaction from any
    "SPI floods the arbiter" effect (SPI is the fastest frame source). Asserts
    BOTH UART and I2C reach DDR with correct identity, NO SPI frames appear
    (SPI never enabled/driven), and no cross-talk / alien (domain,datalogger_id).
    """
    if min(UART_DATALOGGERS, I2C_DATALOGGERS) == 0:
        dut._log.info("Pair test requires UART and I2C; skipped in this mix.")
        return

    axi_ram = await bring_up(dut)

    # Enable ONLY UART + I2C (id-gated CONFIG writes). SPI left disabled.
    await latch_config(dut, UART0_ID, cfg_bits=0b011)
    await latch_config(dut, I2C0_ID,  cfg_bits=0b001)
    await ClockCycles(dut.CLK, 50)

    uart = UartSource(dut.UART0_TX, baud=460_800, bits=8, stop_bits=1, parity="none")
    i2c = I2cMasterDriver(dut_env_shim(dut), sda=dut.I2C0_SDA, scl=dut.I2C0_SCL,
                          sda_in=dut.I2C0_SDA, timing=I2cTiming(period_ns=10_000))
    await i2c.initialize()

    import random
    _rng = random.Random(0x5EED)
    await uart.write_bytes([_rng.randint(0, 0xFF) for _ in range(150)])  # baud-lock
    await uart.write_bytes([0x7E, 0x42])        # UART marker
    await i2c.write_transaction(0x50, [0xAB, 0xCD], read=False)

    await drain_pipeline(dut, max_us=600)

    frames = extract_frames(axi_ram, DDR_SIZE)
    overflow_cnt = _gather_overflow_cnt(dut)
    assert frames, (
        "UART+I2C pair: ZERO zs_msg frames in DDR with both enabled + driven. "
        "File T-tier."
    )

    EXPECTED = {(DOMAIN_UART, UART0_ID), (DOMAIN_I2C, I2C0_ID)}
    seen = set()
    spi_frames = []
    aliens = []
    for i, (hdr, _pw) in enumerate(frames):
        assert hdr.magic == ZS_MAGIC, (
            f"UART+I2C frame {i}: bad magic 0x{hdr.magic:04x}. File T-tier.")
        ident = (hdr.domain, hdr.datalogger_id)
        if ident == (DOMAIN_SPI, SPI0_ID):
            spi_frames.append(i)
        elif ident in EXPECTED:
            seen.add(ident)
        else:
            aliens.append((i, ident, hex(hdr.msg_type)))

    # SPI was never enabled/driven — any SPI frame means a disabled datalogger
    # leaked into the ring (config gating or arbiter mux bug).
    assert not spi_frames, (
        f"UART+I2C pair: {len(spi_frames)} SPI frame(s) appeared though SPI was "
        f"never enabled — a disabled datalogger leaked into the ring. File T-tier.")
    assert not aliens, (
        f"UART+I2C pair: {len(aliens)} alien (domain,datalogger_id) frame(s): "
        f"{aliens[:5]}. Cross-talk / arbiter mux corruption. File T-tier.")
    missing = EXPECTED - seen
    assert not missing, (
        f"UART+I2C pair: {sorted(missing)} produced ZERO frames despite being "
        f"enabled + driven — one path is broken or starved by the arbiter "
        f"(the exact DS-T2.24 HW symptom). File T-tier.")
    assert overflow_cnt == 0, (
        f"UART+I2C pair: gather input-FIFO overflowed {overflow_cnt} time(s) — "
        f"frames dropped at the datalogger→arbiter boundary. File T-tier.")
    dut._log.info(f"UART+I2C pair: {len(frames)} frames; both identities present "
                  f"{sorted(seen)}; no SPI leak; overflowCnt=0.")


@cocotb.test()
async def test_axi3_burst_cap_under_backpressure(dut):
    """DS-T2.60: the datalogger→DDR write DMA must never emit AWLEN>15.

    The write burst is AXI4-wide (AWLEN is 8-bit), but the silicon target is the
    Zynq-7 S_AXI_HP port, which is AXI3 — AWLEN is 4-bit, so 16 beats is the hard
    maximum. Left uncapped, ddr_stream_axi's burstw_setter only saturates to 255,
    so under sustained AW→W back-pressure (DRAM refresh / HP interconnect arb) the
    512-word write FIFO fills and it emits AWLEN up to 255. The Zynq-7 SmartConnect
    must then split-or-drop that oversized burst → SILENT DATA LOSS on silicon.

    tb_datalogger_to_ddr instantiates u_ddr (rr.ddr_stream_axi) with
    max_burst_len_g=>16 — mirroring deepskopion_top's u_ddr_burst — so every
    emitted burst must satisfy AWLEN<=15. This guard holds the DDR sink fully
    back-pressured while it drives enough datalogger traffic to buffer well past 16
    words, then releases the sink and watches the AW channel. It asserts the
    largest burst emitted is EXACTLY 15 — which proves both that the DMA wanted a
    burst >15 (so the stimulus really exercised the cap) AND that the cap clamped
    it. PROVEN teeth: with max_burst_len_g left at the AXI4 default (256) this
    exact stimulus emits AWLEN=254 (255 beats) — the T2.60 defect this guard
    exists to catch (regression-guard rule); removing the tb cap reintroduces it.
    """
    if UART_DATALOGGERS == 0:
        dut._log.info("AXI3 burst cap test requires UART0; skipped in this mix.")
        return

    import random

    axi_ram = await bring_up(dut)

    # Hold the DDR write sink fully back-pressured: never accept AW or W beats.
    # The IP's write FIFO fills and holds — nothing drains while we load it.
    # (The datalogger sources are far slower than any draining AXI sink, so only a
    # FULLY stalled sink lets the FIFO accumulate past the 16-beat cap.)
    def _hold_ready_low():
        while True:
            yield True

    def _never_pause():
        while True:
            yield False

    axi_ram.write_if.aw_channel.set_pause_generator(_hold_ready_low())
    axi_ram.write_if.w_channel.set_pause_generator(_hold_ready_low())

    # Passive AW-channel monitor — record AWLEN on every AWVALID&AWREADY beat.
    # Never softens an assert (bug-finding-is-success): it only observes. Also
    # track whether AWVALID ever asserted, to distinguish "DMA never issued a
    # burst" from "sink never accepted one".
    seen_awlen = []
    awvalid_ever = [False]

    async def _aw_monitor():
        while True:
            await RisingEdge(dut.CLK)
            try:
                awv = int(dut.M_AXI_AWVALID.value)
                awr = int(dut.M_AXI_AWREADY.value)
            except ValueError:
                continue  # X during reset settle — not a beat
            if awv == 1:
                awvalid_ever[0] = True
                if awr == 1:
                    seen_awlen.append(int(dut.M_AXI_AWLEN.value))

    _mon = cocotb.start_soon(_aw_monitor())

    # Drive UART0 traffic. Each post-calibration byte → one zs_msg frame
    # (FRAME_WORDS DDR words), so ~48 bytes ≫ 16 words even before counting the
    # calibration run. The sink is paused, so every word accumulates in the FIFO.
    await latch_config(dut, UART0_ID, cfg_bits=0b011)
    await ClockCycles(dut.CLK, 50)
    rng = random.Random(0xB025)
    uart = UartSource(dut.UART0_TX, baud=460_800, bits=8, stop_bits=1, parity="none")
    await uart.write_bytes([rng.randint(0, 0xFF) for _ in range(150)])  # baud calibration
    await uart.write_bytes([rng.randint(0, 0xFF) for _ in range(48)])   # post-cal FIFO fill
    await uart.write_bytes([0x00, 0xFF, 0x00, 0xFF])                    # flush the pipe tail

    # Give the framer time to push the whole burst into the (paused) FIFO.
    await ClockCycles(dut.CLK, 300)
    dut._log.info(f"DS-T2.60: pre-release AWVALID asserted={awvalid_ever[0]}")

    # Release the sink (explicit never-pause generator — clearing alone can hold
    # the last paused value). The DMA now drains the full FIFO in bursts.
    axi_ram.write_if.aw_channel.set_pause_generator(_never_pause())
    axi_ram.write_if.w_channel.set_pause_generator(_never_pause())
    await drain_pipeline(dut, max_us=800)
    await ClockCycles(dut.CLK, 200)
    _mon.kill()
    dut._log.info(
        f"DS-T2.60: AWVALID ever={awvalid_ever[0]}, bursts captured={len(seen_awlen)}, "
        f"AWLENs={seen_awlen}")

    assert seen_awlen, (
        "no AW beats captured — this guard did NOT exercise DS-T2.60. "
        f"AWVALID ever asserted={awvalid_ever[0]} (True ⇒ the sink never "
        "accepted after release; False ⇒ the DMA never issued a burst — check "
        "stimulus / ring pointers / back-pressure).")
    max_awlen = max(seen_awlen)
    # The AXI3 correctness invariant — the property that fails on silicon.
    assert max_awlen <= 15, (
        f"DS-T2.60: write DMA emitted AWLEN={max_awlen} (>15) under back-pressure "
        f"— illegal on Zynq-7 S_AXI_HP (AXI3, 16-beat max) → SmartConnect "
        f"split/drop → silent data loss. bursts={seen_awlen}")
    # Teeth: prove the FIFO actually filled past the cap, so this run would have
    # FAILED without max_burst_len_g (an all-<15 run would pass vacuously).
    assert max_awlen == 15, (
        f"guard has no teeth: max AWLEN was {max_awlen}, so the FIFO never filled "
        f"past the 16-beat cap and this run would pass even with the cap removed. "
        f"Increase the stimulus. bursts={seen_awlen}")
    dut._log.info(
        f"DS-T2.60: {len(seen_awlen)} write burst(s) under back-pressure, "
        f"max AWLEN={max_awlen} (capped at 15 = AXI3-safe).")


# ── env shim for the I2C BFM ─────────────────────────────────────────────────
#
# The SDK's strict protocol monitors (routertl 32332abc) sample the bus on
# `env.clk`, so a shim carrying only `.log` fails at BFM construction. This
# testbench does its own clocking and reset in bring_up(), which is why it
# passes a shim rather than a full TbEnv — but the shim must still satisfy the
# attribute contract the BFMs rely on (DS-T2.66).

class _EnvShim:
    def __init__(self, dut):
        self.log = dut._log
        self.clk = dut.CLK


def dut_env_shim(dut):
    return _EnvShim(dut)


def ddr_sources(pkg_path: Path, gather_path: Path) -> dict:
    from routertl_core.sim.simulation import _resolve_scope_to_dut_libraries
    libs = _resolve_scope_to_dut_libraries("tb_datalogger_to_ddr", "tich")
    tich_files = []
    for f in libs.get("tich", []):
        f_p = Path(f)
        if f_p.name == "datalogger_config_pkg.vhd":
            tich_files.append(str(pkg_path))
        elif f_p.name == "datalogger_gather.vhd":
            tich_files.append(str(gather_path))
        else:
            tich_files.append(str(f))
    libs["tich"] = tich_files
    return libs



def _clean_nvc_build() -> None:
    from engine.paths import get_build_dir_for_simulator
    import shutil
    build_dir = get_build_dir_for_simulator("nvc")
    shutil.rmtree(build_dir, ignore_errors=True)


if __name__ == "__main__":
    profile_name, pkg_path, gather_path = _resolve_mix_paths()
    custom_libs = None
    if profile_name != "default":
        _clean_nvc_build()
        custom_libs = ddr_sources(pkg_path, gather_path)

    run_simulation(
        top_level="tb_datalogger_to_ddr",
        module=Path(__file__).stem,
        generics=GENERICS,
        scope_to_dut="tb_datalogger_to_ddr",
        work_lib="tich",
        waves=True,
        verbose=True,
        custom_libraries=custom_libs,
    )
