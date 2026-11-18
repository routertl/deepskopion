"""CanReassembler unit tests (DS-P3.104).

Feed the per-message CAN zs_msg stream (the exact order can_frame_fsm emits:
frame_begin → control → id → data → crc → ack → frame_end) and assert the ONE
reassembled summary per frame is byte-exact. Hard-coded expecteds (ROUTERTL-002).
"""
from __future__ import annotations

import pytest

from sw.stream import decoders, zs_msg
from sw.stream.can_reassembler import CanReassembler

EV   = decoders.CAN_T_EVENT
ID   = decoders.CAN_T_ID
CTRL = decoders.CAN_T_CONTROL
DATA = decoders.CAN_T_DATA
CRC  = decoders.CAN_T_CRC

SOF      = decoders.CAN_EV_FRAME_BEGIN
EOF      = decoders.CAN_EV_FRAME_END
ACK_OBS  = decoders.CAN_EV_ACK_OBS
ACK_ERR  = decoders.CAN_EV_ACK_ERR
CRC_ERR  = decoders.CAN_EV_CRC_ERR
STUFF_ERR = decoders.CAN_EV_STUFF_ERR


def _hdr(et: int) -> zs_msg.ZsHeader:
    return zs_msg.make_header(datalogger_id=20, domain=zs_msg.DOMAIN_CAN,
                              event_id=et, severity=zs_msg.SEV_INFO, payload_len=4)


def _feed(reasm: CanReassembler, seq):
    """Feed (type, byte) pairs; return the list of emitted summaries."""
    out = []
    for et, b in seq:
        s = reasm.feed(_hdr(et), bytes([b & 0xFF, 0, 0, 0]))
        if s is not None:
            out.append(s)
    return out


# ── CAN-REQ-3: standard data frame ──────────────────────────────────────────
# ID=0x123, DLC=2, data=[0xAB,0xCD], CRC bytes 0xAA/0xAA → CRC=0x5555, ACKed.
_STD = [
    (EV, SOF), (CTRL, 0x02), (ID, 0x01), (ID, 0x23),
    (DATA, 0xAB), (DATA, 0xCD), (CRC, 0xAA), (CRC, 0xAA),
    (EV, ACK_OBS), (EV, EOF),
]


def test_standard_frame_reassembles_byte_exact():
    summaries = _feed(CanReassembler(), _STD)
    assert len(summaries) == 1, f"expected exactly one summary, got {len(summaries)}"
    s = summaries[0]
    assert s.kind == "can_frame"
    assert s.fields == {
        "id": 0x123, "ide": False, "rtr": False, "dlc": 2,
        "data": [0xAB, 0xCD], "crc": 0x5555, "ack": True, "errors": [],
        # uniform schema (DS-P2.64): classic frames carry FD keys at their defaults
        "fd": False, "brs": False, "esi": False, "data_len": 2,
        "stuff_count": None, "stuff_parity_ok": None,
    }
    assert s.text == "CAN id=0x123 STD DLC=2 data=[AB CD] CRC=0x5555 ACK"
    assert s.is_error is False


# ── CAN-REQ-4: extended (29-bit) frame ──────────────────────────────────────
# FullID=0x12345678, IDE=1, DLC=1, data=[0x5A]. control = 0x81. CRC bytes
# 0x24/0x68 → CRC=(0x24<<7)|(0x68>>1) = 0x1234.
_EXT = [
    (EV, SOF), (CTRL, 0x81),
    (ID, 0x12), (ID, 0x34), (ID, 0x56), (ID, 0x78),
    (DATA, 0x5A), (CRC, 0x24), (CRC, 0x68),
    (EV, ACK_OBS), (EV, EOF),
]


def test_extended_frame_reassembles():
    s = _feed(CanReassembler(), _EXT)[0]
    assert s.fields["id"] == 0x12345678 and s.fields["ide"] is True
    assert s.fields["dlc"] == 1 and s.fields["data"] == [0x5A]
    assert s.fields["crc"] == 0x1234 and s.fields["ack"] is True
    assert s.text == "CAN id=0x12345678 EXT DLC=1 data=[5A] CRC=0x1234 ACK"
    assert s.is_error is False


# ── CAN-REQ-6: remote frame (RTR=1) — no data ───────────────────────────────
# ID=0x7A0, RTR=1, DLC=2 (but a remote frame carries no data). control = 0x42.
_REMOTE = [
    (EV, SOF), (CTRL, 0x42), (ID, 0x07), (ID, 0xA0),
    (CRC, 0x1E), (CRC, 0x1E), (EV, ACK_OBS), (EV, EOF),
]


def test_remote_frame_no_data():
    s = _feed(CanReassembler(), _REMOTE)[0]
    assert s.fields["id"] == 0x7A0 and s.fields["rtr"] is True
    assert s.fields["dlc"] == 2 and s.fields["data"] == []
    assert "RTR" in s.text and "data=" not in s.text


