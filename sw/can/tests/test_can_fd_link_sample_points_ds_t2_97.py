# rr unknown-llm (AI-generated)
"""DS-T2.97: can_fd_link reads the link's bit timing back and programs the CAN
datalogger with the bus's sample points.

The reply literal is the bit timing read on the Zybo-Z7-20 after `setup can0
--bitrate 250000 --dbitrate 2000000` (2026-11-10, `ip -details link show can0`):
bitrate 249999, sample-point 0.875, tq 20, prop-seg 111, phase-seg1 63, phase-seg2
25, sjw 12, brp 2; dbitrate 1999999, dsample-point 0.740, dtq 10, dprop-seg 18,
dphase-seg1 18, dphase-seg2 13, dsjw 6, dbrp 1.
"""
import struct

import pytest

from sw.can import can_fd_link as L
from sw.stream.regbank import Regs

NOMINAL = (249999, 875, 20, 111, 63, 25, 12, 2)
DATA = (1999999, 740, 10, 18, 18, 13, 6, 1)


def _reply(nominal=NOMINAL, data=DATA, mtype=L.RTM_NEWLINK):
    can = (L.nlattr(L.IFLA_CAN_BITTIMING, struct.pack("<8I", *nominal))
           + L.nlattr(L.IFLA_CAN_DATA_BITTIMING, struct.pack("<8I", *data)))
    linkinfo = L.nlattr(L.IFLA_LINKINFO, L.nlattr(L.IFLA_INFO_KIND, b"can\0") + L.nlattr(L.IFLA_INFO_DATA, can))
    body = struct.pack("<BBHiII", 0, 0, 0, 2, 0, 0) + L.nlattr(3, b"can0\0") + linkinfo
    return struct.pack("<IHHII", 16 + len(body), mtype, 0, 1, 0) + body


def test_parse_link_bittiming_reads_both_timings():
    bt = L.parse_link_bittiming(_reply())
    assert (bt["bitrate"], bt["sample_point"], bt["tq"], bt["prop_seg"], bt["phase_seg1"],
            bt["phase_seg2"], bt["sjw"], bt["brp"]) == NOMINAL
    assert (bt["dbitrate"], bt["dsample_point"], bt["dtq"], bt["dprop_seg"], bt["dphase_seg1"],
            bt["dphase_seg2"], bt["dsjw"], bt["dbrp"]) == DATA


def test_parse_link_bittiming_error_reply_raises():
    err = struct.pack("<IHHII", 36, L.NLMSG_ERROR, 0, 1, 0) + struct.pack("<i", -19) + bytes(16)
    with pytest.raises(OSError):
        L.parse_link_bittiming(err)


@pytest.mark.parametrize("tenths, frac", [(875, 224), (740, 189), (750, 192), (800, 205), (1000, 256)])
def test_frac256_rounds_to_nearest(tenths, frac):
    assert L.frac256(tenths) == frac


class _Recorder:
    def __init__(self):
        self.words = []

    def write_u32(self, off, val):
        if off == Regs.DATALOGGER_CONFIG:
            self.words.append(val)


def test_arm_datalogger_programs_bus_sample_points_then_data_cyc():
    rec = _Recorder()
    res = L.arm_datalogger("can0", rec, bittiming=lambda _if: L.parse_link_bittiming(_reply()))
    assert rec.words == [(20 << 11) | 0x400 | 0x200 | 224,
                         (20 << 11) | 0x400 | 0x200 | 0x100 | 189,
                         (20 << 11) | 0x400 | 50]
    assert (res["nominal_sp_256"], res["data_sp_256"], res["data_cyc"]) == (224, 189, 50)
    assert (res["sample_point"], res["dsample_point"]) == (0.875, 0.74)


def test_arm_datalogger_refuses_out_of_range_timing():
    rec = _Recorder()
    slow = (100000, 875, 20, 111, 63, 25, 12, 2)            # data 100 kbit/s → data_cyc 1000 > 511
    with pytest.raises(ValueError):
        L.arm_datalogger("can0", rec, bittiming=lambda _if: L.parse_link_bittiming(_reply(data=slow)))
    assert rec.words == []
