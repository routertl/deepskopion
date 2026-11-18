# Trigger Engine Architecture

The system handles Triggers by treating the **Trigger Engine** as a specialized "Virtual Datalogger".

## 1. Core Concept: "Trigger as a Datalogger"

Instead of complex hardware logic to manage pre/post-trigger buffers in the PL, the Trigger Engine simply:
1.  **Monitors** internal event buses (Event Codes from other dataloggers).
2.  **Matches** configured conditions (e.g., "UART Parity Error AND I2C Start").
3.  **Emits** a strictly timestamped `TRIG_MATCH` message into the standard data stream.

### Why this approach?
*   **Simplicity:** The PL focuses on mechanisms (timestamps, matching), while the PS (Software) handles policy (recording depth, file management).
*   **Robustness:** Avoiding complex "circular buffer rewind" logic in hardware reduces bugs.
*   **Flexibility:** All data flows through the same DMA path. The software can decide offline whether to keep 1 second or 1 hour of data around the trigger.

## 2. Message Flow & Ordering
*   **Timestamping:** The timestamp is latched at the exact cycle the condition is met.
*   **Transport:** The `TRIG_MATCH` message enters the same FIFO/DMA path as the data.
*   **Ordering:** Even if the trigger message arrives slightly out of order relative to high-bandwidth data bursts, the **Global Timestamp** serves as the universal sorting key.

## 3. Software Handling Strategies

Since the Trigger is just a message, software has two main modes of operation:

### A. Streaming Mode (Markers)
*   **Action:** Software captures everything continuously to disk/network.
*   **Logic:** When `TRIG_MATCH` is detected, it logs the Event and Timestamp in a side channel or index.
*   **Use Case:** Long-term logging where the user wants to "Find all occurrences of X" later.

### B. Capture Window Mode (Scope-like)
*   **Action:** Software mimics a traditional oscilloscope.
*   **Logic:**
    1.  DMA writes to a Ring Buffer in RAM.
    2.  Software parses incoming messages.
    3.  When `TRIG_MATCH` is seen:
        *   Software allows $N$ more bytes/time to be captured (Post-Trigger Buffer).
        *   Software stops the capture.
        *   Software flushes the Ring Buffer to the UI.
*   **Use Case:** Real-time waveform debugging.

### C. Pause-on-Fill Mode (Forensic / One-Shot)
*   **Action:** Like Capture Window but the ring **stays frozen** after dispatch — no automatic resume.
*   **Use Case:** Long unattended runs where the rare bug must be captured immutably; the post-trigger window can't be allowed to overwrite the gold sample.
*   **Mode:** Distinct mode bit in the FSM action surface; consumer issues an explicit regbank restart command to resume.

## 4. Multi-Trigger Support
Because triggers carry IDs (`trigger_id`), the system naturally supports complex workflows:
*   `ID=1`: "UART Error"
*   `ID=2`: "External GPIO Trigger"
*   `ID=3`: "Pattern Match"

Software can filter, count, or color-code these events independently.

---

## 5. The HVI-Style Roadmap (where this is going)

The "Trigger as a Datalogger" foundation above is correct and load-bearing. The full architecture extends it into a **declarative-authoring → compile-to-FSM → cycle-accurate-execution** pipeline, modelled on Keysight's HVI (Hardware Virtual Instrument) on the M3xxx AWG/digitizer platform.

```
Authoring  (host-side, declarative)
  user writes YAML / DSL describing the trigger as a state graph:
  states, transitions, conditions, timeouts, actions on entry
                ↓
Compilation  (routertl-side, deterministic)
  rr trigger compile emits regbank programming sequence:
  - state-transition table (BRAM image)
  - condition-match patterns + masks
  - timeout counter init values
  - action enables (TRIG_MATCH emit, PMOD pulse, regbank reconfig, ring freeze...)
                ↓
Execution  (PL-side, deterministic, no host involvement in the matching loop)
  trigger_dispatcher IP loads the table at boot via regbank.
  FSM runs against the datalogger event stream with zero PS round-trip.
  TRIG_MATCH messages enter the data path on final-state entry.
  Actions fire at cycle-accurate timing.
```

### Phasing

| Phase | Scope | Status |
|---|---|---|
| **Phase 1** | Staged FSM, hard-baked at synth time, single-channel sequencing (SOF → EOF → ERROR → fire). Smallest valuable shipped slice; proves the model end-to-end without runtime reload. | Implemented |
| **Phase 2** | BRAM-backed table + runtime regbank reload — the HVI-style win. Generic 16-state × 4-condition dispatcher (~200-500 LUTs). DSL → table compiler. Reprogramming without re-synth. | Planned |
| **Phase 3** | Cross-instrument time-grid sync. Multi-deepskopion triggers ("fire when board A sees X AND board B sees Y within 1 µs"). Needs the shared-clock distribution story. | Planned |

### Sibling capabilities (plug into the same dispatcher IP)

