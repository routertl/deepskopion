# I2C Datalogger Protocol

> **Source:** [i2c_datalogger.vhd](i2c_datalogger.vhd)
> **Machine-readable:** [i2c_datalogger_protocol.yml](i2c_datalogger_protocol.yml)

## Message Structure (32-bit)

| Field | Bits | Width | Description |
|-------|------|-------|-------------|
| `device_id` | [31:27] | 5 | Datalogger device ID |
| `type` | [26:24] | 3 | `100` for events, `0xx` for payload |
| `timestamp` | [23:8] | 16 | Lower bits of global timestamp |
| `event_code` / `data` | [7:0] | 8 | Event type or I2C data byte |

---

## Event Message

Sent for START, STOP, Repeated START, enable/disable, and errors.

```
[31:27] device_id  (5 bits)
[26:24] type       (3 bits) = 0b100
[23:8]  timestamp  (16 bits)
[7:0]   event_code (8 bits)
```

### Event Codes

| Code | Name | Description |
|------|------|-------------|
| `0x03` | Enabled | Datalogger enabled |
| `0x00` | Disabled | Datalogger disabled |
| `0x09` | Start | I2C START condition |
| `0x0A` | Stop | I2C STOP condition |
| `0x0B` | Repeated Start | Repeated START detected |
| `0x05` | Stop Error | Unexpected STOP condition |
| `0x06` | Stretch Error | SCL stuck low (timeout 25ms) |
| `0x07` | Repeated Start Error | NACK before repeated START |
| `0x08` | Glitch Error | SDA/SCL transient (< 30ns) |

---

## Payload Message

Sent for address and data bytes.

```
[31:27] device_id  (5 bits)
[26:25] type       (2 bits) = 0b00
[24]    ack_nack   (1 bit)  = 0=ACK, 1=NACK
[23:8]  timestamp  (16 bits)
[7:0]   data       (8 bits)
```

### Data Field Interpretation

| Byte Type | Bits [7:1] | Bit [0] |
|-----------|------------|---------|
| **Address** | 7-bit I2C address | R/W (0=Write, 1=Read) |
| **Data** | Upper data bits | LSB of data |

---

## Configuration Register (16-bit)

| Field | Bits | Width | Description |
|-------|------|-------|-------------|
| `device_id` | [15:11] | 5 | Target device ID |
| `reserved` | [10:1] | 10 | Reserved |
| `enable` | [0] | 1 | 0=Disabled, 1=Enabled |

---

## Constants

| Name | Value | Description |
|------|-------|-------------|
| `C_DATALOGGER_MSG_WIDTH` | 32 | Total message width |
| `G_I2C_ID_WIDTH` | 5 | Device ID width |
| `G_I2C_DEV_ID` | 28 | Default device ID |
| `C_TIMESTAMP_SLICE_HIGH` | 16 | Timestamp slice width |
| `C_SCL_TIMEOUT_MS` | 25 | SCL timeout in ms |
| `C_DEBOUNCE_TIME_CYCLES` | 3 | Glitch filter (~30ns) |
