# rr unknown-llm (AI-generated)
"""Stateful CAN frame reassembler (DS-P3.104).

The 1:1 :func:`decoders.decode_can` annotates each individual ID / control /
data / CRC byte and lifecycle event. This module is the *cross-frame* layer: it
consumes the same CAN zs_msg frames in capture order and reassembles ONE summary
annotation per complete frame (``frame_begin .. frame_end``), with the decoded
arbitration ID, IDE/RTR, DLC, data bytes, CRC-15 and ACK status — the
N:1 counterpart to the 1:1 decoder (mirrors jtag_reassembler).

Wire layout (matches can_datalogger_protocol.yml, MSB-first):
  * STANDARD ID = 2 id bytes: byte0 = ``00000|id[10:8]``, byte1 = ``id[7:0]``
    → ``id = (byte0 & 0x07) << 8 | byte1``  (11-bit).
  * EXTENDED ID = 4 id bytes: byte0 = ``000|id[28:24]``, byte1..3 = the rest
    → ``id = (byte0 & 0x1F) << 24 | byte1 << 16 | byte2 << 8 | byte3`` (29-bit).
    IDE (from the control byte) selects 2 vs 4 id bytes.
  * CRC-15 = 2 crc bytes: byte0 = ``CRC[14:7]``, byte1 = ``CRC[6:0] << 1``
    → ``crc = (byte0 << 7) | (byte1 >> 1)``  (15-bit).

Abort vs complete: crc_error / ack_error / form_error are NOTED and the frame
still reaches frame_end (the FSM continues). stuff_error ABORTS the frame to
idle (no frame_end), so it emits an aborted-frame summary directly. A
frame_begin arriving while a frame is still open (a dropped frame_end under a
flooded ring) flushes the stale partial as aborted before starting the new one.
"""
from __future__ import annotations

from typing import List, Optional

from . import zs_msg
from .decoders import (
    DecodedFrame,
    CAN_T_ID, CAN_T_DATA, CAN_T_CONTROL, CAN_T_EVENT, CAN_T_CRC,
    CAN_T_FD_CTRL, CAN_T_FD_AUX,
    CAN_EV_FRAME_BEGIN, CAN_EV_FRAME_END, CAN_EV_ACK_OBS, CAN_EV_ACK_ERR,
    CAN_EV_CRC_ERR, CAN_EV_STUFF_ERR, CAN_EV_FORM_ERR, CAN_EV_STUFF_CNT_ERR,
)
# CAN-FD DLC → data-byte count (the non-linear table, ISO 11898-1:2015 / Bosch FD).
# DLC 0..8 = that many; 9..15 = 12/16/20/24/32/48/64. A pure host-decode constant —
# it is NOT in any *_datalogger_protocol.yml (the HW never decodes DLC→len), so it lives
# here with its consumer rather than in the generated protocol_codes.py (the drift
# gate forbids hand-edits there).
CAN_FD_DLC_BYTES = {**{i: i for i in range(9)},
                    9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64}

_FRAME_COLOR   = "#3a7ca8"   # blue — a clean reassembled frame
_ERROR_COLOR   = "#cc3333"   # red  — a frame carrying a fault
_ABORT_COLOR   = "#cc8833"   # amber — aborted (stuff error / lost end)

# Sanity bounds — a healthy classic-CAN frame never exceeds these. Beyond them
# the stream has desynced (dropped/overwritten frames under a wrapping ring);
# we stop growing and flag the summary rather than emit an impossible frame.
_MAX_ID_BYTES   = 4
_MAX_DATA_BYTES = 8       # classic; CAN-FD raises this to 64 (see _data_cap)
_MAX_CRC_BYTES  = 2       # classic CRC-15; CAN-FD CRC-17/21 = 3 bytes


