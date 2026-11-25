# rr unknown-llm (AI-generated)
"""can_datalogger CAN-FD BRS dual-rate — ISO CRC-delimiter switch-back REPRO (DS-T2.48).

Silicon (DS-P3.112, Zybo bit 0x80b87c20, real CTU CAN FD traffic) showed a BRS
frame's DATA phase decodes byte-exact at the matched data rate, yet the frame
raises form_error + ack_error — ONLY on BRS frames.

ROOT CAUSE (DS-T2.48): can_frame_fsm drives RATE_SEL='1' over FdDataPhase =
{S_ESI..S_FDCRC} and drops it at the START of S_CRCDELIM, so the datalogger counts a
FULL NOMINAL delimiter bit. But per ISO 11898-1:2015 (CAN FD bit-rate switching;
Bosch CAN FD Spec 1.0 §3), cross-checked against the ctu-fee/ctucanfd_ip_core
oracle (D.26), the data->nominal switch-back happens AT the CRC delimiter's SAMPLE
POINT — the delimiter's leading phase is still DATA-rate (a SHORT bit). The
datalogger therefore places the delimiter sample point far too late; it lands in the
ACK slot -> form_error (delimiter read wrong) + ack_error.

WHY THE EXISTING DUAL-RATE TESTS MISS IT (test_can_datalogger_datacfg): the BFM's
build_fd_frame_rated drove the CRC delimiter at NOMINAL rate — the SAME wrong
boundary as the RTL. BFM and DUT agreed, so sim was green while the ISO-conformant
CTU diverged on silicon. This is the D.26 motivating case: an un-cited BFM
assumption silently ratified the DUT bug.

This test drives the ISO-faithful wire (iso_delim=True -> CRC delimiter data-rate)
and asserts the datalogger raises NEITHER form_error NOR ack_error. On the UNFIXED
RTL it DOES raise them -> this test fails. That failure IS the repro / regression
guard for DS-T2.48; when the RATE_SEL switch-back is fixed it passes. crc_error
(0x42) stays EXPECTED — that is the separate DS-P3.107 CRC-coverage gap, not this.
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
CAN_CFG_TYPE_DATA = 1 << 10       # config-word type bit: 1 = data-phase prescaler bank (DS-P3.108b)
NUM_TQ = 16
PRESCALER = 4
BIT_TIME = NUM_TQ * PRESCALER     # nominal = 64 clk/bit
DATA_CYC = 30                     # fast data-phase bit period (clk cycles)

T_ID, T_DATA, T_CTRL, T_EVENT, T_CRC, T_FDCTRL, T_FDAUX = 1, 2, 3, 4, 5, 6, 7
EV_FRAME_BEGIN, EV_FRAME_END = 0x10, 0x20
EV_FORM_ERR, EV_ACK_ERR = 0x41, 0x43   # can_frame_fsm C_EvFormErr / C_EvAckErr


def mtype(m):   return (m >> 24) & 0x7
def payload(m): return m & 0xFF


def cfg(datalogger_id, payload_bits):
    return ((datalogger_id & 0x1F) << 11) | (payload_bits & 0x7FF)


def start_collector(dut, got):
    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.CAN_MSG_VLD.value) == 1 and int(dut.CAN_MSG_RDY.value) == 1:
                got.append(int(dut.CAN_MSG_OUT.value))
    cocotb.start_soon(collect())


async def reset(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.RST.value = 1
    dut.CAN_PINS.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.CAN_MSG_RDY.value = 1
    await ClockCycles(dut.CLK, 6)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 2)


async def write_cfg(dut, value):
    dut.CONFIG.value = value
    await ClockCycles(dut.CLK, 4)


# DS-T2.48 standing regression guard. Was the repro (expect_fail) — the RATE_SEL
# switch-back fix (can_frame_fsm FdDataPhase now includes S_CRCDELIM) made it pass,
# so the expect_fail was removed (cocotb flagged the xpass). A future regression of
# the CRC-delimiter switch-back re-raises form_error/ack_error and fails here.
@cocotb.test()
async def fd_iso_crc_delim_switchback(dut):
    """DS-T2.48 guard. Drive a BRS=1 frame whose CRC delimiter is timed per ISO
    (data-rate up to its sample point) and assert the datalogger raises no form/ack
    error — the data->nominal switch-back must happen AT the delimiter sample point,
    not its leading edge. crc_error (0x42) stays expected = DS-P3.107."""
    await reset(dut)
    await write_cfg(dut, cfg(DEV_ID, CAN_CFG_TYPE_DATA | DATA_CYC))   # data bank (type 1): DataCyc=30
    await write_cfg(dut, cfg(DEV_ID, (PRESCALER << 1) | 1))           # primary (type 0): enable + prescaler

    got = []
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=4)
    await bfm.send_fd_dualrate(data_bit_time=DATA_CYC, can_id=0x123,
                               data=[0x5A, 0xA5, 0x3C], ide=False, esi=0,
                               stuff_count=0, iso_delim=True)
    await ClockCycles(dut.CLK, 30 * BIT_TIME)

    events = [payload(m) for m in got if mtype(m) == T_EVENT]
    assert EV_FRAME_BEGIN in events, (
        f"datalogger never engaged on the ISO dual-rate frame — events={[hex(e) for e in events]}")

    assert EV_FORM_ERR not in events, (
        "DS-T2.48: form_error on an ISO-faithful CRC delimiter — the datalogger treats the "
        "delimiter as a full NOMINAL bit (RATE_SEL drops at S_CRCDELIM start) instead of "
        "switching back at its SAMPLE POINT, so it samples the (short, data-rate) delimiter "
        f"too late. events={[hex(e) for e in events]}")
    assert EV_ACK_ERR not in events, (
        "DS-T2.48: ack_error — the CRC-delimiter mis-sample cascaded into the ACK slot. "
        f"events={[hex(e) for e in events]}")

    dut._log.info("PASS fd_iso_crc_delim_switchback — DS-T2.48 fixed: ISO CRC-delimiter "
                  "switch-back clean, no form/ack error on a real dual-rate BRS frame")


def main():
    run_simulation(
        top_level="can_datalogger",
        module="test_can_datalogger_fd_dualrate_iso",
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
