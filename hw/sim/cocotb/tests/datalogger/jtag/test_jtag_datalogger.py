"""Contract-first unit test for jtag_datalogger (DS-P3.56, JTAG P1).

A cocotb TAP-master BFM drives TCK/TMS/TDI/TDO (the datalogger watches them
PASSIVELY); we assert the emitted 32-bit zs_msg stream against the JTAG-REQ-N
contract in hw/rtl/datalogger/jtag/requirements.yml. Expected values are
HARD-CODED (ROUTERTL-002), never derived from DUT output at runtime.

Run:  rr sim run test_jtag_datalogger
"""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, ClockCycles

try:
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH
except ImportError:
    run_simulation = None
    SDK_SRC_PATH = ""

try:
    from sdk.cocotb_helpers import requires
except ImportError:
    def requires(*_a, **_k):
        def _d(f):
            return f
        return _d

_RTL = Path(__file__).resolve().parents[5] / "rtl"   # deepskopion/hw/rtl

PERIOD = 10           # ns, 100 MHz clk_sys
HALF   = 4            # clk_sys cycles per TCK half-period (TCK ~ 12.5 MHz)
DEV_ID = 16
CFG_EN = (DEV_ID << 11) | 1     # CONFIG: device_id[15:11]=16, enable[0]=1  -> 0x8001

# Message types / event codes (jtag_datalogger_protocol.yml)
T_TDI, T_TDO, T_TAP_STATE, T_EVENT = 0b001, 0b010, 0b011, 0b100
CFG_VERBOSE = (DEV_ID << 11) | 0b101   # enable[0] + verbose_tap[2] (DS-T3.16)

# TAP state codes (jtag_datalogger_protocol.yml tap_states / RTL StateType'pos)
TS_RTI, TS_SEL_DR, TS_CAP_DR, TS_SHIFT_DR = 0x1, 0x2, 0x3, 0x4
TS_EXIT1_DR, TS_UPDATE_DR = 0x5, 0x8
EV_DISABLED, EV_ENABLED, EV_TAP_RESET = 0x00, 0x03, 0x0C
EV_IR_BEGIN, EV_IR_END, EV_DR_BEGIN, EV_DR_END = 0x10, 0x20, 0x30, 0x40

GENERICS = {
    "G_JTAG_ID_WIDTH": 5, "G_JTAG_DEV_ID": DEV_ID, "G_JTAG_PINS": 4,
    "G_TIMESTAMP_WIDTH": 16, "G_CONFIG_WIDTH": 16, "G_ERROR_WIDTH": 2,
    "G_FIFO_DEPTH_LOG2": 4,
}


def mtype(m):  return (m >> 24) & 0x7
def devid(m):  return (m >> 27) & 0x1F
def payload(m): return m & 0xFF


class TapMaster:
    """Drives JTAG_PINS = [0]TCK [1]TMS [2]TDI [3]TDO. The datalogger oversamples
    on clk_sys, so each TCK is held HALF clk cycles per phase. We also drive TDO
    (playing the device) so both lanes are checkable."""
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
        # IDLE -> SELECT-DR (-> SELECT-IR) -> CAPTURE -> SHIFT
        await self.edge(1)                          # SELECT-DR-SCAN
        if select_ir:
            await self.edge(1)                      # SELECT-IR-SCAN
        await self.edge(0)                          # CAPTURE
        await self.edge(0)                          # -> SHIFT (entry; no bit, *_shift_begin)
        n = len(tdi_bits)
        for i in range(n):
            last = (i == n - 1)
            await self.edge(1 if last else 0, tdi_bits[i], tdo_bits[i])  # shift bit i
        await self.edge(1)                          # EXIT1 -> UPDATE
        await self.edge(0)                          # UPDATE -> RUN-TEST-IDLE

    async def shift_dr(self, tdi_bits, tdo_bits):
        await self._shift(False, tdi_bits, tdo_bits)

    async def shift_ir(self, tdi_bits, tdo_bits):
        await self._shift(True, tdi_bits, tdo_bits)


def bits_lsb_first(byte):
    return [(byte >> i) & 1 for i in range(8)]


