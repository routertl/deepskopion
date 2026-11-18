# I2C Datalogger - Features to Implement

> **Source:** [i2c_datalogger.vhd](i2c_datalogger.vhd)
> **Protocol:** [I2C_DATALOGGER_PROTOCOL.md](I2C_DATALOGGER_PROTOCOL.md)

## Proposed New Events

### High Priority

| Event | Code | RTL Complexity | Description |
|-------|------|----------------|-------------|
| **NACK on Address** | `0x13` | Low | First byte NACK - device not present or not responding |
| **NACK on Data** | `0x14` | Low | Data byte NACK - receiver rejected data |
| **Byte Count** | `0x15` | Medium | Emit transaction byte count at STOP |

### Medium Priority

| Event | Code | RTL Complexity | Description |
|-------|------|----------------|-------------|
| **10-bit Address** | `0x0E` | Medium | Detect 10-bit addredataloggerng (first byte = 11110xx) |
| **General Call** | `0x0F` | Low | General call address (0x00) detected |
| **Arbitration Lost** | `0x0D` | High | Multi-master conflict (requires SDA monitoring during write) |

### Low Priority

| Event | Code | RTL Complexity | Description |
|-------|------|----------------|-------------|
| **Bus Busy** | `0x0C` | Medium | SDA/SCL held low for extended period |
| **High-Speed Mode** | `0x12` | High | HS-mode master code (00001xxx) detected |

---

## Implementation Notes

### NACK on Address vs NACK on Data

- **Current state:** Single `ack_r` signal captures ACK/NACK
- **Change:** Add `isAddressByte_r` flag, set after START, cleared after first ACK sampling
- **Event logic:** If `ack_r = '1'` (NACK) and `isAddressByte_r = '1'` → emit `NACK_ADDRESS`

### Byte Count

- **Current state:** No byte counting
- **Change:** Add `byteCnt_r` counter, increment on each ACK
- **Event logic:** At STOP, emit count in `event_code` field (or use payload slot)
- **Consideration:** May require new message type if count > 255

### 10-bit Addredataloggerng

- **Detection:** First byte matches pattern `11110xx R/W`
- **Change:** Parse first byte, if match → set `is10BitAddr_r` flag
- **Behavior:** Capture second address byte before emitting address event

### Arbitration Lost

- **Complexity:** Requires comparing driven SDA with actual SDA during master write
- **Consideration:** Datalogger is padataloggerve - may not be able to detect this reliably
- **Alternative:** Report if SDA deviates from expected pattern mid-byte

---

## Cocotb Test Cases

### test_nack_address

```python
# Drive START + address + NACK (no device responds)
# Verify: NACK_ADDRESS event (0x13) is emitted
```

### test_nack_data

```python
# Drive START + address + ACK + data + NACK
# Verify: NACK_DATA event (0x14) is emitted
```

### test_byte_count

```python
# Drive full transaction: START + addr + N data bytes + STOP
# Verify: BYTE_COUNT event with correct count
```

### test_10bit_address

```python
# Drive START + 11110xx + addr_low + ACK
# Verify: 10BIT_ADDRESS event (0x0E) is emitted
```

### test_general_call

```python
# Drive START + 0x00 + ACK
# Verify: GENERAL_CALL event (0x0F) is emitted
```
