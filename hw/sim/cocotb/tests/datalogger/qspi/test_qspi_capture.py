# rr unknown-llm (AI-generated)
"""DS-P3.23 — qspi_datalogger real-coverage (adversarial) data-path tests.

The pre-existing test_qspi_datalogger covers only reset/lifecycle (QSPI-REQ-50/51).
This file drives REAL SPI traffic through the flat ``QSPI_PINS`` vector and
asserts the captured payload bytes against the driven values — the actual
decode path (646 lines of RTL) that had ~zero functional coverage.

Per the sim definition-of-done (bug-finding-is-success): these tests attack the
decode with hard-coded expected values (ROUTERTL-002), across all four SPI
modes, the quad data path, CPOL auto-detect, framing edges and backpressure. A
miscompare is a real DUT defect → file T-tier, do NOT weaken the assertion.

This file runs the QUAD build (G_QSPI_PINS=6 → C_QSPI_DATA_PINS=4), so the
payload word is the quad format:
    [31:27] dev_id  [26:24] type=001  [23:16] reserved
    payloadA: [15:8]=Q1  [7:0]=Q0      payloadB: [15:8]=Q3  [7:0]=Q2
The 2-data-pin SPI format (timestamp-embedded, type A=001/B=010) is validated
separately at G_QSPI_PINS=4 in test_qspi_spi2pin.py.

Flat pin map (qspi_datalogger.vhd:159-163):
    bit0=SCLK  bit1=CSn(active-low)  bit2=IO0  bit3=IO1  bit4=IO2  bit5=IO3

Run:  RR_SKIP_CLOSURE_PREFLIGHT=1 rr sim run test_qspi_capture
"""

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from sdk.cocotb_helpers import requires  # noqa: E402

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[6]
DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"

