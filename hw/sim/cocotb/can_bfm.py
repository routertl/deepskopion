"""Shared, importable CAN bus BFM for deepskopion cocotb tests (DS-P3.100).

The FIRST promoted datalogger BFM: the i2c/uart/spi/jtag BFMs each live inline in
their test file, but the CAN encode side (CRC-15 + bit-stuffing + frame layout)
is substantial and is needed by BOTH the can_datalogger integration test and the
datalogger_gather routing harness — so it lives here, single-sourced.

Two halves:

  * ENCODE (pure functions) — `build_frame()` lays out a classic-CAN 2.0A/2.0B
    frame (SOF → arbitration ID → control → data → CRC-15 → ACK → EOF), computes
    the real CRC-15 over the de-stuffed covered bits, and applies CAN
    bit-stuffing. Adversarial-from-birth: knobs emit MALFORMED frames
    (`force_crc` corrupts the CRC, `ack=1` leaves the slot recessive/un-ACKed) so
    the error-provenance REQs are driven, not just the happy path.

  * DRIVE — `CanBusBFM` bit-bangs a single RX line (dominant='0'/recessive='1'),
    holding each bit `prescaler*num_tq` clk_sys cycles. It drives ANY signal
    handle (a scalar `dut.CAN_RX` or a 1-bit `dut.CAN_PINS` vector), so the same
    BFM serves the top-level integration tb and the gather pin-mapper tb.

ROUTERTL-002: this is STIMULUS (the encode side). Test checkers assert against
hard-coded expected values, never against a value re-derived from the DUT.
"""
from cocotb.triggers import ClockCycles

EMITS = ("CAN-FD.CRC17", "CAN-FD.CRC21")

POLY = 0x4599                       # CAN CRC-15 generator (x^15+x^14+x^10+x^8+x^7+x^4+x^3+1)


def bits_msb(value, width):
    """MSB-first list of `width` bits of `value` (the CAN wire order)."""
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def crc15(bits):
    """Bosch CRC-15 over the de-stuffed covered bits (SOF..end-of-data)."""
    crc = 0
    for b in bits:
        crcnext = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if crcnext:
            crc ^= POLY
    return crc


def bit_stuff(bits):
    """Insert one complementary bit after every 5 identical (the stuff seed)."""
    out, run, prev = [], 0, None
    for b in bits:
        run = run + 1 if b == prev else 1
        prev = b
        out.append(b)
        if run == 5:
            s = 1 - b
            out.append(s)
            prev, run = s, 1
    return out


def build_frame(can_id, data, ide=False, rtr=False, dlc=None,
                force_crc=None, ack=0):
    """Build a classic-CAN frame's on-wire bit list + a metadata dict.

    Returns (wire_bits, meta). wire_bits = bit_stuff(SOF..CRC) + fixed-form
    trailer (CRC-delim, ACK, ACK-delim, EOF(7), IFS(3)). meta carries:
      crc      — the CRC-15 placed on the wire (computed unless force_crc given)
      covered  — the de-stuffed bits the CRC spans (SOF..end-of-data)
      stuffed  — the stuffed SOF..CRC region (len > len(covered)+15 ⇒ stuffing fired)
      ndata    — data bytes actually emitted (0 for a remote frame)

    Adversarial knobs: `force_crc` puts a wrong CRC on the wire (→ crc_error);
    `ack=1` drives a recessive (un-ACKed) slot (→ ack_error)."""
    if dlc is None:
        dlc = len(data)
    n_data = 0 if rtr else min(dlc, 8)

    covered = [0]                                   # SOF (dominant)
    if not ide:
        covered += bits_msb(can_id, 11)             # 11-bit base ID
        covered += [1 if rtr else 0, 0, 0]          # RTR, IDE=0, r0
    else:
        covered += bits_msb((can_id >> 18) & 0x7FF, 11)  # base ID[28:18]
        covered += [1, 1]                           # SRR=1, IDE=1
        covered += bits_msb(can_id & 0x3FFFF, 18)   # ext ID[17:0]
        covered += [1 if rtr else 0, 0, 0]          # RTR, r1, r0
    covered += bits_msb(dlc, 4)                      # DLC
    for byte in data[:n_data]:
        covered += bits_msb(byte, 8)                # data (MSB-first)

    crc = force_crc if force_crc is not None else crc15(covered)
    stuffed = bit_stuff(covered + bits_msb(crc, 15))  # stuff region = SOF..last CRC bit
    trailer = [1, ack, 1] + [1] * 7 + [1] * 3       # CRC-delim, ACK, ACK-delim, EOF, IFS
    return stuffed + trailer, dict(crc=crc, covered=covered, stuffed=stuffed,
                                   ndata=n_data)


