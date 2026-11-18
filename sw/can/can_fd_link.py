"""Configure a SocketCAN CAN FD link and exchange frames, with the stdlib only.

The DE25 Linux image ships neither iproute2 nor can-utils (DS-P2.227), and
busybox's `ip` cannot configure a CAN link, so this module does their two jobs
on any board that runs Python (DS-P2.226):

* ``setup``: rtnetlink RTM_NEWLINK, the equivalent of
  ``ip link set <if> down; ip link set <if> type can bitrate B dbitrate D fd on;
  ip link set <if> up``. Only the bit rates are sent; the kernel derives the
  timing segments, as iproute2 does when given ``bitrate``.
* ``exchange``: AF_CAN CAN_RAW with CAN_RAW_FD_FRAMES; sends a frame sequence on
  one interface and reads it back on another on the same bus, the equivalent of
  cansend + candump.
* ``arm-datalogger``: reads the link's bit timing back (RTM_GETLINK, the values
  ``ip -details link show`` prints) and programs the CAN datalogger with the bus's
  nominal and data sample points (1/256 of a bit, round to nearest) and data_cyc
  (DS-T2.97). A CAN FD transmitter switches to the data rate at its own sample
  point; Linux sets 87.5 % at 250 kbit/s, and a datalogger left at 75 % reads every
  field after BRS one bit late. Run it after ``setup``.

Usage on the board (250 kbit/s nominal, docs/hardware/BRING_UP_CAN.md:64)::

    # The datalogger arms only at 250 kbit/s nominal: 500k is not representable at 16 TQ.
    python3 -m sw.can.can_fd_link setup can0 --bitrate 250000 --dbitrate 2000000
    python3 -m sw.can.can_fd_link setup can1 --bitrate 250000 --dbitrate 2000000
    python3 -m sw.can.can_fd_link arm-datalogger can0
    python3 -m sw.can.can_fd_link exchange --tx can0 --rx can1

``exchange`` prints one JSON object: the frames sent, the frames received and
``ok`` (every sent frame received, in order, byte-identical).

Layouts: include/uapi/linux/{netlink,rtnetlink,if_link}.h,
include/uapi/linux/can/{netlink,raw}.h, include/uapi/linux/can.h.
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
from typing import Callable

# netlink / rtnetlink
NLMSG_ERROR = 2
RTM_NEWLINK = 16
RTM_GETLINK = 18
NLM_F_REQUEST = 0x1
NLM_F_ACK = 0x4
IFF_UP = 0x1
IFLA_LINKINFO = 18
IFLA_INFO_KIND = 1
IFLA_INFO_DATA = 2
# linux/can/netlink.h
IFLA_CAN_BITTIMING = 1
IFLA_CAN_CTRLMODE = 5
IFLA_CAN_DATA_BITTIMING = 9    # 10 is IFLA_CAN_DATA_BITTIMING_CONST (DS-T2.91)
CAN_CTRLMODE_FD = 0x20
# linux/can.h
CAN_EFF_FLAG = 0x8000_0000
CAN_RTR_FLAG = 0x4000_0000
CAN_ERR_FLAG = 0x2000_0000
CAN_SFF_MASK = 0x7FF
CAN_EFF_MASK = 0x1FFF_FFFF
CANFD_BRS = 0x01
CANFD_ESI = 0x02
CANFD_FDF = 0x04
CANFD_MTU = 72
CAN_MTU = 16
FD_LENGTHS = frozenset([*range(9), 12, 16, 20, 24, 32, 48, 64])

# A fixed, known sequence: standard and extended ids, with and without BRS,
# and lengths 0, 8, 12, 64 (classic-length and FD-only lengths).
DEFAULT_SEQUENCE = [
    {"id": 0x123, "ext": False, "brs": True, "data": "deadbeef0badf00d"},
    {"id": 0x456, "ext": False, "brs": False, "data": bytes(range(12)).hex()},
    {"id": 0x1ABCDE, "ext": True, "brs": True, "data": bytes(range(64)).hex()},
    {"id": 0x7A0, "ext": False, "brs": True, "data": ""},
    {"id": 0x1F00_0001, "ext": True, "brs": False, "data": "55aa55aa55aa55aa"},
]


# --- rtnetlink encoding ------------------------------------------------------

def nlattr(attr_type: int, payload: bytes) -> bytes:
    """One struct nlattr plus its payload, padded to a 4-byte boundary."""
    length = 4 + len(payload)
    return struct.pack("<HH", length, attr_type) + payload + bytes((-length) % 4)


def bittiming_attr(attr_type: int, bitrate: int, sample_point: int = 0) -> bytes:
    """struct can_bittiming with only bitrate (and optionally sample_point) set."""
    return nlattr(attr_type, struct.pack("<8I", bitrate, sample_point, 0, 0, 0, 0, 0, 0))


def ctrlmode_attr(mask: int, flags: int) -> bytes:
    return nlattr(IFLA_CAN_CTRLMODE, struct.pack("<II", mask, flags))


def _ifinfomsg(ifindex: int, flags: int = 0, change: int = 0) -> bytes:
    return struct.pack("<BBHiII", socket.AF_UNSPEC, 0, 0, ifindex, flags, change)


def _nlmsg(body: bytes, seq: int) -> bytes:
    return struct.pack("<IHHII", 16 + len(body), RTM_NEWLINK,
                       NLM_F_REQUEST | NLM_F_ACK, seq, 0) + body


def newlink_config_msg(ifindex: int, bitrate: int, dbitrate: int, seq: int,
                       sample_point: int = 0, dsample_point: int = 0) -> bytes:
    """RTM_NEWLINK setting nominal + data bit rates and CAN FD mode on a down link."""
    data = (bittiming_attr(IFLA_CAN_BITTIMING, bitrate, sample_point)
            + bittiming_attr(IFLA_CAN_DATA_BITTIMING, dbitrate, dsample_point)
            + ctrlmode_attr(CAN_CTRLMODE_FD, CAN_CTRLMODE_FD))
    linkinfo = nlattr(IFLA_LINKINFO,
                      nlattr(IFLA_INFO_KIND, b"can\0") + nlattr(IFLA_INFO_DATA, data))
    return _nlmsg(_ifinfomsg(ifindex) + linkinfo, seq)


def newlink_updown_msg(ifindex: int, up: bool, seq: int) -> bytes:
    return _nlmsg(_ifinfomsg(ifindex, IFF_UP if up else 0, IFF_UP), seq)


def check_ack(reply: bytes, seq: int) -> None:
    """Raise OSError unless *reply* holds an NLMSG_ERROR ack with error 0 for *seq*."""
    off = 0
    while off + 16 <= len(reply):
        length, mtype, _flags, rseq, _pid = struct.unpack_from("<IHHII", reply, off)
        if length < 16:
            break
        if mtype == NLMSG_ERROR and rseq == seq:
            (err,) = struct.unpack_from("<i", reply, off + 16)
            if err:
                raise OSError(-err, f"rtnetlink seq {seq}: errno {-err}")
            return
        off += (length + 3) & ~3
    raise OSError(0, f"rtnetlink seq {seq}: no ack in reply")


def _rtnl(msgs: list[bytes]) -> None:
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as s:
        s.settimeout(5)
        s.bind((0, 0))
        for msg in msgs:
            s.send(msg)
            check_ack(s.recv(65536), struct.unpack_from("<I", msg, 8)[0])


_BT_FIELDS = ("bitrate", "sample_point", "tq", "prop_seg", "phase_seg1", "phase_seg2", "sjw", "brp")


def getlink_msg(ifindex: int, seq: int) -> bytes:
    """RTM_GETLINK for one interface."""
    body = _ifinfomsg(ifindex)
    return struct.pack("<IHHII", 16 + len(body), RTM_GETLINK, NLM_F_REQUEST, seq, 0) + body


def _attrs(buf: bytes, off: int, end: int) -> dict:
    out = {}
    while off + 4 <= end:
        length, atype = struct.unpack_from("<HH", buf, off)
        if length < 4:
            break
        out[atype & 0x3FFF] = buf[off + 4:off + length]
        off += (length + 3) & ~3
    return out


def parse_link_bittiming(reply: bytes) -> dict:
    """Nominal and data struct can_bittiming from an RTM_NEWLINK reply (sample points
    in tenths of a percent, as the kernel reports them; tq in ns)."""
    length, mtype = struct.unpack_from("<IH", reply, 0)
    if mtype == NLMSG_ERROR:
        (err,) = struct.unpack_from("<i", reply, 16)
        raise OSError(-err, f"RTM_GETLINK: errno {-err}")
    if mtype != RTM_NEWLINK:
        raise OSError(0, f"RTM_GETLINK: unexpected reply type {mtype}")
    top = _attrs(reply, 32, length)
    info = _attrs(top[IFLA_LINKINFO], 0, len(top[IFLA_LINKINFO])) if IFLA_LINKINFO in top else {}
    data = info.get(IFLA_INFO_DATA)
    if data is None:
        raise OSError(0, "RTM_GETLINK: no CAN link data (not a CAN interface?)")
    can = _attrs(data, 0, len(data))
    out = {}
    for prefix, attr in (("", IFLA_CAN_BITTIMING), ("d", IFLA_CAN_DATA_BITTIMING)):
        if attr not in can:
            raise OSError(0, f"RTM_GETLINK: no {'data ' if prefix else ''}bit timing")
        for k, v in zip(_BT_FIELDS, struct.unpack_from("<8I", can[attr], 0)):
            out[prefix + k] = v
    return out


def link_bittiming(ifname: str) -> dict:
    """The bit timing the kernel holds for *ifname* (``ip -details link show``)."""
    idx = socket.if_nametoindex(ifname)
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as s:
        s.settimeout(5)
        s.bind((0, 0))
        s.send(getlink_msg(idx, 1))
        return parse_link_bittiming(s.recv(65536))


DATALOGGER_CLK_HZ = 100_000_000   # the CAN datalogger's clk_sys (targets: ds_clk_hz)


def frac256(tenths_of_percent: int) -> int:
    """A sample point in tenths of a percent (875) as a fraction of a bit in 1/256,
    rounded to nearest (224)."""
    return (tenths_of_percent * 256 + 500) // 1000


def arm_datalogger(ifname: str, regbank, bittiming: Callable[[str], dict] = link_bittiming) -> dict:
    """Program the CAN datalogger with *ifname*'s sample points and data rate (DS-T2.97)."""
    from sw.stream.data_source import set_can_sample_points
    bt = bittiming(ifname)
    sp, dsp = frac256(bt["sample_point"]), frac256(bt["dsample_point"])
    data_cyc = round(DATALOGGER_CLK_HZ / bt["dbitrate"])
    err = set_can_sample_points(regbank, sp, dsp, data_cyc)
    if err:
        raise ValueError(f"arm-datalogger {ifname}: {err}")
    return {"if": ifname, "sample_point": bt["sample_point"] / 1000, "dsample_point": bt["dsample_point"] / 1000,
            "nominal_sp_256": sp, "data_sp_256": dsp, "data_cyc": data_cyc,
            "bitrate": bt["bitrate"], "dbitrate": bt["dbitrate"]}