GENERICS = {
    "G_QSPI_ID_WIDTH": 5,
    "G_QSPI_DEV_ID": 28,
    "G_QSPI_PINS": 6,
    "G_CONFIG_WIDTH": 16,
    "G_ERROR_WIDTH": 2,
    "G_ENABLE_INFILTER": 0,
    "G_ILA_EN": 1,
    # DS-P3.92: scaled-down sample clock so the SCLK-stuck timeout
    # (C_TIMEOUT_CNT = G_CLK_FREQ_HZ/1000 * 1ms) is reachable in sim.
    # 100_000 → 100 cycles: well above the ~20-cycle inter-edge gap of the
    # 100 ns half-period traffic (no false-fire), reachable by a short stall.
    "G_CLK_FREQ_HZ": 100_000,
}
WATCHDOG_CYCLES = (GENERICS["G_CLK_FREQ_HZ"] // 1000) * 1   # mirrors C_TIMEOUT_CNT

DEV_ID = GENERICS["G_QSPI_DEV_ID"]
CLK_PERIOD_NS = 10            # 100 MHz sample clock
SCLK_HALF_NS = 100           # 100 ns half-period → 10 sample-clocks/half

TYPE_PAYLOAD = 0b001
TYPE_EVENT = 0b100
TYPE_ERROR = 0b010

EV_START = 0x03
EV_STOP = 0x00
EV_CS_DOWN = 0x09
EV_CS_UP = 0x0A
EV_TIMEOUT = 0x04
EV_CS_UP_INCOMPLETE = 0x0B
EV_CS_DOWN_NEW_POL_H = 0x10
EV_CS_DOWN_NEW_POL_L = 0x11

P_SCLK = 0
P_CSN = 1
P_IO = (2, 3, 4, 5)


def _cfg_word(dev_id: int, payload: int) -> int:
    return ((dev_id & 0x1F) << 11) | (payload & 0x7FF)


# config payload bits (deviceLoadConfig): 0=enable 1=CPHA 2=halfDuplex 3=noCS
def _enable_payload(cpha: int = 0, half_duplex: int = 0, no_cs: int = 0) -> int:
    return 0x001 | (cpha << 1) | (half_duplex << 2) | (no_cs << 3)


def decode(raw: int) -> dict:
    return {
        "raw": raw,
        "dev_id": (raw >> 27) & 0x1F,
        "type": (raw >> 24) & 0x7,
        "reserved": (raw >> 16) & 0xFF,
        "lo": raw & 0xFF,
        "hi": (raw >> 8) & 0xFF,
        "ts16": (raw >> 8) & 0xFFFF,
        "code": raw & 0xFF,
    }


class FrameMon:
    """Collects every QSPI_MSG_OUT word asserted with QSPI_MSG_VLD=1."""

    def __init__(self, dut):
        self.dut = dut
        self.frames = []

    async def run(self):
        while True:
            await RisingEdge(self.dut.CLK)
            if int(self.dut.QSPI_MSG_VLD.value) == 1:
                self.frames.append(decode(int(self.dut.QSPI_MSG_OUT.value)))

    def payloads(self):
        return [f for f in self.frames if f["type"] == TYPE_PAYLOAD]

    def events(self):
        return [f for f in self.frames if f["type"] == TYPE_EVENT]

    def event_codes(self):
        return [f["code"] for f in self.events()]


class QspiDriver:
    """Drives the flat QSPI_PINS vector as an SPI master, all 4 modes.

    Each data line carries its own independent 8-bit MSB-first stream sampled
    over 8 SCLK edges — matching the datalogger's per-line shift model.
    """

    def __init__(self, dut, cpol: int, cpha: int, half_ns: int = SCLK_HALF_NS):
        self.dut = dut
        self.cpol = cpol
        self.cpha = cpha
        self.half = half_ns
        self._pins = 0
        self._set(P_SCLK, cpol)
        self._set(P_CSN, 1)
        for b in P_IO:
            self._set(b, 0)
        self._flush()

    def _set(self, bit, val):
        if val:
            self._pins |= (1 << bit)
        else:
            self._pins &= ~(1 << bit)

    def _flush(self):
        self.dut.QSPI_PINS.value = self._pins

    def _present(self, lines: dict, bit_pos: int):
        for line_idx, byte in lines.items():
            self._set(P_IO[line_idx], (byte >> bit_pos) & 1)
        self._flush()

    async def _assert_cs(self):
        self._set(P_SCLK, self.cpol)
        self._flush()
        await Timer(self.half, unit="ns")
        self._set(P_CSN, 0)
        self._flush()
        await Timer(self.half * 2, unit="ns")   # CS-to-first-edge setup

    async def _deassert_cs(self):
        self._set(P_SCLK, self.cpol)
        self._flush()
        await Timer(self.half, unit="ns")
        self._set(P_CSN, 1)
        for b in P_IO:
            self._set(b, 0)
        self._flush()
        await Timer(self.half, unit="ns")

    async def _one_byte(self, lines: dict, n_bits: int = 8):
        for bit_pos in range(n_bits - 1, -1, -1):
            if not self.cpha:
                self._present(lines, bit_pos)
                await Timer(self.half, unit="ns")
                self._set(P_SCLK, 0 if self.cpol else 1)   # sampling edge
                self._flush()
                await Timer(self.half, unit="ns")
                self._set(P_SCLK, 1 if self.cpol else 0)   # return edge
                self._flush()
            else:
                self._set(P_SCLK, 0 if self.cpol else 1)   # first edge
                self._flush()
                self._present(lines, bit_pos)
                await Timer(self.half, unit="ns")
                self._set(P_SCLK, 1 if self.cpol else 0)   # sampling edge
                self._flush()
                await Timer(self.half, unit="ns")

    async def transfer(self, lines: dict, n_bits: int = 8):
        """One CS-framed transfer of a single byte-per-line group."""
        await self._assert_cs()
        await self._one_byte(lines, n_bits)
        await self._deassert_cs()

    async def frame(self, byte_groups: list, n_bits: int = 8):
        """One CS-framed transfer of multiple back-to-back byte groups."""
        await self._assert_cs()
        for grp in byte_groups:
            await self._one_byte(grp, n_bits)
        await self._deassert_cs()


async def _bringup(dut):
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    dut.RST.value = 1
    dut.QSPI_PINS.value = 0b000010   # CS high, SCLK low, data low
    dut.QSPI_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 4)
    mon = FrameMon(dut)
    cocotb.start_soon(mon.run())
    return mon


async def _enable(dut, mon, cpha=0, half_duplex=0, no_cs=0):
    dut.CONFIG.value = _cfg_word(DEV_ID, _enable_payload(cpha, half_duplex, no_cs))
    for _ in range(200):
        await RisingEdge(dut.CLK)
        if EV_START in mon.event_codes():
            return
    raise AssertionError("datalogger never emitted START(0x03) after enable")


