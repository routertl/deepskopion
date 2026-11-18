"""Unit tests for the stateful JTAG IR/DR reassembler (DS-P3.56 P2).

Feed a hand-built JTAG frame sequence (the exact wire shape the gather emits)
and assert the reassembled register value / IDCODE. Expecteds are hard-coded
(ROUTERTL-002) — a known Zynq-7020 PL-TAP IDCODE is the headline case.
"""
from __future__ import annotations

import struct

from sw.stream import zs_msg, decoders, jtag_devices
from sw.stream.jtag_reassembler import JtagReassembler


def _frame(event_id: int, data: int):
    """One JTAG (header, payload) frame as datalogger_gather emits it."""
    hdr = zs_msg.make_header(
        datalogger_id=16, domain=zs_msg.DOMAIN_JTAG,
        event_id=event_id, severity=zs_msg.SEV_INFO, payload_len=4,
    )
    word = ((16 << 27) | ((event_id & 0x7) << 24) | (data & 0xFF))
    return hdr, struct.pack("<I", word)


def _ev(code):    return _frame(decoders.JTAG_T_EVENT, code)
def _tdi(byte):   return _frame(decoders.JTAG_T_TDI, byte)
def _tdo(byte):   return _frame(decoders.JTAG_T_TDO, byte)


def _partial(value: int, nbits: int) -> int:
    """The datalogger's TOP-aligned representation of a sub-byte final byte."""
    return (value << (8 - nbits)) & 0xFF


def _feed_all(ra: JtagReassembler, frames):
    """Feed frames; return the list of non-None summary annotations."""
    out = []
    for hdr, payload in frames:
        r = ra.feed(hdr, payload)
        if r is not None:
            out.append(r)
    return out


def _dr_scan_full_bytes(value_bytes):
    """A DR scan whose TDO carries value_bytes (shift order), TDI = 0."""
    seq = [_ev(decoders.JTAG_EV_DR_BEGIN)]
    for b in value_bytes:
        seq.append(_tdi(0x00))
        seq.append(_tdo(b))
    seq.append(_ev(decoders.JTAG_EV_DR_END))   # nibble 0 -> all full bytes
    return seq


# ── IDCODE ────────────────────────────────────────────────────────────

class TestIdcode:
    # Zynq-7020 PL TAP: 0x23727093 -> Xilinx (mfr 0x049), part 0x3727, rev 2.
    IDCODE = 0x23727093
    LE_BYTES = [0x93, 0x70, 0x72, 0x23]   # idcode byte k = (idcode >> 8k) & 0xFF

    def test_byte_split_is_shift_order(self):
        assert self.LE_BYTES == [(self.IDCODE >> (8 * k)) & 0xFF for k in range(4)]

    def test_idcode_after_reset(self):
        ra = JtagReassembler()
        frames = [_ev(decoders.JTAG_EV_TAP_RESET)] + \
            _dr_scan_full_bytes(self.LE_BYTES)
        summaries = _feed_all(ra, frames)
        assert len(summaries) == 1
        s = summaries[0]
        assert s.kind == "jtag_idcode"
        assert s.text == "IDCODE 0x23727093 (Xilinx part=0x3727 rev=2)"

    def test_idcode_without_explicit_reset_event(self):
        # Constructor seeds _after_tap_reset=True, so the very first DR scan
        # is still treated as the post-reset IDCODE read.
        ra = JtagReassembler()
        summaries = _feed_all(ra, _dr_scan_full_bytes(self.LE_BYTES))
        assert len(summaries) == 1 and summaries[0].kind == "jtag_idcode"

    def test_second_dr_is_not_idcode(self):
        ra = JtagReassembler()
        first = _feed_all(ra, _dr_scan_full_bytes(self.LE_BYTES))
        second = _feed_all(ra, _dr_scan_full_bytes(self.LE_BYTES))
        assert first[0].kind == "jtag_idcode"
        assert second[0].kind == "jtag_dr"          # after_reset cleared
        assert "tdo=0x23727093" in second[0].text

    def test_tap_reset_rearms_idcode(self):
        ra = JtagReassembler()
        _feed_all(ra, _dr_scan_full_bytes(self.LE_BYTES))          # IDCODE
        _feed_all(ra, _dr_scan_full_bytes(self.LE_BYTES))          # plain DR
        rearmed = _feed_all(
            ra, [_ev(decoders.JTAG_EV_TAP_RESET)]
            + _dr_scan_full_bytes(self.LE_BYTES))
        assert rearmed[0].kind == "jtag_idcode"

    def test_one_bit_bypass_dr_is_not_idcode(self):
        # After reset a no-IDCODE device returns a 1-bit BYPASS DR.
        ra = JtagReassembler()
        seq = [_ev(decoders.JTAG_EV_TAP_RESET),
               _ev(decoders.JTAG_EV_DR_BEGIN),
               _tdi(_partial(0, 1)), _tdo(_partial(1, 1)),
               _ev(decoders.JTAG_EV_DR_END | 1)]       # 1 valid bit
        summaries = _feed_all(ra, seq)
        assert len(summaries) == 1
        assert summaries[0].kind == "jtag_dr"
        assert "/1b" in summaries[0].text