def partial_payload(bits):
    # N<8 valid bits land in the TOP N byte positions (LSB-first right-shift:
    # newest bit at MSB). For N==8 this reduces to the original byte.
    n = len(bits)
    return sum((b & 1) << (8 - n + i) for i, b in enumerate(bits))


async def setup(dut, enable=True):
    cocotb.start_soon(Clock(dut.CLK, PERIOD, units="ns").start())
    dut.RST.value = 1
    dut.JTAG_PINS.value = 0
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.JTAG_MSG_RDY.value = 1          # perpetual consumer (max-throughput sink)
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 3)
    msgs = []

    async def _collect():
        while True:
            await FallingEdge(dut.CLK)
            try:
                if int(dut.JTAG_MSG_VLD.value) == 1 and int(dut.JTAG_MSG_RDY.value) == 1:
                    msgs.append(int(dut.JTAG_MSG_OUT.value))
            except ValueError:
                pass
    cocotb.start_soon(_collect())

    if enable:
        dut.CONFIG.value = CFG_EN
        await ClockCycles(dut.CLK, 6)   # let config_control latch the enable
        dut.CONFIG.value = 0            # idle (does not disable dev 16)
        await ClockCycles(dut.CLK, 6)
    return msgs


# ── JTAG-REQ-1: reset determinism ──────────────────────────────────────────
@cocotb.test()
@requires("JTAG-REQ-1")
async def reset_determinism(dut):
    msgs = await setup(dut, enable=False)
    # No enable, no TAP activity -> no messages, VLD low.
    await ClockCycles(dut.CLK, 30)
    assert int(dut.JTAG_MSG_VLD.value) == 0, "VLD asserted post-reset with no enable"
    assert not msgs, f"messages emitted before enable: {[hex(m) for m in msgs]}"
    dut._log.info("JTAG-REQ-1 PASS: silent + VLD low post-reset.")


# ── JTAG-REQ-2: enable lifecycle event + identity ──────────────────────────
@cocotb.test()
@requires("JTAG-REQ-2")
async def enable_event(dut):
    msgs = await setup(dut, enable=True)
    await ClockCycles(dut.CLK, 5)
    assert msgs, "no enabled event emitted"
    m = msgs[0]
    assert mtype(m) == T_EVENT and payload(m) == EV_ENABLED, \
        f"first msg 0x{m:08x} is not an ENABLED event"
    assert devid(m) == DEV_ID, f"device_id {devid(m)} != {DEV_ID}"
    dut._log.info(f"JTAG-REQ-2 PASS: ENABLED event, device_id={devid(m)}.")


# ── JTAG-REQ-3 / -6: DR shift -> begin, TDI byte, TDO byte, end ─────────────
@cocotb.test()
@requires("JTAG-REQ-3", "JTAG-REQ-6")
async def dr_shift_byte(dut):
    msgs = await setup(dut, enable=True)
    tap = TapMaster(dut)
    await tap.goto_reset(); await tap.goto_idle()
    msgs.clear()
    TDI, TDO = 0xA5, 0x3C
    await tap.shift_dr(bits_lsb_first(TDI), bits_lsb_first(TDO))
    await ClockCycles(dut.CLK, 20)
    # expect: DR_BEGIN, payload_tdi(0xA5), payload_tdo(0x3C), DR_END(full byte -> 0x40)
    evs = [(mtype(m), payload(m)) for m in msgs]
    assert (T_EVENT, EV_DR_BEGIN) in evs, f"missing dr_shift_begin: {[(t,hex(p)) for t,p in evs]}"
    assert (T_TDI, TDI) in evs, f"missing payload_tdi 0x{TDI:02x}: {[(t,hex(p)) for t,p in evs]}"
    assert (T_TDO, TDO) in evs, f"missing payload_tdo 0x{TDO:02x}: {[(t,hex(p)) for t,p in evs]}"
    assert (T_EVENT, EV_DR_END) in evs, f"missing dr_shift_end(0x40): {[(t,hex(p)) for t,p in evs]}"
    dut._log.info(f"JTAG-REQ-3/6 PASS: DR begin + TDI 0x{TDI:02x} + TDO 0x{TDO:02x} + end.")


