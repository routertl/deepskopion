# rr unknown-llm (AI-generated)
"""Stateful JTAG IR/DR reassembler + IDCODE parser (DS-P3.56 P2).

The 1:1 :func:`decoders.decode_jtag` annotates each individual TDI/TDO byte
and TAP event. This module is the *cross-frame* layer: it consumes the same
JTAG zs_msg frames in shift order and reassembles the per-register bitstreams
that a Shift-IR / Shift-DR scan carries, emitting ONE summary annotation when
a register scan completes (on the ``*_shift_end`` event).

It is the N:1 counterpart to the 1:1 decoder protocol — distinct enough that
it lives here rather than in :mod:`decoders` (mirroring the EDGE_BATCH note).

Bit-ordering (matches jtag_datalogger.vhd, IEEE-1149.1 LSB-first):
  * The first bit shifted into a register is its LSB (bit 0). JTAG shifts
    LSB-first, and IDCODE bit 0 (the constant '1') comes out of TDO first.
  * The datalogger captures each completed byte LSB-aligned: byte value bit i =
    the i-th bit shifted within that byte. So a register reassembles as
    ``value = sum(byte_k << (8*k))`` over the full bytes in shift order.
  * A sub-byte final byte (JTAG-REQ-5) carries its N valid bits in the TOP N
    positions, so its shift-order value is ``byte >> (8 - N)`` and it
    contributes the highest N register bits.

IDCODE heuristic (generic, no BSDL): the first DR scan after a TAP reset
returns the device IDCODE when present — 32 bits with bit 0 == 1. Knowing the
device-specific IDCODE *instruction* value (to catch later explicit reads) is
chain-aware territory and lands in P3.
"""

from __future__ import annotations

from typing import List, Optional

from . import zs_msg
from .jtag_devices import JtagDevice, JtagChain
from .decoders import (
    DecodedFrame,
    JTAG_T_TDI, JTAG_T_TDO, JTAG_T_EVENT,
    JTAG_EV_ENABLED, JTAG_EV_DISABLED, JTAG_EV_TAP_RESET,
    JTAG_EV_IR_BEGIN, JTAG_EV_IR_END, JTAG_EV_DR_BEGIN, JTAG_EV_DR_END,
)

# JEP106 manufacturer codes as they appear in IDCODE[11:1] (11-bit field).
_JEDEC_MFR = {
    0x049: "Xilinx",
    0x06E: "Altera/Intel",
    0x043: "Lattice",
    0x029: "Microchip",
    0x01F: "Atmel",
    0x017: "TI",
    0x04B: "ARM",
    0x070: "Qualcomm",
}

_IR_COLOR     = "#7a5cc8"   # purple
_DR_COLOR     = "#5c8cc8"   # blue
_IDCODE_COLOR = "#c83c8c"   # magenta — the headline "chip is alive" event
_DESYNC_COLOR = "#cc8833"   # amber — a recovered desync

# DS-T3.7: under a flooded/wrapping ring, frames get dropped/overwritten. A lost
# *_END (or a TAP-RESET that itself was dropped) would otherwise let the
# accumulator append forever and emit physically-impossible registers (we saw a
# 192-bit IR and 1244-bit DRs on silicon). Bound both.
_MAX_SCAN_BYTES = 256       # cap accumulation at 2048 bits (legit DRs fit; a
                            # never-ending dropped-END can't grow past this)
_MAX_IR_BITS = 64           # any IR wider than this is a desync, not a real IR