# ── CAN-FD encode (DS-P2.64) ────────────────────────────────────────────────
# FD DLC → data byte count (non-linear table, ISO 11898-1:2015 / Bosch FD v1.0).
FD_DLC_BYTES = {**{i: i for i in range(9)},
                9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64}


def _gray3(n):
    """3-bit gray code of n (0..7), MSB-first."""
    n &= 7
    g = n ^ (n >> 1)
    return [(g >> 2) & 1, (g >> 1) & 1, g & 1]


def fixed_stuff(real_bits, last_wire_bit):
    """CAN-FD fixed stuffing: a stuff bit (inverse of the preceding wire bit) at
    stream positions 0,5,10,… (one stuff + four real). Stimulus only."""
    out, prev, ri = [], last_wire_bit, 0
    while ri < len(real_bits):
        s = 1 - prev
        out.append(s)
        prev = s
        for _ in range(4):
            if ri >= len(real_bits):
                break
            out.append(real_bits[ri])
            prev = real_bits[ri]
            ri += 1
    return out


def _drop_trailing_stuff(dyn, dyn_stuffed):
    """TRAILING DYNAMIC STUFF BIT (``ctu_trailing_stuff``, DS-P2.255 / DS-T2.96).
    When the dynamic region (SOF..end-of-data) ends on a run of 5, the builders
    append a dynamic stuff bit and then fixed_stuff() adds the fixed stuff bit
    before the stuff count: two bits, the first counted in the stuff count. The
    CTU CAN FD core (ctucanfd_ip_core src/can_core/bit_stuffing.vhd: the
    regular->fixed change and the stuff level coincide) inserts ONE stuff bit there,
    a fixed one, outside the stuff count and the CRC, as ISO 11898-1:2015 10.5
    requires ("there shall be only the fixed stuff bit, there shall not be two
    consecutive stuff bits"; DS-T2.96, wire checked bit for bit against the CTU
    core). ``ctu_trailing_stuff=True`` (the default) drops the appended dynamic
    stuff bit, so fixed_stuff() then puts the single (same-valued) fixed stuff bit
    on the wire; the caller passes the stuff count and CRC that exclude it.
    ``False`` builds the old two-bit wire, which no conforming node sends."""
    if len(dyn_stuffed) > len(dyn):
        last_real = dyn[-1][0] if isinstance(dyn[-1], tuple) else dyn[-1]
        last_wire = dyn_stuffed[-1][0] if isinstance(dyn_stuffed[-1], tuple) else dyn_stuffed[-1]
        if last_wire != last_real:
            return dyn_stuffed[:-1]
    return dyn_stuffed


def build_fd_frame(can_id, data, ide=False, brs=0, esi=0, dlc=None,
                   stuff_count=0, force_crc=None, ack=0, ctu_trailing_stuff=True):
    """Build a CAN-FD frame's on-wire bit list + metadata (NOMINAL rate only — a
    BRS=1 dual-rate drive needs the data phase at the fast bit time and is a
    silicon-stimulus concern, DS-P3.15; here BRS is captured/decoded but the wire
    stays single-rate).

    Field order (de-stuffed): SOF, ID, RRS, IDE, FDF=1, res=0, BRS, ESI, FD-DLC,
    data, stuff-count(gray+even-parity), CRC-17/21. Dynamic stuffing covers
    SOF..end-of-data; FIXED stuffing covers stuff-count + CRC.

    `force_crc` puts an explicit value on the wire. The FD CRC coverage (which
    stuffed bits feed the engine + the leading-1 preset) is NOT yet anchored
    (needs a golden FD frame, DS-P3.15), so integration tests pass an ARBITRARY
    CRC and EXPECT a crc_error — validating the whole FD chain EXCEPT the CRC
    verdict, never faking the verdict green."""
    if dlc is None:
        n = len(data)
        dlc = next((k for k, v in FD_DLC_BYTES.items() if v == n), min(n, 15))
    n_data = FD_DLC_BYTES[dlc]
    is21 = n_data > 16
    crc_w = 21 if is21 else 17
    crc = 0x15555 if force_crc is None else force_crc

    # dynamic region: SOF .. end-of-data
    dyn = [0]                                            # SOF (dominant)
    if not ide:
        dyn += bits_msb(can_id, 11) + [0, 0, 1]         # RRS=0, IDE=0, FDF=1
    else:
        dyn += (bits_msb((can_id >> 18) & 0x7FF, 11) + [1, 1]      # base, SRR=1, IDE=1
                + bits_msb(can_id & 0x3FFFF, 18) + [0, 1])         # extID, RRS=0, FDF=1
    dyn += [0, brs, esi]                                 # res=0, BRS, ESI
    dyn += bits_msb(dlc, 4)
    for byte in data[:n_data]:
        dyn += bits_msb(byte, 8)
    dyn_stuffed = bit_stuff(dyn)
    if ctu_trailing_stuff:
        dyn_stuffed = _drop_trailing_stuff(dyn, dyn_stuffed)

    # fixed region: stuff-count (3 gray + even parity) + CRC
    gc = _gray3(stuff_count)
    par = gc[0] ^ gc[1] ^ gc[2]                          # even parity over the field
    stuffcnt_bits = gc + [par]
    real_fixed = stuffcnt_bits + bits_msb(crc, crc_w)
    fixed = fixed_stuff(real_fixed, last_wire_bit=dyn_stuffed[-1])

    trailer = [1, ack, 1] + [1] * 7 + [1] * 3           # CRC-delim, ACK, ACK-delim, EOF, IFS
    wire = dyn_stuffed + fixed + trailer
    meta = dict(crc=crc, crc_w=crc_w, is21=is21, dlc=dlc, n_data=n_data,
                brs=brs, esi=esi, ide=ide, stuff_count=stuff_count & 7,
                gray=(gc[0] << 2) | (gc[1] << 1) | gc[2], parity_ok=True)
    return wire, meta