# ── CAN-REQ-7: corrupted CRC → crc_error noted, frame still completes ────────
def test_crc_error_frame_flags_error():
    seq = [
        (EV, SOF), (CTRL, 0x02), (ID, 0x01), (ID, 0x23),
        (DATA, 0xAB), (DATA, 0xCD), (CRC, 0xAA), (CRC, 0xAA),
        (EV, CRC_ERR), (EV, ACK_OBS), (EV, EOF),
    ]
    s = _feed(CanReassembler(), seq)[0]
    assert s.fields["errors"] == ["crc_error"]
    assert s.is_error is True and "!crc_error" in s.text


# ── CAN-REQ-9: un-ACKed frame → ack_error, ack False ────────────────────────
def test_ack_error_frame():
    seq = [
        (EV, SOF), (CTRL, 0x02), (ID, 0x01), (ID, 0x23),
        (DATA, 0xAB), (DATA, 0xCD), (CRC, 0xAA), (CRC, 0xAA),
        (EV, ACK_ERR), (EV, EOF),
    ]
    s = _feed(CanReassembler(), seq)[0]
    assert s.fields["ack"] is False and s.fields["errors"] == ["ack_error"]
    assert "noACK" in s.text and s.is_error is True


# ── CAN-REQ-8: stuff error ABORTS the frame (no frame_end) ───────────────────
def test_stuff_error_aborts_frame():
    seq = [
        (EV, SOF), (CTRL, 0x02), (ID, 0x01),
        (EV, STUFF_ERR),                      # FSM aborts to idle — no EOF
    ]
    summaries = _feed(CanReassembler(), seq)
    assert len(summaries) == 1, "stuff error must emit an aborted summary"
    s = summaries[0]
    assert "stuff_error" in s.fields["errors"] and s.is_error is True


# ── CAN-REQ-10: back-to-back frames ─────────────────────────────────────────
def test_back_to_back_frames():
    reasm = CanReassembler()
    summaries = _feed(reasm, _STD + _EXT)
    assert len(summaries) == 2
    assert summaries[0].fields["id"] == 0x123
    assert summaries[1].fields["id"] == 0x12345678


# ── desync: a dropped frame_end (new SOF mid-frame) flushes the stale partial ─
def test_dropped_end_flushes_partial_on_next_sof():
    reasm = CanReassembler()
    seq = [
        (EV, SOF), (CTRL, 0x02), (ID, 0x01), (ID, 0x23),   # frame 1, no EOF
        (EV, SOF),                                          # frame 2 SOF → flush
    ]
    summaries = _feed(reasm, seq)
    assert len(summaries) == 1, "the stale partial must flush on the new SOF"
    assert "desync" in summaries[0].fields["errors"]
    assert reasm.desync_count == 1


# ── field bytes before any SOF are ignored ──────────────────────────────────
def test_bytes_before_sof_ignored():
    reasm = CanReassembler()
    summaries = _feed(reasm, [(ID, 0xFF), (DATA, 0x11), (CRC, 0x22)])
    assert summaries == []


# ════════════════════════════ CAN-FD (DS-P2.64) ════════════════════════════
FD_CTRL = decoders.CAN_T_FD_CTRL
FD_AUX  = decoders.CAN_T_FD_AUX
STUFF_CNT_ERR = decoders.CAN_EV_STUFF_CNT_ERR


# ── CAN-REQ-12/13/16/19: standard FD, BRS=1, DLC=3 (CRC-17) ──────────────────
# fd_control = {IDE0,BRS1,ESI0,0,DLC0011}=0x43; id 0x123 → 0x01,0x23; 3 data bytes;
# fd_aux = {parity_ok1,0000,gray000}=0x80; CRC-17 0x1ABCD → bytes 0x01,0xAB,0xCD.
def test_fd_standard_brs_crc17():
    seq = [
        (EV, SOF), (FD_CTRL, 0x43), (ID, 0x01), (ID, 0x23),
        (DATA, 0xDE), (DATA, 0xAD), (DATA, 0xBE),
        (FD_AUX, 0x80), (CRC, 0x01), (CRC, 0xAB), (CRC, 0xCD),
        (EV, ACK_OBS), (EV, EOF),
    ]
    s = _feed(CanReassembler(), seq)[0]
    assert s.kind == "canfd_frame"  # LT-P3.216: FD frames carry their own kind
    assert s.fields["fd"] is True and s.fields["brs"] is True and s.fields["esi"] is False
    assert s.fields["id"] == 0x123 and s.fields["ide"] is False and s.fields["dlc"] == 3
    assert s.fields["data"] == [0xDE, 0xAD, 0xBE] and s.fields["data_len"] == 3
    assert s.fields["crc"] == 0x1ABCD          # 17-bit
    assert s.fields["stuff_count"] == 0 and s.fields["stuff_parity_ok"] is True
    assert s.fields["ack"] is True and s.fields["errors"] == []
    assert s.text == "FD id=0x123 STD BRS DLC=3 data=[DE AD BE] CRC=0x1ABCD ACK"
    assert s.is_error is False


