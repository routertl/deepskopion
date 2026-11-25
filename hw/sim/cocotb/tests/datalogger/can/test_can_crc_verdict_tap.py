# rr unknown-llm (AI-generated)
"""Validate the DS-T2.49 FD CRC-17 VERDICT tap (the new REA instrumentation) in sim
before trusting it on silicon. can_frame_fsm latches, at the S_FDCRC verdict,
DbgCrcCap=Cap17 (captured wire CRC) and DbgCrcComp=CRC17_COMPUTED, and pulses
DbgCrcFired on mismatch. The BFM drives an ARBITRARY wire CRC (0x15555) so a single
FD frame must throw crc_error ⇒ the tap must read Cap17=0x15555 (captured) and a
DIFFERENT computed value — proving the tap exposes captured-vs-computed correctly.
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

DEV_ID, NUM_TQ, PRESCALER = 20, 16, 4
BIT_TIME = NUM_TQ * PRESCALER


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
async def crc_verdict_tap_exposes_cap_vs_computed(dut):
    await setup(dut)
    fired = []
    async def watch():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.DbgCrcFired.value) == 1:
                fired.append((int(dut.DbgCrcCap.value), int(dut.DbgCrcComp.value)))
    cocotb.start_soon(watch())

    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=8)
    # arbitrary wire CRC (BFM default 0x15555) ⇒ crc_error expected.
    await bfm.send_fd(can_id=0x2AA, data=[0x55, 0xAA, 0x55, 0xAA],
                      ide=False, brs=0, esi=0, stuff_count=0)
    await ClockCycles(dut.CLK, 24 * BIT_TIME)

    assert len(fired) >= 1, "DbgCrcFired never pulsed — verdict tap broken"
    cap, comp = fired[0]
    dut._log.info(f"verdict tap: Cap17(captured)=0x{cap:05x}  CRC17_COMPUTED=0x{comp:05x}")
    assert cap == 0x15555, f"DbgCrcCap=0x{cap:05x} != driven wire CRC 0x15555 (capture tap wrong)"
    # 0x17065 = the CORRECT ISO-FD CRC-17 for this frame (id 0x2AA, [55,AA,55,AA],
    # sc forced 0) under the DS-T2.49 coverage fix — dynamic stuff IN, fixed stuff
    # OUT, MSB-preset init. Independently anchored by can_ctu_oracle (NOT read from
    # the DUT). The pre-fix 0x04a57 encoded the buggy coverage (fixed stuff folded in).
    assert comp == 0x17065, f"DbgCrcComp=0x{comp:05x} != expected computed 0x17065"
    assert cap != comp, "tap inconsistent: cap==comp yet crc_fired pulsed"
    dut._log.info("PASS — the REA verdict tap exposes captured-vs-computed correctly")


def main():
    run_simulation(
        top_level="can_datalogger",
        module="test_can_crc_verdict_tap",
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