def _bit_stuff_rated(rated):
    """Dynamic stuffing over (bit, is_data) tuples; a stuff bit inherits the rate
    of the run it terminates."""
    out, run, prev = [], 0, None
    for b, isd in rated:
        run = run + 1 if b == prev else 1
        prev = b
        out.append((b, isd))
        if run == 5:
            s = 1 - b
            out.append((s, isd))
            prev, run = s, 1
    return out


def build_fd_frame_rated(can_id, data, ide=False, esi=0, dlc=None,
                         stuff_count=0, force_crc=None, ack=0, iso_delim=False,
                         ctu_trailing_stuff=True):
    """Build a BRS=1 CAN-FD frame as (bit, is_data) tuples (DS-P3.108 dual-rate
    test). is_data=True from ESI through the FD CRC (the rate-switched span); SOF..
    BRS and the ACK/EOF tail are nominal. BRS is forced 1 (the whole point).
    Dynamic stuffing runs SOF..end-of-data (carrying its rate tag across the BRS
    boundary); the stuff-count + CRC are fixed-stuffed, all data-rate.

    CRC-DELIMITER RATE (``iso_delim``) — the DS-T2.48 fault line.
    Per ISO 11898-1:2015 (CAN FD bit-rate switching; Bosch CAN FD Spec 1.0 §3),
    cross-checked against the ctu-fee/ctucanfd_ip_core oracle (D.26), the data->
    nominal switch-back happens AT the CRC delimiter's SAMPLE POINT — so the
    delimiter's leading phase is still DATA-rate (the bit is shorter than a full
    nominal bit). ``iso_delim=True`` models that (delimiter driven data-rate).
    Default ``False`` keeps the legacy 'delimiter fully nominal' wire, which
    matches the pre-fix RTL (FdDataPhase ends at S_FDCRC) — i.e. BFM and DUT agree
    on the WRONG boundary, which is exactly why sim was green while silicon failed."""
    if dlc is None:
        n = len(data)
        dlc = next((k for k, v in FD_DLC_BYTES.items() if v == n), min(n, 15))
    n_data = FD_DLC_BYTES[dlc]
    is21 = n_data > 16
    crc_w = 21 if is21 else 17
    crc = 0x15555 if force_crc is None else force_crc

    dyn = [(0, False)]                                        # SOF (nominal)
    if not ide:
        dyn += [(b, False) for b in bits_msb(can_id, 11)] + [(0, False), (0, False), (1, False)]  # ID,RRS,IDE,FDF
    else:
        dyn += ([(b, False) for b in bits_msb((can_id >> 18) & 0x7FF, 11)] + [(1, False), (1, False)]
                + [(b, False) for b in bits_msb(can_id & 0x3FFFF, 18)] + [(0, False), (1, False)])
    dyn += [(0, False), (1, False)]                          # res, BRS=1 (nominal)
    dyn += [(esi, True)]                                     # ESI — FIRST data-rate bit
    dyn += [(b, True) for b in bits_msb(dlc, 4)]
    for byte in data[:n_data]:
        dyn += [(b, True) for b in bits_msb(byte, 8)]
    dyn_stuffed = _bit_stuff_rated(dyn)
    if ctu_trailing_stuff:
        dyn_stuffed = _drop_trailing_stuff(dyn, dyn_stuffed)

    gc = _gray3(stuff_count)
    par = gc[0] ^ gc[1] ^ gc[2]
    real_fixed = gc + [par] + bits_msb(crc, crc_w)
    fixed = fixed_stuff(real_fixed, last_wire_bit=dyn_stuffed[-1][0])
    fixed_rated = [(b, True) for b in fixed]                 # fixed region all data-rate

    # CRC-delim rate per iso_delim (see docstring); ACK..EOF/IFS always nominal.
    crc_delim = (1, bool(iso_delim))
    tail = [crc_delim, (ack, False), (1, False)] + [(1, False)] * 7 + [(1, False)] * 3
    wire = dyn_stuffed + fixed_rated + tail
    meta = dict(crc=crc, crc_w=crc_w, is21=is21, dlc=dlc, n_data=n_data, brs=1, esi=esi,
                ide=ide, stuff_count=stuff_count & 7, gray=(gc[0] << 2) | (gc[1] << 1) | gc[2])
    return wire, meta


