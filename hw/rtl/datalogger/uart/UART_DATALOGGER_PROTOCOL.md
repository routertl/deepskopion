# UART Datalogger Protocol Specification

This document defines the FPGA-SW message protocol for the auto-calibrating UART datalogger.

## 1. Message Structure (32-bit)

All UART datalogger messages follow a consistent 32-bit layout:

| Field | Bits | Width | Description |
| :--- | :--- | :--- | :--- |
| `device_id` | [31:27] | 5 | Datalogger Instance ID |
| `type` | [26:24] | 3 | Message type identifier |
| `timestamp` | [23:8] | 16 | Lower bits of global 1µs timestamp |
| `data` | [7:0] | 8 | Captured data byte or event code |

## 2. Message Types

| Type | Code | Name | Description |
| :--- | :---: | :--- | :--- |
| **Payload** | `0b010` | PAYLOAD | Captured UART data (≤8-bit mode, or 9-bit with bit8 = 0) |
| **Payload B9** | `0b011` | PAYLOAD_9BIT | Captured UART data with 9th bit (bit8) = 1 |
| **Config** | `0b001` | CONFIG | Post-calibration configuration details |
| **Status** | `0b100` | STATUS | Calibration events or runtime errors |

## 3. Detailed Formats

### 3.1 Payload Message (Types 0b010 / 0b011)

Captured data bits. The type field encodes the 9th bit for 9-bit mode:

| Mode | Behavior |
| :---: | :--- |
| **5-8 bits** | Type = `0b010`, data left-aligned in `[7:0]` |
| **9 bits** | Type = `0b010` if bit[8]=0, `0b011` if bit[8]=1. Data `[7:0]` in payload. |

**Decoding (all modes):**

```python
if msg_type in (0b010, 0b011):
    bit9 = 1 if msg_type == 0b011 else 0
    data_8bit = msg & 0xFF
    full_word = (bit9 << 8) | data_8bit
```

### 3.2 Config Message (Type 0b001)

Sent once after successful auto-calibration. Uses the full 24-bit data field [23:0].

| Bits | Description |
| :--- | :--- |
| [23:8] | Baudrate Cycles (BR_CYCLES) |
| [7] | Reserved |
| [6] | Stop Bit Mode (1: 1 stop bit) |
| [5] | Parity Type (0: Even, 1: Odd) |
| [4] | Parity Enable |
| [3:0] | Word Length (5-9) |

### 3.3 Status Message (Type 0b100)

| Code | Name | Description |
| :---: | :--- | :--- |
| 0x00 | CALIBRATION_DONE | Auto-calibration completed |
| 0x01 | CALIBRATION_TIMEOUT | No valid frames detected: an autoSync calibration matched no format instance (calibrationFail). Sent once per failed calibration, after the ENABLED frame; the data logger then emits nothing until it is re-armed |
| 0x02 | PARITY_ERROR | Parity mismatch |
| 0x03 | START_ERROR | Invalid start bit |
| 0x04 | STOP_ERROR | Invalid stop bit |
| 0x05 | ENABLED | Lifecycle: the datalogger was armed (enable edge) |
| 0x06 | DISABLED | Lifecycle: the datalogger was disarmed (enable edge) |

## 4. Discovery Phase

The UART datalogger performs auto-calibration before streaming:

- **Burst Requirement**: ~70-80 bytes of traffic before lock
- **Timeout**: 1.5ms at 100MHz
- **Data Playback**: Calibration bytes are replayed after lock
- **Activation**: CONFIG message followed by payload stream