# ───────────────────────────────────────────────────────────────────────────
# MODE 0/1/2/3 single-byte decode
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-12")
async def test_mode0_single_byte_io0(dut):
    """QSPI-REQ-12: MODE 0 (CPOL=0,CPHA=0) single byte on IO0 == 0xA5."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: 0xA5})
    await ClockCycles(dut.CLK, 40)
    pays = mon.payloads()
    assert pays, f"no payload; frames={[hex(f['raw']) for f in mon.frames]}"
    p = pays[0]
    assert p["lo"] == 0xA5, f"MODE0 Q0 got 0x{p['lo']:02x} != 0xA5 (raw=0x{p['raw']:08x})"
    assert p["hi"] == 0x00, f"undriven Q1 got 0x{p['hi']:02x} != 0 (raw=0x{p['raw']:08x})"
    dut._log.info(f"MODE0 0xA5 OK raw=0x{p['raw']:08x}")


@cocotb.test()
@requires("QSPI-REQ-13")
async def test_mode1_single_byte_io0(dut):
    """QSPI-REQ-13: MODE 1 (CPOL=0,CPHA=1) single byte on IO0 == 0x3C."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=1)
    await QspiDriver(dut, cpol=0, cpha=1).transfer({0: 0x3C})
    await ClockCycles(dut.CLK, 40)
    pays = mon.payloads()
    assert pays, f"no payload; frames={[hex(f['raw']) for f in mon.frames]}"
    p = pays[0]
    assert p["lo"] == 0x3C, f"MODE1 Q0 got 0x{p['lo']:02x} != 0x3C (raw=0x{p['raw']:08x})"
    dut._log.info(f"MODE1 0x3C OK raw=0x{p['raw']:08x}")


@cocotb.test()
@requires("QSPI-REQ-14")
async def test_mode2_single_byte_io0(dut):
    """QSPI-REQ-14: MODE 2 (CPOL=1,CPHA=0) — CPOL auto-detected from idle-high
    SCLK at CS-assert. Single byte on IO0 == 0x96."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    await QspiDriver(dut, cpol=1, cpha=0).transfer({0: 0x96})
    await ClockCycles(dut.CLK, 40)
    pays = mon.payloads()
    assert pays, f"no payload; frames={[hex(f['raw']) for f in mon.frames]}"
    p = pays[0]
    assert p["lo"] == 0x96, f"MODE2 Q0 got 0x{p['lo']:02x} != 0x96 (raw=0x{p['raw']:08x})"
    dut._log.info(f"MODE2 0x96 OK raw=0x{p['raw']:08x}")


@cocotb.test()
@requires("QSPI-REQ-15")
async def test_mode3_single_byte_io0(dut):
    """QSPI-REQ-15: MODE 3 (CPOL=1,CPHA=1) — CPOL auto-detected. byte == 0xC3."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=1)
    await QspiDriver(dut, cpol=1, cpha=1).transfer({0: 0xC3})
    await ClockCycles(dut.CLK, 40)
    pays = mon.payloads()
    assert pays, f"no payload; frames={[hex(f['raw']) for f in mon.frames]}"
    p = pays[0]
    assert p["lo"] == 0xC3, f"MODE3 Q0 got 0x{p['lo']:02x} != 0xC3 (raw=0x{p['raw']:08x})"
    dut._log.info(f"MODE3 0xC3 OK raw=0x{p['raw']:08x}")


