# Datalogger Compliance Checklist

> Derived from the UART datalogger reference implementation (Feb 2026).
> Every future datalogger (I2C, SPI, QSPI, Edge, Level, I2S) must be
> validated against this checklist before integration into the platform.

---

## 1. Hardware Word Format

| # | Requirement | Mandatory | Notes |
| :- | :---------- | :-------: | :---- |
| H1 | Message width = `C_DATALOGGER_MSG_WIDTH` (32 bits) | ✅ | Defined in `datalogger_pkg.vhd` |
| H2 | `device_id` in bits [31:27] (5 bits) | ✅ | Must match `G_*_DEV_ID` generic |
| H3 | 3-bit `type` field in bits [26:24] | ⚠️ | Required for standard dataloggers. Non-standard (`nonstd/`) may use these bits for flags with documented justification. |
| H4 | 16-bit `timestamp` in bits [23:8] | ✅ | Source: global 1µs timestamp bus |
| H5 | 8-bit `data/payload` in bits [7:0] | ✅ | Content depends on message type |
| H6 | Must use `datalogger_pkg.all` for shared constants | ✅ | |

## 2. Message Types

| # | Requirement | Mandatory | Notes |
| :- | :---------- | :-------: | :---- |
| M1 | At least one PAYLOAD message type | ✅ | The core data-carrying message |
| M2 | CONFIG message after initialization | ⚠️ | Required if datalogger has auto-calibration or configurable parameters. Omit with justification for fixed-config dataloggers. |
| M3 | STATUS messages for runtime events | ⚠️ | Required for error reporting (parity, framing, overflow). Omit with justification if no error conditions exist. |
| M4 | Message types documented in YAML | ✅ | Each type with `type_code` and field descriptions |
| M5 | Type codes must not conflict with other dataloggers | ✅ | Type codes are per-datalogger (scoped by `device_id`) |

## 3. Timestamp Handling

| # | Requirement | Mandatory | Notes |
| :- | :---------- | :-------: | :---- |
| T1 | Accept external `TIMESTAMP` port (≥16 bits wide) | ✅ | `G_TIMESTAMP_WIDTH` generic |
| T2 | Use lower 16 bits in message `[23:8]` | ✅ | For ZS header timestamp alignment |
| T3 | Document timestamp resolution | ✅ | Typically 1µs at 100 MHz |
| T4 | If sub-µs extension is used, document in YAML | ⚠️ | Edge Detector uses `[7:0]` for this |
| T5 | **Stamp at CAPTURE, not delivery** | ✅ | Freeze the timestamp at the capture instant; never read `TIMESTAMP` combinationally into an RDY-stallable output |

### T5 — capture-time stamping (the rule, the why, the how)

A datalogger timestamp must mark **when the event happened on the bus**, not when
the message was finally accepted downstream. The bug class: a datalogger builds its output message with a **combinational**
`TIMESTAMP` (a concurrent `msg <= … & TIMESTAMP(15:0) & …` or a per-cycle
re-evaluated next-state) and then **holds that message at an `*_MSG_RDY`-gated
boundary**. When the gather arbiter is full, delivery stalls — and the embedded
timestamp slides forward with it, so the frame reports the *delivery* time. The
error is **unbounded** (the whole backpressure window) and silently destroys the
timing the datalogger exists to record (two events captured 1 µs apart but drained
back-to-back after a stall get near-identical timestamps).

**Two valid patterns — pick one:**

1. **`fifo`** — write the `(timestamp, payload)` tuple into the message FIFO
   *together, at the capture instant*; the `RDY`/full backpressure gates the FIFO
   *read*, but the captured timestamp rides with the data. Exemplars: `uart`
   (`fifoUartDin`), `jtag` (staged → `MsgRam`), `edge` (`fifoEdDin`).
2. **`latch`** — register the timestamp (and the payload bits) into a dedicated
   reg at the capture/decode cycle, and build the output message from the *latched*
   reg, never live `TIMESTAMP`. Exemplars: `i2c` (`msgPayload_r`/`msgEvent_r`
   registered at the FSM capture cycle, fire-and-forget — no RDY wait), `qspi`
   (`tsPayload_r`/`tsEvent_r`), `level` (`tsCaptured_r`/`pinsCaptured_r`).