# ── IR reassembly ───────────────────────────────────────────────────────

class TestIrReassembly:
    def test_ir_full_byte(self):
        ra = JtagReassembler()
        seq = [_ev(decoders.JTAG_EV_IR_BEGIN),
               _tdi(0x25), _tdo(0x00),
               _ev(decoders.JTAG_EV_IR_END)]            # nibble 0 -> 8 bits
        s = _feed_all(ra, seq)[0]
        assert s.kind == "jtag_ir" and s.text == "IR=0x25/8b"

    def test_ir_bypass_all_ones(self):
        ra = JtagReassembler()
        seq = [_ev(decoders.JTAG_EV_IR_BEGIN),
               _tdi(0xFF), _tdo(0x00),
               _ev(decoders.JTAG_EV_IR_END)]
        s = _feed_all(ra, seq)[0]
        assert s.text == "IR=0xFF/8b BYPASS"

    def test_ir_subbyte(self):
        # 6-bit IR = 0x09 -> partial byte 0x24 (top-6-aligned).
        ra = JtagReassembler()
        seq = [_ev(decoders.JTAG_EV_IR_BEGIN),
               _tdi(_partial(0x09, 6)), _tdo(0x00),
               _ev(decoders.JTAG_EV_IR_END | 6)]
        s = _feed_all(ra, seq)[0]
        assert s.kind == "jtag_ir" and s.text == "IR=0x9/6b"


# ── sub-byte DR reassembly ──────────────────────────────────────────────

class TestSubByteDr:
    def test_dr_mixed_full_and_partial(self):
        # 12-bit DR value 0xABC: byte0 = 0xBC (full), then 4 high bits 0xA.
        # shift order: reg = byte0 | (partial>>4 << 8). partial nibble=4.
        ra = JtagReassembler()
        _feed_all(ra, _dr_scan_full_bytes([0]))         # burn the IDCODE slot
        seq = [_ev(decoders.JTAG_EV_DR_BEGIN),
               _tdi(0x00), _tdo(0xBC),                  # full low byte
               _tdi(0x00), _tdo(_partial(0xA, 4)),      # 4 high bits
               _ev(decoders.JTAG_EV_DR_END | 4)]
        s = _feed_all(ra, seq)[0]
        assert s.kind == "jtag_dr"
        assert "tdo=0xABC" in s.text and "/12b" in s.text


# ── Chain-aware decode with a device profile (DS-P3.56 P3) ──────────────


def _ir_scan_full_byte(ir_byte):
    return [_ev(decoders.JTAG_EV_IR_BEGIN),
            _tdi(ir_byte), _tdo(0x00),
            _ev(decoders.JTAG_EV_IR_END | 6)]      # 6-bit IR (7-series)