class JtagReassembler:
    """Feed JTAG frames in order; get a summary annotation per completed scan.

    Usage::

        ra = JtagReassembler()
        for header, payload in frames:
            summary = ra.feed(header, payload)   # None until a scan completes
            if summary is not None:
                emit(summary)                    # on the jtag_reg lane
    """

    def __init__(self, device: Optional[JtagDevice] = None,
                 chain: Optional[JtagChain] = None) -> None:
        # Three modes, in priority order:
        #   chain  — split a multi-device combined IR/DR per device (DS-P3.58)
        #   device — single-TAP instruction names + BSCAN/IDCODE (DS-P3.56 P3)
        #   None   — generic BSDL-free: raw IR, anonymous DR, post-reset IDCODE
        self._chain = chain
        self._device = device
        self._reset_accum()
        self._cur_ir: Optional[int] = None
        self._cur_chain_ir = None        # list[(JtagDevice, ir_field)] TDI..TDO
        # After a TAP reset the first DR scan reads IDCODE (or BYPASS).
        self._after_tap_reset: bool = True
        self._desync_count: int = 0      # DS-T3.7: recovered desyncs

    @property
    def desync_count(self) -> int:
        return self._desync_count

    def _reset_accum(self) -> None:
        self._mode: Optional[str] = None      # 'ir' | 'dr' | None
        self._tdi: List[int] = []             # full bytes in shift order
        self._tdo: List[int] = []
        self._overflow: bool = False          # accumulation hit _MAX_SCAN_BYTES

    # ── frame intake ──────────────────────────────────────────────────
    def feed(self, header: zs_msg.ZsHeader,
             payload: bytes) -> Optional[DecodedFrame]:
        if header.domain != zs_msg.DOMAIN_JTAG or len(payload) < 1:
            return None
        et = header.event_id
        data = payload[0]
        if et == JTAG_T_TDI:
            if self._mode is not None:
                if len(self._tdi) < _MAX_SCAN_BYTES:
                    self._tdi.append(data)
                else:
                    self._overflow = True     # DS-T3.7: stop growing; flag at END
            return None
        if et == JTAG_T_TDO:
            if self._mode is not None:
                if len(self._tdo) < _MAX_SCAN_BYTES:
                    self._tdo.append(data)
                else:
                    self._overflow = True
            return None
        if et == JTAG_T_EVENT:
            return self._feed_event(data)
        return None

    def _feed_event(self, code: int) -> Optional[DecodedFrame]:
        hi = code & 0xF0
        if code == JTAG_EV_TAP_RESET:
            self._reset_accum()
            self._cur_ir = None
            self._cur_chain_ir = None
            self._after_tap_reset = True
            return None
        if code in (JTAG_EV_ENABLED, JTAG_EV_DISABLED):
            return None
        if hi == JTAG_EV_IR_BEGIN:
            self._reset_accum()
            self._mode = "ir"
            return None
        if hi == JTAG_EV_DR_BEGIN:
            self._reset_accum()
            self._mode = "dr"
            return None
        if hi == JTAG_EV_IR_END:
            return self._finish_ir(code & 0x0F)
        if hi == JTAG_EV_DR_END:
            return self._finish_dr(code & 0x0F)
        return None

    # ── reassembly ────────────────────────────────────────────────────
    @staticmethod
    def _reassemble(byte_list: List[int], nibble: int) -> tuple[int, int]:
        """(value, width_bits) from shift-order bytes; nibble = valid bits in
        the final byte (0 == the final byte was a full 8 bits)."""
        value = 0
        shift = 0
        n = len(byte_list)
        for i, b in enumerate(byte_list):
            if i == n - 1 and nibble != 0:
                value |= (b >> (8 - nibble)) << shift
                shift += nibble
            else:
                value |= b << shift
                shift += 8
        return value, shift

    def _finish_ir(self, nibble: int) -> Optional[DecodedFrame]:
        ir_val, width = self._reassemble(self._tdi, nibble)
        overflow = self._overflow
        self._mode = None
        if width == 0:
            return None
        # DS-T3.7: a real IR is small; an oversized/overflowed one is a desync
        # (a dropped IR_END merged scans). Recover instead of emitting garbage.
        if overflow or width > _MAX_IR_BITS:
            return self._desync("IR", width)
        if self._chain is not None:
            return self._finish_ir_chain(ir_val, width)
        self._cur_ir = ir_val
        # Prefer the device profile's mnemonic; fall back to the generic
        # all-ones BYPASS recognition when there's no profile (or no match).
        name = self._device.instr_name(ir_val) if self._device else None
        if name is None and ir_val == (1 << width) - 1:
            name = "BYPASS"
        suffix = f" {name}" if name else ""
        ir_fields: dict = {"ir": ir_val, "width": width}   # DS-P3.88
        if name:
            ir_fields["instr"] = name
        return DecodedFrame(
            kind="jtag_ir",
            text=f"IR=0x{ir_val:X}/{width}b{suffix}",
            color=_IR_COLOR,
            is_error=False,
            fields=ir_fields,
        )

    def _finish_dr(self, nibble: int) -> Optional[DecodedFrame]:
        tdo_val, width = self._reassemble(self._tdo, nibble)
        tdi_val, _ = self._reassemble(self._tdi, nibble)
        overflow = self._overflow
        self._mode = None
        if width == 0:
            return None
        # DS-T3.7: an overflowed DR (a dropped DR_END swallowed the next scan)
        # is a desync — recover rather than emit a runaway register.
        if overflow:
            return self._desync("DR", width)
        first_after_reset = self._after_tap_reset
        self._after_tap_reset = False

        if self._chain is not None:
            return self._finish_dr_chain(tdo_val, tdi_val, width, first_after_reset)

        # Chain-aware: label the DR scan by the active instruction.
        if self._device is not None and self._cur_ir is not None:
            cur = self._cur_ir
            # Explicit IDCODE instruction read — decode regardless of reset.
            if self._device.is_idcode_instr(cur) and width == 32 \
                    and (tdo_val & 1) == 1:
                return self._idcode_annotation(tdo_val)
            instr = self._device.instr_name(cur)
            if self._device.is_bscan_instr(cur):
                # USERn -> fabric (BSCANE2). This is the REA / embedded-analyzer
                # path: the DR payload is the fabric register's capture/update.
                return DecodedFrame(
                    kind="jtag_bscan",
                    text=f"{instr} BSCAN tdo=0x{tdo_val:X} "
                         f"tdi=0x{tdi_val:X} /{width}b",
                    color=_DR_COLOR,
                    is_error=False,
                    fields={"instr": instr, "tdo": tdo_val,  # DS-P3.88
                            "tdi": tdi_val, "width": width},
                )
            if instr is not None:
                return DecodedFrame(
                    kind="jtag_dr",
                    text=f"{instr} DR tdo=0x{tdo_val:X} "
                         f"tdi=0x{tdi_val:X} /{width}b",
                    color=_DR_COLOR,
                    is_error=False,
                )

        # Generic (no profile): IDCODE by post-reset heuristic, else raw DR.
        if first_after_reset and width == 32 and (tdo_val & 1) == 1:
            return self._idcode_annotation(tdo_val)
        return DecodedFrame(
            kind="jtag_dr",
            text=f"DR tdo=0x{tdo_val:X} tdi=0x{tdi_val:X} /{width}b",
            color=_DR_COLOR,
            is_error=False,
        )

    # ── multi-device chain (DS-P3.58) ─────────────────────────────────
    def _finish_ir_chain(self, ir_val: int, width: int) -> DecodedFrame:
        fields = self._chain.split_ir(ir_val)
        self._cur_chain_ir = fields
        parts = " ".join(f"{(d.short or d.name)}={d.instr_label(f)}"
                         for d, f in fields)
        return DecodedFrame(kind="jtag_ir", text=f"IR[{parts}] /{width}b",
                            color=_IR_COLOR, is_error=False)

    def _finish_dr_chain(self, tdo_val: int, tdi_val: int, width: int,
                         after_reset: bool) -> DecodedFrame:
        devs = self._chain.devices
        fields = self._cur_chain_ir
        # No IR seen yet: after a TAP reset every TAP holds its IDCODE (or a
        # 1-bit BYPASS if it has none). Synthesise that instruction vector.
        if fields is None:
            if not after_reset:
                return DecodedFrame("jtag_dr",
                                    f"DR tdo=0x{tdo_val:X} tdi=0x{tdi_val:X} /{width}b",
                                    _DR_COLOR, False)
            fields = [(d, d.idcode_instr) for d in devs]

        # Per-device DR width: IDCODE=32, BYPASS=1, the single variable
        # (USER/data) instruction takes the remaining bits.
        widths: List[int] = [0] * len(devs)
        fixed = 0
        var_idx: Optional[int] = None
        for i, d in enumerate(devs):
            irf = fields[i][1]
            name = d.instr_name(irf) if irf is not None else None
            if irf is not None and d.is_idcode_instr(irf):
                widths[i] = 32; fixed += 32
            elif irf is not None and d.is_bypass(irf):
                widths[i] = 1; fixed += 1
            elif name is not None and name.startswith("USER"):
                var_idx = i                       # active BSCAN register, remainder
            elif after_reset and irf is None:
                widths[i] = 32 if d.idcode_instr is not None else 1
                fixed += widths[i]
            else:
                # Unknown / non-data instruction (e.g. the DAP held at 0x1 while
                # the PL is read): assume a minimal 1-bit (BYPASS-like) DR, so a
                # single active USER register stays the sole variable.
                widths[i] = 1; fixed += 1
        if var_idx is not None:
            widths[var_idx] = max(0, width - fixed)

        # Slice LSB-up — the TDO-nearest device (last in chain order) is at the
        # LSB end, mirroring split_ir.
        slices = [(0, 0, 0)] * len(devs)
        pos = 0
        for i in range(len(devs) - 1, -1, -1):
            w = widths[i]
            mask = (1 << w) - 1
            slices[i] = ((tdo_val >> pos) & mask, (tdi_val >> pos) & mask, w)
            pos += w

        parts: List[str] = []
        kind = "jtag_dr"
        for i, d in enumerate(devs):
            tdo_i, tdi_i, w = slices[i]
            if w == 0:
                continue
            irf = fields[i][1]
            name = d.instr_name(irf) if irf is not None else None
            if w == 32 and ((tdi_i & 1) == 1 or (tdo_i & 1) == 1) and \
                    (after_reset or (irf is not None and d.is_idcode_instr(irf))):
                idc = tdi_i if (tdi_i & 1) == 1 else tdo_i
                parts.append(f"{d.short}:IDCODE 0x{idc:08X}")
                if kind == "jtag_dr":
                    kind = "jtag_idcode"
            elif name is not None and name.startswith("USER"):
                parts.append(f"{d.short}:{name} BSCAN "
                             f"tdo=0x{tdo_i:X} tdi=0x{tdi_i:X}/{w}b")
                kind = "jtag_bscan"
            elif w == 1:
                lbl = d.instr_label(irf).lower() if irf is not None else "bypass"
                parts.append(f"{d.short}:{lbl}")
            else:
                parts.append(f"{d.short}:{name or 'DR'} "
                             f"tdo=0x{tdo_i:X} tdi=0x{tdi_i:X}/{w}b")
        color = _IDCODE_COLOR if kind == "jtag_idcode" else _DR_COLOR
        return DecodedFrame(kind=kind, text="DR[" + " | ".join(parts) + f"] /{width}b",
                            color=color, is_error=False)

    def _desync(self, what: str, width: int) -> DecodedFrame:
        self._desync_count += 1
        return DecodedFrame(
            kind="jtag_desync",
            text=f"{what} desync — dropped-END/overflow ({width}b), recovered",
            color=_DESYNC_COLOR,
            is_error=False,
        )

    @staticmethod
    def _idcode_annotation(idcode: int) -> DecodedFrame:
        version = (idcode >> 28) & 0xF
        part = (idcode >> 12) & 0xFFFF
        mfr = (idcode >> 1) & 0x7FF
        mfr_name = _JEDEC_MFR.get(mfr, f"mfr=0x{mfr:03X}")
        return DecodedFrame(
            kind="jtag_idcode",
            text=f"IDCODE 0x{idcode:08X} ({mfr_name} part=0x{part:04X} rev={version})",
            color=_IDCODE_COLOR,
            is_error=False,
            fields={"idcode": idcode, "mfr": mfr_name, "part": part,  # DS-P3.88
                    "rev": version},
        )
