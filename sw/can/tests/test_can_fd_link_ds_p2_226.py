"""DS-P2.226: the stdlib CAN FD link helper encodes exactly what the kernel reads.

The DE25 Linux image carries neither iproute2 nor can-utils (DS-P2.227), so
`ip link set canX type can ... fd on` and cansend are unavailable there.
sw/can/can_fd_link.py does the same two jobs with the Python stdlib only:
rtnetlink RTM_NEWLINK to set the nominal and data bit rates and CAN FD mode,
and AF_CAN CAN_RAW with CAN_RAW_FD_FRAMES to send and receive frames.

Every expected byte string below is written out by hand from the kernel
structures (include/uapi/linux/{netlink,rtnetlink,if_link}.h,
include/uapi/linux/can/{netlink,raw}.h, include/uapi/linux/can.h), never
computed by the module under test.
"""
from __future__ import annotations

import pytest

from sw.can import can_fd_link as L

# --- rtnetlink ---------------------------------------------------------------

# struct can_bittiming = 8 x u32: bitrate, sample_point, tq, prop_seg,
# phase_seg1, phase_seg2, sjw, brp. Only the bitrate is set: the kernel
# derives the rest. nlattr {u16 len=4+32=0x24, u16 type}.
BT_500K = bytes.fromhex("24000100" "20a10700") + bytes(28)        # IFLA_CAN_BITTIMING = 1
DBT_2M = bytes.fromhex("24000900" "80841e00") + bytes(28)         # IFLA_CAN_DATA_BITTIMING = 9
# struct can_ctrlmode {u32 mask, u32 flags}; CAN_CTRLMODE_FD = 0x20.
CTRL_FD = bytes.fromhex("0c000500" "20000000" "20000000")          # IFLA_CAN_CTRLMODE = 5
KIND_CAN = bytes.fromhex("08000100" "63616e00")                    # IFLA_INFO_KIND = 1, "can\0"
INFO_DATA = bytes.fromhex("58000200") + BT_500K + DBT_2M + CTRL_FD  # IFLA_INFO_DATA = 2, 4+84
LINKINFO = bytes.fromhex("64001200") + KIND_CAN + INFO_DATA         # IFLA_LINKINFO = 18, 4+96
# struct ifinfomsg {u8 family=AF_UNSPEC, u8 pad, u16 type, i32 index, u32 flags, u32 change}
IFI_IDX3 = bytes.fromhex("00" "00" "0000" "03000000" "00000000" "00000000")
# struct nlmsghdr {u32 len, u16 type=RTM_NEWLINK 16, u16 flags=REQUEST|ACK 5, u32 seq, u32 pid}
CONFIG_MSG = (bytes.fromhex("84000000" "1000" "0500" "07000000" "00000000")
              + IFI_IDX3 + LINKINFO)                                 # 16 + 16 + 100 = 132
UP_MSG = bytes.fromhex("20000000" "1000" "0500" "08000000" "00000000"
                       "00" "00" "0000" "03000000" "01000000" "01000000")  # IFF_UP set, change IFF_UP
DOWN_MSG = bytes.fromhex("20000000" "1000" "0500" "09000000" "00000000"
                         "00" "00" "0000" "03000000" "00000000" "01000000")


def test_bittiming_attribute_bytes():
    assert L.bittiming_attr(L.IFLA_CAN_BITTIMING, 500_000) == BT_500K


def test_data_bittiming_attribute_bytes():
    assert L.bittiming_attr(L.IFLA_CAN_DATA_BITTIMING, 2_000_000) == DBT_2M


def test_ctrlmode_fd_attribute_bytes():
    assert L.ctrlmode_attr(L.CAN_CTRLMODE_FD, L.CAN_CTRLMODE_FD) == CTRL_FD


def test_full_config_message_bytes():
    msg = L.newlink_config_msg(ifindex=3, bitrate=500_000, dbitrate=2_000_000, seq=7)
    assert msg == CONFIG_MSG
    assert len(msg) == 132


def test_link_up_and_down_message_bytes():
    assert L.newlink_updown_msg(ifindex=3, up=True, seq=8) == UP_MSG
    assert L.newlink_updown_msg(ifindex=3, up=False, seq=9) == DOWN_MSG


def test_nlattr_payload_is_padded_to_four_bytes():
    assert L.nlattr(1, b"ab") == bytes.fromhex("06000100") + b"ab" + b"\0\0"


