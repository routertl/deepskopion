# Level Datalogger — Features

**Build status:** the level datalogger is not instantiated in any current board build (`C_LEVEL_DRIVERS = 0` in `pkg/datalogger_config_pkg.vhd`); it is verified by simulation only.

## Implemented

| Feature | Description |
|---|---|
| Level-change capture | Emits a payload frame (type `0b000`) whenever any of the 8 `LEVEL_PINS` lines changes state; `[7:0]` carries the new pin snapshot |
| Capture-time timestamp | `timestamp` + pins are latched at the **change instant** (`tsCaptured_r`/`pinsCaptured_r`), so they stay correct under `LEVEL_MSG_RDY` backpressure |
| Uniform lifecycle | ENABLED (`0x03`) / DISABLED (`0x00`) markers via the shared `datalogger_lifecycle` (event type `0b100`) |
| Device-id filter | `CONFIG[15:11]` addresses one instance; non-matching `CONFIG` is ignored |
| Per-datalogger reset | Disable drives `CAPTURE_RST` — flushes `levelPins_r` so a re-enable re-emits the current level |
| Backpressure-safe | `LEVEL_MSG_RDY` holds delivery; the captured tuple is frozen until accepted |
| One frame per change | A change that is delivered is not re-flagged in its delivery cycle, so one `LEVEL_PINS` change gives one delivered frame under steady, pulsed or random `LEVEL_MSG_RDY` (`test_level_frames_per_change`) |

## Design notes

- **No error path.** `LEVEL_ERRORS` is tied to all-zero — a passive level datalogger
  has no protocol violations to flag (unlike i2c/qspi). This is a deliberate
  contract, pinned by `test_reset_determinism`.
- **Single pending change.** The capture holds one pending change between
  deliveries (no FIFO). Under sustained back-to-back changes while backpressured,
  intermediate transitions coalesce — the frame reports the first change with its
  correct change-time stamp. Acceptable for a level monitor; a deeper history
  would need a FIFO (see Proposed below).

## Proposed (future REQs)

### Medium priority
- **Edge-direction annotation** — distinguish which line(s) rose vs fell since the
  last frame (currently the consumer diffs successive snapshots).
- **Change-mask field** — a per-line "changed since last frame" bitmask, so a
  consumer needn't keep prior state.

### Low priority
- **Optional capture FIFO** — buffer multiple rapid transitions instead of
  coalescing, for high-rate parallel buses.
- **Configurable debounce** — a `G_ENABLE_INFILTER`-style glitch filter on the
  parallel lines (mirrors the qspi/uart input filters).
