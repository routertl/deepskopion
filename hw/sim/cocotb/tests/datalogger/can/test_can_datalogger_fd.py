# rr unknown-llm (AI-generated)
"""can_datalogger CAN-FD full-chain integration test (DS-P2.64 step 5).

Drives a RAW, STUFFED CAN-FD frame through the FULL chain — can_bit_timing →
can_destuff (BOTH dynamic + fixed modes, switched live by the FSM's STUFF_MODE) →
can_frame_fsm FD fork + can_crc17/21 — exercising the integration the per-block FD
tests stubbed: real bit recovery, real dynamic stuffing through the data field,
the FSM-driven dynamic→fixed STUFF_MODE handshake in the stuff-count + CRC fields,
and the FD message emission end-to-end.

NOMINAL RATE ONLY (BRS=0). A BRS=1 dual-rate drive needs the data phase at the
fast bit time and is a silicon-stimulus concern (DS-P3.15, CTU CAN FD) — the
single-rate chain still proves FDF detection, the FD walk, both de-stuff modes,
stuff-count parity, and CRC-17/21 selection compose correctly.

HONEST CRC SCOPE: the FD CRC coverage (which stuffed bits feed the engine + the
leading-1 preset) is NOT yet anchored (needs a golden FD frame). So the BFM puts
an ARBITRARY CRC on the wire and this test EXPECTS a crc_error — validating the
whole FD chain EXCEPT the CRC verdict, never faking the verdict green. All other
fields are asserted byte-exact against hard-coded values (ROUTERTL-002).
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
sys.path.insert(0, str(DS_ROOT / "hw" / "sim" / "cocotb"))
from can_bfm import CanBusBFM, build_fd_frame   # noqa: E402

DEV_ID = 20
NUM_TQ = 16
PRESCALER = 4
BIT_TIME = NUM_TQ * PRESCALER

T_ID, T_DATA, T_CTRL, T_EVENT, T_CRC, T_FDCTRL, T_FDAUX = 1, 2, 3, 4, 5, 6, 7
EV_ENABLED = 0x03
EV_FRAME_BEGIN, EV_FRAME_END = 0x10, 0x20
EV_ACK_OBS = 0x30
EV_CRC_ERR = 0x42
EV_STUFFCNT_ERR = 0x45


def mtype(m):   return (m >> 24) & 0x7
def devid(m):   return (m >> 27) & 0x1F
def payload(m): return m & 0xFF


def make_config(enable, prescaler, dev_id=DEV_ID):
    return (dev_id << 11) | ((prescaler & 0x3FF) << 1) | (enable & 1)


def start_collector(dut, got):
    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.CAN_MSG_VLD.value) == 1 and int(dut.CAN_MSG_RDY.value) == 1:
                m = int(dut.CAN_MSG_OUT.value)
                got.append(m)
                assert devid(m) == DEV_ID, f"misrouted dev={devid(m)} m={m:#010x}"
    cocotb.start_soon(collect())


async def setup(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.RST.value = 1
    dut.CAN_PINS.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.CAN_MSG_RDY.value = 1
    await ClockCycles(dut.CLK, 6)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 2)
    dut.CONFIG.value = make_config(1, PRESCALER)
    await ClockCycles(dut.CLK, 4)


def frame_window(got):
    """Decode (type,payload) from frame_begin..frame_end inclusive (ignore lifecycle)."""
    seq = [(mtype(m), payload(m)) for m in got]
    begin = next((i for i, (t, p) in enumerate(seq) if t == T_EVENT and p == EV_FRAME_BEGIN), None)
    end = next((i for i, (t, p) in enumerate(seq) if t == T_EVENT and p == EV_FRAME_END), None)
    assert begin is not None and end is not None and end > begin, f"no frame window in {seq}"
    return seq[begin:end + 1]


def expect_fd(ide, base_id, ext_id, brs, esi, dlc, data, stuffcnt_gray, parity_ok,
              crc, crc_w):
    exp = [(T_EVENT, EV_FRAME_BEGIN)]
    exp.append((T_FDCTRL, (ide << 7) | (brs << 6) | (esi << 5) | (dlc & 0xF)))
    if ide == 0:
        exp += [(T_ID, (base_id >> 8) & 0x7), (T_ID, base_id & 0xFF)]
    else:
        full = (base_id << 18) | ext_id
        exp += [(T_ID, (full >> 24) & 0x1F), (T_ID, (full >> 16) & 0xFF),
                (T_ID, (full >> 8) & 0xFF), (T_ID, full & 0xFF)]
    exp += [(T_DATA, b) for b in data]
    exp.append((T_FDAUX, ((1 if parity_ok else 0) << 7) | (stuffcnt_gray & 0x7)))
    if not parity_ok:
        exp.append((T_EVENT, EV_STUFFCNT_ERR))
    if crc_w == 21:
        exp += [(T_CRC, (crc >> 16) & 0x1F), (T_CRC, (crc >> 8) & 0xFF), (T_CRC, crc & 0xFF)]
    else:
        exp += [(T_CRC, (crc >> 16) & 0x1), (T_CRC, (crc >> 8) & 0xFF), (T_CRC, crc & 0xFF)]
    exp.append((T_EVENT, EV_CRC_ERR))          # arbitrary wire CRC ≠ computed → EXPECTED
    exp.append((T_EVENT, EV_ACK_OBS))
    exp.append((T_EVENT, EV_FRAME_END))
    return exp


def _diff(got, exp):
    return ("FD full-chain mismatch:\n"
            f"  got     : {[(t, hex(p)) for t, p in got]}\n"
            f"  expected: {[(t, hex(p)) for t, p in exp]}")


@cocotb.test()
async def fd_standard_full_chain(dut):
    """Standard FD, DLC=2 (CRC-17), through real bit-timing/de-stuff/FSM."""
    await setup(dut)
    got = []
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=4)
    meta = await build_fd_frame_and_send(bfm, can_id=0x123, data=[0xAB, 0xCD],
                                         ide=False, brs=0, esi=0, stuff_count=0)
    await ClockCycles(dut.CLK, 20 * BIT_TIME)

    win = frame_window(got)
    exp = expect_fd(0, 0x123, 0, 0, 0, 2, [0xAB, 0xCD], stuffcnt_gray=0, parity_ok=True,
                    crc=meta["crc"], crc_w=meta["crc_w"])
    assert win == exp, _diff(win, exp)
    dut._log.info(f"PASS fd_standard_full_chain — {len(win)} msgs byte-exact through the "
                  f"real chain; crc_error EXPECTED (coverage deferred to DS-P3.15)")


@cocotb.test()
async def fd_extended_full_chain(dut):
    """Extended (29-bit) FD, DLC=3, ESI=1 — FDF fork at S_R1 path through the chain."""
    await setup(dut)
    got = []
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=4)
    cid = 0x12345678
    meta = await build_fd_frame_and_send(bfm, can_id=cid, data=[0x11, 0x22, 0x33],
                                         ide=True, brs=0, esi=1, stuff_count=0)
    await ClockCycles(dut.CLK, 20 * BIT_TIME)

    win = frame_window(got)
    exp = expect_fd(1, (cid >> 18) & 0x7FF, cid & 0x3FFFF, 0, 1, 3,
                    [0x11, 0x22, 0x33], stuffcnt_gray=0, parity_ok=True,
                    crc=meta["crc"], crc_w=meta["crc_w"])
    assert win == exp, _diff(win, exp)
    dut._log.info(f"PASS fd_extended_full_chain — 29-bit ESI=1, {len(win)} msgs byte-exact")


@cocotb.test()
async def fd_adjacency_full_chain(dut):
    """ADVERSARIAL: data byte 0x1F ends in 5 ones, so the dynamic region ends on a
    stuff condition right at the data->stuff-count boundary. ISO 11898-1:2015 10.5:
    "there shall be only the fixed stuff bit, there shall not be two consecutive
    stuff bits" -- ONE stuff bit, the fixed one, outside the stuff count and the
    CRC (DS-T2.96). The CTU CAN FD core sends this frame with stuff count 1 and
    CRC-17 0x1008D (recorded in simulation, test_can_datalogger_fd_trailing_stuff_
    ds_t2_96.py), so the real values go on the wire and no crc_error is expected.
    If the destuffer anchors the fixed counter one bit late, the stuff count
    decodes wrong and a form_error follows. Byte-exact => handled."""
    await setup(dut)
    got = []
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=4)
    await build_fd_frame_and_send(bfm, can_id=0x123, data=[0x1F], ide=False, brs=0, esi=0,
                                  stuff_count=1, force_crc=0x1008D)
    await ClockCycles(dut.CLK, 20 * BIT_TIME)

    win = frame_window(got)
    exp = expect_fd(0, 0x123, 0, 0, 0, 1, [0x1F], stuffcnt_gray=0b001, parity_ok=True,
                    crc=0x1008D, crc_w=17)
    exp.remove((T_EVENT, EV_CRC_ERR))          # the real CRC is on the wire: no crc_error
    assert win == exp, _diff(win, exp)
    assert (T_DATA, 0x1F) in win, "the adjacency data byte was lost/corrupted"
    dut._log.info("PASS fd_adjacency_full_chain — single fixed stuff at the boundary, sc 1, CRC 0x1008D")


async def build_fd_frame_and_send(bfm, **kw):
    """Drive a FD frame via the BFM; return its meta."""
    return await bfm.send_fd(**kw)


def main():
    run_simulation(
        top_level="can_datalogger",
        module="test_can_datalogger_fd",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [
            str(RTL / "pkg" / "datalogger_config_pkg.vhd"),
            str(RTL / "pkg" / "datalogger_pkg.vhd"),
            str(RTL / "datalogger" / "infra" / "config_control" / "config_control.vhd"),
            str(RTL / "datalogger" / "can" / "can_bit_timing.vhd"),
            str(RTL / "datalogger" / "can" / "can_destuff.vhd"),
            str(RTL / "datalogger" / "can" / "can_crc15.vhd"),
            str(RTL / "datalogger" / "can" / "can_crc17.vhd"),
            str(RTL / "datalogger" / "can" / "can_crc21.vhd"),
            str(RTL / "datalogger" / "can" / "can_frame_fsm.vhd"),
            str(RTL / "datalogger" / "can" / "can_datalogger.vhd"),
        ]},
    )


if __name__ == "__main__":
    main()
