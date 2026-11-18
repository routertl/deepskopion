"""can_datalogger CAN-FD back-to-back, WITH the silicon RX conditioning in the loop
(CAN-REQ-25 / DS-T2.49). Identical adversarial intent to test_can_datalogger_fd_b2b,
but the DUT is tb_can_datalogger_cond = glitch_filter (2-FF synchroniser + de-glitcher)
→ can_datalogger, matching the datalogger_gather per-pin conditioning the bare-can_datalogger
test bypasses.

DS-T2.49 is silicon-only (the bare functional model is clean); this closes the
biggest sim gap — the synchroniser + de-glitcher in front of the RX line. If the
back-to-back FD CRC coverage bleeds once the conditioning is modelled, it shows
HERE (and then it's a functional/deterministic effect we can read off in sim, not a
metastability we can only see on silicon). If this is ALSO clean, the silicon-only
cause is narrower still (an unconstrained path, or genuine async-RX metastability a
zero-delay sim cannot model).

bug-finding-is-success: expected to FAIL if the conditioning reproduces the bug.
ROUTERTL-002: the invariant is frame-independence, not a re-derived value.
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
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(DS_ROOT / "hw" / "sim" / "cocotb"))
from can_bfm import CanBusBFM   # noqa: E402

DEV_ID = 20
NUM_TQ = 16
PRESCALER = 4
BIT_TIME = NUM_TQ * PRESCALER

T_ID, T_DATA, T_CTRL, T_EVENT, T_CRC, T_FDCTRL, T_FDAUX = 1, 2, 3, 4, 5, 6, 7
EV_FRAME_BEGIN, EV_FRAME_END = 0x10, 0x20


def mtype(m):   return (m >> 24) & 0x7
def payload(m): return m & 0xFF
def devid(m):   return (m >> 27) & 0x1F


def make_config(enable, prescaler, dev_id=DEV_ID):
    return (dev_id << 11) | ((prescaler & 0x3FF) << 1) | (enable & 1)


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


@cocotb.test()
async def fd_b2b_crc_coverage_invariant_conditioned(dut):
    """CAN-REQ-25 / DS-T2.49 with the RX synchroniser + de-glitcher in the loop —
    the same FD frame's computed FD CRC must be identical standalone and back-to-back
    through the real conditioning. Computed CRC probed one level down (dut.i_can)."""
    await setup(dut)

    got = []
    fd_computed = []
    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.CAN_MSG_VLD.value) == 1 and int(dut.CAN_MSG_RDY.value) == 1:
                m = int(dut.CAN_MSG_OUT.value)
                assert devid(m) == DEV_ID, f"misrouted dev={devid(m)} m={m:#010x}"
                got.append((mtype(m), payload(m)))
                if mtype(m) == T_FDAUX:
                    fd_computed.append(int(dut.i_can.Crc17Computed.value))
    cocotb.start_soon(collect())

    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)

    F = dict(can_id=0x2AA, data=[0x55, 0xAA, 0x55, 0xAA], ide=False, brs=0, esi=0, stuff_count=0)
    P = dict(can_id=0x7FF, data=[0xFF] * 8,              ide=False, brs=0, esi=0, stuff_count=0)

    # (1) F standalone — preceded by a long idle (the de-glitcher settles).
    await bfm.idle(bits=14)
    await bfm.send_fd(**F)
    await ClockCycles(dut.CLK, 24 * BIT_TIME)
    # (2) P immediately followed by F — minimum-IFS back-to-back through the de-glitcher.
    await bfm.send_fd(**P)
    await bfm.send_fd(**F)
    await ClockCycles(dut.CLK, 24 * BIT_TIME)

    assert len(fd_computed) >= 3, (
        f"expected 3 FDAUX captures (F-solo, P, F-b2b); got {len(fd_computed)}: "
        f"{[hex(c) for c in fd_computed]} — frames did not all complete")
    c_solo, c_b2b = fd_computed[0], fd_computed[2]
    dut._log.info(f"[CONDITIONED] F computed FD CRC-17: standalone=0x{c_solo:05x}  "
                  f"back-to-back=0x{c_b2b:05x}")
    assert c_solo == c_b2b, (
        f"CAN-REQ-25 / DS-T2.49 — WITH the synchroniser + de-glitcher in the loop, "
        f"the FD CRC coverage BLED across the back-to-back boundary: identical frame "
        f"F computed a DIFFERENT FD CRC by predecessor (standalone=0x{c_solo:05x}, "
        f"back-to-back=0x{c_b2b:05x}). The conditioning reproduces DS-T2.49 in sim "
        f"⇒ deterministic/functional, NOT metastability. Fix the FD_CRC_EN boundary.")

    frames, cur = [], None
    for t, p in got:
        if t == T_EVENT and p == EV_FRAME_BEGIN:
            cur = [(t, p)]
        elif cur is not None:
            cur.append((t, p))
            if t == T_EVENT and p == EV_FRAME_END:
                frames.append(cur); cur = None
    assert len(frames) >= 3, f"expected 3 frame windows, got {len(frames)}"
    assert frames[0] == frames[2], (
        "CAN-REQ-25 / DS-T2.49 (conditioned) — frame F decoded DIFFERENTLY "
        f"back-to-back vs standalone:\n  standalone : {[(t, hex(p)) for t, p in frames[0]]}\n"
        f"  back-to-back: {[(t, hex(p)) for t, p in frames[2]]}")
    dut._log.info("PASS [CONDITIONED] — even with the synchroniser + de-glitcher in "
                  "the loop, the FD CRC coverage is frame-independent. DS-T2.49 still "
                  "does NOT reproduce in sim ⇒ silicon-only narrows further.")


def main():
    run_simulation(
        top_level="tb_can_datalogger_cond",
        module="test_can_datalogger_fd_b2b_cond",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [
            str(RTL / "pkg" / "datalogger_config_pkg.vhd"),
            str(RTL / "pkg" / "datalogger_pkg.vhd"),
            str(RTL / "datalogger" / "infra" / "config_control" / "config_control.vhd"),
            str(RTL / "emio" / "glitch_filter.vhd"),
            str(RTL / "datalogger" / "can" / "can_bit_timing.vhd"),
            str(RTL / "datalogger" / "can" / "can_destuff.vhd"),
            str(RTL / "datalogger" / "can" / "can_crc15.vhd"),
            str(RTL / "datalogger" / "can" / "can_crc17.vhd"),
            str(RTL / "datalogger" / "can" / "can_crc21.vhd"),
            str(RTL / "datalogger" / "can" / "can_frame_fsm.vhd"),
            str(RTL / "datalogger" / "can" / "can_datalogger.vhd"),
            str(HERE / "tb_can_datalogger_cond.vhd"),
        ]},
    )


if __name__ == "__main__":
    main()
