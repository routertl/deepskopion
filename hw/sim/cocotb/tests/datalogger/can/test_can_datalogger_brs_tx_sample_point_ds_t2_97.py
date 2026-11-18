"""DS-T2.97 — the datalogger follows the bus's sample points at the BRS switch.

Measured on the Zybo-Z7-20 CTU cores after `can_fd_link setup` 250k/2M (2026-11-10,
BTR 0x60133FEF, BTR_FD 0x3009A912): nominal sample point 87.5 % and data sample
point 74 % (PH2 13). The transmitter switches to the data rate at ITS sample point:
350 clk into the 400-clk BRS bit; BRS ends at 350 + 13 = 363 clk, and the
transmitter samples ESI at 363 + 37 = 400 clk. The bench programs the datalogger
with nominal SP 224/256 and data SP 189/256 (CONFIG bank 1 sub-words).

1. tx_875_brs_frames_decode_exactly: BFM transmitter at (0.875, 0.74), BRS=1 frames
   with ESI 0 and ESI 1 (no edge at ESI = no hard sync to hide a mis-anchored data
   phase). Every frame decodes token-exact, and per frame the logger's rate switch
   and its ESI sample land within +-1/8 data bit (+-6 clk) of the transmitter's
   (350 and 400 clk after the BRS edge). The logger's switch is the instant its data
   phase is anchored to: the BRS sample (can_bit_timing continues the data phase
   from that sample, cnt := DATA_SAMPLE_CYC + clocks since it). Both instants are
   BIT_VALID strobes less the 2-clk RX synchroniser; the RATE_SEL rise, which comes
   a few pipeline clocks later, is logged. The sub-words are written first and
   data_cyc last.
2. sp_write_leaves_data_cyc: a sample-point sub-word does not move data_cyc.
3. data_cyc_write_leaves_sp: a data_cyc write does not move either sample point.

Frames, stuff counts and CRCs are literals (ROUTERTL-002); the ESI=1 CRCs come from
the FD CRC oracle of test_can_fd_crc_golden.py (_fd_crc_region + _fd_crc_oracle),
which reproduces the ESI=0 values the CTU core sends (0x00FF0, 0x2CF4D).
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
NOMINAL_CLK = 400
DATA_CLK = 50
TX_SP = (0.875, 0.74)               # the bus's sample points, as measured
TX_SWITCH = 350                     # clk after the BRS edge
TX_ESI_SAMPLE = 363 + 37            # BRS end + data sample point
SYNC_CLK = 2                        # RX synchroniser depth (can_bit_timing rx_m, rx_s)
TOL = DATA_CLK // 8                 # +-1/8 data bit

CFG_PRIMARY = 0x033
CFG_DATA_CYC_50 = 0x400 | 50
CFG_DATA_CYC_100 = 0x400 | 100
CFG_NOM_SP_224 = 0x400 | 0x200 | 224
CFG_DATA_SP_189 = 0x400 | 0x200 | 0x100 | 189

T_ID, T_DATA, T_EVENT, T_CRC, T_FDCTRL, T_FDAUX = 1, 2, 4, 5, 6, 7
EV_BEGIN, EV_END, EV_ACK = 0x10, 0x20, 0x30
EV_ERRORS = {0x40, 0x41, 0x42, 0x43, 0x45}
FD_DLC = {8: 8, 64: 15}
DEAD = bytes.fromhex("deadbeef0badf00d")

# (id, ext, esi, data, stuff_count, gray, crc)
FRAMES = [
    (0x123, False, 0, DEAD, 3, 0b010, 0x00FF0),
    (0x123, False, 1, DEAD, 3, 0b010, 0x0871C),
    (0x1ABCDE, True, 0, bytes(range(64)), 2, 0b011, 0x02CF4D),
    (0x1ABCDE, True, 1, bytes(range(64)), 3, 0b010, 0x18F823),
]


def expected_tokens(can_id, ext, esi, data, gray, crc):
    toks = [(T_EVENT, EV_BEGIN), (T_FDCTRL, (int(ext) << 7) | (1 << 6) | (esi << 5) | FD_DLC[len(data)])]
    if ext:
        toks += [(T_ID, (can_id >> 24) & 0x1F), (T_ID, (can_id >> 16) & 0xFF),
                 (T_ID, (can_id >> 8) & 0xFF), (T_ID, can_id & 0xFF)]
    else:
        toks += [(T_ID, (can_id >> 8) & 0x7), (T_ID, can_id & 0xFF)]
    toks += [(T_DATA, b) for b in data] + [(T_FDAUX, 0x80 | gray)]
    toks += [(T_CRC, (crc >> 16) & 0xFF), (T_CRC, (crc >> 8) & 0xFF), (T_CRC, crc & 0xFF)]
    return toks + [(T_EVENT, EV_ACK), (T_EVENT, EV_END)]


async def setup(dut, cfg_words):
    cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
    dut.RST.value = 1
    dut.CAN_PINS.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.CAN_MSG_RDY.value = 1
    await ClockCycles(dut.CLK, 6)
    dut.RST.value = 0
    await ClockCycles(dut.CLK, 2)
    for p in cfg_words:
        dut.CONFIG.value = (DEV_ID << 11) | p
        await ClockCycles(dut.CLK, 4)


@cocotb.test()
async def tx_875_brs_frames_decode_exactly(dut):
    """Transmitter at 87.5 % / 74 % (switch 350, BRS end 363): exact decode, switch
    and ESI sample within +-6 clk of the transmitter's."""
    await setup(dut, [CFG_PRIMARY, CFG_NOM_SP_224, CFG_DATA_SP_189, CFG_DATA_CYC_50])
    got, rises, rate_ups, valids, cyc = [], [], [], [], [0]

    async def monitor():
        last_pin, last_rate = 1, 0
        while True:
            await RisingEdge(dut.CLK)
            cyc[0] += 1
            pin, rate = int(dut.CAN_PINS.value), int(dut.RateSel.value)
            if pin == 1 and last_pin == 0:
                rises.append(cyc[0])
            if rate == 1 and last_rate == 0:
                rate_ups.append(cyc[0])
            if int(dut.BitValid.value) == 1:
                valids.append(cyc[0])
            if int(dut.CAN_MSG_VLD.value) == 1 and int(dut.CAN_MSG_RDY.value) == 1:
                m = int(dut.CAN_MSG_OUT.value)
                got.append(((m >> 24) & 0x7, m & 0xFF))
            last_pin, last_rate = pin, rate
    cocotb.start_soon(monitor())

    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=NOMINAL_CLK // 16)
    await bfm.idle(bits=11)
    for can_id, ext, esi, data, sc, _gray, crc in FRAMES:
        await bfm.send_fd_dualrate(data_bit_time=DATA_CLK, iso_brs=True, tx_sample_point=TX_SP,
                                   iso_delim=True, can_id=can_id, data=list(data), ide=ext,
                                   esi=esi, stuff_count=sc, force_crc=crc)
        await bfm.idle(bits=11)
    await ClockCycles(dut.CLK, 40 * NOMINAL_CLK)

    wins, cur = [], None
    for tok in got:
        if tok == (T_EVENT, EV_BEGIN):
            cur = [tok]
            wins.append(cur)
        elif cur is not None:
            cur.append(tok)
    assert len(wins) == len(FRAMES), (f"expected {len(FRAMES)} frame windows, got {len(wins)} with the "
                                    f"transmitter switching at {TX_SWITCH} clk and BRS ending at 363 clk: {wins}")
    for win, (can_id, ext, esi, data, _sc, gray, crc) in zip(wins, FRAMES):
        exp = expected_tokens(can_id, ext, esi, data, gray, crc)
        errs = [p for t, p in win if t == T_EVENT and p in EV_ERRORS]
        assert not errs and win == exp, (
            f"0x{can_id:x} esi={esi}: decode at the bus sample point (transmitter switch "
            f"{TX_SWITCH} clk, BRS end 363 clk) is not exact; error events {[hex(e) for e in errs]}"
            f"\n  got     : {[(t, hex(p)) for t, p in win]}\n  expected: {[(t, hex(p)) for t, p in exp]}")

    assert len(rate_ups) >= len(FRAMES), f"only {len(rate_ups)} rate switches for {len(FRAMES)} BRS frames"
    for n, up in enumerate(rate_ups[:len(FRAMES)]):
        brs_edge = max(r for r in rises if r < up)
        brs_valid = max(v for v in valids if v < up)
        esi_valid = min(v for v in valids if v > up)
        sw_err = (brs_valid - SYNC_CLK) - (brs_edge + TX_SWITCH)
        esi_err = (esi_valid - SYNC_CLK) - (brs_edge + TX_ESI_SAMPLE)
        dut._log.info(f"frame {n}: BRS edge {brs_edge}, switch (BRS sample) {brs_valid} ({sw_err:+d}), "
                      f"RATE_SEL rise {up} ({up - brs_edge - TX_SWITCH:+d}), ESI sample {esi_valid} ({esi_err:+d})")
        assert abs(sw_err) <= TOL, f"frame {n}: logger switch {sw_err:+d} clk from the transmitter's (limit {TOL})"
        assert abs(esi_err) <= TOL, f"frame {n}: logger ESI sample {esi_err:+d} clk from the transmitter's (limit {TOL})"