def setup(ifname: str, bitrate: int, dbitrate: int) -> dict:
    """Bring *ifname* down, set the CAN FD bit rates, bring it up."""
    idx = socket.if_nametoindex(ifname)
    _rtnl([newlink_updown_msg(idx, False, 1),
           newlink_config_msg(idx, bitrate, dbitrate, 2),
           newlink_updown_msg(idx, True, 3)])
    return {"if": ifname, "ifindex": idx, "bitrate": bitrate, "dbitrate": dbitrate, "fd": True}


# --- CAN frames -------------------------------------------------------------

def encode_canfd_frame(can_id: int, data: bytes, brs: bool = True, ext: bool = False) -> bytes:
    """struct canfd_frame (72 bytes) for a CAN FD data frame."""
    if len(data) not in FD_LENGTHS:
        raise ValueError(f"CAN FD length {len(data)} is not one of {sorted(FD_LENGTHS)}")
    limit = CAN_EFF_MASK if ext else CAN_SFF_MASK
    if not 0 <= can_id <= limit:
        raise ValueError(f"id {can_id:#x} out of range for a {'29' if ext else '11'}-bit id")
    word = can_id | (CAN_EFF_FLAG if ext else 0)
    flags = CANFD_FDF | (CANFD_BRS if brs else 0)
    return struct.pack("<IBBBB", word, len(data), flags, 0, 0) + data + bytes(64 - len(data))


