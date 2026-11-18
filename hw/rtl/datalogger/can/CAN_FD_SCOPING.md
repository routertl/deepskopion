# CAN-FD datalogger — scoping & architecture (v0.2 DRAFT)

Extends the silicon-validated classic-CAN datalogger (`v0.3.0-can-emio-silicon`,
checkpoint C: byte-exact decode of STD/extended/remote/stuffed
frames on a Zybo Z7-20) to **CAN-FD**. This doc is the **design + build plan**
captured from the design walkthrough. The test contract is the
spec-linked `requirements.yml` (`CAN-REQ-12..23`); the message contract is
`can_datalogger_protocol.yml` (FD type/event space already reserved). No FD RTL
yet — this scopes it.

> **Status of the pieces:** contract written (CAN-REQ-12..23, 11 spec-linked to
> ISO 11898-1:2015 + Bosch CAN FD v1.0, CAN-REQ-23 the declared non-spec
> exception). RTL = paper design below. BFM/verification = strategy below.
> Tooling notes: requirements-schema migration; FD frame source for silicon = CTU CAN FD.

---

## 1. Why CAN-FD is the demon (what breaks vs classic)

CAN-FD keeps the same wire and arbitration but breaks every assumption the v0.1
FSM/timing makes:

| | Classic CAN | CAN-FD |
|---|---|---|
| Payload | 0–8 bytes | **0–64** (DLC 9–15 → 12/16/20/24/32/48/64) |
| Bitrate | one rate, whole frame | **two** — slow arbitration, **fast data phase** (BRS) |
| CRC | CRC-15 | **CRC-17** (≤16 B) / **CRC-21** (>16 B) + fixed stuff bits |
| New control bits | — | **FDF** (it's FD), **BRS** (rate switch), **ESI** (error state) |
| Remote frames | RTR | **gone** — RTR slot is RRS, always dominant |
| Stuffing | dynamic everywhere | dynamic in arbitration; **fixed** in stuff-count + CRC |

**The validation curveball:** the Zynq-7 PS CAN controllers are **classic-only**
— they cannot emit FD frames. So the fabric 2-node loopback that validated v0.1
on silicon **cannot be the FD stimulus.** Validation begins in **sim**;
silicon validation later needs a soft FD source (CTU CAN FD) or an
external FD device. Sequence: bit-rate detector → FSM FD-fork → CRC-17/21 →
destuff modes → host decode → *then* silicon via a PL FD source.

---

## 2. Block-by-block attack plan

Pipeline today (all silicon-validated):
```
RX ─► can_bit_timing ─► can_destuff ─► can_frame_fsm ─► zs_msg
       (16 TQ, 1 rate)   (dynamic)      (S_IDLE…S_EOF)    (CRC-15)
```

| Block | Change | Difficulty |
|---|---|---|
| `can_bit_timing` | **dual-rate** unified direct-count sampler; switch on BRS, back on CRC-delim | 🔥🔥🔥 |
| `can_frame_fsm` | FD fork at FDF; `res/BRS/ESI` states; DLC 0–64; CRC-engine select; FD stuff region | 🔥🔥 |
| `can_destuff` | **two modes** — dynamic + fixed-position | 🔥 |
| `can_crc17` / `can_crc21` | two new CRC engines (classic `can_crc15` stays) | 🔥 |
| `can_datalogger` top | wire `DATA_PRESCALER` from CONFIG; route FDF/BRS/RATE_SEL/STUFF_MODE | easy |
| `DATALOGGER_CONFIG` | add a `data_prescaler` field (config word grows) | easy |
| host decode | zs_msg events for FDF/BRS/ESI + stuff-count; reassembler → 64 B, CRC-17/21 | medium |

---

## 3. Bit-rate detector — the unified direct-count sampler (B2)

**Decision: B2 — both phases are direct cycle-counters, one sampler with two
`{cyc_per_bit, sample_cyc}` config pairs.** Rationale (from the walkthrough):

- The prescaler/TQ subdivision is a **slow-signal** tool — it buys fine
  sample-point placement + a wide rate range. Both benefits **vanish** when a
  bit is only tens of cycles long (fast data phase). Worse: at 100 MHz, 2 Mbit
  = 50 cyc/bit ⇒ `presc = 50/16 = 3.125` — **not representable**. Same wall as
  UART oversampling at 1 Mb/s.
- FF-count is a wash (`log2(cyc/bit)` bits either way); the real win is
  **representability + honest sample placement**. So drop the prescaler/TQ
  abstraction; count cycles directly.

```
 RX ─►[2FF sync + edge det]─►┌───────────────────────────────────────────┐
                             │ counter 0..cyc_per_bit-1                    │─► BIT_START @ 0
   {cyc_per_bit, sample_cyc} │ hard-sync: falling edge → counter:=0        │─► BIT_VALID @ sample_cyc
        ▲ RATE_SEL picks ────┤ sample BIT_VALUE when counter==sample_cyc   │
        (nom pair / data pair)└───────────────────────────────────────────┘
```
Nominal pair `{400,300}` ≡ TQ12-of-16 @ presc 25 (250 kbit). Data pair e.g.
2 Mbit `{50,37}`.

**Refactor safety (CAN-REQ-23, the silicon-validation carry-over):** B2 rewrites
the *silicon-validated* v0.1 timing block, but its nominal config is
**bit-identical by construction** — `sample_cyc = G_SAMPLE_TQ × presc`,
`cyc_per_bit = G_NUM_TQ × presc`. The de-risk is a **back-to-back equivalence
TB**: drive the SAME stimulus (randomized + the **real captured silicon edges**
from the v0.3.0 bring-up) into the old TQ block and the new unified block on the
nominal config, assert `BIT_VALID/BIT_VALUE/BIT_START` are bit-identical. Green
⇒ the proven behaviour transfers. The metastability-sensitive front-end (2-FF
sync + edge detect) is **untouched** — only the deterministic counter changes,
so silicon risk isn't reopened.

---

## 4. FSM ↔ timing handshake — the rate switch

Only the FSM knows frame structure, but it learns a bit's value *after* it's
sampled+de-stuffed — that lag is the whole problem.

```
        ┌──────────────────────────┐   RATE_SEL (level)         ┌─────────────────────────┐
        │        can_frame_fsm      │ ─────────────────────────► │  can_bit_timing (B2)    │
        │  RATE_SEL = brs_r AND      │   1 = data, 0 = nominal    │  edge-detect RATE_SEL:  │
        │   state ∈ data-phase set   │ ◄──── DS_VALID/DS_BIT ──── │  on ANY edge →          │
        └──────────────────────────┘   (the stream it decides on)│  FORCED RESYNC          │
                                                                  │   load pair; re-anchor  │
                                                                  └─────────────────────────┘
```

- `RATE_SEL` is a **Moore level**, not a pulse — `'1'` while in any data-phase
  state — so it rises entering `S_ESI` and falls entering `S_CRCDELIM`,
  satisfying CAN-REQ-13 (switch on BRS) + CAN-REQ-22 (switch back) in one line.
  **Gated by `brs_r`** (the captured BRS bit): a BRS=dominant FD frame stays
  nominal — so `RATE_SEL` is state AND `brs_r`, *not* pure Moore.
- The timing block triggers the **forced resync** on the `RATE_SEL` *edge*
  (1-FF delayed copy) and loads the new pair. That's CAN-REQ-14: the resync
  without a falling edge to lock to. On the rising edge the data phase continues
  from the data sample point: the switch happens at the sample point of the BRS
  bit, which is the data-rate sample point of the same bit, and the rest of BRS is
  the data phase-2 segment (`cnt := DATA_SAMPLE_CYC + clocks since the BRS
  sample`). On the falling edge (CRC delimiter) `cnt := 0`.
- **The sample points are the bus's, not a constant.** The transmitter switches
  at ITS nominal sample point. Both sample points are software-set in 1/256 of a
  bit (CAN-REQ-26; reset 75 %). Measured on the Zybo CTU cores at 250 k / 2 M:
  87.5 % nominal (switch 350 clk into the 400-clk BRS) and data phase-2 13 clk, so
  BRS ends at 363 clk. A datalogger left at 75 % switches at 300 clk, samples ESI
  inside BRS and reads every later field one bit late.

**Latency (250 k / 2 M, bus sample points set):** BRS sampled at `cnt=350`; ~2–3
cyc later the FSM state flips to `S_ESI` and raises `RATE_SEL`; the timing block
continues the data phase from that sample, so ESI is sampled within a few clk of
the transmitter's own data sample point.
We get ~100 cyc of slack (25 % of a nominal bit) vs ~3 cyc of FSM latency — never
late — and the first data-phase falling edge hard-syncs us exactly (convergence
1–3 data bits, bounded). **Caveat to test (CAN-REQ-14):** the very first data
bit can be sampled a hair off-centre until that first edge — drive an
edge-starved data run and assert no mis-sample.

---

## 5. De-stuffer mode switch

```
 DYNAMIC (SOF..end-of-data):  value-driven — stuff after 5 identical (current RTL)
 FIXED   (stuff-count + CRC):  position-driven — a stuff before the field + after every 4 bits,
                               each = complement of the previous bit
```
Add **`STUFF_MODE`** (Moore: `'1'` in `{S_STUFFCNT, S_FDCRC}`) alongside the
existing `STUFF_EN`. **Different boundary than the rate switch** (data→stuff-count,
not BRS). Latency is easy — the FSM commands off a deterministic DLC-derived data-bit
count (no edge to wait for).

**The corner — dynamic→fixed adjacency (CAN-REQ-17 TB vector):** if the dynamic
region ends on 5 identical bits, the wire carries **one** stuff bit before the
stuff-count: the fixed one. ISO 11898-1:2015 10.5: "There shall be a fixed stuff
bit before the first bit of the stuff count, even if the last bits of the preceding
field are a sequence of five consecutive bits of identical value, there shall be
only the fixed stuff bit, there shall not be two consecutive stuff bits."
```
 …[last data bit = 5th of a run]  [fixed stuff]  [stuff-count b0]…
                                   satisfies run==5 AND is pos==0
```
That bit is not counted in the stuff count and not covered by the CRC (the CTU CAN
FD core sends exactly this wire). Resolution: on `STUFF_MODE` rising at `run==5`,
the de-stuffer checks the bit as the pending stuff (6 identical → `STUFF_ERROR`)
and consumes it as fixed position 0; the next bit is stuff-count b0. Fixed stuff
bits are **value-checked** (must be complement of prior) → `FORM_ERROR` on mismatch,
never a silent drop.

```
 STUFF_EN ───┐                ┌── DYNAMIC engine (run-counter, current) ──┐
 STUFF_MODE ─┤                │     drop @run==5, check → STUFF_ERR        ├─MUX─► OUT
 IN_VALID ───┼──────────────► │                                            │      + FIXED_STUFF_ERR
 IN_BIT      │                └── FIXED engine (pos-counter mod 5) ─────────┘
             │                      drop @pos==0, check → FORM_ERR
             │                      ▲ anchor: first bit after STUFF_MODE rises
```

---

## 6. FSM FD fork

**FDF reuses a classic reserved bit — position depends on frame type:**
- STANDARD: FDF = classic `r0` → state **`S_R0`**.
- EXTENDED: FDF = classic `r1` → state **`S_R1`** (and `r0` becomes `res`).

The fork is **additive** — at the FDF position, sample the bit: dominant ⇒ the
classic untouched path (CAN-REQ-12, no regression, for free); recessive ⇒ FDF=1,
branch into the FD chain.

```
 ──S_IDE──IDE=0──► S_R0 ──┬─dom (r0)─────────────────────────► S_DLC ─►S_DATA─►S_CRC15─►S_CRCDELIM─►S_ACK─►…
                          └─rec (FDF=1)──┐                                                  ▲  (CLASSIC: untouched)
 ──S_EXTRTR──► S_R1 ──┬─dom (r1)──► S_R0(=r0)──► S_DLC ─►…(classic ext)                     │
                      └─rec (FDF=1)──┤                                                      │
                                     ▼                                                      │
              S_FDRES ─► S_BRS ─► S_ESI ─► S_FDDLC ─► S_FDDATA ─► S_STUFFCNT ─► S_FDCRC ────┘
                ▲          │capture │capture   │decode     │count        │fixed stuff   │CRC17/21
              (res bit)   brs_r    esi_r     dlc→bytes,  data bytes
                                              crc_sel
```

**The asymmetry gotcha:** `S_R0` is reached two ways — from `S_IDE` (standard ⇒
FDF fork) *and* from `S_R1`-dominant (extended-classic ⇒ it's just `r0`, decision
already made). Guard it with the `ide_r` flag (latched at `S_IDE`):
```
 at S_R0:  if ide_r='1' (ext) → S_DLC          -- classic r0, no fork
           else (std): rec → S_FDRES | dom → S_DLC   -- the FDF fork
 at S_R1:  rec → S_FDRES | dom → S_R0           -- the extended FDF fork
```
Both FD paths **converge at `S_FDRES`** → one FD chain to maintain, not two. The
tail (`S_CRCDELIM/S_ACK/S_ACKDELIM/S_EOF`) is **shared** — always nominal-rate,
fixed-form.

**Control outputs are all born in this chain** (FSM = single source of frame
structure):

| Signal | Driven | Subtlety |
|---|---|---|
| `STUFF_EN` | `S_SOF … S_FDCRC` | extended to FD stuffed fields |
| `STUFF_MODE` | `{S_STUFFCNT, S_FDCRC}` | fixed stuffing (REQ-17) |
| `RATE_SEL` | `brs_r AND state ∈ {S_ESI…S_FDCRC}` | gated by captured BRS (REQ-13) |
| `CRC_SEL` | latched after `S_FDDLC`: ≤16 B→17, >16 B→21 | from DLC→bytes (REQ-19/20) |

`S_FDDLC` decodes the **non-linear** DLC table (REQ-16) feeding both the
`S_FDDATA` byte counter and `CRC_SEL`; `S_ESI` captures the error-state flag
(REQ-15).

---

## 7. CRC-17 / CRC-21 + DLC table (datapath — next to design in detail)

- `dlc_to_bytes(dlc)`: 0–8 ⇒ that many; 9–15 ⇒ 12/16/20/24/32/48/64. Lift the
  table FROM the spec (REQ-16).
- `can_crc17` (≤16 B) / `can_crc21` (>16 B), selected by `CRC_SEL`. Polynomials
  per ISO 11898-1 (REQ-19/20) — **quote the hex from the spec, verify; do not
  hand-derive.** Fixed stuff bits ARE included in the covered bits per spec.

---

## 8. BFM & verification strategy

Extend `hw/sim/cocotb/can_bfm.py` (`CanBusBFM`, already "adversarial-from-birth")
with `build_fd_frame()`: FD field order, dynamic stuffing through data, stuff-count
(gray+parity), fixed stuffing in stuff-count+CRC, `crc17()/crc21()`, and a
**dual-rate `drive_bits`** (nominal arbitration, data post-BRS, switching at the
BRS sample point) — which is exactly what exercises `RATE_SEL`/forced-resync.

**THE central hazard — circular green.** Classic got away with the BFM as oracle
because CRC-15 is folklore. CAN-FD's CRC-17/21 are not: new polynomials, fixed
stuff bits in the covered bits, init, stuff-count gray+parity. Write `crc17()` in
the BFM and `can_crc17.vhd` from the **same misreading** of the spec and the test
goes **green and wrong** — the testbench's own `registry fake-green`. A spec
*cite* (REQ-19/20) doesn't execute; you need an **independent oracle**.

```
 LAYER 0 — GOLDEN ANCHOR (a handful, fully hard-coded, from an EXTERNAL reference)
   reference FD frames: {id,data,brs} → known on-wire bits INCLUDING CRC, from a
   source that is NOT our code: Bosch/ISO worked CRC examples • CTU CAN FD model
   frames • an independent crc17/21 reference impl.
     assert BFM.build_fd_frame(...) == golden bits   → validates the BFM
     assert datalogger decodes them byte-exact          → validates the RTL
 LAYER 1 — FUZZ (broad, BFM now TRUSTED)
   random {id,ide,dlc,data,brs} → BFM builds spec-legal → datalogger decodes;
   hard-coded expected = the test's CHOSEN id/data/dlc (ROUTERTL-002).
 LAYER 2 — ADVERSARIAL (the hand-designed corners; each must be able to FIND a bug)
```
**Layer 0 is the keystone** — the only layer whose expected values come from
*outside our two implementations*, so the only one that catches a shared BFM+RTL
spec misreading. This is where "link to the public spec" stops being a doc nicety
and saves the project: the spec's worked example *is* Layer 0.

**Adversarial map (Layer 2 → REQ):**

| Vector | Targets | Bug hunted |
|---|---|---|
| Last 5 data bits identical | CAN-REQ-17 | dynamic→fixed stuff adjacency → stuff-count off |
| Edge-starved data run | CAN-REQ-14 | forced-resync drift before first edge |
| DLC = 8 / 9 / 15 (→8/12/64 B) | CAN-REQ-16 | non-linear DLC table off-by-one |
| Payload 16 vs 20 B | CAN-REQ-19/20 | CRC-17↔CRC-21 selection boundary |
| FD frame, BRS dominant | CAN-REQ-13 | datalogger switches rate when it shouldn't |
| Bad fixed-stuff / parity / corrupt CRC | CAN-REQ-17/18/19 | silent accept instead of form/crc_error |
| Classic + FD interleaved | CAN-REQ-12 | FD fork regresses classic decode |

Definition-of-done bites: **a green Layer-2 that finds nothing is a yellow flag.**
New FD RTL has bugs; the adversarial layer's job is to surface them as T-tickets.

**Sanctioned flow:** `requirements.yml` → `rr sim scaffold` (skeletons anchored to
`@requires(CAN-REQ-N)`) → fill BFM stimulus + hard-coded asserts → `rr sim run` →
`rr sim coverage-map` enforces the mapping. Every file ends with
`engine.simulation.run_simulation(...)` (ROUTERTL-001).

---

## Locked decisions (v0.2 design) — build against these

1. **B2 unified direct-count sampler** (not dual-prescaler, not fast-clock).
   Nominal config bit-identical to v0.1 by construction; equivalence TB carries
   the silicon validation (CAN-REQ-23).
2. **Forced resync anchored to the BRS bit boundary** (decision (i)); first
   data-phase edge refines.
3. **`RATE_SEL` = Moore level gated by `brs_r`**; forced resync on its edge.
4. **`STUFF_MODE` separate Moore line**, switches at data→stuff-count; honors a
   pending dynamic stuff before fixed positional counting.
5. **FDF fork additive**, guarded by `ide_r`; std forks at `S_R0`, ext at `S_R1`;
   both converge at `S_FDRES`. Classic path bit-for-bit untouched.
6. **Verification = golden anchor → fuzz → adversarial.** Layer 0 (external
   golden) is mandatory; CRC/stuffing constants lifted from the spec, not derived.
7. **Sim is primary** (PS CAN is classic-only); silicon FD stimulus via CTU CAN FD.

## Staged plan (sim-first, every step hardware-free until silicon)

1. `can_bit_timing` → B2 unified sampler + equivalence TB (CAN-REQ-23, -14).
2. `can_crc17`/`can_crc21` + Layer-0 golden CRC vectors (CAN-REQ-19/20).
3. `can_destuff` fixed mode + adjacency TB (CAN-REQ-17/18).
4. `can_frame_fsm` FD fork + control outputs (CAN-REQ-12/13/15/16/22).
5. Full-chain `can_datalogger` FD + gather harness (Layer 1 fuzz, Layer 2 adversarial).
6. Host decode: zs_msg FD events + reassembler 0–64 B + CRC-17/21.
7. Silicon: CTU CAN FD source → repeat checkpoint C on FD frames.