**Anti-pattern (forbidden):** a concurrent `msg`/`payload` built from live
`TIMESTAMP` that is delivered through an `*_MSG_RDY` hold without a capture latch.

**Regression guard (mandatory for any datalogger that *had* this bug; recommended
for all):** the *backpressure discriminator* cocotb test — hold `TIMESTAMP=V`
while the event is captured under `MSG_RDY=0`, advance `TIMESTAMP=W` during the
stall, release `RDY`, and assert the frame carries **V**. The fix makes it pass;
the un-fixed RTL makes it fail (discriminator-verified). Reference tests:
`test_qspi_spi2pin.test_timestamp_is_capture_not_send_time`,
`test_level_datalogger.test_timestamp_is_change_time_not_delivery`. A structural
guard (`tools/datalogger-builder/tests/test_datalogger_timestamp_capture_contract.py`)
fails the build if a new `*_datalogger.vhd` with an `*_MSG_RDY` port is not
classified here.

## 4. Configuration Interface

| # | Requirement | Mandatory | Notes |
| :- | :---------- | :-------: | :---- |
| C1 | Accept `CONFIG` port (`G_CONFIG_WIDTH` wide) | ✅ | Shared bus from control plane |
| C2 | Use `config_control` entity for ID filtering | ✅ | From `datalogger/infra/` |
| C3 | `device_id` in CONFIG `[15:11]` (default width) | ✅ | Must match `G_*_DEV_ID` |
| C4 | `enable` bit in CONFIG `[0]` (minimum) | ✅ | Enable/disable control |
| C5 | Document all CONFIG codes in YAML | ✅ | |

## 5. FIFO Interface

| # | Requirement | Mandatory | Notes |
| :- | :---------- | :-------: | :---- |
| F1 | Use the project `fifo_sync` boundary backed by open-logic | ✅ | Portable, registry-pinned FIFO implementation |
| F2 | FWFT (First Word Fall Through) mode | ✅ | For zero-latency reads |
| F3 | Output valid/ready handshake | ✅ | `*_MSG_VLD` / `*_MSG_RDY` ports |
| F4 | Block writes during `RST_BUSY` | ✅ | Prevents corruption after reset |
| F5 | Document FIFO depth in YAML | ✅ | |

## 6. Error Reporting

| # | Requirement | Mandatory | Notes |
| :- | :---------- | :-------: | :---- |
| E1 | `*_ERRORS` port (`G_ERROR_WIDTH` wide) | ✅ | Asynchronous error flags |
| E2 | Error-type STATUS messages in data stream | ⚠️ | Recommended for timestamped error correlation. Exception for dataloggers without error conditions. |
| E3 | Document error codes/flags in YAML | ✅ | |

## 7. YAML Specification

| # | Requirement | Mandatory | Notes |
| :- | :---------- | :-------: | :---- |
| Y1 | `protocol` section with `name`, `version`, `message_width` | ✅ | |
| Y2 | `constants` section with field widths and codes | ✅ | |
| Y3 | `messages` section with per-field bit positions | ✅ | Every field must trace to an RTL signal |
| Y4 | `config` section with register bit mapping | ✅ | |
| Y5 | `errors` section with error flag definitions | ✅ | |
| Y6 | `examples` section with Python decode function | ✅ | |
| Y7 | YAML filename: `<protocol>_datalogger_protocol.yml` | ✅ | Located in the datalogger's RTL directory |

## 8. Documentation Artifacts

| # | Requirement | Mandatory | Notes |
| :- | :---------- | :-------: | :---- |
| D1 | `<PROTOCOL>_DATALOGGER_PROTOCOL.md` — interface specification | ✅ | |
| D2 | `<protocol>_datalogger_protocol.yml` — machine-readable HW word format | ✅ | |
| D3 | `<PROTOCOL>_DATALOGGER_FEATURES.md` — roadmap/backlog | ✅ | |
| D4 | `test_<protocol>_datalogger*.py` — cocotb verification suite | ✅ | |
| D5 | `COVERAGE.md` — verification maturity matrix | ⚠️ | Required for production dataloggers |