# ───────────────────────────────────────────────────────────────────────────
# Quad 4-line decode (payload A {Q1,Q0} + payload B {Q3,Q2})
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-22")
async def test_quad_four_lines(dut):
    """QSPI-REQ-22: drive 4 distinct bytes on IO0..IO3 (MODE 0); the datalogger
    emits payloadA={Q1,Q0} then payloadB={Q3,Q2}. Asserts all four bytes."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    bytes_io = {0: 0x12, 1: 0x34, 2: 0x56, 3: 0x78}
    await QspiDriver(dut, cpol=0, cpha=0).transfer(bytes_io)
    await ClockCycles(dut.CLK, 60)
    pays = mon.payloads()
    assert len(pays) >= 2, f"expected 2 payload frames (A,B), got {len(pays)}: {[hex(f['raw']) for f in pays]}"
    a, b = pays[0], pays[1]
    assert a["lo"] == 0x12 and a["hi"] == 0x34, (
        f"payloadA Q0/Q1 got 0x{a['lo']:02x}/0x{a['hi']:02x} != 0x12/0x34 (raw=0x{a['raw']:08x})")
    assert b["lo"] == 0x56 and b["hi"] == 0x78, (
        f"payloadB Q2/Q3 got 0x{b['lo']:02x}/0x{b['hi']:02x} != 0x56/0x78 (raw=0x{b['raw']:08x})")
    dut._log.info(f"QUAD OK A=0x{a['raw']:08x} B=0x{b['raw']:08x}")


# ───────────────────────────────────────────────────────────────────────────
# Multi-byte back-to-back within one CS frame
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-10")
async def test_multibyte_backtoback_io0(dut):
    """QSPI-REQ-10: three bytes back-to-back on IO0 in one CS frame.

    In the quad build EACH byte emits an A frame (payloadA={Q1,Q0}) followed by
    a B frame (payloadB={Q3,Q2}) — both carry type tag 001 and are distinguished
    only by delivery order (A then B). Driving IO0 only, the A frames carry the
    byte in Q0 and the B frames are all-zero. So 3 bytes → 6 payload frames,
    pattern [byte, 0, byte, 0, byte, 0]."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    seq = [0x11, 0x22, 0x33]
    await QspiDriver(dut, cpol=0, cpha=0).frame([{0: v} for v in seq])
    await ClockCycles(dut.CLK, 80)
    pays = mon.payloads()
    assert len(pays) == 2 * len(seq), (
        f"expected {2*len(seq)} payload frames (A,B per byte), got {len(pays)}: "
        f"{[hex(p['raw']) for p in pays]}")
    a_los = [p["lo"] for p in pays[0::2]]   # payloadA = Q0
    b_los = [p["lo"] for p in pays[1::2]]   # payloadB = Q2 (undriven → 0)
    assert a_los == seq, f"multibyte A/Q0 sequence got {[hex(x) for x in a_los]} != {[hex(x) for x in seq]}"
    assert b_los == [0, 0, 0], f"multibyte B/Q2 should be all-zero, got {[hex(x) for x in b_los]}"
    dut._log.info(f"MULTIBYTE OK A={[hex(x) for x in a_los]} B={[hex(x) for x in b_los]}")


# ───────────────────────────────────────────────────────────────────────────
# Framing events (CS down / CS up)
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-11")
async def test_cs_framing_events(dut):
    """QSPI-REQ-11: a complete byte transfer emits a CS-down event (plain 0x09
    or new-polarity variant) and a CS-up event (0x0A) around the payload."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: 0x5A})
    await ClockCycles(dut.CLK, 60)
    codes = mon.event_codes()
    assert any(c in (EV_CS_DOWN, EV_CS_DOWN_NEW_POL_H, EV_CS_DOWN_NEW_POL_L) for c in codes), (
        f"no CS-down event in {[hex(c) for c in codes]}")
    assert EV_CS_UP in codes, f"no CS-up(0x0A) event in {[hex(c) for c in codes]}"
    dut._log.info(f"FRAMING events OK {[hex(c) for c in codes]}")


# ───────────────────────────────────────────────────────────────────────────
# Idle bus — adversarial: SCLK toggling while CS HIGH must capture nothing
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-100")
async def test_idle_bus_cs_high_no_payload(dut):
    """QSPI-REQ-100: with CS held HIGH, SCLK toggling must NOT be sampled — the
    edge detectors are CE-gated by (not CSn). Zero payload frames."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    drv = QspiDriver(dut, cpol=0, cpha=0)
    # CS stays high (deasserted); just toggle SCLK + data many times
    for _ in range(20):
        drv._present({0: 1, 1: 1, 2: 1, 3: 1}, 0)
        drv._set(P_SCLK, 1)
        drv._flush()
        await Timer(SCLK_HALF_NS, unit="ns")
        drv._set(P_SCLK, 0)
        drv._flush()
        await Timer(SCLK_HALF_NS, unit="ns")
    await ClockCycles(dut.CLK, 40)
    assert not mon.payloads(), (
        f"CS-high bus produced {len(mon.payloads())} payloads: "
        f"{[hex(p['raw']) for p in mon.payloads()]}")
    dut._log.info("IDLE-BUS no payload OK")


