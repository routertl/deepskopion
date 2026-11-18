"""DS-P3.99 — protocol-level JTAG traffic through the full datalogger_gather.

A REAL IEEE-1149.1 TAP sequence (cocotb TapMaster driving TCK/TMS/TDI/TDO) is
driven onto the gather's JTAG0 datalogger pins (TCK=24, TMS=25, TDI=26, TDO=27);
every other pin idles high so only the CONFIG-enabled JTAG0 instance emits. This
is the LAST member of the SGA-REQ-3 BFM-harness family (I2C/UART/SPI/EDGE → JTAG)
— a real DR shift, not synthetic edges.

The jtag_datalogger is a PASSIVE TAP observer: it mirrors the 16-state TAP FSM and
captures the IR/DR shift streams (TDI host->device, TDO device->host) byte by
byte, LSB-first, bracketed by *_shift_begin / *_shift_end events. Verification =
the gather routes the DR-shift begin/end events + the exact TDI and TDO byte
sequences to DOUT_DATALOGGER_ID = 16, byte-exact (DS-T2.44 standard).

JTAG message (jtag_datalogger_protocol.yml): [31:27]=device_id, [26:24]=type
(0b001 TDI, 0b010 TDO, 0b100 event), [7:0]=byte. Events: dr_shift_begin=0x30,
dr_shift_end=0x40|valid_bits, enabled=0x03.

NB: TCK idles LOW — JTAG directly benefits from the DS-T2.45 glitch_filter fix
(track the line through reset), which is why no startup phantom appears on TCK.

bug-finding-is-success: a green bar means the gather routed a real TAP DR shift
byte-for-byte to the JTAG device id; any drop / mis-route / mangled byte is a
real DUT defect — file a T-ticket, do NOT weaken the assertion (ROUTERTL-002).
"""
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT / "hw" / "sim" / "cocotb"))   # ds_sim_sources
from ds_sim_sources import scoped_libraries   # noqa: E402

JTAG0_ID = 16          # C_JTAG0_ID
PERIOD_NS = 10
HALF = 4               # clk_sys cycles per TCK half-period (TCK ~ 11 MHz) — must be
                       # << clk_sys so the P1 oversampling front-end tracks it.

# Message types / event codes (jtag_datalogger_protocol.yml).
T_TDI, T_TDO, T_EVENT = 0b001, 0b010, 0b100
EV_ENABLED, EV_IR_BEGIN, EV_IR_END, EV_DR_BEGIN, EV_DR_END = 0x03, 0x10, 0x20, 0x30, 0x40

# Hard-coded shift streams (ROUTERTL-002). Repeated bytes (0xA5 in TDI, 0x34 in
# TDO) exercise multiplicity (DS-T2.44).
TDI_BYTES = [0xA5, 0x3C, 0xA5, 0x5A]
TDO_BYTES = [0x12, 0x34, 0x34, 0x78]


def mtype(m):   return (m >> 24) & 0x7
def payload(m): return m & 0xFF


def bits_lsb_first(byte):
    return [(byte >> i) & 1 for i in range(8)]


class TapMaster:
    """Drives JTAG_PINS = [0]TCK [1]TMS [2]TDI [3]TDO onto the gather tb. The
    datalogger oversamples on clk_sys, so each TCK phase is held HALF clk cycles.
    Also drives TDO (playing the device) so both lanes are checkable. (Mirrors
    the jtag_datalogger unit-test BFM — kept self-contained per the harness family
    pattern.)"""
    def __init__(self, dut):
        self.dut = dut
        self.tck = self.tms = self.tdi = self.tdo = 0

    def _drive(self):
        self.dut.JTAG_PINS.value = (self.tdo << 3) | (self.tdi << 2) | (self.tms << 1) | self.tck

    async def edge(self, tms, tdi=0, tdo=0):
        self.tms, self.tdi, self.tdo, self.tck = tms, tdi, tdo, 0
        self._drive()
        await ClockCycles(self.dut.CLK, HALF)
        self.tck = 1; self._drive()
        await ClockCycles(self.dut.CLK, HALF)
        self.tck = 0; self._drive()
        await ClockCycles(self.dut.CLK, 1)

    async def goto_reset(self):
        for _ in range(5):
            await self.edge(1)          # 5x TMS=1 -> TEST-LOGIC-RESET

    async def goto_idle(self):
        await self.edge(0)              # RESET -> RUN-TEST-IDLE

    async def _shift(self, select_ir, tdi_bits, tdo_bits):
        await self.edge(1)                          # SELECT-DR-SCAN
        if select_ir:
            await self.edge(1)                      # SELECT-IR-SCAN
        await self.edge(0)                          # CAPTURE
        await self.edge(0)                          # -> SHIFT (entry; *_shift_begin)
        n = len(tdi_bits)
        for i in range(n):
            last = (i == n - 1)
            await self.edge(1 if last else 0, tdi_bits[i], tdo_bits[i])
        await self.edge(1)                          # EXIT1 -> UPDATE
        await self.edge(0)                          # UPDATE -> RUN-TEST-IDLE

    async def shift_dr(self, tdi_bits, tdo_bits):
        await self._shift(False, tdi_bits, tdo_bits)

    async def shift_ir(self, tdi_bits, tdo_bits):
        await self._shift(True, tdi_bits, tdo_bits)


