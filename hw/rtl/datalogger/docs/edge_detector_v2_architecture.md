# Edge Detector v2 — Architecture Specification

> **Origin**: Conversation 18 audit (Feb 25, 2026)
> **Scope**: Datalogger-subsystem-level design decisions affecting the Edge
> Detector, Arbiter, and Gathering infrastructure.
> **Status**: Approved — Phase 1 (documentation) complete. Phases 2–4
> pending RTL implementation via RouteRTL SDK.

---

## 1. Context

The Edge Detector is the simplest datalogger in the Deepskopion platform.
Its Conv18 audit revealed that while functionally correct, it operates
with a non-standard 32-bit word format and lacks several contract
compliance features. Three architectural decisions were made to evolve it
into the reference implementation for non-standard dataloggers.

## 2. Approved Decisions

### Decision 1: Centralized Error Reporting (Phase 2)

**Problem**: Each datalogger has an `*_ERRORS` port, but the Edge
Detector's `ED_ERRORS` is wired to `open` in `datalogger_gather.vhd`
(line 715). The `F` bit (FIFO pressure) pollutes the data stream.
No timestamped error events exist.

**Decision**: Create a new `infra/error_reporter.vhd` entity. The
arbiter is already complex; error reporting should not add to it.

**Design**:

- The Error Reporter monitors `FULL` signals from all arbiter input FIFOs
- On any overflow event, it emits a standard 32-bit message:
  `DEV_ID[31:27] + TYPE=STATUS(0b100)[26:24] + TIMESTAMP[23:8] + SOURCE_INDEX[7:0]`
- It has its own slot in the burst arbiter (or a dedicated bypass)
- Individual dataloggers no longer need to embed error flags in their data

**Impact**: The `F` bit is removed from the Edge Detector word format.
The `*_ERRORS` port contract is preserved but becomes optional for
individual dataloggers.

### Decision 2: Standard Type Field with Auto Initial State (Phase 3)

**Problem**: The Edge Detector uses bits [26:24] as three independent
flags (S/F/E) instead of the standard 3-bit message type discriminator.
The "reading" command (`C_ENABLE_READ_TRIGGER_*`) adds complexity.

**Decision**: Option B — Replace S/F/E with standard type codes and
add automatic initial-state readback on enable.

**New word format**:

| Bits    | Width | Field       | Values |
| :------ | :---: | :---------- | :----- |
| [31:27] |   5   | `device_id` | From `G_ED_DEV_ID` |
| [26:24] |   3   | `type`      | `0b010` = Rising edge, `0b011` = Falling edge, `0b001` = Initial state |
| [23:8]  |  16   | `timestamp` | 16-bit global 1µs |
| [7:0]   |   8   | `extension` | Sub-µs counter (unchanged) |

**Behavior changes**:

- On `configEnable` rising edge → emit one `INITIAL_STATE (0b001)` message
  with `extension[0]` = current pin level
- On each detected edge → emit `RISING (0b010)` or `FALLING (0b011)`
- Remove all ReadEvent logic (S bit, `C_ENABLE_READ_TRIGGER_*` codes)
- CONFIG simplifies to: `00` = disabled, `01` = enabled

### Decision 3: Adaptive Mode with Streamer/RLE (Phase 4)

**Problem**: The Edge Detector is 3.125% bandwidth-efficient as a burst
datalogger (32 bits per edge for 1 bit of information). At high toggle rates
(>100 kHz), FIFO overflow occurs within microseconds.

**Decision**: Add an optional streamer mode, selectable via CONFIG.

**Proposed modes** (CONFIG[3:2]):

| Code | Mode | Description |
| :--- | :--- | :---------- |
| `00` | Event (default) | One message per edge, full timestamp |
| `01` | Stream-1 | Pack 24 samples/word at programmed rate |
| `10` | Stream-RLE | Pack 3 × 8-bit RLE deltas per word |
| `11` | Reserved | — |

**Stream routing**: When in stream mode, the Edge Detector output routes
to `epStreamArbiterInputs` instead of `epArbiterInputs`. This
requires a MUX in `datalogger_gather.vhd` controlled by the CONFIG mode.

**RLE mode specifics**: Each 8-bit delta encodes the number of extension
ticks until the next toggle. Max delta = 255 ticks. If a delta exceeds
255, a continuation word is emitted. At 100 MHz, 255 ticks = 2.55 µs,
covering signals up to ~196 kHz without continuation.

## 3. Implementation Roadmap

All RTL work will use the **RouteRTL SDK** for simulation and
verification (NVC simulator, Cocotb 2.0+).

| Phase | Scope | Files | Verification |
| :---- | :---- | :---- | :----------- |
| **2** | Error Reporter | `infra/error_reporter.vhd`, `datalogger_gather.vhd` | `test_error_reporter.py` — force FIFO overflow via phase accumulator edge detector |
| **3** | Type reclassification | `nonstd/edge_datalogger.vhd` | `test_edge_datalogger_v2.py` — verify type codes, initial state message |
| **4** | Streamer mode | `nonstd/edge_datalogger.vhd`, `datalogger_gather.vhd` | `test_edge_streamer.py` — sample packing, RLE encoding, waveform reconstruction accuracy |

### SDK Verification Strategy

Each phase follows the RouteRTL verification pattern:

1. **TbEnv** for standardized testbench environment
2. **Reusable drivers** in `vendor/RouteRTL/sim/cocotb/tb/drivers/`
3. **Datalogger decoders** in `vendor/RouteRTL/sim/cocotb/tb/datalogger/`
4. **Pre-commit anchor set**: The existing `test_edge_datalogger.py` must
   remain green throughout all phases

### Key Constraints

- **NVC only** — GHDL is incompatible with Cocotb 2.0+
- **Bug → Test Rule** — every RTL change must include a regression test
- **No legacy code** — SDK remains generic, no project-specific hacks
- Cocotb log level: use `self.log.info()` for diagnostic prints (debug
  level is silently dropped in log files)

## 4. System-Wide Implications

### Arbiter Changes (Phase 2)

The Error Reporter needs a new input slot in the burst arbiter. Options:

1. **Dedicated input**: Add `C_ERROR_REPORTER` to `C_NUM_ARB_INPUTS` in
   `datalogger_config_pkg.vhd`
2. **Sideband injection**: The Error Reporter writes directly into the
   arbiter's output FIFO (bypassing round-robin)

Option 1 is simpler and keeps the arbiter logic unchanged.

### Gathering Topology Changes (Phase 4)

When the Edge Detector switches to stream mode, it must route to the
stream arbiter path. This requires:

- A MUX per edge datalogger instance in `datalogger_gather.vhd`
- The CONFIG mode bits must be accessible to the gathering logic
- The stream arbiter needs token messages for the edge detector
  (new entry in `C_STREAM_TOKEN`)

### Software Impact

The logic-trace `EdgeTranslator` (future) must handle:

- Type field decoding (`0b010` rising, `0b011` falling, `0b001` initial)
- Automatic initial state → first Channel transition
- Stream mode: unpack samples from 24-bit payload or RLE deltas
- Error Reporter messages: annotate waveform gaps

## 5. Open Questions for Future Conversations

1. **Stream sample rate**: Should it be compile-time (generic) or
   runtime (CONFIG register)? Runtime is more flexible but requires
   more CONFIG bits.
2. **Multi-channel edge detection**: Currently one `EDGE_IN` per
   instance. Should we support multiple pins per instance (like the
   Level Datalogger) in stream mode?
3. **Error Reporter scope**: Should it only monitor FIFO overflow, or
   also detect "stuck" dataloggers (no output for N µs while enabled)?