@cocotb.test()
async def sp_write_leaves_data_cyc(dut):
    """data_cyc 100, then both sample-point sub-words: data_cyc stays 100."""
    await setup(dut, [CFG_PRIMARY, CFG_DATA_CYC_100, CFG_NOM_SP_224, CFG_DATA_SP_189])
    await ClockCycles(dut.CLK, 4)
    assert int(dut.DataCyc.value) == 100, f"a sample-point write moved data_cyc to {int(dut.DataCyc.value)}"


@cocotb.test()
async def data_cyc_write_leaves_sp(dut):
    """Both sample-point sub-words, then data_cyc 100: both sample points unchanged."""
    await setup(dut, [CFG_PRIMARY, CFG_NOM_SP_224, CFG_DATA_SP_189, CFG_DATA_CYC_100])
    await ClockCycles(dut.CLK, 4)
    assert int(dut.DataCyc.value) == 100, f"data_cyc {int(dut.DataCyc.value)}, expected 100"
    assert int(dut.NomSpFrac.value) == 224, f"nominal SP {int(dut.NomSpFrac.value)}/256 after a data_cyc write, expected 224"
    assert int(dut.DataSpFrac.value) == 189, f"data SP {int(dut.DataSpFrac.value)}/256 after a data_cyc write, expected 189"


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
