"""DS-T2.96 — CAN-FD frames whose dynamic region ends on a run of five equal bits.

ISO 11898-1:2015 paragraph 10.5 "Frame coding", as quoted verbatim in the Linux
kernel commit "can: length: fix bitstuffing count" (2023): "There shall be a fixed
stuff bit before the first bit of the stuff count, even if the last bits of the
preceding field are a sequence of five consecutive bits of identical value, there
shall be only the fixed stuff bit, there shall not be two consecutive stuff bits."
So at this corner the wire carries ONE stuff bit, the fixed one: it is not counted
in the stuff count and is not covered by the CRC.

CTU_WIRES are the real CTU CAN FD core's CAN_tx, recorded in simulation (self-test
loopback, 24 clocks per bit at both rates, ESI=1 from an error-passive transmitter),
SOF up to the last transition (the remaining bits through the CRC delimiter are
recessive). The stuff count and CRC of each frame are read from that wire. These
are the hard-coded expected values (ROUTERTL-002).

1. ctu_wire_is_the_bfm_default_wire: can_bfm.build_fd_frame, with its defaults,
   reproduces every CTU wire bit for bit, SOF through the CRC delimiter.
2. corner_frames_decode_exactly_at_bench_rates: the datalogger, armed as on the
   bench (0x033: 250 kbit/s nominal; data bank 2 Mbit/s), decodes each frame
   token-exact, with no stuff/form/CRC/stuff-count error event and an ACK.
   0x7A0 BRS=0 DLC 0 is the control: its dynamic region does not end on a run
   of five, so both stuffing readings give the same wire.
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
ARM_PAYLOAD = 0x033            # enable + bit_prescaler 25: 400 clk = 250 kbit/s @ 100 MHz
NOMINAL_CLK = 400
DATA_CLK = 50                  # data bank power-on default (2 Mbit/s)

T_ID, T_DATA, T_EVENT, T_CRC, T_FDCTRL, T_FDAUX = 1, 2, 4, 5, 6, 7
EV_BEGIN, EV_END, EV_ACK = 0x10, 0x20, 0x30
EV_ERRORS = {0x40, 0x41, 0x42, 0x43, 0x45}   # stuff, form, crc, ack, stuff-count

# (id, data, brs, esi, stuff_count, stuff_count_gray, crc17, CTU CAN_tx from SOF)
FRAMES = [
    (0x123, b"\x1f", 0, 0, 1, 0b001, 0x1008D,
     "00010010001100100000101000111110001101000100001010010110"),
    (0x123, b"\x1f", 1, 0, 0, 0b000, 0x07D20,
     "000100100011001010000100011111000001001101110110010000010"),
    (0x123, b"\x1f", 0, 1, 0, 0b000, 0x05841,
     "0001001000110010010001000111110000010010111001001010000"),
    (0x123, b"\x1f", 1, 1, 0, 0b000, 0x14636,
     "000100100011001011000100011111000001101010011000010101100"),
    (0x7A0, b"", 1, 0, 1, 0b001, 0x0CA62,
     "01111010000010010100000100110011010101000110000100"),
    (0x7A0, b"", 0, 0, 2, 0b011, 0x1A757,           # control: no run of five at the end
     "01111010000010010000010010110111010001101010110110"),
]


def expected_tokens(can_id, brs, esi, data, gray, crc):
    return ([(T_EVENT, EV_BEGIN), (T_FDCTRL, (brs << 6) | (esi << 5) | len(data)),
             (T_ID, (can_id >> 8) & 0x7), (T_ID, can_id & 0xFF)]
            + [(T_DATA, b) for b in data]
            + [(T_FDAUX, 0x80 | gray),
               (T_CRC, (crc >> 16) & 0xFF), (T_CRC, (crc >> 8) & 0xFF), (T_CRC, crc & 0xFF),
               (T_EVENT, EV_ACK), (T_EVENT, EV_END)])


@cocotb.test()
async def ctu_wire_is_the_bfm_default_wire(dut):
    """build_fd_frame's default FD wire equals the CTU core's, SOF..CRC delimiter."""
    for can_id, data, brs, esi, sc, _gray, crc, ctu in FRAMES:
        wire, _meta = build_fd_frame(can_id=can_id, data=list(data), ide=False, brs=brs,
                                     esi=esi, stuff_count=sc, force_crc=crc, ack=1)
        n = len(wire) - 12                              # drop ACK, ACK-delim, EOF, IFS
        want = ctu + "1" * (n - len(ctu))
        got = "".join(str(b) for b in wire[:n])
        assert got == want, (f"0x{can_id:x} brs={brs} esi={esi}: BFM wire differs from CTU at bit "
                             f"{next(i for i, (a, b) in enumerate(zip(got, want)) if a != b)}"
                             f"\n  bfm: {got}\n  ctu: {want}")


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


@cocotb.test()
async def corner_frames_decode_exactly_at_bench_rates(dut):
    """Every FRAMES entry, on the CTU wire at 400/50 clk, decodes token-exact."""
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

    got = []
    start_collector(dut, got)
    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=NOMINAL_CLK // 16)
    await bfm.idle(bits=11)
    for can_id, data, brs, esi, sc, _gray, crc, _ctu in FRAMES:
        if brs:
            await bfm.send_fd_dualrate(data_bit_time=DATA_CLK, iso_brs=True, tx_sample_point=(0.75, 0.75),
                                       iso_delim=True,
                                       can_id=can_id, data=list(data), ide=False, esi=esi,
                                       stuff_count=sc, force_crc=crc, ctu_trailing_stuff=True)
        else:
            await bfm.send_fd(can_id=can_id, data=list(data), ide=False, brs=0, esi=esi,
                              stuff_count=sc, force_crc=crc, ctu_trailing_stuff=True)
        await bfm.idle(bits=11)
    await ClockCycles(dut.CLK, 40 * NOMINAL_CLK)

    wins = frame_windows(got)
    assert len(wins) == len(FRAMES), f"expected {len(FRAMES)} frame windows, got {len(wins)}: {wins}"
    for win, (can_id, data, brs, esi, _sc, gray, crc, _ctu) in zip(wins, FRAMES):
        exp = expected_tokens(can_id, brs, esi, data, gray, crc)
        errs = [p for t, p in win if t == T_EVENT and p in EV_ERRORS]
        assert not errs, f"0x{can_id:x} brs={brs} esi={esi}: error events {[hex(e) for e in errs]}"
        assert win == exp, (f"0x{can_id:x} brs={brs} esi={esi} decode mismatch"
                            f"\n  got     : {[(t, hex(p)) for t, p in win]}"
                            f"\n  expected: {[(t, hex(p)) for t, p in exp]}")


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
