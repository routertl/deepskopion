"""DS-P2.255 — can_datalogger CAN-FD decode at the bench rates (250 kbit/s nominal,
2 Mbit/s data) on golden frames.

The frames are sw/can/can_fd_link.py's DEFAULT_SEQUENCE (what the Zybo DS-P2.218
session sends between the two CTU cores): standard and extended ids, BRS on and
off, lengths 0, 8, 12 and 64. Each is driven with its REAL FD CRC and stuff count
(hard-coded below; the values come from the oracle of test_can_fd_crc_golden.py,
_fd_crc_region + _fd_crc_oracle, which reproduces that file's silicon edge-capture
goldens exactly, stuff counts included).

The datalogger is armed as on the bench: primary payload 0x033 (enable, bit_prescaler
25: 16 TQ x 25 = 400 clk = 250 kbit/s @ 100 MHz, docs/hardware/BRING_UP_CAN.md) and
the data bank at its power-on default 50 clk (2 Mbit/s). Nominal:data = 8.

The wire is ISO 11898-1 at both rate boundaries: the nominal->data switch at the
BRS sample point (can_bfm iso_brs, DS-P2.255) and the data->nominal switch at the
CRC-delimiter sample point (iso_delim, DS-T2.48). Before iso_brs existed the BFM held
BRS a full nominal bit; at nominal 208 clk and above (data 50) every BRS=1 frame then
misread ESI/DLC (scratch sweep, DS-P2.255 description), which is the harness, not
the datalogger: with the ISO BRS length the same frames decode exactly.

Expected tokens are built only from the literals in FRAMES (ROUTERTL-002). Every
frame must decode exactly, with no stuff/form/stuff-count/CRC error event and an
observed ACK.

Not here: DEFAULT_SEQUENCE's fourth frame, 0x7A0 BRS=1 DLC 0. Its dynamic region
ends on a run of 5 (ESI + four DLC zeros), where the CTU core puts a single fixed
stuff bit, outside the stuff count and the CRC, before the stuff count; the
datalogger misdecodes that wire. It is tracked, with its own red test, by DS-T2.96
(no xfail here). So this test covers 4 of the 5 frames, 12 windows.
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
ARM_PAYLOAD = 0x033            # enable + bit_prescaler 25 (the bench word 0xA033 for id 20)
NOMINAL_CLK = 400              # 16 TQ x 25
DATA_CLK = 50                  # data bank power-on default (2 Mbit/s)

T_ID, T_DATA, T_EVENT, T_CRC, T_FDCTRL, T_FDAUX = 1, 2, 4, 5, 6, 7
EV_BEGIN, EV_END, EV_ACK = 0x10, 0x20, 0x30
EV_ERRORS = {0x40, 0x41, 0x42, 0x43, 0x45}   # stuff, form, crc, ack, stuff-count
FD_DLC = {8: 8, 12: 9, 64: 15}

# (id, ext, brs, data, stuff_count, stuff_count_gray, crc) — CRC-17 if <= 16 B else CRC-21
FRAMES = [
    (0x123, False, True, bytes.fromhex("deadbeef0badf00d"), 3, 0b010, 0x00FF0),
    (0x456, False, False, bytes(range(12)), 3, 0b010, 0x1DA47),
    (0x1ABCDE, True, True, bytes(range(64)), 2, 0b011, 0x02CF4D),
    (0x1F000001, True, False, bytes.fromhex("55aa55aa55aa55aa"), 5, 0b111, 0x0A313),
]


def expected_tokens(can_id, ext, brs, data, gray, crc):
    toks = [(T_EVENT, EV_BEGIN),
            (T_FDCTRL, (int(ext) << 7) | (int(brs) << 6) | FD_DLC[len(data)])]
    if ext:
        toks += [(T_ID, (can_id >> 24) & 0x1F), (T_ID, (can_id >> 16) & 0xFF),
                 (T_ID, (can_id >> 8) & 0xFF), (T_ID, can_id & 0xFF)]
    else:
        toks += [(T_ID, (can_id >> 8) & 0x7), (T_ID, can_id & 0xFF)]
    toks += [(T_DATA, b) for b in data]
    toks.append((T_FDAUX, 0x80 | gray))
    toks += [(T_CRC, (crc >> 16) & 0xFF), (T_CRC, (crc >> 8) & 0xFF), (T_CRC, crc & 0xFF)]
    toks += [(T_EVENT, EV_ACK), (T_EVENT, EV_END)]
    return toks


def start_collector(dut, got):
    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.CAN_MSG_VLD.value) == 1 and int(dut.CAN_MSG_RDY.value) == 1:
                m = int(dut.CAN_MSG_OUT.value)
                got.append(((m >> 24) & 0x7, m & 0xFF))
    cocotb.start_soon(collect())


def frame_windows(got):
    out, cur = [], None
    for tok in got:
        if tok == (T_EVENT, EV_BEGIN):
            cur = [tok]
            out.append(cur)
        elif cur is not None:
            cur.append(tok)
    return out


async def reset_and_arm(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.RST.value = 1
    dut.CAN_PINS.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.CAN_MSG_RDY.value = 1
    await ClockCycles(dut.CLK, 6)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 2)
    dut.CONFIG.value = (DEV_ID << 11) | ARM_PAYLOAD
    await ClockCycles(dut.CLK, 4)


async def drive_sequence(dut, repeats):
    got = []
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=NOMINAL_CLK // 16)
    await bfm.idle(bits=11)
    for _ in range(repeats):
        for can_id, ext, brs, data, sc, _gray, crc in FRAMES:
            if brs:
                await bfm.send_fd_dualrate(data_bit_time=DATA_CLK, iso_brs=True, tx_sample_point=(0.75, 0.75),
                                           can_id=can_id,
                                           data=list(data), ide=ext, esi=0, stuff_count=sc,
                                           force_crc=crc, iso_delim=True)
            else:
                await bfm.send_fd(can_id=can_id, data=list(data), ide=ext, brs=0, esi=0,
                                  stuff_count=sc, force_crc=crc)
            await bfm.idle(bits=11)
    await ClockCycles(dut.CLK, 40 * NOMINAL_CLK)
    return got


@cocotb.test()
async def bench_rates_default_sequence_decodes_exactly(dut):
    """DEFAULT_SEQUENCE (less the DS-T2.96 corner frame) x3 at 250 kbit/s / 2 Mbit/s:
    12 frames, each token-exact."""
    await reset_and_arm(dut)
    wins = frame_windows(await drive_sequence(dut, repeats=3))
    assert len(wins) == 12, f"expected 12 frame windows, got {len(wins)}: {wins}"
    for k, win in enumerate(wins):
        can_id, ext, brs, data, _sc, gray, crc = FRAMES[k % len(FRAMES)]
        exp = expected_tokens(can_id, ext, brs, data, gray, crc)
        errs = [p for t, p in win if t == T_EVENT and p in EV_ERRORS]
        assert not errs, f"frame {k} (id 0x{can_id:x}): error events {[hex(e) for e in errs]}"
        assert win == exp, (f"frame {k} (id 0x{can_id:x}) decode mismatch"
                            f"\n  got     : {[(t, hex(p)) for t, p in win]}"
                            f"\n  expected: {[(t, hex(p)) for t, p in exp]}")
    dut._log.info("PASS DS-P2.255: 12/12 frames token-exact at 400/50 clk")


def main():
    run_simulation(
        top_level="can_datalogger",
        module=Path(__file__).stem,
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