class CanReassembler:
    """Feed it every CAN zs_msg frame in order; it returns a summary
    :class:`DecodedFrame` when a frame completes (or aborts), else ``None``."""

    def __init__(self) -> None:
        self._reset()
        self._desync_count = 0

    @property
    def desync_count(self) -> int:
        return self._desync_count

    def _reset(self) -> None:
        self._active = False
        self._id_bytes: List[int] = []
        self._data_bytes: List[int] = []
        self._crc_bytes: List[int] = []
        self._ide = False
        self._rtr = False
        self._dlc = 0
        self._have_control = False
        self._ack: Optional[bool] = None
        self._errors: List[str] = []
        self._overflow = False
        # ── CAN-FD (DS-P2.64) ──
        self._is_fd = False
        self._brs = False
        self._esi = False
        self._fd_data_len = 0            # expected bytes from the FD DLC table
        self._stuff_count: Optional[int] = None   # decoded count (gray-decoded from wire)
        self._stuff_parity_ok: Optional[bool] = None

    # CAN-FD CRC-17 (<=16 B) vs CRC-21 (>16 B) — selected by payload length.
    @property
    def _crc_is21(self) -> bool:
        return self._is_fd and self._fd_data_len > 16

    @property
    def _data_cap(self) -> int:
        return 64 if self._is_fd else _MAX_DATA_BYTES

    @property
    def _crc_cap(self) -> int:
        return 3 if self._is_fd else _MAX_CRC_BYTES

    # ── reassembly math ────────────────────────────────────────────────
    def _reassemble_id(self) -> int:
        b = self._id_bytes
        if self._ide and len(b) >= 4:
            return ((b[0] & 0x1F) << 24) | (b[1] << 16) | (b[2] << 8) | b[3]
        if not self._ide and len(b) >= 2:
            return ((b[0] & 0x07) << 8) | b[1]
        # desync — fewer id bytes than the IDE flag implies; best-effort pack
        val = 0
        for byte in b:
            val = (val << 8) | byte
        return val

    def _reassemble_crc(self) -> Optional[int]:
        b = self._crc_bytes
        if self._is_fd:
            if len(b) < 3:
                return None
            # FD CRC = 3 bytes, big-endian zero-extended: byte0=hi, byte1, byte2.
            #   CRC-21: byte0 = 000|crc[20:16]; CRC-17: byte0 = 0000000|crc[16].
            hi_mask = 0x1F if self._crc_is21 else 0x01
            return ((b[0] & hi_mask) << 16) | (b[1] << 8) | b[2]
        if len(b) < 2:
            return None
        return ((b[0] << 7) | (b[1] >> 1)) & 0x7FFF

    def _summary(self, *, aborted: bool) -> DecodedFrame:
        can_id = self._reassemble_id()
        crc = self._reassemble_crc()
        errs = list(self._errors)
        if self._overflow:
            errs.append("desync")
        if aborted and "stuff_error" not in errs and "desync" not in errs:
            errs.append("aborted")

        kind_disp = "EXT" if self._ide else "STD"
        idw = 8 if self._ide else 3   # hex digits to show the id
        proto = "FD" if self._is_fd else "CAN"
        parts = [f"{proto} id=0x{can_id:0{idw}X} {kind_disp}"]
        if self._is_fd:
            if self._brs:
                parts.append("BRS")
            if self._esi:
                parts.append("ESI")
        elif self._rtr:
            parts.append("RTR")
        parts.append(f"DLC={self._dlc}")
        if self._data_bytes:
            parts.append("data=[" + " ".join(f"{x:02X}" for x in self._data_bytes) + "]")
        if crc is not None:
            cw = 6 if self._crc_is21 else (5 if self._is_fd else 4)   # hex digits
            parts.append(f"CRC=0x{crc:0{cw}X}")
        if self._ack is True:
            parts.append("ACK")
        elif self._ack is False:
            parts.append("noACK")
        if errs:
            parts.append("!" + ",".join(errs))
        text = " ".join(parts)

        is_err = bool(errs)
        color = _ABORT_COLOR if (aborted or self._overflow) and not self._errors \
            else (_ERROR_COLOR if is_err else _FRAME_COLOR)
        fields = {
            "id": can_id, "ide": self._ide, "rtr": self._rtr, "dlc": self._dlc,
            "data": list(self._data_bytes),
            "crc": crc, "ack": self._ack, "errors": errs,
            # CAN-FD (DS-P2.64); fd=False for classic frames
            "fd": self._is_fd, "brs": self._brs, "esi": self._esi,
            "data_len": self._fd_data_len if self._is_fd else len(self._data_bytes),
            "stuff_count": self._stuff_count, "stuff_parity_ok": self._stuff_parity_ok,
        }
        # LT-P3.216: a CAN-FD frame carries its own annotation kind so the
        # RouteWave Protocol-Analyzer can promote CANFD to a first-class filter
        # chip purely off the wire `kind` (the single protocol-label contract
        # surface) — the FD-ness must NOT be inferred from the `fd` payload field
        # downstream. Classic frames stay `can_frame`. fields["fd"] is retained
        # for the detail pane and back-compat.
        kind = "canfd_frame" if self._is_fd else "can_frame"
        return DecodedFrame(kind, text, color, is_err, fields=fields)

    # ── feed ───────────────────────────────────────────────────────────
    def feed(self, header: zs_msg.ZsHeader,
             payload: bytes) -> Optional[DecodedFrame]:
        if header.domain != zs_msg.DOMAIN_CAN or len(payload) < 1:
            return None
        data = payload[0]
        et = header.event_id

        if et == CAN_T_EVENT:
            return self._feed_event(data)
        if not self._active:
            return None                       # ignore field bytes until a SOF
        if et == CAN_T_ID:
            if len(self._id_bytes) < _MAX_ID_BYTES:
                self._id_bytes.append(data)
            else:
                self._overflow = True
        elif et == CAN_T_CONTROL:
            self._ide = bool(data & 0x80)
            self._rtr = bool(data & 0x40)
            self._dlc = data & 0x0F
            self._have_control = True
        elif et == CAN_T_FD_CTRL:
            # CAN-FD control: {IDE[7],BRS[6],ESI[5],0[4],DLC[3:0]}. FDF implied.
            self._is_fd = True
            self._ide = bool(data & 0x80)
            self._brs = bool(data & 0x40)
            self._esi = bool(data & 0x20)
            self._rtr = False                      # FD has no remote frames
            self._dlc = data & 0x0F
            self._fd_data_len = CAN_FD_DLC_BYTES[self._dlc]
            self._have_control = True
        elif et == CAN_T_FD_AUX:
            # CAN-FD stuff-count: {parity_ok[7],0000,gray[2:0]}. The 3-bit field is
            # GRAY-coded on the wire (ISO 11898-1:2015 / Bosch CAN FD) — gray-decode
            # it to the actual count. DS-P3.107: reading it raw reported the gray
            # bits (sc 2->0b011->3, sc 4->0b110->6) as the count, the "1,3,6 vs
            # true 1,2,4" misattributed to a silicon defect; the RTL emits correct
            # gray (REA-proven), the host decode was the bug.
            self._stuff_parity_ok = bool(data & 0x80)
            g = data & 0x07
            b2 = (g >> 2) & 1
            b1 = b2 ^ ((g >> 1) & 1)
            b0 = b1 ^ (g & 1)
            self._stuff_count = (b2 << 2) | (b1 << 1) | b0
        elif et == CAN_T_DATA:
            if len(self._data_bytes) < self._data_cap:
                self._data_bytes.append(data)
            else:
                self._overflow = True
        elif et == CAN_T_CRC:
            if len(self._crc_bytes) < self._crc_cap:
                self._crc_bytes.append(data)
            else:
                self._overflow = True
        return None

    def _feed_event(self, code: int) -> Optional[DecodedFrame]:
        if code == CAN_EV_FRAME_BEGIN:
            stale = None
            if self._active:                  # dropped frame_end → flush partial
                self._desync_count += 1
                self._overflow = True
                stale = self._summary(aborted=True)
            self._reset()
            self._active = True
            return stale
        if not self._active:
            return None
        if code == CAN_EV_ACK_OBS:
            self._ack = True
        elif code == CAN_EV_ACK_ERR:
            self._ack = False
            self._errors.append("ack_error")
        elif code == CAN_EV_CRC_ERR:
            self._errors.append("crc_error")
        elif code == CAN_EV_FORM_ERR:
            self._errors.append("form_error")
        elif code == CAN_EV_STUFF_CNT_ERR:
            self._errors.append("stuff_count_error")
        elif code == CAN_EV_STUFF_ERR:
            # the FSM aborts to idle on a stuff error — no frame_end follows
            self._errors.append("stuff_error")
            summary = self._summary(aborted=True)
            self._reset()
            return summary
        elif code == CAN_EV_FRAME_END:
            summary = self._summary(aborted=False)
            self._reset()
            return summary
        return None
