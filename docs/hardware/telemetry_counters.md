# Telemetry Counters — Hardware Design Document

## Overview

`telemetry_counters` is a centralized instrumentation module inside
`datalogger_gather` that provides dashboard-ready performance counters for
FPGA observability. It taps existing handshake, error, and FIFO status
signals without modifying the data path.

**Spec**: `hw/rtl/datalogger/infra/telemetry_counters_spec.yml`
**Regbank**: Registers 48–70 in `regbank_schema.yml`
**Roadmap**: `routertl/docs/roadmap/fpga_observability_dashboards.md`

---

## Register Map

| Address | Name | Type | Description |
|:-------:|------|:----:|-------------|
| 48 | `HEARTBEAT_CNT` | RO | Free-running PL liveness counter |
| 49 | `FIFO_OVERFLOW_CNT` | RO | Burst FIFO overflow events |
| 50 | `FIFO_OVERFLOW_CNT_RSVD` | RO | Stream FIFO overflow events |
| 51 | `PKT_CNT_I2C` | RO | I2C accepted messages |
| 52 | `PKT_CNT_SPI` | RO | SPI accepted messages |
| 53 | `PKT_CNT_UART` | RO | UART accepted messages |
| 54 | `PKT_CNT_QSPI` | RO | QSPI accepted messages |
| 55 | `PKT_CNT_EDGE` | RO | Edge detector events |
| 56 | `PKT_CNT_I2S` | RO | I2S stream samples |
| 57 | `ERR_CNT_I2C` | RO | I2C protocol errors |
| 58 | `ERR_CNT_SPI` | RO | SPI protocol errors |
| 59 | `ERR_CNT_UART` | RO | UART frame/parity errors |
| 60 | `ERR_CNT_QSPI` | RO | QSPI protocol errors |
| 61 | `ERR_CNT_EDGE` | RO | Edge detector errors |
| 62 | `ARB_GRANT_CNT` | RO | Burst arbiter total grants |
| 63 | `ARB_GRANT_CNT_RSVD` | RO | Stream arbiter total grants |
| 64 | `DROP_CNT_I2C` | RO | I2C dropped (VLD && !RDY) |
| 65 | `DROP_CNT_SPI` | RO | SPI dropped |
| 66 | `DROP_CNT_UART` | RO | UART dropped |
| 67 | `FIFO_HIGH_WATERMARK` | RO | Peak burst FIFO occupancy |
| 68 | `FIFO_HIGH_WATERMARK_RSVD` | RO | Peak stream FIFO occupancy |
| 69 | `TELEMETRY_CTRL` | RW | `[0]` = write-1 to clear all counters |
| 70 | `TELEMETRY_SNAPSHOT_TRIGGER` | RW | `[0]` = write-1 to snapshot |
| 92 | `EMIT_VALID_CNT` | RO | (`0x0170`) loss-frame probe — clk_sys cycles `error_event_emitter` holds a pending in-band loss frame (>0 ⇒ emitter latched) |
| 93 | `EMIT_DONE_CNT` | RO | (`0x0174`) loss-frame probe — emitter→framer handshakes (loss frame accepted; pairs with EMIT_VALID_CNT to localise where a loss frame stalls) |

> The `RING_*` ring-locator block (`0x0150`–`0x016C`) sits between
> `TELEMETRY_SNAPSHOT_TRIGGER` (`0x0144`) and the probes above; see
> `project_regbank.yml` for the authoritative full map.

---

## Architecture

```
                        datalogger_gather
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  ┌──────────┐  VLD/RDY  ┌───────────────────┐  PKT_CNTs        │
│  │ I2C (×4) │──────────→│                   │──────────→ Regbank│
│  │ SPI (×4) │──────────→│  telemetry_       │  ERR_CNTs         │
│  │ UART(×6) │──────────→│  counters         │──────────→ Regbank│
│  │ QSPI(×1) │──────────→│                   │  DROP_CNTs        │
│  │ Edge(×5) │──────────→│  (taps signals,   │──────────→ Regbank│
│  │ I2S (×2) │──────────→│   no data path    │  OVERFLOW_CNTs    │
│  └──────────┘  ERRORS   │   modification)   │──────────→ Regbank│
│       │        ────────→│                   │  HEARTBEAT        │
│       │                 │   FIFO_FULL ──────│──────────→ Regbank│
│       │                 │   FIFO_CNT  ──────│  WATERMARKS       │
│  ┌────▼─────┐           │                   │──────────→ Regbank│
│  │ Arbiters │──GRANT───→│                   │                   │
│  │ (2-layer)│           └───────────────────┘                   │
│  └──────────┘                    ▲  ▲                           │
│                           RESET──┘  └──SNAPSHOT (from regbank)  │
└──────────────────────────────────────────────────────────────────┘
```

---

## Counter Behavior

### Saturating Counters

All counters are 32-bit saturating (cap at `0xFFFF_FFFF`). This avoids
wrap-around ambiguity during extended operation.

### Counting Rules

| Counter Type | Signal | Trigger |
|-----------|--------|---------|
| Packet count | `VLD && RDY` | Rising edge (accepted message) |
| Drop count | `VLD && !RDY` | Rising edge (backpressure loss) |
| Error count | `OR-reduce(ERRORS)` | Rising edge (any error bit) |
| Overflow count | `FIFO_FULL` | Rising edge (0→1 transition only) |
| Watermark | `FIFO_CNT` | Continuous max (if cnt > watermark) |
| Heartbeat | — | Increments every clock cycle |

### Atomic Snapshot

Writing 1 to `TELEMETRY_SNAPSHOT_TRIGGER` (address 70) latches all live
counters into shadow registers in a single clock cycle. Subsequent reads
return consistent shadow values. This prevents counter skew when the PS
reads 23 registers sequentially over AXI-Lite.

### Reset

- **Hardware reset** (`RST`): Clears all counters including heartbeat
- **Software reset** (`TELEMETRY_CTRL[0]`): Clears addresses 48–68.
  Heartbeat continues running. Self-clearing bit.

---

## Resource Estimate

| Resource | Count | Notes |
|----------|------:|-------|
| FFs (live counters) | 736 | 23 × 32-bit |
| FFs (shadow bank) | 736 | 23 × 32-bit |
| FFs (edge detectors) | ~15 | One per counted signal |
| **Total FFs** | **~1,490** | <1% of Zynq-7020 |
| LUTs | ~200 | Saturating adders + comparators |
| BRAM | 0 | — |
| Critical path impact | None | Registered increments only |

---

## Integration Checklist

- [ ] Create `hw/rtl/datalogger/infra/telemetry_counters.vhd`
- [ ] Wire Edge Detector `ED_ERRORS` (currently `open` in legacy gather)
- [ ] Instantiate inside both `datalogger_gather.vhd` variants
- [ ] Connect FIFO occupancy count signals to module inputs
- [ ] Map outputs to regbank addresses 48–70
- [ ] Regenerate `system_regbank_pkg.vhd` from updated schema
- [ ] Add cocotb test for counter increment and snapshot behavior
- [ ] Verify counter reset clears all telemetry registers
- [ ] Run regression to confirm zero data path impact