def decode_can_frame(buf: bytes) -> dict:
    """Decode a struct canfd_frame (72 B) or struct can_frame (16 B)."""
    if len(buf) not in (CANFD_MTU, CAN_MTU):
        raise ValueError(f"unexpected CAN frame size {len(buf)}")
    word, length, flags = struct.unpack_from("<IBB", buf, 0)
    fd = len(buf) == CANFD_MTU
    ext = bool(word & CAN_EFF_FLAG)
    return {
        "id": word & (CAN_EFF_MASK if ext else CAN_SFF_MASK),
        "ext": ext,
        "len": length,
        "fd": fd,
        "brs": fd and bool(flags & CANFD_BRS),
        "esi": fd and bool(flags & CANFD_ESI),
        "data": buf[8:8 + length].hex(),
    }


def _frame_record(f: dict) -> dict:
    data = bytes.fromhex(f["data"])
    return decode_can_frame(encode_canfd_frame(f["id"], data, f["brs"], f["ext"]))


def _open_raw(ifname: str, timeout: float = 2.0):
    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FD_FRAMES, 1)
    s.settimeout(timeout)
    s.bind((ifname,))
    return s


def exchange(tx: str, rx: str, frames: list[dict],
             open_tx: Callable = _open_raw, open_rx: Callable = _open_raw) -> dict:
    """Send *frames* on *tx*, read them back on *rx*; ``ok`` iff all match in order."""
    rsock = open_rx(rx)            # bound before the first send, so nothing is missed
    tsock = open_tx(tx)
    sent, received = [], []
    try:
        for f in frames:
            tsock.send(encode_canfd_frame(f["id"], bytes.fromhex(f["data"]), f["brs"], f["ext"]))
            sent.append(_frame_record(f))
        for _ in frames:
            try:
                received.append(decode_can_frame(rsock.recv(CANFD_MTU)))
            except (TimeoutError, socket.timeout):
                break
    finally:
        tsock.close()
        rsock.close()
    return {"tx": tx, "rx": rx, "sent": sent, "received": received,
            "ok": len(received) == len(sent) and received == sent}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="can_fd_link", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="set CAN FD bit rates and bring the link up")
    s.add_argument("ifname")
    s.add_argument("--bitrate", type=int, default=250_000)   # BRING_UP_CAN.md:64
    s.add_argument("--dbitrate", type=int, default=2_000_000)
    e = sub.add_parser("exchange", help="send the default sequence and read it back")
    e.add_argument("--tx", default="can0")
    e.add_argument("--rx", default="can1")
    d = sub.add_parser("arm-datalogger",
                       help="program the CAN datalogger with the link's sample points (DS-T2.97)")
    d.add_argument("ifname")
    a = ap.parse_args(argv)
    if a.cmd == "setup":
        print(json.dumps(setup(a.ifname, a.bitrate, a.dbitrate)))
        return 0
    if a.cmd == "arm-datalogger":
        from sw.stream.regbank import MmioRegbank
        res = arm_datalogger(a.ifname, MmioRegbank())
        print("CAN datalogger sample points programmed: " + json.dumps(res))
        return 0
    res = exchange(a.tx, a.rx, DEFAULT_SEQUENCE)
    print(json.dumps(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
