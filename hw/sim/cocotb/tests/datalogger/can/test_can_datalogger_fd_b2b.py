# rr unknown-llm (AI-generated)
"""can_datalogger CAN-FD BACK-TO-BACK adversarial test (CAN-REQ-25 / DS-T2.49).

The FD analog of CAN-REQ-10 ("no field bleeds across the frame_end/frame_begin
boundary"), applied to the FD CRC verdict. A CAN-FD frame's FD CRC coverage (the
FdCrcCovered window over SOF..stuff-count) MUST be independent of any preceding
frame at the minimum interframe space. This drives the SAME FD frame standalone and
immediately after a (different) predecessor and asserts the datalogger's INTERNAL
Crc17Computed for the frame is bit-identical in both cases.

Why the internal signal and not crc_error: the BFM puts an ARBITRARY CRC on the
wire (the FD CRC coverage boundary is unanchored, CAN-REQ-19), so crc_error is
always present — the boolean is blind to a coverage *bleed*. The COMPUTED CRC is
the discriminator: identical frame ⇒ identical computed CRC, UNLESS a covered bit
bled across the boundary (SOF first-covered bit dropped, a boundary bit counted on
the wrong side of the R.State register flip, or stale engine state carried in).

bug-finding-is-success: this is EXPECTED to FAIL until DS-T2.49 is fixed — on
silicon the datalogger emits a spurious crc_error storm (~1 per frame) on back-to-back
FD frames while the bus is verifiably clean (CTU controllers TEC/REC=0). RTL-
localized to FD_CRC_EN = BIT_VALID and FdCrcCovered(R.State) (can_frame_fsm:298),
asymmetric vs the de-stuffed-gated classic CRC_EN (:294). A GREEN run is the yellow
flag (either fixed, or the BFM's clean bit-timing doesn't reproduce a silicon-only
timing margin — in which case escalate to a rated/edge-driven or on-silicon check).
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
async def fd_b2b_crc_coverage_invariant(dut):
    """CAN-REQ-25 / DS-T2.49 — the same FD frame's computed FD CRC must be identical
    standalone and back-to-back. Bleed ⇒ they differ (the spurious-crc_error root)."""
    await setup(dut)

    # Collector: record every (type, payload); at each FDAUX (emitted right after the
    # FD CRC coverage window closes at end-of-stuff-count) latch the computed FD CRC.
    got = []           # all (type, payload) in order
    fd_computed = []   # Crc17Computed per frame, in emission order
    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.CAN_MSG_VLD.value) == 1 and int(dut.CAN_MSG_RDY.value) == 1:
                m = int(dut.CAN_MSG_OUT.value)
                assert devid(m) == DEV_ID, f"misrouted dev={devid(m)} m={m:#010x}"
                got.append((mtype(m), payload(m)))
                if mtype(m) == T_FDAUX:
                    fd_computed.append(int(dut.Crc17Computed.value))
    cocotb.start_soon(collect())

    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)

    # F = frame under test (CRC-17, ≤16 B). P = a DIFFERENT predecessor whose CRC-
    # engine state is maximally unlike F's, so a bleed clearly corrupts F's CRC.
    F = dict(can_id=0x2AA, data=[0x55, 0xAA, 0x55, 0xAA], ide=False, brs=0, esi=0, stuff_count=0)
    P = dict(can_id=0x7FF, data=[0xFF] * 8,              ide=False, brs=0, esi=0, stuff_count=0)

    # (1) F standalone — preceded by a long idle.
    await bfm.idle(bits=12)
    await bfm.send_fd(**F)
    await ClockCycles(dut.CLK, 24 * BIT_TIME)
    # (2) P immediately followed by F — minimum-IFS back-to-back (P's wire trailer
    #     already carries EOF(7)+IFS(3); the next send_fd begins F's SOF).
    await bfm.send_fd(**P)
    await bfm.send_fd(**F)
    await ClockCycles(dut.CLK, 24 * BIT_TIME)

    # fd_computed == [F_solo, P, F_b2b]
    assert len(fd_computed) >= 3, (
        f"expected 3 FDAUX captures (F-solo, P, F-b2b); got {len(fd_computed)}: "
        f"{[hex(c) for c in fd_computed]} — frames did not all complete")
    c_solo, c_b2b = fd_computed[0], fd_computed[2]
    dut._log.info(f"F computed FD CRC-17: standalone=0x{c_solo:05x}  back-to-back=0x{c_b2b:05x}")
    assert c_solo == c_b2b, (
        f"CAN-REQ-25 / DS-T2.49 — FD CRC coverage BLED across the back-to-back "
        f"boundary: identical frame F computed a DIFFERENT FD CRC depending on its "
        f"predecessor (standalone=0x{c_solo:05x}, back-to-back=0x{c_b2b:05x}). The "
        f"FdCrcCovered window covered a different bit set for F → spurious crc_error "
        f"on silicon. Fix the FD_CRC_EN boundary (can_frame_fsm:298); do NOT loosen "
        f"this assertion.")
    # Captured-side + field check: F's full decoded window must also be identical
    # standalone vs back-to-back (catches a CAPTURED-CRC or field bleed, complementary
    # to the computed-CRC invariant above). Split got into frame_begin..frame_end
    # windows; the F windows are the 1st and 3rd (F-solo, P, F-b2b).
    frames, cur = [], None
    for t, p in got:
        if t == T_EVENT and p == EV_FRAME_BEGIN:
            cur = [(t, p)]
        elif cur is not None:
            cur.append((t, p))
            if t == T_EVENT and p == EV_FRAME_END:
                frames.append(cur); cur = None
    assert len(frames) >= 3, f"expected 3 frame windows, got {len(frames)}"
    f_solo, f_b2b = frames[0], frames[2]
    assert f_solo == f_b2b, (
        "CAN-REQ-25 / DS-T2.49 — frame F decoded DIFFERENTLY back-to-back vs "
        f"standalone (captured CRC / field bleed across the boundary):\n"
        f"  standalone : {[(t, hex(p)) for t, p in f_solo]}\n"
        f"  back-to-back: {[(t, hex(p)) for t, p in f_b2b]}")
    dut._log.info("PASS fd_b2b_crc_coverage_invariant — FD CRC coverage AND full "
                  "decode are frame-independent in functional sim (computed + "
                  "captured + fields). DS-T2.49 does NOT reproduce here ⇒ silicon-only.")


def main():
    run_simulation(
        top_level="can_datalogger",
        module="test_can_datalogger_fd_b2b",
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