## 8b. routertl IP Contract

Each datalogger must additionally ship the routertl IP manifest pair so
`rr ip` / `rr sim contract-check` / `rr sim coverage-map` can validate
the entity surface + REQ coverage automatically.

| # | Requirement | Mandatory | Notes |
| :- | :---------- | :-------: | :---- |
| R1 | `ip.yml` — routertl IP manifest in the datalogger directory | ✅ | `name`, `library: tich`, `language: vhdl`, `hdl.top_module`, `synthesis.sources` list, `requirements: requirements.yml` pointer. UART pilot at `hw/rtl/datalogger/uart/ip.yml`. |
| R2 | `requirements.yml` — sibling to `ip.yml` | ✅ | Two sections: `port_contract` (structural — scalars / clocks / resets / parameters, machine-checked by `rr sim contract-check`) and `functional_contract` (REQ-N list, machine-checked by `rr sim coverage-map`). |
| R3 | REQ ID convention: `<PROTO>-REQ-N` | ✅ | Uppercase protocol prefix, dash, sequential integer. Sub-ranges: 1-99 functional, 100-199 adversarial/negative. Example: `UART-REQ-1`, `I2C-REQ-100`. |
| R4 | Every declared REQ has at least one `@requires(<PROTO>-REQ-N)` test | ✅ | Use `from sdk.cocotb_helpers import requires`. Multiple REQs per test allowed: `@requires("UART-REQ-1", "UART-REQ-2")`. |
| R5 | Adversarial cadence: behavioral REQ paired with `kind: negative` REQ when the inverse claim is non-trivial | ⚠️ | Example pair: REQ-30 "CONFIG[0]=1 enables capture" + REQ-100 "CONFIG[0]=0 produces zero messages". |
| R6 | `version` field in `requirements.yml` bumped on shape change | ✅ | Adding REQs / port bundles is a non-breaking bump (0.1 → 0.2). Removing or renaming is a breaking bump (0.x → 1.0). |
| R7 | Grow the contract by adding REQs WITH their tests | ✅ | Declaring a REQ without a paired test is a contract without a proof — coverage-map fails with "uncovered REQ". Don't ship aspirational contracts. |
| R8 | `rr sim contract-check <ip_dir>` and `rr sim coverage-map <ip_dir>` both pass before commit | ✅ | Hook integration TBD. |

Bootstrap a new datalogger's contract:

```bash
rr sim contract-init hw/rtl/datalogger/<proto>/<proto>_datalogger.vhd \
    --ip <proto>_datalogger
# Review the scaffold (RST polarity, parameters, bundle inference)
# Hand-write functional_contract REQs anchored to behaviour you can prove
# Tag tests with @requires(<PROTO>-REQ-N)
rr sim contract-check hw/rtl/datalogger/<proto>/
rr sim coverage-map  hw/rtl/datalogger/<proto>/
```

## 9. Software Integration (ZS Header)

| # | Requirement | Mandatory | Notes |
| :- | :---------- | :-------: | :---- |
| S1 | 32-bit HW word maps to 4-byte ZS payload | ✅ | `payload_len = 4` in `zs_msg_header_t` |
| S2 | `device_id` maps to `datalogger_id` in ZS header | ✅ | |
| S3 | `type` field maps to `msg_type` in ZS header | ⚠️ | Non-standard dataloggers use `msg_type = 0` (DATA) |
| S4 | 16-bit timestamp expandable to 64-bit global time | ✅ | Via timestamp synchronization infrastructure |
| S5 | Document the HW word → ZS header mapping in YAML | ✅ | |

---

## 10. Datalogger Lifecycle

Every **standard-format** datalogger emits a *symmetric, uniform* lifecycle pair so
a passive multi-channel consumer learns — in-band, on the data timeline —
exactly when each channel armed and disarmed:

| Frame | Edge | type [26:24] | code [7:0] |
| :---- | :--- | :----------: | :--------: |
| ENABLED  | per-device ENABLE rising edge  | `100` (event) | `0x03` |
| DISABLED | per-device ENABLE falling edge | `100` (event) | `0x00` |