# ───────────────────────────────────────────────────────────────────────────
# Backpressure — QSPI_MSG_RDY held low during a transfer, then released
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-10")
async def test_backpressure_holds_payload(dut):
    """QSPI-REQ-10 (backpressure): hold QSPI_MSG_RDY=0 across a single-byte
    transfer; once RDY is raised, the held payload must still deliver 0x7E
    intact (the seq FSM gates msgVld on RDY)."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    dut.QSPI_MSG_RDY.value = 0
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: 0x7E})
    await ClockCycles(dut.CLK, 30)
    # nothing should have been validated while RDY low (events also gated)
    held = mon.payloads()
    dut.QSPI_MSG_RDY.value = 1
    await ClockCycles(dut.CLK, 40)
    pays = mon.payloads()
    assert pays, "no payload delivered after RDY raised — backpressured payload lost"
    assert any(p["lo"] == 0x7E for p in pays), (
        f"held payload corrupted: Q0 bytes seen {[hex(p['lo']) for p in pays]} (want 0x7E)")
    dut._log.info(f"BACKPRESSURE OK (held-while-low={len(held)}) delivered {[hex(p['lo']) for p in pays]}")


# ───────────────────────────────────────────────────────────────────────────
# Partial byte → CS up before 8 bits → CS_UP_INCOMPLETE + padded payload
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-40")
async def test_partial_byte_padding(dut):
    """QSPI-REQ-40: CS rises after only 5 bits. The RTL must (a) emit the
    CS_UP_INCOMPLETE event (0x0B) and (b) emit a payload with the 5 captured
    bits LEFT-aligned (MSB-justified) + zero-padded — that is the documented
    intent of the pad shift register (it shifts zeros in from the LSB after the
    captured bits, which left-justifies them).

    Drive bits 1,0,1,1,0 (MSB-first) → value 0b10110 in bits[4:0] = 0x16.
    Left-aligned padded byte = 0b10110000 = 0xB0.

    Regression for DS-T2.37 (FIXED): the dead qspiSrQxPad_r register was removed
    and the pad path now left-justifies qspiSrQx_r itself (exactly 8-N shifts
    during PAD_PACKET). Before the fix this emitted the right-aligned 0x16.
    """
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    five_bits = 0b10110              # sent MSB-first over 5 SCLK edges
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: five_bits}, n_bits=5)
    await ClockCycles(dut.CLK, 60)
    codes = mon.event_codes()
    assert EV_CS_UP_INCOMPLETE in codes, (
        f"no CS_UP_INCOMPLETE(0x0B) event for a 5-bit transfer; events={[hex(c) for c in codes]}")
    pays = mon.payloads()
    assert pays, f"no payload emitted for padded partial byte; frames={[hex(f['raw']) for f in mon.frames]}"
    expected = (five_bits << 3) & 0xFF      # left-justified: 0xB0
    assert pays[0]["lo"] == expected, (
        f"padded partial byte mis-aligned: got Q0=0x{pays[0]['lo']:02x}, "
        f"expected left-justified 0x{expected:02x} (raw=0x{pays[0]['raw']:08x})")
    dut._log.info(f"PARTIAL-PAD OK raw=0x{pays[0]['raw']:08x}")


# ───────────────────────────────────────────────────────────────────────────
# Half-duplex (3-wire): config(2)=1 → payload A only, B suppressed
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-10")
async def test_half_duplex_no_b_payload(dut):
    """QSPI-REQ-10 (3-wire): with halfDuplexMode (config bit 2) set, each byte
    must emit ONLY payloadA — payloadB is suppressed (qspi_datalogger.vhd:493).
    Drive one byte 0x5A on IO0 → exactly ONE payload frame, lo=0x5A."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0, half_duplex=1)
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: 0x5A})
    await ClockCycles(dut.CLK, 60)
    pays = mon.payloads()
    assert len(pays) == 1, (
        f"half-duplex must emit ONE payload (A only), got {len(pays)}: "
        f"{[hex(p['raw']) for p in pays]}")
    assert pays[0]["lo"] == 0x5A, f"half-duplex Q0 got 0x{pays[0]['lo']:02x} != 0x5A"
    dut._log.info(f"HALF-DUPLEX OK raw=0x{pays[0]['raw']:08x}")