# ── CAN-REQ-16/20: FD DLC=0xB → 20 bytes, CRC-21 ────────────────────────────
# fd_control = {IDE0,BRS0,ESI1,0,DLC1011}=0x2B; CRC-21 0x102233 → 0x10,0x22,0x33.
def test_fd_20byte_crc21():
    data = [(i + 1) & 0xFF for i in range(20)]
    seq = ([(EV, SOF), (FD_CTRL, 0x2B), (ID, 0x01), (ID, 0x10)]
           + [(DATA, d) for d in data]
           + [(FD_AUX, 0x80), (CRC, 0x10), (CRC, 0x22), (CRC, 0x33),
              (EV, ACK_OBS), (EV, EOF)])
    s = _feed(CanReassembler(), seq)[0]
    assert s.kind == "canfd_frame"  # LT-P3.216
    assert s.fields["fd"] is True and s.fields["esi"] is True and s.fields["brs"] is False
    assert s.fields["dlc"] == 0xB and s.fields["data_len"] == 20
    assert len(s.fields["data"]) == 20 and s.fields["data"] == data
    assert s.fields["crc"] == 0x102233         # 21-bit
    assert "CRC=0x102233" in s.text and "ESI" in s.text and s.is_error is False


# ── CAN-REQ-18: FD stuff-count parity mismatch → stuff_count_error ──────────
def test_fd_stuff_count_parity_error():
    seq = [
        (EV, SOF), (FD_CTRL, 0x41), (ID, 0x01), (ID, 0x23), (DATA, 0x77),
        (FD_AUX, 0x02),                         # parity_ok=0 (bit7=0), gray=010 → count 3
        (EV, STUFF_CNT_ERR),
        (CRC, 0x01), (CRC, 0x23), (CRC, 0x45),
        (EV, ACK_OBS), (EV, EOF),
    ]
    s = _feed(CanReassembler(), seq)[0]
    assert s.kind == "canfd_frame"  # LT-P3.216: error FD frame still keeps the FD kind
    assert s.fields["fd"] is True
    # gray 010 decodes to count 3 (NOT raw 2) — DS-P3.107.
    assert s.fields["stuff_parity_ok"] is False and s.fields["stuff_count"] == 3
    assert "stuff_count_error" in s.fields["errors"] and s.is_error is True


# ── DS-P3.107: the fd_aux stuff-count field is GRAY-coded on the wire; the host
#    must gray-decode it. Reading it raw reported gray bits as the count
#    (sc 2→0b011→3, sc 4→0b110→6) — misattributed to a silicon fixed-stuff
#    capture defect until the REA trace proved the RTL emits correct gray. These
#    fd_aux bytes (0x81/0x83/0x86) are the EXACT values captured off silicon for
#    the 456/111/7AB anchors (true sc 1/2/4). Reading raw would give 1/3/6.
@pytest.mark.parametrize("aux_byte,expected_sc", [
    (0x81, 1),   # parity_ok=1, gray=001  (456 anchor)
    (0x83, 2),   # parity_ok=1, gray=011  (111 anchor — raw would read 3)
    (0x86, 4),   # parity_ok=1, gray=110  (7AB anchor — raw would read 6)
    (0x80, 0),   # gray=000 → 0
    (0x85, 6),   # gray=101 → 6 (raw would read 5)
    (0x84, 7),   # gray=100 → 7 (raw would read 4)
])
def test_fd_stuff_count_gray_decoded(aux_byte, expected_sc):
    seq = [
        (EV, SOF), (FD_CTRL, 0x41), (ID, 0x01), (ID, 0x23), (DATA, 0x77),
        (FD_AUX, aux_byte),
        (CRC, 0x01), (CRC, 0x23), (CRC, 0x45),
        (EV, ACK_OBS), (EV, EOF),
    ]
    s = _feed(CanReassembler(), seq)[0]
    assert s.fields["stuff_count"] == expected_sc, (
        f"fd_aux 0x{aux_byte:02x}: stuff_count={s.fields['stuff_count']} "
        f"expected {expected_sc} (gray-decoded, not raw {aux_byte & 0x07})")
    assert s.fields["stuff_parity_ok"] is True


# ── mixed classic + FD on one bus, back to back (CAN-REQ-12 no bleed) ────────
def test_classic_then_fd_back_to_back():
    fd = [
        (EV, SOF), (FD_CTRL, 0x43), (ID, 0x01), (ID, 0x23),
        (DATA, 0xDE), (DATA, 0xAD), (DATA, 0xBE),
        (FD_AUX, 0x80), (CRC, 0x01), (CRC, 0xAB), (CRC, 0xCD),
        (EV, ACK_OBS), (EV, EOF),
    ]
    summaries = _feed(CanReassembler(), _STD + fd)
    assert len(summaries) == 2
    # LT-P3.216: kind tracks FD-ness per frame with no bleed across the pair
    assert summaries[0].kind == "can_frame" and summaries[1].kind == "canfd_frame"
    assert summaries[0].fields["fd"] is False and summaries[0].fields["id"] == 0x123
    assert summaries[1].fields["fd"] is True and summaries[1].fields["data_len"] == 3