class TestChainAware:
    def test_ir_instruction_name(self):
        ra = JtagReassembler(device=jtag_devices.XILINX_7SERIES)
        # IR=0x02 over 6 bits -> partial byte top-aligned; USER1.
        seq = [_ev(decoders.JTAG_EV_IR_BEGIN),
               _tdi(_partial(0x02, 6)), _tdo(0x00),
               _ev(decoders.JTAG_EV_IR_END | 6)]
        s = _feed_all(ra, seq)[0]
        assert s.kind == "jtag_ir" and s.text == "IR=0x2/6b USER1"

    def test_bypass_named_from_profile(self):
        ra = JtagReassembler(device=jtag_devices.XILINX_7SERIES)
        seq = [_ev(decoders.JTAG_EV_IR_BEGIN),
               _tdi(_partial(0x3F, 6)), _tdo(0x00),
               _ev(decoders.JTAG_EV_IR_END | 6)]
        s = _feed_all(ra, seq)[0]
        assert s.text == "IR=0x3F/6b BYPASS"

    def test_user1_dr_is_bscan_rea_path(self):
        """The headline REA scenario: IR=USER1 selects the BSCANE2 fabric
        path, so the following DR scan is the REA/ILA payload."""
        ra = JtagReassembler(device=jtag_devices.XILINX_7SERIES)
        # Select USER1, then a 16-bit DR carrying fabric data 0xBEEF on TDO.
        seq = _ir_scan_full_byte(_partial(0x02, 6)) + [
            _ev(decoders.JTAG_EV_DR_BEGIN),
            _tdi(0x00), _tdo(0xEF),       # low byte
            _tdi(0x00), _tdo(0xBE),       # high byte
            _ev(decoders.JTAG_EV_DR_END),
        ]
        out = _feed_all(ra, seq)
        ir, dr = out[0], out[1]
        assert ir.text == "IR=0x2/6b USER1"
        assert dr.kind == "jtag_bscan"
        assert "USER1 BSCAN" in dr.text and "tdo=0xBEEF" in dr.text and "/16b" in dr.text

    def test_explicit_idcode_instruction_read(self):
        """IR=IDCODE (0x09) then a 32-bit DR decodes as IDCODE even when it's
        not the post-reset scan."""
        ra = JtagReassembler(device=jtag_devices.XILINX_7SERIES)
        # Burn the post-reset slot with an unrelated scan first.
        _feed_all(ra, _dr_scan_full_bytes([0x01, 0x00, 0x00, 0x00]))
        seq = _ir_scan_full_byte(_partial(0x09, 6)) + \
            _dr_scan_full_bytes([0x93, 0x70, 0x72, 0x23])
        out = _feed_all(ra, seq)
        idcode = [a for a in out if a.kind == "jtag_idcode"]
        assert len(idcode) == 1
        assert idcode[0].text == "IDCODE 0x23727093 (Xilinx part=0x3727 rev=2)"

    def test_named_dr_for_non_bscan_instruction(self):
        ra = JtagReassembler(device=jtag_devices.ARM_DAP)
        # ARM DAP DPACC (0xA), 4-bit IR; then a DR scan.
        seq = [_ev(decoders.JTAG_EV_IR_BEGIN),
               _tdi(_partial(0xA, 4)), _tdo(0x00),
               _ev(decoders.JTAG_EV_IR_END | 4)] + \
            _dr_scan_full_bytes([0x11, 0x22, 0x33, 0x00])
        out = _feed_all(ra, seq)
        assert out[0].text == "IR=0xA/4b DPACC"
        assert out[1].kind == "jtag_dr" and out[1].text.startswith("DPACC DR")

    def test_no_profile_preserves_generic_behaviour(self):
        # Back-compat: device=None still finds IDCODE by the post-reset rule.
        ra = JtagReassembler()
        s = _feed_all(ra, _dr_scan_full_bytes([0x93, 0x70, 0x72, 0x23]))[0]
        assert s.kind == "jtag_idcode"