| # | Requirement | Mandatory | Notes |
| :- | :---------- | :-------: | :---- |
| L1 | Symmetric pair: every DISABLED has a preceding ENABLED for the same device | ✅ | No "disable-only" feedback (the original i2c asymmetry). |
| L2 | Lifecycle lives in the ALWAYS-ON (global-RST) domain, NOT inside the per-datalogger `CAPTURE_RST` decode reset | ✅ | If it lived in the decode core, asserting `CAPTURE_RST` while disabled would *suppress* the DISABLED frame (H.96). This decoupling is what lets a datalogger adopt `CAPTURE_RST` on its decode core (Phase 2) without losing the markers. |
| L3 | Codes: ENABLED=`0x03`, DISABLED=`0x00`, event type=`100` | ✅ | Uniform across all carrying dataloggers. |
| L4 | Implemented via the shared `datalogger_lifecycle` component (SL-REQ-1..4) OR an inline emitter proven equivalent | ⚠️ | New adopters use the component; jtag/qspi keep an equivalent inline emitter (see matrix). |
| L5 | Non-standard / no-type-slot formats are EXEMPT with documented justification (cf. H3) | ⚠️ | Their enable/disable is observable via stream start/stop; document the exemption here + in the datalogger's `requirements.yml`. |

### Lifecycle conformance matrix

| Datalogger | Lifecycle | How | Reset domain |
| :------ | :-------: | :-- | :----------- |
| i2c     | ✅ | shared `datalogger_lifecycle` | global RST |
| uart    | ✅ | shared `datalogger_lifecycle` | global RST (coexists with `dataloggerRst`) |
| level   | ✅ | shared `datalogger_lifecycle` | global RST |
| jtag    | ✅ | inline (`C_TypeEvent` + `C_EvEnabled`/`C_EvDisabled`) — reference impl | global RST (ESA two-process) |
| qspi    | ✅ | inline (enable→`0x03` / disable→`0x00`, type `100`) | global RST |
| edge    | ❌ EXEMPT | bespoke frame: the type bits hold S/F/E flags (cf. H3 ❌, justified). Enable/disable observable via stream start/stop. | — |
| timestamp_sync | ❌ EXEMPT | infra heartbeat: reuses type `100` for its payload and its low byte spans `{0x00,0x40,0x80,0xC0}` (DISABLED=`0x00` collides). The heartbeat cadence IS the liveness signal. | — |

Carrying dataloggers MUST declare a lifecycle REQ (e.g. `UART-REQ-18`,
`LEVEL-REQ-51`, `I2C-REQ-1`/`I2C-REQ-2`, `SL-REQ-1..4`) with a paired test.
Exempt dataloggers MUST state the exemption + rationale in their `requirements.yml`.

### Per-datalogger reset (CAPTURE_RST) — Phase 2

With the lifecycle decoupled from the reset domain (L2), each datalogger's DECODE
core gets a clean, SW-reachable per-datalogger reset on `ENABLE=0`. `config_control`
emits `CAPTURE_RST = global RST OR (latched ENABLE=0)` (CC-REQ-51). A datalogger
either **wires** it into its decode regs, or its FSM **self-resets** on disable —
both satisfy the contract "ENABLE=0 resets this datalogger". The lifecycle emitter
and `config_control` always stay on global RST so the DISABLED marker is never
suppressed (H.96).

| Datalogger | Decode reset on ENABLE=0 | Why |
| :------ | :----------------------- | :-- |
| uart  | wires `CAPTURE_RST` | calibration / FIFO / BRAM / read-FSM don't self-flush (UART-REQ-15) |
| level | wires `CAPTURE_RST` | `levelPins_r` / pending-change persist (LEVEL-REQ-52) |
| i2c   | wires `CAPTURE_RST` | shift-reg / ack / sample-count persist (I2C-REQ-11) |
| qspi  | FSM self-resets | FSM returns to STOP/idle + clears sample count on disable; lifecycle is inline in the FSM (QSPI-REQ-51) |
| jtag  | FSM self-resets | capture gated on ConfigEnable, TAP resyncs; lifecycle inline in the two-process block (JTAG-REQ-10) |
| edge  | n/a (lifecycle-exempt) | continuous edge stream; enable/disable via stream start/stop |
| timestamp_sync | n/a (lifecycle-exempt) | heartbeat cadence is the liveness signal |

