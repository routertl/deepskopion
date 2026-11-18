# QSPI Datalogger - Features to Implement

> **Source:** [qspi_datalogger.vhd](qspi_datalogger.vhd)
> **Protocol:** [QSPI_DATALOGGER_PROTOCOL.md](QSPI_DATALOGGER_PROTOCOL.md)

## Proposed New Events

### High Priority

| Event | Code | RTL Complexity | Description |
|-------|------|----------------|-------------|
| **Mode Switch Quad** | `0x13` | Medium | Transition from SPI to Quad mode detected |
| **Mode Switch Dual** | `0x14` | Medium | Transition from SPI to Dual mode detected |
| **Command Detected** | `0x12` | Medium | Known flash command opcode in first byte |

### Medium Priority

| Event | Code | RTL Complexity | Description |
|-------|------|----------------|-------------|
| **Dummy Cycles** | `0x17` | Medium | Dummy clock cycles detected (high-speed read) |
| **Byte Count** | `0x1C` | Low | Transaction byte count at CS release |
| **Status Poll** | `0x18` | Medium | Status register read loop detected |

### Low Priority

| Event | Code | RTL Complexity | Description |
|-------|------|----------------|-------------|
| **XIP Enter** | `0x15` | High | Execute-in-Place mode entered |
| **XIP Exit** | `0x16` | High | Execute-in-Place mode exited |
| **Deep Power Down** | `0x19` | Low | Command 0xB9 detected |
| **Wake Up** | `0x1A` | Low | Command 0xAB detected |

---

## Implementation Notes

### Mode Switch Detection

- **Current state:** Mode is configured statically via `halfDuplexMode` (3-wire) / `noChipSelectMode` (2-wire)
- **Change:** Monitor IO2/IO3 activity after command phase
- **Detection:** If IO2/IO3 transition during data phase → Quad mode active
- **Consideration:** Requires understanding command structure (1-byte cmd → addr → data)

### Command Detected

- **Current state:** First byte is captured but not decoded
- **Change:** Add lookup table for common flash commands
- **Common commands:**
  - `0x03` = Read
  - `0x0B` = Fast Read
  - `0x02` = Page Program
  - `0x20` = Sector Erase (4KB)
  - `0xD8` = Block Erase (64KB)
  - `0x05` = Read Status Register
  - `0x06` = Write Enable
- **Event logic:** After first 8 bits, compare with known opcodes

### Dummy Cycles Detection

- **Current state:** All clock cycles are treated as data
- **Change:** After command + address, count cycles before IO activity
- **Consideration:** Dummy cycle count varies by command and frequency

### Byte Count

- **Current state:** No transaction-level tracking
- **Change:** Add counter, increment on each complete byte
- **Event logic:** Emit at CS rising edge

---

## Cocotb Test Cases

### test_mode_switch_quad

```python
# Drive: CS low + cmd byte (single) + addr (single) + data (quad)
# Verify: MODE_SWITCH_QUAD event (0x13) after first quad data
```

### test_command_detected

```python
# Drive: CS low + 0x03 (Read command)
# Verify: COMMAND_DETECTED event with cmd=0x03 in payload
```

### test_dummy_cycles

```python
# Drive: CS low + 0x0B (Fast Read) + addr + 8 dummy clocks + data
# Verify: DUMMY_CYCLES event with count=8
```

### test_byte_count

```python
# Drive: CS low + cmd + N data bytes + CS high
# Verify: BYTE_COUNT event with correct count
```

### test_status_poll_loop

```python
# Drive: repeated (CS low + 0x05 + status + CS high) x N
# Verify: STATUS_POLL event after detecting pattern
```

### test_deep_power_down

```python
# Drive: CS low + 0xB9 + CS high
# Verify: DEEP_POWER_DOWN event (0x19)
```

### test_wake_up

```python
# Drive: CS low + 0xAB + CS high
# Verify: WAKE_UP event (0x1A)
```
