# CAN datalogger — scoping & architecture (v0.1 DRAFT)

Passive CAN / CAN-FD bus datalogger for deepskopion. This doc is the **design +
build plan** captured from the initial scoping session. The test contract is the
spec-linked `requirements.yml` (`CAN-REQ-1..11`); the message contract is
`can_datalogger_protocol.yml`. No RTL exists yet (that's the point — this doc
pins the shape before synthesis / simulation).

---

## 1. What we are building

A single-channel passive listener that connects to a CAN bus (via an external
transceiver or a routed-fabric loopback) and emits standard 24-byte `zs_msg`
frames over the deepskopion gather/framer/DDR pipe.

- **Standard-format datalogger** (`C_ZS_MSG_CLASS_DATA = 0x00`, `DOMAIN_CAN = 0x05`,
  uses the standard 3-bit type field `[26:24]`, symmetric lifecycle markers).
- **Classic CAN 2.0A/B first**: standard (11-bit) and extended (29-bit) IDs,
  data frames (0..8 B), remote frames (RTR=1), error frame detection,
  overload frame ignore, bus-off detection.
- **Configurable bitrate**: bit-timing register (`prescaler`, `prop_seg`,
  `phase_seg1`, `phase_seg2`, `sjw`) written via `DATALOGGER_CONFIG` register.
  Optional auto-bitrate detection in Phase 2.
- **FD-ready entity**: the port shape and internal FSM forks leave clean seams
  for CAN-FD (FDF / BRS / ESI / 64-byte payload / CRC-17 / CRC-21) without
  breaking the 2.0A/B core.
- **Reuses existing infrastructure**:
  - `config_control` for device-id matching and enable/config latching.
  - `glitch_filter` on the RX pin (debounces transceiver edge ringing).
  - `datalogger_lifecycle` component for ENABLED / DISABLED frames.

---

## 2. Re-use vs. build-new

Do **not** write a complete CAN controller. Deepskopion is a *datalogger*
(passive listener), not a node. We do not transmit, do not arbitrate, do not
acknowledge (ACK slot is passively sampled, not driven dominant), do not emit
active error flags (passive error monitoring only).

What that saves:
- No TX path, no bit-stuffing on TX, no TX queue / priority arbiter.
- No ACK driver (we must NOT disturb the bus — pure listen-only).
- No error-frame *generation* (we observe bus error frames, never drive them).
- Transceiver delay compensation (TDC) can be deferred to CAN-FD high-rate.

Reuse: `config_control` (enable latch, hardened), the message-RAM/FIFO pattern,
the `glitch_filter` (via the gather), and the standard CRC-15/LFSR primitives.

---

## 3. Bus & frame facts the RTL must honour (classic CAN 2.0A/B)

- Wire levels: **dominant = 0**, **recessive = 1**; idle bus = recessive.
- Frame: `SOF(1 dom)` → **arbitration** `ID[10:0]` (+`SRR`,`IDE`,`ID[28:11]` for
  extended) `+ RTR` → **control** `IDE,r0,DLC[3:0]` → **data** `0..8 bytes`
  (MSB-first) → **CRC-15 + delim** → **ACK slot + delim** → **EOF (7 rec)** →
  **IFS (3 rec)**.
- **Bit stuffing** (SOF..CRC): after 5 consecutive identical bits the
  transmitter inserts one complementary stuff bit; the datalogger must remove them.
  6 identical bits where a stuff bit was due = **stuff error**.
- Standard bit rates: 125k/250k/500k/1 Mbit. v0.1 targets a **configured** rate.

## 3. RTL architecture — `can_datalogger.vhd` (the next-session build)

Standard datalogger entity (mirror `jtag_datalogger`): ports `CLK, RST,
CAN_PINS(0 downto 0)=RX, CONFIG, TIMESTAMP, CAN_MSG_OUT/VLD/RDY, CAN_ERRORS`.
Internally, four staged blocks feeding the same message-RAM/FIFO pattern the
other dataloggers use:

1. **Bit-timing recovery** (`bit_timing`): oversample RX on `clk_sys`; hard-sync
   the bit clock on the SOF recessive→dominant edge; CAN soft-resync (SJW) on
   every recessive→dominant edge; emit a 1-cycle `bit_sample` strobe at the
   configured sample point + the sampled bit. v0.1 uses `CONFIG.bit_prescaler`
   (no auto-baud). *This is the one genuinely new building block.*
2. **De-stuff** (`destuff`): track the run of identical sampled bits; drop the
   stuff bit after 5; flag `stuff_error` on 6. Gated to the stuff-active window
   (SOF..CRC). Outputs an unstuffed `bit_valid`/`bit` stream.
3. **Frame FSM** (`frame_fsm`): the SOF→ARB→CTRL→DATA→CRC→ACK→EOF state machine.
   Shifts the unstuffed bits into ID / DLC / data / CRC accumulators; runs the
   running **CRC-15**; watches the fixed-form fields (form/ack errors).
4. **Message staging**: on field completion, stage the
   `frame_begin/control/id/data/crc/ack/frame_end` messages (capture order)
   into the shared message-RAM → FIFO → `CAN_MSG_*` handshake — identical
   plumbing to `jtag_datalogger` (up to N staged msgs per event, pushed 1/clk).

Reuse: `config_control` (enable latch, hardened), the message-RAM/FIFO
+ lifecycle emit pattern from `jtag_datalogger`, `glitch_filter` (via the gather).
CRC-15 is a small LFSR. **No CDC in v0.1** (oversampled, single clock) — same
simplification jtag P1 took; a TCK-style fast front-end is a later option.

## 4. Declaration diffs — APPLY WHEN THE RTL LANDS (not before)