Rule of thumb: **wire `CAPTURE_RST` where decode state does NOT self-flush;
otherwise document the FSM-self-reset.** Forcing a blunt reset onto a datalogger
whose lifecycle is woven into its decode FSM (qspi/jtag) risks suppressing the
inline DISABLED marker (H.96) and violates "don't generate non-necessary
resets" — so those self-reset by construction and prove it with a reconfig
regression. Each reset-adopting datalogger carries a reconfig regression
(disable → re-enable yields clean state).

> **NDA / sanctioned reconfig:** disable a specific device with an explicit
> matching-id CONFIG word (`dev_id<<11 | 0`), NOT `CONFIG=0` — `config_control`
> only re-latches on an ID match, so `CONFIG=0` (id 0) does not disable a
> non-zero device-id (H.98). Sanctioned arm sequence: reset (ENABLE=0) →
> ENABLE=1 + config → then start traffic (enable-before-traffic).

---

## Validation Matrix Template

Copy this table into each datalogger's `COVERAGE.md`:

| # | Requirement | Status | Artifact / RTL Line |
| :- | :---------- | :----: | :------------------ |
| H1 | 32-bit width | | |
| H2 | device_id [31:27] | | |
| H3 | type [26:24] | | |
| H4 | timestamp [23:8] | | |
| H5 | data [7:0] | | |
| M1 | PAYLOAD message | | |
| M2 | CONFIG message | | |
| M3 | STATUS message | | |
| C1–C5 | CONFIG interface | | |
| F1–F5 | FIFO interface | | |
| E1–E3 | Error reporting | | |
| Y1–Y7 | YAML spec | | |
| D1–D5 | Documentation | | |
| S1–S5 | SW integration | | |

### UART Datalogger (Reference)

| # | Req | Status | Notes |
| :- | :-- | :----: | :---- |
| H1–H6 | HW format | ✅ | Full standard compliance |
| M1 | PAYLOAD | ✅ | Types `0b010`, `0b011` |
| M2 | CONFIG | ✅ | Type `0b001`, post-calibration |
| M3 | STATUS | ✅ | Type `0b100`, 5 status codes |
| C1–C5 | CONFIG | ✅ | Single enable bit, auto-calibration |
| F1–F5 | FIFO | ✅ | FWFT, depth configurable |
| E1–E3 | Errors | ✅ | Port + STATUS messages |
| Y1–Y7 | YAML | ✅ | `uart_datalogger_protocol.yml` |
| D1–D5 | Docs | ⚠️ | Missing FEATURES.md, COVERAGE.md |

### Edge Detector Datalogger

| # | Req | Status | Notes |
| :- | :-- | :----: | :---- |
| H1–H2 | HW format | ✅ | 32-bit, device_id compliant |
| H3 | type [26:24] | ❌ | Uses S/F/E flags (justified divergence) |
| H4 | timestamp | ✅ | Standard 16-bit timestamp |
| H5 | data [7:0] | ❌ | Uses sub-µs extension (justified) |
| M1 | PAYLOAD | ⚠️ | Implicit — every message is payload |
| M2 | CONFIG | ❌ | Not applicable (justified) |
| M3 | STATUS | ❌ | **Needs RTL review** |
| C1–C5 | CONFIG | ✅ | Uses config_control, 4 control codes |
| F1–F5 | FIFO | ✅ | FWFT, depth 8, width 27 |
| E1 | Error port | ✅ | ED_ERRORS port exists |
| E2–E3 | Error msgs | ❌ | **Needs RTL review** |
| Y1–Y7 | YAML | ✅ | Updated in this audit |
| D1–D5 | Docs | ❌ | Missing PROTOCOL.md, FEATURES.md, COVERAGE.md |
