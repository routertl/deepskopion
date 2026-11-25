# rr unknown-llm (AI-generated)
"""can_datalogger CAN-FD runtime data-prescaler test (DS-P3.108b, CAN-REQ-24) + BRS=1
dual-rate.

Contract-first: CAN-REQ-24 (requirements.yml) defines the BANKED data config — one
device_id, payload bit [10] a type bit (0=primary {bit_prescaler,enable}, 1=data
{data_cyc}). This test validates the data-phase bit rate is SW-settable that way
(no pseudo-id) — and, as a bonus, exercises the full BRS=1 DUAL-RATE chain in sim
(arbitration nominal, data phase fast, the FSM-driven RATE_SEL switch + forced
resync), which the BRS=0 full-chain test could not.

Two frames through the REAL can_datalogger:
  1. NO data-cfg write → the power-on default (DataCyc=50) → drive the data phase
     at 50 clk/bit → must decode byte-exact.
  2. banked data write (id 20, type 1, DataCyc=30) → drive the data phase at 30
     clk/bit → must decode byte-exact. If the bank did nothing (DataCyc stuck at 50), the
     30-clk data bits sample at the wrong rate and the payload garbles — so a
     byte-exact decode PROVES the SW value reached the sampler.

Hard-coded expected (ROUTERTL-002). The arbitrary wire CRC ⇒ an EXPECTED crc_error
(FD CRC coverage stays deferred to the DS-P3.109 golden frame).
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
DEFAULT_DATA_CYC = 50             # can_datalogger G_DATA_CYC_PER_BIT default

T_ID, T_DATA, T_CTRL, T_EVENT, T_CRC, T_FDCTRL, T_FDAUX = 1, 2, 3, 4, 5, 6, 7
EV_FRAME_BEGIN, EV_FRAME_END, EV_ACK_OBS, EV_CRC_ERR = 0x10, 0x20, 0x30, 0x42


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
    """One CONFIG write (config_control latches on the change edge)."""
    dut.CONFIG.value = value
    await ClockCycles(dut.CLK, 4)


def frame_window(got):
    seq = [(mtype(m), payload(m)) for m in got]
    b = next((i for i, (t, p) in enumerate(seq) if t == T_EVENT and p == EV_FRAME_BEGIN), None)
    e = next((i for i, (t, p) in enumerate(seq) if t == T_EVENT and p == EV_FRAME_END), None)
    assert b is not None and e is not None and e > b, f"no frame window in {seq}"
    return seq[b:e + 1]


def expect_fd_std(base_id, esi, dlc, data, gray, crc, crc_w):
    exp = [(T_EVENT, EV_FRAME_BEGIN),
           (T_FDCTRL, (0 << 7) | (1 << 6) | (esi << 5) | (dlc & 0xF)),   # BRS=1
           (T_ID, (base_id >> 8) & 0x7), (T_ID, base_id & 0xFF)]
    exp += [(T_DATA, b) for b in data]
    exp.append((T_FDAUX, (1 << 7) | (gray & 0x7)))
    if crc_w == 21:
        exp += [(T_CRC, (crc >> 16) & 0x1F), (T_CRC, (crc >> 8) & 0xFF), (T_CRC, crc & 0xFF)]
    else:
        exp += [(T_CRC, (crc >> 16) & 0x1), (T_CRC, (crc >> 8) & 0xFF), (T_CRC, crc & 0xFF)]
    exp += [(T_EVENT, EV_CRC_ERR), (T_EVENT, EV_ACK_OBS), (T_EVENT, EV_FRAME_END)]
    return exp


def _diff(got, exp):
    return (f"\n  got     : {[(t, hex(p)) for t, p in got]}"
            f"\n  expected: {[(t, hex(p)) for t, p in exp]}")


async def _run_one(dut, data_cyc, data_bytes, can_id=0x123):
    got = []
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=4)
    meta = await bfm.send_fd_dualrate(data_bit_time=data_cyc, can_id=can_id,
                                      data=data_bytes, ide=False, esi=0, stuff_count=0)
    await ClockCycles(dut.CLK, 30 * BIT_TIME)
    win = frame_window(got)
    exp = expect_fd_std(can_id, 0, len(data_bytes), data_bytes, gray=0,
                        crc=meta["crc"], crc_w=meta["crc_w"])
    return win, exp


@cocotb.test()
async def fd_default_data_rate(dut):
    """BRS=1 dual-rate at the power-on default DataCyc=50 — no data-cfg write."""
    await reset(dut)
    await write_cfg(dut, cfg(DEV_ID, (PRESCALER << 1) | 1))     # enable + nominal prescaler
    win, exp = await _run_one(dut, DEFAULT_DATA_CYC, [0xAB, 0xCD])
    assert win == exp, "default-rate dual-rate decode mismatch:" + _diff(win, exp)
    dut._log.info("PASS fd_default_data_rate — BRS=1 dual-rate decodes at default DataCyc=50")


@cocotb.test()
async def fd_sw_set_data_rate(dut):
    """A banked data write (id 20, type 1) sets DataCyc=30; the data phase driven at
    30 clk/bit must decode byte-exact — proving the SW value reached the B2 sampler."""
    await reset(dut)
    await write_cfg(dut, cfg(DEV_ID, CAN_CFG_TYPE_DATA | 30))   # data bank (type 1): DataCyc=30
    await write_cfg(dut, cfg(DEV_ID, (PRESCALER << 1) | 1))     # primary bank (type 0): enable + prescaler
    win, exp = await _run_one(dut, 30, [0x5A, 0xA5, 0x3C])
    assert win == exp, "SW-set-rate dual-rate decode mismatch (DataCyc not applied?):" + _diff(win, exp)
    n_data = sum(1 for t, _ in win if t == T_DATA)
    assert n_data == 3, f"expected 3 data bytes at the SW rate, got {n_data}"
    dut._log.info("PASS fd_sw_set_data_rate — banked data write (type 1) DataCyc=30 applied; "
                  "30 clk/bit data phase decoded byte-exact (DS-P3.108b proven)")


@cocotb.test()
async def data_cfg_does_not_disturb_enable(dut):
    """Coexistence: a type-1 (data) write must NOT reset the primary config (enable/
    bit_prescaler, type 0) — the two banked config_controls hold independently."""
    await reset(dut)
    await write_cfg(dut, cfg(DEV_ID, (PRESCALER << 1) | 1))    # enable first (primary bank, type 0)
    await write_cfg(dut, cfg(DEV_ID, CAN_CFG_TYPE_DATA | 30))  # then data bank (type 1) — must not disable
    got = []
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)
    await bfm.idle(bits=4)
    await bfm.send(can_id=0x321, data=[0x11, 0x22])           # a CLASSIC frame
    await ClockCycles(dut.CLK, 20 * BIT_TIME)
    win = frame_window(got)
    assert (T_CTRL, 0x02) in win and (T_DATA, 0x11) in win and (T_DATA, 0x22) in win, (
        f"classic frame did not decode after a data-cfg write (config disturbed?): {win}")
    dut._log.info("PASS data_cfg_does_not_disturb_enable — type-1 write left the type-0 enable intact")


def main():
    run_simulation(
        top_level="can_datalogger",
        module="test_can_datalogger_datacfg",
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