async def reset(dut, n=10):
    dut.RST.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.DOUT_REQ.value = 0
    dut.JTAG_PINS.value = 0
    await ClockCycles(dut.CLK, n)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


async def _ramp_timestamp(dut):
    t = 0
    while True:
        await RisingEdge(dut.CLK)
        t = (t + 1) & 0xFFFFFFFF
        dut.TIMESTAMP.value = t


class DoutCollector:
    def __init__(self, dut):
        self.dut = dut
        self.frames = []   # (datalogger_id, payload)
        self._run = True

    async def run(self):
        self.dut.DOUT_REQ.value = 1
        while self._run:
            await RisingEdge(self.dut.CLK)
            if int(self.dut.DOUT_VALID.value) == 1:
                self.frames.append((int(self.dut.DOUT_DATALOGGER_ID.value),
                                    int(self.dut.DOUT_PAYLOAD.value)))

    def stop(self):
        self._run = False


@cocotb.test()
@requires("SGA-REQ-3")
async def test_jtag_single_source_routing(dut):
    """SGA-REQ-3: a real TAP DR shift driven through the whole gather emerges on
    DOUT routed to the JTAG device_id (16), with the DR begin/end events and the
    exact TDI + TDO byte sequences recovered byte-exact."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset(dut)
    cocotb.start_soon(_ramp_timestamp(dut))

    coll = DoutCollector(dut)
    cocotb.start_soon(coll.run())

    # Enable the JTAG0 datalogger (device_id=16). config_control hardened (DS-P3.96).
    dut.CONFIG.value = (JTAG0_ID << 11) | 0x1
    await ClockCycles(dut.CLK, 16)
    dut.CONFIG.value = 0                 # idle (does not disable dev 16)
    await ClockCycles(dut.CLK, 8)

    tap = TapMaster(dut)
    await tap.goto_reset()
    await tap.goto_idle()

    tdi_bits = [b for byte in TDI_BYTES for b in bits_lsb_first(byte)]
    tdo_bits = [b for byte in TDO_BYTES for b in bits_lsb_first(byte)]
    await tap.shift_dr(tdi_bits, tdo_bits)

    await ClockCycles(dut.CLK, 64)
    coll.stop()

    assert coll.frames, "gather emitted NO DOUT frames for a real TAP DR shift"

    # Single-source routing: every frame must carry the JTAG0 device id.
    misrouted = sorted({sid for sid, _ in coll.frames if sid != JTAG0_ID})
    assert not misrouted, f"frames mis-routed to device_ids {misrouted} (expected {JTAG0_ID})"

    msgs = [pw for _, pw in coll.frames]
    events = [payload(m) for m in msgs if mtype(m) == T_EVENT]
    assert EV_DR_BEGIN in events, f"DR_BEGIN (0x30) not captured: events={[hex(e) for e in events]}"
    assert any(0x40 <= e <= 0x47 for e in events), (
        f"DR_END (0x40..0x47) not captured: events={[hex(e) for e in events]}")

    # Byte-EXACT routing (DS-T2.44): the captured TDI and TDO payload bytes must
    # EQUAL the driven sequences — exact count, order, multiplicity.
    got_tdi = [payload(m) for m in msgs if mtype(m) == T_TDI]
    got_tdo = [payload(m) for m in msgs if mtype(m) == T_TDO]
    assert got_tdi == TDI_BYTES, (
        f"JTAG TDI stream not byte-exact through the gather: got "
        f"{[hex(x) for x in got_tdi]}, expected {[hex(x) for x in TDI_BYTES]}")
    assert got_tdo == TDO_BYTES, (
        f"JTAG TDO stream not byte-exact through the gather: got "
        f"{[hex(x) for x in got_tdo]}, expected {[hex(x) for x in TDO_BYTES]}")

    dut._log.info(
        f"SGA-REQ-3 JTAG protocol routing PASS — DR shift: TDI {[hex(x) for x in got_tdi]} "
        f"+ TDO {[hex(x) for x in got_tdo]} byte-exact, begin/end events present, "
        f"all device_id={JTAG0_ID}")


@cocotb.test()
@requires("SGA-REQ-3")
async def test_jtag_ir_shift_routing(dut):
    """SGA-REQ-3 (IR path): a real Shift-IR through the whole gather routes the
    IR_BEGIN/IR_END events + the IR TDI byte to device_id 16, byte-exact. The IR
    register path is distinct from DR (SELECT-IR-SCAN branch of the TAP FSM) and
    is exercised here so the gather's JTAG routing isn't only proven on DR."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset(dut)
    cocotb.start_soon(_ramp_timestamp(dut))

    coll = DoutCollector(dut)
    cocotb.start_soon(coll.run())

    dut.CONFIG.value = (JTAG0_ID << 11) | 0x1
    await ClockCycles(dut.CLK, 16)
    dut.CONFIG.value = 0
    await ClockCycles(dut.CLK, 8)

    tap = TapMaster(dut)
    await tap.goto_reset()
    await tap.goto_idle()

    IR = [0xAB, 0xCD]      # two-byte IR (host->device); device TDO held 0
    tdi_bits = [b for byte in IR for b in bits_lsb_first(byte)]
    await tap.shift_ir(tdi_bits, [0] * len(tdi_bits))

    await ClockCycles(dut.CLK, 64)
    coll.stop()

    assert coll.frames, "gather emitted NO DOUT frames for a real TAP IR shift"
    misrouted = sorted({sid for sid, _ in coll.frames if sid != JTAG0_ID})
    assert not misrouted, f"frames mis-routed to device_ids {misrouted} (expected {JTAG0_ID})"

    msgs = [pw for _, pw in coll.frames]
    events = [payload(m) for m in msgs if mtype(m) == T_EVENT]
    assert EV_IR_BEGIN in events, f"IR_BEGIN (0x10) not captured: events={[hex(e) for e in events]}"
    assert any(0x20 <= e <= 0x27 for e in events), (
        f"IR_END (0x20..0x27) not captured: events={[hex(e) for e in events]}")

    got_tdi = [payload(m) for m in msgs if mtype(m) == T_TDI]
    assert got_tdi == IR, (
        f"JTAG IR TDI stream not byte-exact through the gather: got "
        f"{[hex(x) for x in got_tdi]}, expected {[hex(x) for x in IR]}")

    dut._log.info(
        f"SGA-REQ-3 JTAG IR-shift routing PASS — IR TDI {[hex(x) for x in got_tdi]} "
        f"byte-exact, IR begin/end events present, all device_id={JTAG0_ID}")