# ───────────────────────────────────────────────────────────────────────────
# CPOL change between two CS frames → auto-detect re-latch + NEW_POL event
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-14")
async def test_cpol_change_between_frames(dut):
    """QSPI-REQ-14: two transfers, first MODE 0 (idle-low SCLK), second MODE 2
    (idle-high SCLK). The CPOL auto-detect must re-latch on each CS-fall and
    decode BOTH bytes correctly, and the second CS-down must report the
    new-polarity-high event (0x10)."""
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    # frame 1: CPOL=0
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: 0xA1})
    await ClockCycles(dut.CLK, 30)
    # frame 2: CPOL=1 (idle high) — new polarity
    await QspiDriver(dut, cpol=1, cpha=0).transfer({0: 0xB2})
    await ClockCycles(dut.CLK, 40)
    pays = mon.payloads()
    los = [p["lo"] for p in pays]
    assert 0xA1 in los, f"frame-1 CPOL=0 byte 0xA1 not captured; got {[hex(x) for x in los]}"
    assert 0xB2 in los, f"frame-2 CPOL=1 byte 0xB2 not captured; got {[hex(x) for x in los]}"
    codes = mon.event_codes()
    assert EV_CS_DOWN_NEW_POL_H in codes, (
        f"CPOL flip to high did not emit NEW_POL_H(0x10); events={[hex(c) for c in codes]}")
    dut._log.info(f"CPOL-CHANGE OK los={[hex(x) for x in los]} events={[hex(c) for c in codes]}")


# ───────────────────────────────────────────────────────────────────────────
# No-chip-select (2-wire) mode — CSn tied low, clock+data only
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-10")
async def test_no_cs_2wire_capture(dut):
    """QSPI-REQ-10 (2-wire): noChipSelectMode (config bit 3) advertises a CS-less
    clock+data capture. With CSn tied LOW the whole time (no CS wire), the
    datalogger must still capture bytes from the SCLK/data stream. Drive 0x6C on
    IO0 over 8 SCLK edges → expect a payload with Q0=0x6C.

    Regression for DS-T2.38 (FIXED): IDLE now self-starts to SAMPLING when
    noChipSelectMode='1' (no CS edge to frame on). Before the fix the FSM stayed
    in IDLE forever and captured nothing but the START event.
    """
    # custom bring-up: CSn held LOW (bit1=0) from reset → no CS edge ever
    cocotb.start_soon(Clock(dut.CLK, CLK_PERIOD_NS, units="ns").start())
    dut.RST.value = 1
    dut.QSPI_PINS.value = 0          # SCLK=0, CSn=0 (tied low), data=0
    dut.QSPI_MSG_RDY.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    await ClockCycles(dut.CLK, 8)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 4)
    mon = FrameMon(dut)
    cocotb.start_soon(mon.run())
    await _enable(dut, mon, no_cs=1)

    # drive 8 SCLK edges with CSn held low (mode-0 style), byte 0x6C on IO0
    byte = 0x6C
    pins = 0                          # CSn=0 throughout
    for bit_pos in range(7, -1, -1):
        bit = (byte >> bit_pos) & 1
        pins = (pins & ~(1 << P_IO[0])) | (bit << P_IO[0])
        pins &= ~(1 << P_SCLK)        # data set before rising edge
        dut.QSPI_PINS.value = pins
        await Timer(SCLK_HALF_NS, unit="ns")
        pins |= (1 << P_SCLK)         # rising sampling edge
        dut.QSPI_PINS.value = pins
        await Timer(SCLK_HALF_NS, unit="ns")
    await ClockCycles(dut.CLK, 40)

    pays = mon.payloads()
    assert pays, (
        "2-wire (no-CS) mode captured NOTHING — the FSM never left IDLE because "
        "no CS falling edge occurred. noChipSelectMode does not provide a "
        "CS-less start path. frames=" + str([hex(f["raw"]) for f in mon.frames]))
    assert pays[0]["lo"] == byte, (
        f"no-CS Q0 got 0x{pays[0]['lo']:02x} != 0x{byte:02x} (raw=0x{pays[0]['raw']:08x})")
    dut._log.info(f"NO-CS 2-wire OK raw=0x{pays[0]['raw']:08x}")