def test_ack_parse_accepts_zero_and_raises_on_errno():
    ok = bytes.fromhex("24000000" "0200" "0000" "07000000" "00000000") + bytes(4) + CONFIG_MSG[:16]
    L.check_ack(ok, seq=7)                                           # NLMSG_ERROR, error 0
    einval = (bytes.fromhex("24000000" "0200" "0000" "07000000" "00000000")
              + (-22).to_bytes(4, "little", signed=True) + CONFIG_MSG[:16])
    with pytest.raises(OSError) as exc:
        L.check_ack(einval, seq=7)
    assert exc.value.errno == 22


# --- CAN FD frames -----------------------------------------------------------

# struct canfd_frame {u32 can_id, u8 len, u8 flags, u8 res0, u8 res1, u8 data[64]}
# CANFD_BRS = 0x01, CANFD_FDF = 0x04, CAN_EFF_FLAG = 0x80000000.
STD_BRS = bytes.fromhex("23010000" "04" "05" "00" "00" "deadbeef") + bytes(60)
EXT_NOBRS = bytes.fromhex("debc1a80" "0c" "04" "00" "00") + bytes(range(12)) + bytes(52)


def test_standard_id_brs_frame_bytes():
    assert L.encode_canfd_frame(0x123, bytes.fromhex("deadbeef"), brs=True) == STD_BRS


def test_extended_id_frame_without_brs_bytes():
    assert L.encode_canfd_frame(0x1ABCDE, bytes(range(12)), brs=False, ext=True) == EXT_NOBRS


def test_frame_is_72_bytes_even_when_empty():
    assert len(L.encode_canfd_frame(0x7FF, b"", brs=False)) == 72


@pytest.mark.parametrize("n", [9, 10, 11, 13, 33, 65])
def test_invalid_fd_lengths_are_refused(n):
    with pytest.raises(ValueError):
        L.encode_canfd_frame(0x123, bytes(n))


def test_out_of_range_ids_are_refused():
    with pytest.raises(ValueError):
        L.encode_canfd_frame(0x800, b"")                 # 11-bit standard id
    with pytest.raises(ValueError):
        L.encode_canfd_frame(0x2000_0000, b"", ext=True)  # 29-bit extended id


def test_decode_reads_back_what_was_encoded():
    assert L.decode_can_frame(STD_BRS) == {
        "id": 0x123, "ext": False, "len": 4, "fd": True, "brs": True, "esi": False,
        "data": "deadbeef"}
    assert L.decode_can_frame(EXT_NOBRS) == {
        "id": 0x1ABCDE, "ext": True, "len": 12, "fd": True, "brs": False, "esi": False,
        "data": bytes(range(12)).hex()}


def test_decode_classic_16_byte_frame():
    classic = bytes.fromhex("23010000" "02" "000000" "abcd") + bytes(6)
    assert L.decode_can_frame(classic) == {
        "id": 0x123, "ext": False, "len": 2, "fd": False, "brs": False, "esi": False,
        "data": "abcd"}


def test_default_sequence_covers_the_frame_shapes():
    seq = L.DEFAULT_SEQUENCE
    assert any(f["ext"] for f in seq) and any(not f["ext"] for f in seq)
    assert any(f["brs"] for f in seq) and any(not f["brs"] for f in seq)
    lengths = {len(bytes.fromhex(f["data"])) for f in seq}
    assert {0, 8, 64} <= lengths and lengths & {12, 16, 20, 24, 32, 48}


# --- exchange over injected sockets -----------------------------------------

class _Wire:
    """Two fake CAN_RAW sockets on one bus: what tx sends, rx receives."""

    def __init__(self, drop_last=False):
        self.queue, self.drop_last = [], drop_last

    def tx(self):
        wire = self

        class S:
            def send(self, buf):
                wire.queue.append(bytes(buf))
                return len(buf)

            def close(self):
                pass
        return S()

    def rx(self):
        wire = self

        class S:
            def recv(self, n):
                if wire.drop_last and len(wire.queue) == 1:
                    raise TimeoutError
                if not wire.queue:
                    raise TimeoutError
                return wire.queue.pop(0)

            def close(self):
                pass
        return S()


def test_exchange_matches_every_frame_sent():
    w = _Wire()
    res = L.exchange("can0", "can1", L.DEFAULT_SEQUENCE,
                     open_tx=lambda _n: w.tx(), open_rx=lambda _n: w.rx())
    assert res["ok"] is True
    assert len(res["sent"]) == len(res["received"]) == len(L.DEFAULT_SEQUENCE)
    assert res["sent"] == res["received"]


def test_exchange_reports_a_missing_frame():
    w = _Wire(drop_last=True)
    res = L.exchange("can0", "can1", L.DEFAULT_SEQUENCE,
                     open_tx=lambda _n: w.tx(), open_rx=lambda _n: w.rx())
    assert res["ok"] is False
    assert len(res["received"]) == len(L.DEFAULT_SEQUENCE) - 1