# ── JTAG-REQ-4: IR shift -> ir_begin .. ir_end ─────────────────────────────
@cocotb.test()
@requires("JTAG-REQ-4")
async def ir_shift_byte(dut):
    msgs = await setup(dut, enable=True)
    tap = TapMaster(dut)
    await tap.goto_reset(); await tap.goto_idle()
    msgs.clear()
    IR = 0x5A
    await tap.shift_ir(bits_lsb_first(IR), [0] * 8)
    await ClockCycles(dut.CLK, 20)
    evs = [(mtype(m), payload(m)) for m in msgs]
    assert (T_EVENT, EV_IR_BEGIN) in evs, f"missing ir_shift_begin: {[(t,hex(p)) for t,p in evs]}"
    assert (T_TDI, IR) in evs, f"missing IR payload_tdi 0x{IR:02x}: {[(t,hex(p)) for t,p in evs]}"
    assert (T_EVENT, EV_IR_END) in evs, f"missing ir_shift_end(0x20): {[(t,hex(p)) for t,p in evs]}"
    dut._log.info(f"JTAG-REQ-4 PASS: IR begin + 0x{IR:02x} + end.")


# ── JTAG-REQ-7: 5x TMS=1 -> TEST-LOGIC-RESET -> tap_reset event ────────────
@cocotb.test()
@requires("JTAG-REQ-7")
async def tap_reset_event(dut):
    msgs = await setup(dut, enable=True)
    tap = TapMaster(dut)
    await tap.goto_reset(); await tap.goto_idle()   # leave reset first
    msgs.clear()
    await tap.goto_reset()                           # re-enter TEST-LOGIC-RESET
    await ClockCycles(dut.CLK, 20)
    evs = [(mtype(m), payload(m)) for m in msgs]
    assert (T_EVENT, EV_TAP_RESET) in evs, f"missing tap_reset(0x0C): {[(t,hex(p)) for t,p in evs]}"
    dut._log.info("JTAG-REQ-7 PASS: tap_reset event on TEST-LOGIC-RESET.")


# ── JTAG-REQ-8: disabled silence ───────────────────────────────────────────
@cocotb.test()
@requires("JTAG-REQ-8")
async def disabled_silence(dut):
    msgs = await setup(dut, enable=False)
    tap = TapMaster(dut)
    await tap.goto_reset(); await tap.goto_idle()
    await tap.shift_dr(bits_lsb_first(0xFF), [0] * 8)   # drive a real shift while DISABLED
    await ClockCycles(dut.CLK, 20)
    assert not msgs, f"datalogger emitted while disabled: {[hex(m) for m in msgs]}"
    dut._log.info("JTAG-REQ-8 PASS: no output while disabled.")