# ── Multi-device chain decode (DS-P3.58) ────────────────────────────────


def _bytes_for_value(value, width):
    """Shift-order byte list for `value` over `width` bits + final nibble."""
    full, rem = width // 8, width % 8
    out = [(value >> (8 * k)) & 0xFF for k in range(full)]
    if rem:
        out.append(_partial((value >> (8 * full)) & ((1 << rem) - 1), rem))
    return out, rem


def _scan(begin_ev, end_ev, value, width, tdo_value=0):
    btdi, rem = _bytes_for_value(value, width)
    btdo, _ = _bytes_for_value(tdo_value, width)
    seq = [_ev(begin_ev)]
    for i in range(len(btdi)):
        seq.append(_tdi(btdi[i])); seq.append(_tdo(btdo[i]))
    seq.append(_ev(end_ev | (rem if rem else 0)))
    return seq


class TestChainDecode:
    CH = jtag_devices.ZYNQ7_CHAIN

    def test_split_ir_isolates_user1(self):
        fields = self.CH.split_ir(0x2F)            # PL=USER1 | DAP=BYPASS
        names = {d.short: d.instr_label(f) for d, f in fields}
        assert names == {"PL": "USER1", "DAP": "BYPASS"}

    def test_chain_ir_named_per_device(self):
        ra = JtagReassembler(chain=self.CH)
        seq = _scan(decoders.JTAG_EV_IR_BEGIN, decoders.JTAG_EV_IR_END, 0x2F, 10)
        s = _feed_all(ra, seq)[0]
        assert s.kind == "jtag_ir"
        assert s.text == "IR[PL=USER1 DAP=BYPASS] /10b"

    def test_chain_user1_dr_is_bscan_on_PL(self):
        ra = JtagReassembler(chain=self.CH)
        # IR: PL=USER1, DAP=BYPASS
        _feed_all(ra, _scan(decoders.JTAG_EV_IR_BEGIN, decoders.JTAG_EV_IR_END, 0x2F, 10))
        # DR: PL BSCAN = 0xBEEF (16b), DAP bypass = 1 bit (0) -> combined 17b
        combined = (0xBEEF << 1) | 0
        s = _feed_all(ra, _scan(decoders.JTAG_EV_DR_BEGIN, decoders.JTAG_EV_DR_END,
                                combined, 17, tdo_value=combined))[0]
        assert s.kind == "jtag_bscan"
        assert "PL:USER1 BSCAN" in s.text and "0xBEEF" in s.text and "/16b" in s.text
        assert "DAP:bypass" in s.text

    def test_chain_post_reset_dual_idcode(self):
        ra = JtagReassembler(chain=self.CH)
        # After reset, both TAPs present IDCODE; DR = [PL idcode][DAP idcode],
        # DAP at the LSB end (TDO). Use real Zynq IDCODEs.
        PL_ID, DAP_ID = 0x23727093, 0x4BA00477
        combined = (PL_ID << 32) | DAP_ID        # PL high, DAP low (TDO)
        seq = [_ev(decoders.JTAG_EV_TAP_RESET)] + \
            _scan(decoders.JTAG_EV_DR_BEGIN, decoders.JTAG_EV_DR_END,
                  combined, 64, tdo_value=combined)
        s = _feed_all(ra, seq)[0]
        assert s.kind == "jtag_idcode"
        assert "PL:IDCODE 0x23727093" in s.text
        assert "DAP:IDCODE 0x4BA00477" in s.text

    def test_chain_back_compat_single_device_untouched(self):
        # device= path still works (no chain)
        ra = JtagReassembler(device=jtag_devices.XILINX_7SERIES)
        s = _feed_all(ra, _dr_scan_full_bytes([0x93, 0x70, 0x72, 0x23]))[0]
        assert s.kind == "jtag_idcode"