> Injecting these before `can_datalogger.vhd` exists would make
> `datalogger_gather_gen.py` instantiate a missing entity and break the build.
> They live here until the RTL is ready; then apply + regenerate.

**a. `system_ids.yml`** — flip the reserved domain to active:
```yaml
  can:  { code: 0xA, status: active, description: "CAN / CAN-FD" }   # was: reserved
```
then `python3 tools/datalogger-builder/id_gen.py gen && … check`.

**b. `tools/datalogger-builder/protocols.yml`** — register the metadata:
```yaml
  CAN:
    category: Burst
    entity: tich.can_datalogger
    signals: [RX]                 # single post-transceiver line -> CAN_PINS(0)
    max_freq_hz: 1_000_000        # classic CAN <=1 Mbit -> glitch window 4 (real rejection)
    generics:
      G_CAN_ID_WIDTH: C_DATALOGGER_ID_WIDTH
      G_CAN_DEV_ID: "{id}"
      G_TIMESTAMP_WIDTH: C_TIMESTAMP_WIDTH
      G_CONFIG_WIDTH: C_DATALOGGER_CONFIG_WIDTH
      G_ERROR_WIDTH: C_ERROR_WIDTH
    ports:
      CLK: CLK
      RST: RST
      CAN_PINS: "{mapped_pins}"
      CONFIG: CONFIG
      TIMESTAMP: TIMESTAMP
      CAN_MSG_OUT: "{msg_out}"
      CAN_MSG_VLD: "{req}"
      CAN_MSG_RDY: "{ack}"
      CAN_ERRORS: "{errors}"
    arbiter_fifo_depth: 8
```

**c. `tools/datalogger-builder/config/datalogger_config.yml`** — instance (id 20, free;
single RX line on a free pin — 28 is free, next after jtag's 24..27):
```yaml
    Can:
      20:
        RX: 28
```
then `python3 tools/datalogger-builder/datalogger_gather_gen.py` (regenerates the
gather block + `C_CAN0_*` + the id-drift-gate) and rebuild.

## 5. BFM & test plan

- **BFM:** a small cocotb `CanBusSource` (TapMaster-style, self-contained):
  given an ID + data + frame-type, it computes the stuffed bitstream + CRC-15
  and bit-bangs RX at the configured rate (driving dominant/recessive, holding
  each bit `prescaler` `clk_sys` cycles). It can also emit *malformed* frames
  (bad CRC, stuff error, no ACK) for the adversarial REQs. (Evaluate
  `cocotbext-can` — but it targets a CAN *controller* DUT; a passive datalogger
  wants a raw RX-line driver, so a ~80-line custom source is likely cleaner and
  has no extra dependency.)
- **Unit tests** (`hw/sim/cocotb/tests/datalogger/can/`): one per CAN-REQ, hard-coded
  expected ID/data (ROUTERTL-002). Std frame, ext frame, de-stuff, RTR, CRC
  good/bad, stuff error, ACK/no-ACK, back-to-back.
- **Gather harness** (CAN-REQ-11): `tb_datalogger_gather_can.vhd` +
  `test_datalogger_gather_can.py` — the family pattern (map RX→`CAPTURE_PINS[28]`,
  rest idle high, enable id 20, drive a frame, assert byte-exact ID/data routing
  to `DOUT_DATALOGGER_ID=20`). Use a burst with a **repeated data byte**.
- **Definition-of-done (sim):** this is fertile bug-hunting ground — de-stuff
  boundaries, CRC over stuffed-vs-unstuffed bits, the ARB→CTRL transition,
  sample-point edge cases. Expect to FILE CAN-T defects, not just go green.

## 6. Staged plan

- **P1 (initial effort):** classic CAN 2.0A/B, configured bit rate,
  CRC-15, the frame FSM + de-stuff + the full message set above. Sim only.
- **P2 (future):** CAN-FD (FDF/BRS/ESI, stuff-count, CRC-17/21, 0..64 B payload —
  the reserved `0b110/0b111` types + `CAN-REQ-12/13`); auto-bitrate; a
  TCK-style fast front-end if needed.
- **P3 (future):** silicon bring-up on a CAN transceiver PMOD; LIN as a cheap
  sibling (single-wire, UART-framed) reusing this scaffold.

## Locked decisions (v0.1) — settled, build against these

1. **Bit timing — cycles-per-TQ prescaler, fixed TQ structure.**
   `CONFIG.bit_prescaler` = `clk_sys` cycles per **time quantum (TQ)**; the bit
   is a **fixed 16 TQ**; **sample point at TQ 12 (75%)**; **SJW = 1 TQ** resync.
   (Later superseded for CAN FD: both sample points are software-set to the bus's,
   in 1/256 of a bit, reset 75 %; see CAN-REQ-26 and CAN_FD_SCOPING.md.)
   Bit clock hard-syncs on the SOF recessive→dominant edge and soft-resyncs (±1
   TQ) on every recessive→dominant edge. This mirrors a real CAN controller's
   BRP and is plenty for a passive datalogger — no per-segment programmability in
   v0.1. (Example: 500 kbit @ 100 MHz → 200 cyc/bit → prescaler = 12 cyc/TQ,
   round; 1 Mbit → 100 cyc/bit → 6 cyc/TQ rounding to 96 cyc/bit.)
2. **CRC — stream `crc_payload` AND flag `crc_error`.** SW gets the raw captured
   CRC *and* the datalogger's independent CRC-15 verdict. Two msgs/frame is cheap;
   capture + verification is the datalogger's whole value.
3. **Emit order — capture/wire order.** The datalogger streams messages as fields
   complete on the wire (begin → control → id → data → crc → ack → end); SW
   groups by `frame_begin..frame_end` of the same `device_id`. Simplest RTL, no
   reorder buffer.