# ───────────────────────────────────────────────────────────────────────────
# Device-id filter — only the addressed instance applies CONFIG
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-31")
async def test_device_id_filter(dut):
    """QSPI-REQ-31: CONFIG[15:11] is a device-id filter — only the instance whose
    id matches G_QSPI_DEV_ID (28) applies the config. A config word addressed to
    a DIFFERENT id must be ignored (no START, no enable); the matching id then
    enables normally."""
    mon = await _bringup(dut)
    # wrong device id (5 != 28) — must NOT enable
    dut.CONFIG.value = _cfg_word(5, _enable_payload())
    for _ in range(150):
        await RisingEdge(dut.CLK)
    assert EV_START not in mon.event_codes(), (
        f"datalogger enabled on a config addressed to id=5 (its id is {DEV_ID}); "
        f"events={[hex(c) for c in mon.event_codes()]}")
    # correct id — must enable
    await _enable(dut, mon)
    assert EV_START in mon.event_codes(), "correct-id config did not enable the datalogger"
    dut._log.info("DEVICE-ID filter OK (id=5 ignored, id=28 enabled)")


# ───────────────────────────────────────────────────────────────────────────
# SCLK-stuck timeout (DS-P3.92) — now sim-reachable via scaled G_CLK_FREQ_HZ
# ───────────────────────────────────────────────────────────────────────────
@cocotb.test()
@requires("QSPI-REQ-40")
async def test_sclk_stall_timeout(dut):
    """QSPI-REQ-40 (timeout): assert CS, drive a few bits, then FREEZE SCLK with
    CS held low. After the (scaled) stuck-SCLK window the datalogger must emit the
    EV_TIMEOUT(0x04) event and return to IDLE — proven by a subsequent clean
    transfer capturing correctly.

    The timeout window is C_TIMEOUT_CNT = G_CLK_FREQ_HZ/1000 cycles (DS-P3.92);
    the old hard-coded 1-second (1e8-cycle) constant made this path unreachable
    in sim by construction. Healthy traffic never trips it (the counter resets on
    every sclkRisingEdge); only a genuine stall fires it.
    """
    mon = await _bringup(dut)
    await _enable(dut, mon, cpha=0)
    drv = QspiDriver(dut, cpol=0, cpha=0)

    # assert CS + drive 3 bits, then leave CS LOW with SCLK frozen (no _deassert)
    await drv._assert_cs()
    await drv._one_byte({0: 0b101}, n_bits=3)   # SCLK ends idle-low, CS still low
    n_before = len(mon.events())

    # stall well past the timeout window
    await ClockCycles(dut.CLK, WATCHDOG_CYCLES + 60)
    codes = [e["code"] for e in mon.events()[n_before:]]
    assert EV_TIMEOUT in codes, (
        f"SCLK stall did not emit EV_TIMEOUT(0x04) within {WATCHDOG_CYCLES}+60 "
        f"cycles; post-stall events={[hex(c) for c in codes]}")

    # FSM must be back in IDLE — a fresh clean transfer captures normally
    await drv._deassert_cs()
    await ClockCycles(dut.CLK, 10)
    n_pay = len(mon.payloads())
    await QspiDriver(dut, cpol=0, cpha=0).transfer({0: 0x42})
    await ClockCycles(dut.CLK, 50)
    fresh = mon.payloads()[n_pay:]
    assert any(p["lo"] == 0x42 for p in fresh), (
        f"after timeout the datalogger did not recover — post-timeout transfer "
        f"captured {[hex(p['lo']) for p in fresh]} (want 0x42)")
    dut._log.info(f"SCLK-STALL timeout OK (fired + recovered) codes={[hex(c) for c in codes]}")


def main():
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH

    run_simulation(
        top_level="qspi_datalogger",
        module="test_qspi_capture",
        custom_libraries={
            "tich": [
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{SDK_SRC_PATH}/units/infilter.vhd",
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/qspi/qspi_datalogger.vhd",
            ]
        },
        generics=GENERICS,
        waves=True,
    )


if __name__ == "__main__":
    main()
