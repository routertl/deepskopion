# QSPI Datalogger Protocol

> **Source:** [qspi_datalogger.vhd](qspi_datalogger.vhd)
> **Machine-readable:** [qspi_datalogger_protocol.yml](qspi_datalogger_protocol.yml)

## Message Structure (32-bit)

| Field | Bits | Width | Description |
|-------|------|-------|-------------|
| `device_id` | [31:27] | 5 | Datalogger device ID |
| `type` | [26:24] | 3 | Message type |
| `payload/timestamp` | [23:8] | 16 | Data or timestamp |
| `data/event_code` | [7:0] | 8 | Data byte or event |

---

## Message Types

| Type | Value | Description |
|------|-------|-------------|
| Event | `0b100` | CS transitions, start/stop, errors |
| Payload | `0b001` | Data payload (or Type A in SPI mode) |
| Payload B | `0b010` | Type B payload (SPI/Dual mode) |
| Error | `0b010` | Error/default packet |

---

## Event Message

```
[31:27] device_id  (5 bits)
[26:24] type       (3 bits) = 0b100
[23:8]  timestamp  (16 bits)
[7:0]   event_code (8 bits)
```

### Event Codes

| Code | Name | Description |
|------|------|-------------|
| `0x03` | Start | Datalogger enabled |
| `0x00` | Stop | Datalogger disabled |
| `0x09` | CS Down | Chip select asserted |
| `0x0A` | CS Up | Chip select released |
| `0x0B` | CS Up Incomplete | Byte padded before CS release |
| `0x04` | Timeout | SCLK stuck during transfer |
| `0x10` | CS Down (CPOL=1) | New polarity detected (high) |
| `0x11` | CS Down (CPOL=0) | New polarity detected (low) |

---

## Payload Messages

### Quad Mode (4 data pins)

Two packets sent per 8-bit sample cycle:

**Payload A (IO0/IO1):**

```
[31:27] device_id  (5 bits)
[26:24] type       (3 bits) = 0b001
[23:16] reserved   (8 bits) = 0x00
[15:8]  data_io1   (8 bits)
[7:0]   data_io0   (8 bits)
```

**Payload B (IO2/IO3):**

```
[31:27] device_id  (5 bits)
[26:24] type       (3 bits) = 0b001
[23:16] reserved   (8 bits) = 0x00
[15:8]  data_io3   (8 bits)
[7:0]   data_io2   (8 bits)
```

### SPI/Dual Mode (2 data pins)

**Payload A (MOSI/IO0):**

```
[31:27] device_id  (5 bits)
[26:24] type       (3 bits) = 0b001
[23:8]  timestamp  (16 bits)
[7:0]   data       (8 bits)
```

**Payload B (MISO/IO1):**

```
[31:27] device_id  (5 bits)
[26:24] type       (3 bits) = 0b010
[23:8]  timestamp  (16 bits)
[7:0]   data       (8 bits)
```

> [!NOTE]  
> In 2-wire or 3-wire mode, only Payload A is sent.

---

## Configuration Register (16-bit)

| Field | Bits | Width | Description |
|-------|------|-------|-------------|
| `device_id` | [15:11] | 5 | Target device ID |
| `reserved` | [10:4] | 7 | Reserved |
| `two_wires_mode` | [3] | 1 | 2-wire mode enable |
| `three_wires_mode` | [2] | 1 | 3-wire mode enable |
| `clock_phase` | [1] | 1 | CPHA (0=first edge, 1=second edge) |
| `enable` | [0] | 1 | 0=Disabled, 1=Enabled |

---

## SPI Mode Reference

| Mode | CPOL | CPHA | SCLK Idle | Data Sampled On |
|------|------|------|-----------|-----------------|
| 0 | 0 | 0 | Low | Rising edge |
| 1 | 0 | 1 | Low | Falling edge |
| 2 | 1 | 0 | High | Falling edge |
| 3 | 1 | 1 | High | Rising edge |

> **CPOL** is auto-detected from SCLK level at CS falling edge.  
> **CPHA** is configured via the `clock_phase` config bit.

---

## Constants

| Name | Value | Description |
|------|-------|-------------|
| `C_DATALOGGER_MSG_WIDTH` | 32 | Total message width |
| `G_QSPI_ID_WIDTH` | 5 | Device ID width |
| `G_QSPI_DEV_ID` | 28 | Default device ID |
| `G_QSPI_PINS` | 6 | Default pin count (4 data + SCLK + CSn) |
| `C_TIMEOUT_CNT` | 100000000 | SCLK timeout cycles (~1s @ 100MHz) |

---

## Error Outputs (2-bit)

| Bit | Name | Description |
|-----|------|-------------|
| [0] | timeout | SCLK timeout error |
| [1] | reserved | Reserved |
