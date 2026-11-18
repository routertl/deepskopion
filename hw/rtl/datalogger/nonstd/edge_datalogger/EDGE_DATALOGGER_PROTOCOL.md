# Edge Detector Datalogger — Interface Specification

> **Version**: 1.0 (current RTL)
> **Source**: [edge_datalogger.vhd](edge_datalogger.vhd)
> **Status**: RTL-complete, pending v2 evolution
> (see [v2 Architecture](../../docs/edge_detector_v2_architecture.md))

---

## 1. Overview

The Edge Detector datalogger captures signal transitions on a single input
pin with clock-cycle-precise timing. It is the simplest datalogger in the
Deepskopion platform and resides in the `nonstd/` (non-standard)
category because it does not use the standard 3-bit message type
discriminator.

## 2. Message Format (32-bit)

| Bits    | Width | Field       | Description |
| :------ | :---: | :---------- | :---------- |
| [31:27] |   5   | `device_id` | Datalogger instance ID (`G_ED_DEV_ID`, default 28) |
| [26]    |   1   | `S` (Send)  | `1` = SW-triggered readback, `0` = edge event |
| [25]    |   1   | `F` (Full)  | `1` = FIFO almost full (backpressure warning) |
| [24]    |   1   | `E` (Edge)  | Edge polarity (inverted for edge events, direct for reads) |
| [23:8]  |  16   | `timestamp` | Lower 16 bits of global 1µs timestamp |
| [7:0]   |   8   | `extension` | Sub-µs counter (0–99 at 100 MHz) |

### Timestamp Precision

Full timestamp reconstruction:

```
timestamp_ns = timestamp_us × 1000 + extension × (1000 / clk_freq_mhz)
```

At 100 MHz: `timestamp_ns = timestamp × 1000 + extension × 10`

### E Bit Polarity

| Context        | E=0          | E=1           |
| :------------- | :----------- | :------------ |
| Edge event (S=0) | Rising edge  | Falling edge  |
| Readback (S=1)   | Pin is low   | Pin is high   |

Note: For edge events, E captures the *previous* level (inverted input).
RTL: `not EDGE_IN when ReadEvent='0' else EDGE_IN` (line 169).

## 3. Configuration

The CONFIG port is 16 bits wide. The `config_control` entity filters
by `device_id` in bits [15:11].

| CONFIG[1:0] | Mode | Behavior |
| :---------- | :--- | :------- |
| `00` | Disabled | No capture (reset state) |
| `01` | Capturing | Capture both rising and falling edges |
| `10` | Read + Capture | SW-triggered readback, capturing continues |
| `11` | Read + Stop | SW-triggered readback, capturing stops |

## 4. FIFO Interface

- **Entity**: `fifo_sync` (portable shared boundary, open-logic implementation)
- **Depth**: 256 entries (`2**C_FIFO_DEPTH`, where `C_FIFO_DEPTH=8`)
- **Width**: 27 bits (S + F + E + timestamp + extension)
- **Mode**: FWFT (First Word Fall Through)
- **Output handshake**: `ED_MSG_VLD` / `ED_MSG_RDY`

### Write Conditions (priority order)

1. Blocked during `RST_BUSY`
2. `ReadEvent` always writes (even if not enabled)
3. Blocked if `configEnable = '0'`
4. Blocked if FIFO full
5. Write on `edgeDetected`
6. Write on `ReadEvent_r` (deferred read+edge coincidence). This
   word is an edge event (S=0) with the E bit and timestamp of the cycle the
   edge was detected. The readback owns S=1.

### Lifecycle frame

On each enable and disable, `ED_MSG_OUT` carries one lifecycle frame:
`device_id & "100" & timestamp[15:0] & code`, code `0x03` ENABLED or `0x00`
DISABLED. `ED_MSG_LIFECYCLE` is `1` on that `ED_MSG_VLD` cycle only.

Bits [26:24] of the frame equal S=1, F=0, E=0, the same bits as a readback of
level 0. The gather therefore tags a flagged frame with event id
`EVT_EDGE_LIFECYCLE` (0x08), and the core frames it on its own as msg_type
`0x0608` instead of batching it onto EDGE_EVENTS.

## 5. Error Reporting

- **Port**: `ED_ERRORS` (2-bit, `G_ERROR_WIDTH`)
- **Status**: **DRIVEN** — bit[0] = `internal_fifo_overflow`: asserts when an
  edge arrives while the internal FWFT FIFO is full and the gather isn't reading
  (i.e. a fabric-rate edge is lost upstream of the ring). Feeds `ERR_CNT_EDGE`
  (regbank `0x0100`).
- **In-band provenance (silicon-proven)**: `error_event_emitter`
  coalesces `ERR_CNT_EDGE` deltas into a rate-limited in-band `SEV_ERROR |
  DOMAIN_EDGE | event_id=0xF0` zs_msg frame ("N edge event(s) lost (internal FIFO
  overflow)"), so a host/RouteWave sees the loss in the same stream as the data.
  See `system_ids.yml` error-event registry + `error_event_emitter.vhd`.
- **Silicon observability**: regbank `EMIT_VALID_CNT` (`0x0170`) and
  `EMIT_DONE_CNT` (`0x0174`) probe the emitter→framer handshake.
- **Self-test routing**: an edge datalogger can be driven from an EMIO-injected bus
  for validation — `EMIO_MUX_SEL` bit[3] routes the full SPI set into EDGE×4,
  bit[4] routes I2C SCL/SDA into EDGE0/EDGE1 — not
  PMOD-only. Both are 2-FF ASYNC_REG synced.
- **Future**: a *centralized* per-datalogger Error Reporter (aggregating all
  dataloggers' error buses) is still future work, orthogonal to the in-band path above.

## 6. Gathering Integration

The Edge Detector routes to the **burst** arbiter path:

```
epArbiterInputs(C_ARB_EDGE_INDEX)
```

Multiple Edge Detector instances (up to `C_EDGE_DATALOGGERS`) are
arbitrated via a local round-robin arbiter before reaching the
2nd-layer burst arbiter.

## 7. Machine-Readable Specification

See [edge_datalogger_protocol.yml](edge_datalogger_protocol.yml)
for the YAML schema with Python decode examples.
