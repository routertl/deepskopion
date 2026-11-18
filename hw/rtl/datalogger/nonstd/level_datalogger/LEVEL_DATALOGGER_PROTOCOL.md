# Level Datalogger Protocol

HW↔SW protocol for `level_datalogger` — a parallel-bus level-change datalogger that
emits a frame whenever any of its `C_LEVEL_INJ_PINS` (=8) input lines changes
state. Message width: **32 bits**. See `level_datalogger_protocol.yml` for the
machine-readable form.

## Message layout

All frames are 32 bits; the top 5 bits are always the device id.

```
 31    27 26  24 23           8 7        0
+--------+------+--------------+----------+
| dev_id | type |  timestamp   |  field   |
|  (5)   | (3)  |    (16)      |   (8)    |
+--------+------+--------------+----------+
```

| Field | Bits | Description |
|---|---|---|
| device_id | `[31:27]` | datalogger instance id (`G_LEVEL_DEV_ID`, default 24) |
| type | `[26:24]` | `0b000` = level payload, `0b100` = event |
| timestamp | `[23:8]` | low 16 bits of `TIMESTAMP`, latched at the **change** instant |
| field | `[7:0]` | payload: the 8 captured `LEVEL_PINS` values; event: the event code |

## Frame types

### Level payload (type `0b000`)
Emitted on a pin-level change. `[7:0]` carries the `LEVEL_PINS` snapshot
**at the change instant** (captured, not read at delivery, so it is
correct under `LEVEL_MSG_RDY` backpressure). `timestamp` marks when the change
occurred.

### Event (type `0b100`)
Lifecycle markers, emitted by the shared `datalogger_lifecycle` component:

| Code `[7:0]` | Event | When |
|---|---|---|
| `0x03` | ENABLED | per-device ENABLE rising edge |
| `0x00` | DISABLED | per-device ENABLE falling edge |

## Configuration (`CONFIG`, `G_CONFIG_WIDTH`=16)

| Field | Bits | Description |
|---|---|---|
| device_id | `[15:11]` | target instance — must equal `G_LEVEL_DEV_ID` to apply |
| enable | `[0]` | 1 = capture enabled, 0 = disabled (emits DISABLED) |

A `CONFIG` word addressed to a different `device_id` is ignored. Disable also
drives the per-datalogger `CAPTURE_RST`, flushing the
last-captured level so a re-enable re-emits the current state.

## Notes

- `LEVEL_ERRORS` is hard-wired to all-zero (the level datalogger has no error
  conditions to report).
- Timestamp resolution: 1 µs at 100 MHz (the low 16 bits; wraps every ~65.5 ms).