class TestChainRealSilicon:
    """The exact pattern seen on the board: combined IR=0x21 -> PL=USER1 with
    the DAP held at 0x1 (an unknown/non-data instr), then a USER1 BSCAN DR."""
    CH = jtag_devices.ZYNQ7_CHAIN

    def test_ir_0x21_pl_user1_dap_unknown(self):
        ra = JtagReassembler(chain=self.CH)
        s = _feed_all(ra, _scan(decoders.JTAG_EV_IR_BEGIN,
                                decoders.JTAG_EV_IR_END, 0x21, 10))[0]
        assert s.text == "IR[PL=USER1 DAP=0x1] /10b"

    def test_user1_bscan_with_dap_at_0x1(self):
        ra = JtagReassembler(chain=self.CH)
        _feed_all(ra, _scan(decoders.JTAG_EV_IR_BEGIN,
                            decoders.JTAG_EV_IR_END, 0x21, 10))   # PL=USER1, DAP=0x1
        combined = (0xCAFE << 1) | 0                              # PL 16b + DAP 1b
        s = _feed_all(ra, _scan(decoders.JTAG_EV_DR_BEGIN, decoders.JTAG_EV_DR_END,
                                combined, 17, tdo_value=combined))[0]
        assert s.kind == "jtag_bscan"
        assert "PL:USER1 BSCAN" in s.text and "0xCAFE" in s.text and "/16b" in s.text
        assert "DAP:0x1" in s.text          # the held-DAP shown, not mislabelled bypass


class TestDesyncRobustness:
    """DS-T3.7: a flooded/wrapping ring drops frames; the reassembler must
    bound accumulation + recover instead of emitting megabit registers."""

    def test_oversized_ir_flagged_not_garbage(self):
        ra = JtagReassembler()
        seq = [_ev(decoders.JTAG_EV_IR_BEGIN)]
        for _ in range(10):                     # 80-bit "IR" — impossible
            seq += [_tdi(0xFF), _tdo(0x00)]
        seq.append(_ev(decoders.JTAG_EV_IR_END))
        s = _feed_all(ra, seq)[0]
        assert s.kind == "jtag_desync" and ra.desync_count == 1

    def test_dropped_end_bounds_accumulation(self):
        ra = JtagReassembler()
        seq = [_ev(decoders.JTAG_EV_IR_BEGIN)]
        for _ in range(400):                    # dropped IR_END: never stops
            seq += [_tdi(0xFF), _tdo(0x00)]
        seq.append(_ev(decoders.JTAG_EV_IR_END))
        s = _feed_all(ra, seq)[0]
        assert s.kind == "jtag_desync"          # overflow flagged, not a 3200b IR

    def test_tap_reset_recovers_midshift(self):
        ra = JtagReassembler()
        seq = ([_ev(decoders.JTAG_EV_IR_BEGIN), _tdi(0xAA), _tdo(0x00)]  # partial IR
               + [_ev(decoders.JTAG_EV_TAP_RESET)]                       # reset mid-shift
               + _dr_scan_full_bytes([0x93, 0x70, 0x72, 0x23]))          # clean IDCODE
        out = _feed_all(ra, seq)
        assert any(s.kind == "jtag_idcode" for s in out)

    def test_clean_scan_after_desync(self):
        ra = JtagReassembler()
        big = [_ev(decoders.JTAG_EV_IR_BEGIN)]
        for _ in range(10):
            big += [_tdi(0xFF), _tdo(0x00)]
        big.append(_ev(decoders.JTAG_EV_IR_END))
        out = _feed_all(ra, big + _dr_scan_full_bytes([0x93, 0x70, 0x72, 0x23]))
        assert out[0].kind == "jtag_desync"
        assert any(s.kind == "jtag_idcode" for s in out)