# ── JTAG-REQ-10: CAPTURE_RST reconfiguration — decode self-resets ─────────────
@cocotb.test()
@requires("JTAG-REQ-10")
async def capture_rst_reconfig(dut):
    """JTAG-REQ-10 (DS-P3.90 Phase 2): jtag's decode SELF-RESETS — all capture is
    gated on ConfigEnable and the TAP FSM resyncs from the bus — so it needs no
    separate config_control.CAPTURE_RST (which would risk suppressing its inline
    ENABLED/DISABLED lifecycle, computed from R.EnableQ in the same two-process
    block — H.96). A disable -> re-enable cleanly re-arms: ENABLED on enable,
    DISABLED on disable, ENABLED again on re-enable. Disable uses an explicit
    matching-id word (CONFIG=0 does NOT disable a non-zero device-id — see the
    setup() note and H.98). JTAG_MSG_OUT / JTAG_MSG_VLD are sampled by the
    setup() collector."""
    _jtag_msg_vld = dut.JTAG_MSG_VLD  # sampled by setup() collector
    _jtag_msg_out = dut.JTAG_MSG_OUT  # sampled by setup() collector
    disable_word = (DEV_ID << 11) | 0          # matching-id, enable bit = 0

    msgs = await setup(dut, enable=True)        # CONFIG=CFG_EN then 0 (idle, still enabled)
    await ClockCycles(dut.CLK, 5)
    assert msgs and mtype(msgs[0]) == T_EVENT and payload(msgs[0]) == EV_ENABLED, \
        f"no ENABLED on first enable: {[hex(m) for m in msgs]}"

    # Disable (explicit matching-id word) -> DISABLED on the falling edge.
    dut.CONFIG.value = disable_word
    await ClockCycles(dut.CLK, 12)

    # Re-enable -> ENABLED again (decode re-armed, lifecycle not suppressed).
    dut.CONFIG.value = CFG_EN
    await ClockCycles(dut.CLK, 12)
    dut.CONFIG.value = 0                         # idle (does not disable dev 16)
    await ClockCycles(dut.CLK, 12)

    codes = [payload(m) for m in msgs if mtype(m) == T_EVENT]
    assert EV_DISABLED in codes, \
        f"no DISABLED after explicit-id disable: {[hex(c) for c in codes]}"
    assert codes[-1] == EV_ENABLED, \
        f"re-enable did not emit ENABLED last: {[hex(c) for c in codes]}"
    dut._log.info("JTAG-REQ-10 PASS: ENABLED/DISABLED/ENABLED across reconfig.")


# ── JTAG-REQ-5: sub-byte final shift -> end nibble = valid-bit count ────────
@cocotb.test()
@requires("JTAG-REQ-5")
async def dr_shift_subbyte(dut):
    msgs = await setup(dut, enable=True)
    tap = TapMaster(dut)
    await tap.goto_reset(); await tap.goto_idle()
    msgs.clear()
    tdi5 = [1, 0, 1, 0, 1]          # 5 valid bits (not a byte multiple)
    tdo5 = [0, 0, 1, 1, 0]
    await tap.shift_dr(tdi5, tdo5)
    await ClockCycles(dut.CLK, 20)
    evs = [(mtype(m), payload(m)) for m in msgs]
    exp_end = EV_DR_END | len(tdi5)            # 0x40 | 5 = 0x45
    exp_tdi = partial_payload(tdi5)            # top-5 positions, newest at MSB
    exp_tdo = partial_payload(tdo5)
    assert (T_EVENT, exp_end) in evs, \
        f"sub-byte dr_end nibble != {len(tdi5)}: {[(t,hex(p)) for t,p in evs]}"
    assert (T_TDI, exp_tdi) in evs, \
        f"missing partial payload_tdi 0x{exp_tdi:02x}: {[(t,hex(p)) for t,p in evs]}"
    assert (T_TDO, exp_tdo) in evs, \
        f"missing partial payload_tdo 0x{exp_tdo:02x}: {[(t,hex(p)) for t,p in evs]}"
    dut._log.info(f"JTAG-REQ-5 PASS: sub-byte end=0x{exp_end:02x}, "
                  f"partial tdi=0x{exp_tdi:02x} tdo=0x{exp_tdo:02x}.")


# ── JTAG-REQ-9: FIFO overflow under backpressure -> counted, never silent ───
@cocotb.test()
@requires("JTAG-REQ-9")
async def fifo_overflow_counts(dut):
    await setup(dut, enable=True)
    tap = TapMaster(dut)
    await tap.goto_reset(); await tap.goto_idle()
    dut.JTAG_MSG_RDY.value = 0          # stall the consumer: FIFO must fill then overflow
    for _ in range(8):                  # 8 DR bytes ~ 32 msgs >> 16-deep capture FIFO
        await tap.shift_dr(bits_lsb_first(0x5A), bits_lsb_first(0xA5))
    await ClockCycles(dut.CLK, 20)
    errs = int(dut.JTAG_ERRORS.value)
    assert errs > 0, "FIFO overflowed under backpressure but JTAG_ERRORS stayed 0 (silent drop)"
    dut._log.info(f"JTAG-REQ-9 PASS: overflow counted, JTAG_ERRORS={errs} (no silent drop).")


