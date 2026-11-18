"""can_frame_fsm CAN-FD fork test (DS-P2.64 step 4, CAN-REQ-12/13/15/16/18/22).

Isolates the FD FRAME WALK exactly as test_can_frame_fsm isolates the classic walk:
drives the DE-STUFFED FD field-bit stream directly (+ the raw SOF strobe), and
drives CRC17/21_COMPUTED directly — so this tests the WALK + field capture + the
crc_error COMPARE, NOT the CRC math/coverage (that needs a golden FD frame via
DS-P3.15, see the can_frame_fsm header). Every expected message is a hard-coded
literal (ROUTERTL-002); the FSM never decides what's correct.

Covers:
  CAN-REQ-12  FDF detection — recessive FDF forks to the FD chain; a mixed classic
              stream still decodes classic (the classic tests prove FDF=0 path)
  CAN-REQ-13  BRS — RATE_SEL is driven high in the FD data phase iff BRS=recessive
  CAN-REQ-15  ESI captured into the fd_control flags
  CAN-REQ-16  non-linear DLC 0..64 table (3, 16, 20, 64 byte cases)
  CAN-REQ-18  stuff-count gray + EVEN parity → fd_aux; a parity mismatch raises
              stuff_count_error (0x45)
  CAN-REQ-19/20  CRC-17 (<=16 B) vs CRC-21 (>16 B) selection + the crc_error compare
  CAN-REQ-22  STUFF_MODE high in the stuff-count + CRC fields (fixed stuffing)
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

DS_ROOT = Path(__file__).resolve().parents[6]
RTL = DS_ROOT / "hw" / "rtl"

DEV_ID = 20
T_ID, T_DATA, T_CTRL, T_EVENT, T_CRC, T_FDCTRL, T_FDAUX = 1, 2, 3, 4, 5, 6, 7
EV_ENABLED = 0x03
EV_FRAME_BEGIN, EV_FRAME_END = 0x10, 0x20
EV_ACK_OBS = 0x30
EV_CRC_ERR = 0x42
EV_STUFFCNT_ERR = 0x45

DS_GAP = 8


def mtype(m):   return (m >> 24) & 0x7
def devid(m):   return (m >> 27) & 0x1F
def payload(m): return m & 0xFF


def bits_msb(value, width):
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


async def ds_bit(dut, b):
    dut.DS_BIT.value = b
    dut.DS_VALID.value = 1
    await RisingEdge(dut.CLK)
    dut.DS_VALID.value = 0
    await ClockCycles(dut.CLK, DS_GAP)


async def raw_sof(dut):
    dut.BIT_VALUE.value = 0
    dut.BIT_VALID.value = 1
    await RisingEdge(dut.CLK)
    dut.BIT_VALID.value = 0
    dut.BIT_VALUE.value = 1
    await ClockCycles(dut.CLK, 3)


async def reset(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.RST.value = 1
    dut.ENABLE.value = 0
    dut.BIT_VALID.value = 0
    dut.BIT_VALUE.value = 1
    dut.DS_VALID.value = 0
    dut.DS_BIT.value = 1
    dut.STUFF_ERROR.value = 0
    dut.FORM_ERROR.value = 0
    dut.CAN_MSG_RDY.value = 1
    dut.TIMESTAMP.value = 0
    dut.CRC_COMPUTED.value = 0
    dut.CRC17_COMPUTED.value = 0
    dut.CRC21_COMPUTED.value = 0
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


def build_fd_ds(ide, base_id, ext_id, brs, esi, dlc, data_bytes, stuffcnt4, wire_crc, crc_is21):
    """Construct the de-stuffed FD field-bit stream (stimulus)."""
    ds = [0]                                                  # SOF echo (swallowed)
    if ide == 0:
        ds += bits_msb(base_id, 11) + [0] + [0] + [1]        # ID, RRS=0, IDE=0, FDF=1 (S_R0)
    else:
        ds += (bits_msb(base_id, 11) + [1] + [1]             # ID, SRR=1, IDE=1
               + bits_msb(ext_id, 18) + [0] + [1])           # extID, RRS=0, FDF=1 (S_R1)
    ds += [0] + [brs] + [esi]                                # res=0, BRS, ESI
    ds += bits_msb(dlc, 4)                                   # FD DLC
    for byte in data_bytes:
        ds += bits_msb(byte, 8)
    ds += list(stuffcnt4)                                    # 3 gray + parity
    ds += bits_msb(wire_crc, 21 if crc_is21 else 17)         # FD CRC
    ds += [1] + [0] + [1]                                    # CRC-delim, ACK(dom), ACK-delim
    ds += [1, 1, 1, 1, 1, 1, 1]                              # EOF
    return ds


def build_fd_expected(ide, base_id, ext_id, brs, esi, dlc, data_bytes, stuffcnt4,
                      wire_crc, crc_is21, crc_mismatch):
    exp = [(T_EVENT, EV_ENABLED), (T_EVENT, EV_FRAME_BEGIN)]
    fd_ctrl = (ide << 7) | (brs << 6) | (esi << 5) | (dlc & 0xF)
    exp.append((T_FDCTRL, fd_ctrl))
    if ide == 0:
        exp.append((T_ID, (base_id >> 8) & 0x7))
        exp.append((T_ID, base_id & 0xFF))
    else:
        full = (base_id << 18) | ext_id
        exp.append((T_ID, (full >> 24) & 0x1F))
        exp.append((T_ID, (full >> 16) & 0xFF))
        exp.append((T_ID, (full >> 8) & 0xFF))
        exp.append((T_ID, full & 0xFF))
    for byte in data_bytes:
        exp.append((T_DATA, byte))
    b0, b1, b2, b3 = stuffcnt4
    par_ok = (b0 ^ b1 ^ b2 ^ b3) == 0
    gray3 = (b0 << 2) | (b1 << 1) | b2
    exp.append((T_FDAUX, ((1 if par_ok else 0) << 7) | gray3))
    if not par_ok:
        exp.append((T_EVENT, EV_STUFFCNT_ERR))
    if crc_is21:
        exp.append((T_CRC, (wire_crc >> 16) & 0x1F))
        exp.append((T_CRC, (wire_crc >> 8) & 0xFF))
        exp.append((T_CRC, wire_crc & 0xFF))
    else:
        exp.append((T_CRC, (wire_crc >> 16) & 0x1))
        exp.append((T_CRC, (wire_crc >> 8) & 0xFF))
        exp.append((T_CRC, wire_crc & 0xFF))
    if crc_mismatch:
        exp.append((T_EVENT, EV_CRC_ERR))
    exp.append((T_EVENT, EV_ACK_OBS))
    exp.append((T_EVENT, EV_FRAME_END))
    return exp


async def run_fd_frame(dut, ds_stream, crc17=0, crc21=0):
    """Drive the FD frame; capture messages + whether RATE_SEL/STUFF_MODE pulsed."""
    got = []
    seen = {"rate": 0, "stuff_mode": 0}

    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.RATE_SEL.value) == 1:
                seen["rate"] = 1
            if int(dut.STUFF_MODE.value) == 1:
                seen["stuff_mode"] = 1
            if int(dut.CAN_MSG_VLD.value) == 1 and int(dut.CAN_MSG_RDY.value) == 1:
                m = int(dut.CAN_MSG_OUT.value)
                got.append(m)
                assert devid(m) == DEV_ID, f"misrouted dev={devid(m)} m={m:#010x}"
    cocotb.start_soon(collect())

    dut.CRC17_COMPUTED.value = crc17
    dut.CRC21_COMPUTED.value = crc21
    dut.ENABLE.value = 1
    await ClockCycles(dut.CLK, 6)
    await raw_sof(dut)
    for b in ds_stream:
        await ds_bit(dut, b)
    await ClockCycles(dut.CLK, 10)
    return [(mtype(m), payload(m)) for m in got], seen


def _diff(got, exp):
    return ("FD frame decode mismatch:\n"
            f"  got     : {[(t, hex(p)) for t, p in got]}\n"
            f"  expected: {[(t, hex(p)) for t, p in exp]}")


@cocotb.test()
async def fd_standard_brs_crc17(dut):
    """Standard FD, BRS=1, ESI=0, DLC=3 (3 B → CRC-17), parity OK. RATE_SEL+STUFF_MODE fire."""
    await reset(dut)
    args = dict(ide=0, base_id=0x123, ext_id=0, brs=1, esi=0, dlc=0x3,
                data_bytes=[0xDE, 0xAD, 0xBE][:3], stuffcnt4=[0, 0, 0, 0],
                wire_crc=0x1ABCD, crc_is21=False)
    ds = build_fd_ds(**args)
    exp = build_fd_expected(**args, crc_mismatch=False)
    got, seen = await run_fd_frame(dut, ds, crc17=0x1ABCD)     # computed==captured → no crc_error
    assert got == exp, _diff(got, exp)
    assert seen["rate"] == 1, "RATE_SEL never asserted for a BRS=1 FD frame (CAN-REQ-13)"
    assert seen["stuff_mode"] == 1, "STUFF_MODE never asserted in FD stuff-count/CRC (CAN-REQ-22)"
    dut._log.info(f"PASS fd_standard — DLC3 CRC17 BRS=1: {len(got)} msgs byte-exact, RATE_SEL+STUFF_MODE OK")


@cocotb.test()
async def fd_extended_no_brs(dut):
    """Extended FD (29-bit), BRS=0 → RATE_SEL must STAY LOW; STUFF_MODE still fires."""
    await reset(dut)
    args = dict(ide=1, base_id=(0x12345678 >> 18) & 0x7FF, ext_id=0x12345678 & 0x3FFFF,
                brs=0, esi=1, dlc=0x2, data_bytes=[0x5A, 0xA5], stuffcnt4=[0, 0, 1, 1],
                wire_crc=0x0BEEF, crc_is21=False)
    ds = build_fd_ds(**args)
    exp = build_fd_expected(**args, crc_mismatch=False)
    got, seen = await run_fd_frame(dut, ds, crc17=0x0BEEF)
    assert got == exp, _diff(got, exp)
    assert seen["rate"] == 0, "RATE_SEL asserted on a BRS=0 frame — must stay nominal (CAN-REQ-13)"
    assert seen["stuff_mode"] == 1, "STUFF_MODE never asserted (CAN-REQ-22)"
    dut._log.info(f"PASS fd_extended — 29-bit, BRS=0 (RATE_SEL stayed low), ESI=1: {len(got)} msgs byte-exact")


@cocotb.test()
async def fd_64byte_crc21(dut):
    """DLC=0xF → 64 bytes (CAN-REQ-16 max), selects CRC-21 (CAN-REQ-20)."""
    await reset(dut)
    data = [(i * 7 + 1) & 0xFF for i in range(64)]
    args = dict(ide=0, base_id=0x456, ext_id=0, brs=1, esi=0, dlc=0xF,
                data_bytes=data, stuffcnt4=[0, 1, 1, 0], wire_crc=0x1F2A3C, crc_is21=True)
    ds = build_fd_ds(**args)
    exp = build_fd_expected(**args, crc_mismatch=False)
    got, seen = await run_fd_frame(dut, ds, crc21=0x1F2A3C)
    assert got == exp, _diff(got, exp)
    n_data = sum(1 for t, _ in got if t == T_DATA)
    assert n_data == 64, f"DLC=0xF must yield 64 data bytes, got {n_data} (CAN-REQ-16 table)"
    dut._log.info(f"PASS fd_64byte — DLC=0xF→64 B, CRC-21 selected: {len(got)} msgs byte-exact")


@cocotb.test()
async def fd_crc17_crc21_boundary(dut):
    """DLC=0xA (16 B) uses CRC-17; DLC=0xB (20 B) uses CRC-21 (CAN-REQ-19/20 boundary)."""
    await reset(dut)
    # 16 bytes → CRC-17
    a16 = dict(ide=0, base_id=0x010, ext_id=0, brs=0, esi=0, dlc=0xA,
               data_bytes=[i & 0xFF for i in range(16)], stuffcnt4=[0, 0, 0, 0],
               wire_crc=0x10001, crc_is21=False)
    got, _ = await run_fd_frame(dut, build_fd_ds(**a16), crc17=0x10001)
    assert got == build_fd_expected(**a16, crc_mismatch=False), "16 B frame not CRC-17"
    # 20 bytes → CRC-21
    await reset(dut)
    a20 = dict(ide=0, base_id=0x011, ext_id=0, brs=0, esi=0, dlc=0xB,
               data_bytes=[(i + 1) & 0xFF for i in range(20)], stuffcnt4=[0, 0, 0, 0],
               wire_crc=0x100002, crc_is21=True)
    got, _ = await run_fd_frame(dut, build_fd_ds(**a20), crc21=0x100002)
    assert got == build_fd_expected(**a20, crc_mismatch=False), "20 B frame not CRC-21"
    dut._log.info("PASS fd_boundary — 16 B→CRC-17, 20 B→CRC-21 selection exact")


@cocotb.test()
async def fd_stuffcount_parity_error(dut):
    """ADVERSARIAL: a stuff-count with WRONG parity raises stuff_count_error (CAN-REQ-18)."""
    await reset(dut)
    # bits [0,0,0,1]: XOR=1 → ODD → parity violation
    args = dict(ide=0, base_id=0x123, ext_id=0, brs=0, esi=0, dlc=0x1,
                data_bytes=[0x77], stuffcnt4=[0, 0, 0, 1], wire_crc=0x12345, crc_is21=False)
    ds = build_fd_ds(**args)
    exp = build_fd_expected(**args, crc_mismatch=False)
    got, _ = await run_fd_frame(dut, ds, crc17=0x12345)
    assert got == exp, _diff(got, exp)
    assert (T_EVENT, EV_STUFFCNT_ERR) in got, f"bad parity did not raise stuff_count_error: {got}"
    dut._log.info("PASS fd_stuffcount_parity — odd parity raised stuff_count_error (0x45)")


@cocotb.test()
async def fd_crc_mismatch_raises_crc_error(dut):
    """The crc_error COMPARE: computed != captured → crc_error (CAN-REQ-19, compare only)."""
    await reset(dut)
    args = dict(ide=0, base_id=0x123, ext_id=0, brs=1, esi=0, dlc=0x2,
                data_bytes=[0xAB, 0xCD], stuffcnt4=[0, 0, 0, 0], wire_crc=0x1ABCD, crc_is21=False)
    ds = build_fd_ds(**args)
    exp = build_fd_expected(**args, crc_mismatch=True)
    got, _ = await run_fd_frame(dut, ds, crc17=0x05555)        # != captured 0x1ABCD → crc_error
    assert got == exp, _diff(got, exp)
    assert (T_EVENT, EV_CRC_ERR) in got, f"CRC mismatch did not raise crc_error: {got}"
    dut._log.info("PASS fd_crc_mismatch — computed 0x05555 vs wire 0x1ABCD raised crc_error (0x42)")


def main():
    run_simulation(
        top_level="can_frame_fsm",
        module="test_can_frame_fsm_fd",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [
            str(RTL / "pkg" / "datalogger_config_pkg.vhd"),
            str(RTL / "pkg" / "datalogger_pkg.vhd"),
            str(RTL / "datalogger" / "can" / "can_frame_fsm.vhd"),
        ]},
    )


if __name__ == "__main__":
    main()
