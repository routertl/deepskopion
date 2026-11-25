# rr unknown-llm (AI-generated)
"""can_datalogger FD CRC verdict — silicon-anchored golden test (DS-T2.49).

THE regression for the DS-T2.49 fix. The CAN datalogger computes the FD CRC over the
WRONG bit set (it folded the FIXED stuff bits of the stuff-count into the CRC),
so on silicon it captured the wire CRC correctly but computed a different value and
raised a spurious crc_error on EVERY CAN-FD frame. The fix (can_frame_fsm
FD_CRC_EN + can_datalogger FdCrcDin mux) feeds the FD CRC the DYNAMIC stuff bits but
NOT the fixed stuff bits, matching ISO 11898-1.

This drives each frame with its REAL ISO CRC — the value read off the bus by the
edge datalogger on real CTU silicon (three independent ground-truth methods agreed) —
and asserts the datalogger (a) raises NO crc_error and (b) computes exactly that CRC.
A NEGATIVE frame (CRC off by one bit) must still raise crc_error, proving the
verdict isn't just disabled. The goldens span CRC-17 (1/8/16 B) and CRC-21 (20 B).

bug-finding-is-success / ROUTERTL-002: the expected CRCs are hard-coded silicon
captures, never re-derived from the DUT. Before the fix this FAILS (crc_error on
every golden); a green run means the coverage now matches the wire.

A BRS=1 row is driven DUAL-RATE (DS-P2.234): nominal bits at 64 clk, data bits at
50 clk (the datalogger's data_cyc default), with the BFM transmitter at the
datalogger's reset sample points (0.75, 0.75: BRS lasts 48 + 12 = 60 clk) and the
switch back at the CRC delimiter. A CAN FD node sends the data phase at the data
rate, and the datalogger switches to it at BRS, so a single-rate wire (every bit at
64 clk) is not a CAN FD frame: the datalogger sampled its data phase on the wrong
grid and computed no CRC (or a wrong one) for the 0x456 CRC-21 row. BRS=0 rows stay
single-rate, which is what they are on the bus.
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
DATA_BIT_TIME = 50          # the datalogger's data_cyc power-on default

T_ID, T_DATA, T_CTRL, T_EVENT, T_CRC, T_FDCTRL, T_FDAUX = 1, 2, 3, 4, 5, 6, 7
EV_FRAME_BEGIN, EV_FRAME_END, EV_CRC_ERR = 0x10, 0x20, 0x42
FD_CRC_POLY = {17: 0x1685B, 21: 0x102899}
FD_DLC_BYTES = {
    0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8,
    9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64,
}


def mtype(m):   return (m >> 24) & 0x7
def payload(m): return m & 0xFF
def devid(m):   return (m >> 27) & 0x1F


def make_config(enable, prescaler, dev_id=DEV_ID):
    return (dev_id << 11) | ((prescaler & 0x3FF) << 1) | (enable & 1)


def _bits_msb(value, width):
    return [(value >> i) & 1 for i in range(width - 1, -1, -1)]


def _bit_stuff(bits):
    out = []
    run = 0
    prev = None
    for bit in bits:
        run = run + 1 if bit == prev else 1
        prev = bit
        out.append(bit)
        if run == 5:
            stuff = 1 - bit
            out.append(stuff)
            prev = stuff
            run = 1
    return out


def _gray3(value):
    gray = (value & 7) ^ ((value & 7) >> 1)
    return [(gray >> 2) & 1, (gray >> 1) & 1, gray & 1]


def _fd_crc_region(can_id, data, ide=False, brs=0, esi=0, stuff_count=0):
    dlc = next((k for k, v in FD_DLC_BYTES.items() if v == len(data)), min(len(data), 15))
    dyn = [0]
    if not ide:
        dyn += _bits_msb(can_id, 11) + [0, 0, 1]
    else:
        dyn += (_bits_msb((can_id >> 18) & 0x7FF, 11) + [1, 1]
                + _bits_msb(can_id & 0x3FFFF, 18) + [0, 1])
    dyn += [0, brs, esi]
    dyn += _bits_msb(dlc, 4)
    for byte in data[:FD_DLC_BYTES[dlc]]:
        dyn += _bits_msb(byte, 8)
    gray = _gray3(stuff_count)
    return _bit_stuff(dyn) + gray + [gray[0] ^ gray[1] ^ gray[2]]


def _fd_crc_oracle(dyn_bits, crc_width):
    crc = 1 << (crc_width - 1)
    mask = (1 << crc_width) - 1
    for bit in dyn_bits:
        crcnext = bit ^ ((crc >> (crc_width - 1)) & 1)
        crc = (crc << 1) & mask
        if crcnext:
            crc ^= FD_CRC_POLY[crc_width]
    return crc


# (can_id, data, stuff_count, crc_width, true wire CRC) — edge-capture silicon goldens
GOLDENS = [
    (0x456, [0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88], 1, 17, 0x02012),
    (0x111, [0x00],                                           2, 17, 0x1B647),
    (0x7AB, list(range(1, 17)),                               4, 17, 0x07288),
    (0x123, list(range(20)),                                  7, 21, 0x0BBD35),
]

GOLDENS_EXT = [
    (0x456, [0xAA] * 20,                                      3, 21, 0x17EF8C, False, 1, 1),  # oracle: _fd_crc_oracle(_fd_crc_region(...), 21)
    (0x1A2B3C4, [0x55, 0xAA, 0xFF, 0x00, 0x11, 0x22, 0x33, 0x44], 2, 17, 0x1EE70, True, 0, 0),  # oracle: _fd_crc_oracle(_fd_crc_region(...), 17)
]


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
async def fd_crc_golden_no_false_error(dut):
    await setup(dut)

    # Collector: split the message stream into frame windows; at each FDAUX (emitted
    # right after the FD CRC coverage window closes) latch the computed FD CRC.
    frames = []     # list of dict(events=[payload...], computed17, computed21)
    cur = None
    async def collect():
        nonlocal cur
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.CAN_MSG_VLD.value) == 1 and int(dut.CAN_MSG_RDY.value) == 1:
                m = int(dut.CAN_MSG_OUT.value)
                assert devid(m) == DEV_ID, f"misrouted dev={devid(m)} m={m:#010x}"
                t, p = mtype(m), payload(m)
                if t == T_EVENT and p == EV_FRAME_BEGIN:
                    cur = dict(events=[], c17=None, c21=None)
                    frames.append(cur)
                if cur is not None:
                    if t == T_EVENT:
                        cur["events"].append(p)
                    if t == T_FDAUX:
                        cur["c17"] = int(dut.Crc17Computed.value)
                        cur["c21"] = int(dut.Crc21Computed.value)
    cocotb.start_soon(collect())

    bfm = CanBusBFM(dut.CAN_PINS, dut.CLK, prescaler=PRESCALER)

    # positive: every golden with its REAL wire CRC; then negatives (CRC ^ 1).
    drive = [(cid, data, sc, w, crc, False, 0, 0, True)
             for cid, data, sc, w, crc in GOLDENS]
    neg = GOLDENS[0]
    drive.append((neg[0], neg[1], neg[2], neg[3], neg[4] ^ 1, False, 0, 0, False))
    neg = GOLDENS[3]
    drive.append((neg[0], neg[1], neg[2], neg[3], neg[4] ^ 1, False, 0, 0, False))
    for cid, data, sc, w, crc, ide, brs, esi in GOLDENS_EXT:
        drive.append((cid, data, sc, w, crc, ide, brs, esi, True))
        drive.append((cid, data, sc, w, crc ^ 1, ide, brs, esi, False))

    for cid, data, sc, w, crc, ide, brs, esi, _good in drive:
        await bfm.idle(bits=12)
        if brs:   # a real CAN FD wire: dual-rate, switching at BRS (DS-P2.234)
            await bfm.send_fd_dualrate(data_bit_time=DATA_BIT_TIME, iso_brs=True,
                                       tx_sample_point=(0.75, 0.75), iso_delim=True,
                                       can_id=cid, data=data, ide=ide, esi=esi,
                                       stuff_count=sc, force_crc=crc)
        else:
            await bfm.send_fd(can_id=cid, data=data, ide=ide, brs=brs, esi=esi,
                              stuff_count=sc, force_crc=crc)
        await ClockCycles(dut.CLK, 28 * BIT_TIME)

    assert len(frames) >= len(drive), (
        f"expected {len(drive)} frame windows, got {len(frames)} — frames did not "
        f"all complete: {frames}")

    for i, (cid, data, sc, w, crc, ide, brs, esi, good) in enumerate(drive):
        fr = frames[i]
        computed = fr["c17"] if w == 17 else fr["c21"]
        has_err = EV_CRC_ERR in fr["events"]
        tag = f"id={cid:#05x} CRC-{w} wire=0x{crc:05x} computed={computed if computed is None else hex(computed)}"
        if good:
            assert computed == crc, (
                f"{tag}: datalogger COMPUTED a different FD CRC than the real wire CRC "
                f"— DS-T2.49 coverage is wrong (fixed stuff bits folded in?). Do NOT "
                f"loosen; fix the FD_CRC_EN / FdCrcDin coverage.")
            assert not has_err, (
                f"{tag}: spurious crc_error on a frame carrying its REAL CRC "
                f"(DS-T2.49 storm). events={[hex(e) for e in fr['events']]}")
            dut._log.info(f"PASS {tag} — no false crc_error, computed == wire golden")
        else:
            assert has_err, (
                f"{tag}: a frame with a 1-bit-corrupted CRC did NOT raise crc_error "
                f"— the verdict is broken/disabled (would mask real corruption).")
            dut._log.info(f"PASS {tag} (negative) — corrupted CRC correctly flagged")

    dut._log.info("PASS fd_crc_golden_no_false_error — FD CRC verdict matches the "
                  "silicon wire across CRC-17/21; DS-T2.49 coverage fixed.")


def main():
    run_simulation(
        top_level="can_datalogger",
        module="test_can_fd_crc_golden",
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