async def _enable_with(dut, cfg_word):
    """Start the collector (enable=False) then latch a custom CONFIG word."""
    msgs = await setup(dut, enable=False)
    dut.CONFIG.value = cfg_word
    await ClockCycles(dut.CLK, 6)       # let config_control latch
    dut.CONFIG.value = 0                 # idle (does not disable dev 16)
    await ClockCycles(dut.CLK, 6)
    return msgs


# ── JTAG-REQ-11: verbose_tap emits a tap_state per FSM transition ───────────
@cocotb.test()
@requires("JTAG-REQ-11")
async def verbose_tap_trajectory(dut):
    """DS-T3.16: with verbose_tap (CONFIG[2]) set, a known TMS trajectory yields
    an EXACT tap_state(0b011) code sequence. goto_idle + one 8-bit DR shift walks
    RTI -> SELECT-DR -> CAPTURE-DR -> SHIFT-DR -> EXIT1-DR -> UPDATE-DR -> RTI."""
    msgs = await _enable_with(dut, CFG_VERBOSE)
    tap = TapMaster(dut)
    await tap.goto_reset()                       # stays in TLR (self-loops, no tap_state)
    await tap.goto_idle()                        # TLR -> RUN-TEST-IDLE
    await tap.shift_dr(bits_lsb_first(0x5A), bits_lsb_first(0xA5))
    await ClockCycles(dut.CLK, 20)

    states = [payload(m) & 0xF for m in msgs if mtype(m) == T_TAP_STATE]
    expected = [TS_RTI, TS_SEL_DR, TS_CAP_DR, TS_SHIFT_DR,
                TS_EXIT1_DR, TS_UPDATE_DR, TS_RTI]
    assert states == expected, (
        f"JTAG-REQ-11: tap_state trajectory {[hex(s) for s in states]} != "
        f"expected {[hex(s) for s in expected]}")
    # device_id must still tag every tap_state word
    bad = [hex(m) for m in msgs if mtype(m) == T_TAP_STATE and devid(m) != DEV_ID]
    assert not bad, f"JTAG-REQ-11: tap_state words mis-tagged device_id: {bad}"
    dut._log.info(f"JTAG-REQ-11 PASS: tap_state trajectory {[hex(s) for s in states]}.")


# ── JTAG-REQ-11 (negative): verbose_tap=0 emits NO tap_state ────────────────
@cocotb.test()
@requires("JTAG-REQ-11")
async def verbose_tap_off_no_state(dut):
    """DS-T3.16: with verbose_tap clear (plain enable), the SAME trajectory emits
    zero tap_state messages — the feature is strictly gated by CONFIG[2]."""
    msgs = await _enable_with(dut, CFG_EN)       # enable, verbose_tap = 0
    tap = TapMaster(dut)
    await tap.goto_reset()
    await tap.goto_idle()
    await tap.shift_dr(bits_lsb_first(0x5A), bits_lsb_first(0xA5))
    await ClockCycles(dut.CLK, 20)

    tap_states = [hex(m) for m in msgs if mtype(m) == T_TAP_STATE]
    assert not tap_states, (
        f"JTAG-REQ-11(neg): verbose_tap=0 must emit NO tap_state, got {tap_states}")
    # sanity: the shift still produced normal traffic (so 'no tap_state' isn't
    # vacuous from a dead datalogger)
    assert msgs, "verbose_tap_off: no messages at all — datalogger never emitted"
    dut._log.info(f"JTAG-REQ-11(neg) PASS: {len(msgs)} msgs, 0 tap_state with verbose off.")


def main():
    if run_simulation is None:
        raise RuntimeError("run via `rr sim run test_jtag_datalogger`")
    run_simulation(
        top_level="jtag_datalogger",
        module=Path(__file__).stem,
        custom_libraries={
            "tich": [
                f"{_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{_RTL}/pkg/datalogger_pkg.vhd",
                f"{_RTL}/datalogger/infra/config_control/config_control.vhd",
                f"{_RTL}/datalogger/jtag/jtag_datalogger.vhd",
            ],
        },
        generics=GENERICS,
        waves=True,
        simulator="nvc",
    )


if __name__ == "__main__":
    main()