class CanBusBFM:
    """Raw RX-line driver. `rx` is the signal handle to drive (scalar or 1-bit
    vector); `clk` is the clock handle. Each CAN bit is held `prescaler*num_tq`
    clk_sys cycles — match the datalogger's CONFIG.bit_prescaler."""
    def __init__(self, rx, clk, prescaler=4, num_tq=16):
        self.rx = rx
        self.clk = clk
        self.bit_time = prescaler * num_tq

    async def idle(self, bits=10):
        self.rx.value = 1                           # recessive idle
        await ClockCycles(self.clk, bits * self.bit_time)

    async def drive_bits(self, bits):
        for b in bits:
            self.rx.value = b
            await ClockCycles(self.clk, self.bit_time)

    async def send(self, **frame_kwargs):
        """Encode + drive a frame; return its meta dict (see build_frame)."""
        wire, meta = build_frame(**frame_kwargs)
        await self.drive_bits(wire)
        self.rx.value = 1                           # back to recessive
        return meta

    async def send_fd(self, **frame_kwargs):
        """Encode + drive a CAN-FD frame (nominal rate); return meta (build_fd_frame)."""
        wire, meta = build_fd_frame(**frame_kwargs)
        await self.drive_bits(wire)
        self.rx.value = 1                           # back to recessive
        return meta

    async def drive_bits_rated(self, rated, data_bit_time):
        """Drive (bit, is_data) tuples: data-rate bits held data_bit_time clk_sys
        cycles, the rest at the nominal self.bit_time."""
        for b, isd in rated:
            self.rx.value = b
            await ClockCycles(self.clk, data_bit_time if isd else self.bit_time)

    async def send_fd_dualrate(self, data_bit_time, iso_brs=False, tx_sample_point=None,
                               **frame_kwargs):
        """Encode + drive a BRS=1 dual-rate FD frame; return meta (build_fd_frame_rated).

        BRS-BIT RATE (``iso_brs``, DS-P2.255) — the mirror of ``iso_delim`` at the
        other boundary. Per ISO 11898-1 the TRANSMITTER switches to the data rate AT
        ITS OWN sample point of the BRS bit, so the BRS bit lasts its nominal span up
        to that sample point plus the data-rate phase after the data sample point:
            round(SP_nom * nominal) + round((1 - SP_data) * data)        (clk_sys).
        ``tx_sample_point=(SP_nom, SP_data)`` is the transmitter's pair, as fractions of
        a bit. It is REQUIRED with ``iso_brs=True`` and has no default (DS-T2.97): a
        default equal to the datalogger's own 75 % made the BFM agree with the DUT by
        construction, so a bus at another sample point (Linux sets 87.5 % at 250 kbit/s;
        measured on the Zybo CTU: BRS ends at 350 + 13 = 363 clk of a 400 clk bit)
        could not be seen in sim.
        ``iso_brs=False`` keeps the legacy wire (BRS held a full nominal bit), which
        the datalogger tolerates only while the excess stays under about half a data
        bit: measured clean at nominal 64 and 112 clk, misread from 208 clk with data
        50 clk. ``tx_sample_point`` must then be None."""
        if iso_brs and tx_sample_point is None:
            raise ValueError("iso_brs=True needs tx_sample_point=(SP_nom, SP_data): the "
                             "transmitter's sample points, with no default (DS-T2.97)")
        if not iso_brs and tx_sample_point is not None:
            raise ValueError("tx_sample_point applies only with iso_brs=True")
        wire, meta = build_fd_frame_rated(**frame_kwargs)
        if not iso_brs:
            await self.drive_bits_rated(wire, data_bit_time)
        else:
            sp_nom, sp_data = tx_sample_point
            brs_len = int(round(sp_nom * self.bit_time)) + int(round((1 - sp_data) * data_bit_time))
            first_data = next(i for i, (_, isd) in enumerate(wire) if isd)
            for i, (b, isd) in enumerate(wire):
                self.rx.value = b
                if i == first_data - 1:          # the BRS bit
                    n = brs_len
                else:
                    n = data_bit_time if isd else self.bit_time
                await ClockCycles(self.clk, n)
        self.rx.value = 1
        return meta