| Feature | Mechanism |
|---|---|
| **Temporal-AND across channels** | Per-condition match latch + countdown timer + AND gate at expiry. Conditions don't need ordering — only co-presence within a rolling window. |
| **Pre-roll window semantics** | Explicit `pre_ms` + `post_ms` + `mode` in the on_match action. PL signals `stop_overwrite_after = post_ms_in_clocks` to ring controller; PS slices the window from the DDR ring on freeze. Ties to DDR-ring sizing — pre-roll's upper bound = ring's natural retention (1–5 s on Zybo Z7-20, 30–60 s on ZynqMP). |
| **Trigger feedback (adaptive capture)** | Action writes new values into regbank shadow registers (level_pin_en_mask, capture_mode, etc.) with optional revert timer. Detail-up only when something interesting starts happening. A per-datalogger enable-mask action is an AND-gate on the host CONFIG `enable` — it never re-enables a host-disabled datalogger, and revert restores the latest host word (datalogger_gather_contract.md §"Enable arbitration"). |
| **Trigger-out (PMOD pulse)** | 1 LUT + countdown stretcher + OBUF. Cross-instrument sync with scopes or a second deepskopion. |
| **Counter/threshold triggers** | Small FIFO of event timestamps; ages out entries older than `window_ms`. Fire when count ≥ threshold. Sustained-error / storm / rate-ratio detectors. |
| **Pause-on-fill (one-shot)** | Action sets `RING_PAUSED` regbank bit; ring controller stops accepting writes. Resume requires explicit `RING_PAUSED <= 0` write. |

### DSL shape (sketch — Phase 2 compiler input)

```yaml
# datalogger_config.yml triggers: section
triggers:
  - id: bus_glitch
    states:
      - name: wait_sof
        condition: { channel: zs_msg, event_id: SOF }
        on_match:  { next: wait_eof, timeout_ms: 100 }

      - name: wait_eof
        condition: { channel: zs_msg, event_id: EOF }
        on_match:  { next: wait_err, timeout_ms: 50 }

      - name: wait_err
        condition:
          any_of:
            - { channel: uart_byte, event_id: UART_PARITY_ERR }
            - { channel: i2c_frame, event_id: I2C_NACK }
        on_match:
          fire: true
          capture_window: { pre_ms: 30000, post_ms: 100, mode: bounded }
          pmod_pulse:     { pin: pmod_ja[7], width_us: 10 }
          reconfig:
            sets:
              - { reg: level_pin_en_mask, value: 0x0F }
            revert_ms: 5000

  - id: error_storm
    type: threshold
    condition: { channel: i2c_frame, event_id: I2C_CRC_ERR }
    window_ms: 1000
    threshold: 10
    on_match: { fire: true, mode: one_shot }
```

The compiler reads this and emits a BRAM image: ~300 bytes for a 16-state graph. Trivial footprint, expressive interface.

### Decoded events as trigger sources (the protocol-semantic win)

Once the sigrok PD adoption lands, decoded annotations feed back into the trigger dispatcher as condition inputs. The dispatcher's matching is channel-id-based; sigrok PD annotations are just another set of channels.

That unlocks triggers expressed against **protocol semantics**, not raw bits:

```yaml
condition: { channel: sigrok.usb, event_id: USB_CRC_ERR }
# or
condition: { channel: sigrok.modbus, event_id: SLAVE_TIMEOUT }
# or
condition: { channel: sigrok.midi, event_id: SYSEX_END }
```

DSLogic and similar external logic analyzers literally cannot do this — they capture raw bits and only know about protocols via host-side decoders that run AFTER capture. Our protocol decoding happens **before** the trigger matcher sees the event, so the matcher operates on semantic primitives, not bit patterns.

---

## 6. Strategic Pitch

Logic-analyzer triggers are bit-pattern matchers — DSLogic's "16-stage trigger sequences" or Saleae's "trigger on this byte at this offset". Edge-detection on a clock, value comparison on a data lane, AND/OR combinators. The model has been the same since HP's 16700 family in the 1990s; the implementations got cheaper and faster but the semantic level didn't move.

The deepskopion trigger model lives one layer up:

*   **Authored declaratively** — YAML/DSL, compiled to a state-table BRAM image, reloaded over regbank in microseconds. No bitstream rebuild to iterate on a trigger. (Same iteration loop as software, not as hardware.)
*   **Executes cycle-accurately in the PL** — no host round-trip, reproducible across runs, immune to scheduling jitter.
*   **Matches on protocol semantics** (via PL-side datalogger decoders + sigrok PD outputs as channel sources) — "fire when I2C address 0x50 NACKs with a UART parity error within 100 µs" is one DSL statement, not a 20-line bit-pattern recipe.
*   **Composes with adaptive capture, pre-roll, threshold counters, cross-instrument sync** — orthogonal features that plug into the same dispatcher IP.
*   **Ships as IP inside the user's bitstream** — runs INSIDE the chip being observed, no external probes, signals inaccessible to external instruments are first-class trigger sources.

The Keysight HVI parallel is the closest external analogue: declarative authoring → deterministic FPGA execution → cross-instrument sync via shared time grid. That's a $50k–$200k differentiator on the M3xxx platform. We can build the in-FPGA analogue as an IP that ships in any Zynq / UltraScale+ / PCIe-FPGA bitstream, with the routertl-stack philosophy: vendor-neutral, scriptable, streamable to RouteWave + agents + Grafana.

The Saleae/DSLogic axis competes on **price per channel per MHz**. We're not on that axis. We're on **"what triggers can you actually express against the silicon you're observing"** — and the answer for deepskopion has a different ceiling.

---

## Cross-references

*   `docs/software/data_plane/wave_events_v1_spec.md` — `TRIG_MATCH` lands as `kind: trigger_fired` event with FSM trace