#: DS-P2.322: the DS-P1.104 default mix has no JTAG logger, so this bench compiles the test-only fixture
#: tools/sim/fixtures/gather_jtag (default mix + JTAG0 id 16 on pins 24-27; generated pair, frozen and held fresh
#: by tools/tests/test_gather_jtag_fixture_fresh_ds_p2_322.py) in place of the default config package and gather.
_DS_ROOT = Path(__file__).resolve().parents[7]
_FIXTURE = _DS_ROOT / "tools" / "sim" / "fixtures" / "gather_jtag"
_SWAP = {
    "hw/rtl/pkg/datalogger_config_pkg.vhd": _FIXTURE / "datalogger_config_pkg.vhd",
    "hw/rtl/datalogger/infra/datalogger_gather/datalogger_gather.vhd": _FIXTURE / "datalogger_gather.vhd",
}


def _fixture_libraries() -> dict:
    """The bench's resolved compile set with the fixture pair swapped in. The default gather instantiates no
    jtag_datalogger, so that entity's own resolved closure is added ahead of the fixture gather."""
    libs = scoped_libraries("tb_datalogger_gather_jtag")
    missing = sorted(set(_SWAP) - set(libs["tich"]))
    assert not missing, f"the default pair moved; update _SWAP: {missing}"
    gather = "hw/rtl/datalogger/infra/datalogger_gather/datalogger_gather.vhd"
    jtag = [f for f in scoped_libraries("jtag_datalogger")["tich"] if f not in libs["tich"]]
    tich = []
    for f in libs["tich"]:
        if f == gather:
            tich.extend(jtag)
        tich.append(str(_SWAP.get(f, f)))
    return {**libs, "tich": tich}


def main():
    run_simulation(
        top_level="tb_datalogger_gather_jtag",
        module=Path(__file__).stem,
        custom_libraries=_fixture_libraries(),
        work_lib="tich",
        # custom_libraries names every source; project-package injection would add the default
        # datalogger_config_pkg beside the fixture's and refuse the pair (RTL-P2.1785), as in the mix sweep.
        include_project_packages=False,
    )


if __name__ == "__main__":
    main()
