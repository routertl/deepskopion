# Protocol Datalogger Documentation Guide

This document defines the standard documentation required for each protocol datalogger in the Deepskopion project.

## Required Documents

Each datalogger module requires the following documentation:

### 1. RTL Directory (`hw/rtl/datalogger/<protocol>/`)

| Document | Purpose | Example |
|----------|---------|---------|
| `<PROTOCOL>_DATALOGGER_PROTOCOL.md` | Binary message format specification | [I2C_DATALOGGER_PROTOCOL.md](https://github.com/routertl/deepskopion/blob/main/hw/rtl/datalogger/i2c/I2C_DATALOGGER_PROTOCOL.md) |
| `<protocol>_datalogger_protocol.yml` | Machine-readable protocol definition | [i2c_datalogger_protocol.yml](https://github.com/routertl/deepskopion/blob/main/hw/rtl/datalogger/i2c/i2c_datalogger_protocol.yml) |
| `<PROTOCOL>_DATALOGGER_FEATURES.md` | Implementation status and roadmap | [I2C_DATALOGGER_FEATURES.md](https://github.com/routertl/deepskopion/blob/main/hw/rtl/datalogger/i2c/I2C_DATALOGGER_FEATURES.md) |

### 2. Test Directory (`hw/sim/cocotb/tests/datalogger/<protocol>/`)

| Document | Purpose | Example |
|----------|---------|---------|
| `COVERAGE.md` | Test coverage matrix and status | [COVERAGE.md](https://github.com/routertl/deepskopion/blob/main/hw/sim/cocotb/tests/datalogger/i2c/COVERAGE.md) |
| `test_<protocol>_datalogger.py` | Cocotb test implementation | [test_i2c_datalogger.py](https://github.com/routertl/deepskopion/blob/main/hw/sim/cocotb/tests/datalogger/i2c/test_i2c_datalogger.py) |

---

## Document Templates

### PROTOCOL.md Template

```markdown
# <Protocol> Datalogger Protocol

## Message Format

| Field | Bits | Description |
|-------|------|-------------|
| DEVICE_ID | [31:27] | Datalogger instance ID |
| MSG_TYPE | [26:24] | 0b100=Event, 0b000/0b001=Payload |
| TIMESTAMP | [23:8] | 16-bit timestamp |
| DATA | [7:0] | Event code or payload byte |

## Event Codes

| Event | Code | Description |
|-------|------|-------------|
| ... | 0xNN | ... |

## Payload Types

| Type | MSG_TYPE | Description |
|------|----------|-------------|
| ... | 0bNNN | ... |
```

### protocol.yml Template

```yaml
protocol:
  name: "<Protocol> Datalogger"
  version: "1.0"
  message_width: 32

fields:
  device_id:
    bits: [31, 27]
    description: "Datalogger instance ID"
  # ... other fields

events:
  - name: "EVENT_NAME"
    code: 0xNN
    description: "..."
  # ... other events
```

### FEATURES.md Template

```markdown
# <Protocol> Datalogger Features

## Implementation Status

| Feature | Status | Notes |
|---------|--------|-------|
| Feature 1 | ✅ Implemented | ... |
| Feature 2 | 🔲 Planned | ... |
| Feature 3 | ❌ Not planned | ... |

## Roadmap

### Phase 1 (Current)
- [ ] Feature A
- [ ] Feature B

### Phase 2 (Future)
- [ ] Feature C
```

### COVERAGE.md Template

```markdown
# <Protocol> Datalogger Test Coverage

## Event Coverage

| Event | Code | Test | Status |
|-------|------|------|--------|
| ... | 0xNN | test_xxx | ✅/❌/🔲 |

## Summary

| Category | Pass | Fail | Not Tested |
|----------|------|------|------------|
| Events | N | N | N |
| Payloads | N | N | N |
```

---

## Current Datalogger Documentation Status

### I2C (Reference Implementation)

| Document | Status | Notes |
|----------|--------|-------|
| `I2C_DATALOGGER_PROTOCOL.md` | ✅ Complete | Message format, events, payloads |
| `i2c_datalogger_protocol.yml` | ✅ Complete | Machine-readable definition |
| `I2C_DATALOGGER_FEATURES.md` | ✅ Complete | Status and roadmap |
| `COVERAGE.md` | ✅ Complete | Test coverage matrix |
| `test_i2c_datalogger.py` | ✅ Complete | 6 tests (4 passing) |

**Assessment**: The I2C datalogger has a complete documentation set and serves as the reference implementation.

### QSPI

| Document | Status | Notes |
|----------|--------|-------|
| `QSPI_DATALOGGER_PROTOCOL.md` | ✅ Complete | Message format with mode variants |
| `qspi_datalogger_protocol.yml` | ✅ Complete | Machine-readable definition |
| `QSPI_DATALOGGER_FEATURES.md` | ✅ Complete | Proposed events and roadmap |
| `COVERAGE.md` | 🔲 Missing | Needs creation |
| `test_qspi_datalogger.py` | ⚠️ Partial | Basic structure only |

**Assessment**: Protocol documentation is complete, but test coverage and COVERAGE.md are missing.

### UART

| Document | Status | Notes |
|----------|--------|-------|
| `UART_DATALOGGER_PROTOCOL.md` | ✅ Complete | Message format, 9-bit optimization |
| `uart_datalogger_protocol.yml` | ✅ Complete | Full protocol definition with examples |
| `UART_DATALOGGER_FEATURES.md` | 🔲 Missing | Needs creation |
| `COVERAGE.md` | 🔲 Missing | Needs creation |
| `test_uart_datalogger*.py` | ✅ Complete | 12/14 tests passing |

**Assessment**: Protocol specification complete. Missing features doc and coverage tracking.

### Non-Standard Dataloggers (Edge, Level)

| Document | Edge | Level | Notes |
|----------|------|-------|-------|
| Protocol Spec | ✅ Complete | 🔲 Missing | `EDGE_DATALOGGER_PROTOCOL.md` |
| YAML Definition | ✅ Complete | 🔲 Missing | `edge_datalogger_protocol.yml` |
| Features Doc | ✅ Complete | 🔲 Missing | `EDGE_DATALOGGER_FEATURES.md` |
| Tests | ✅ Complete | ✅ Complete | `test_edge_datalogger.py`, `test_level_datalogger.py` |
| `COVERAGE.md` | 🔲 Missing | 🔲 Missing | Needs creation |

**Assessment**: Edge Detector has a complete documentation set. Level Datalogger in `nonstd/` still needs documentation.

---

## Future Dataloggers Checklist

When adding a new protocol datalogger:

- [ ] Create RTL in `hw/rtl/datalogger/<protocol>/`
- [ ] Write `<PROTOCOL>_DATALOGGER_PROTOCOL.md` - Message format spec
- [ ] Write `<protocol>_datalogger_protocol.yml` - YAML definition
- [ ] Write `<PROTOCOL>_DATALOGGER_FEATURES.md` - Implementation status
- [ ] Create test in `hw/sim/cocotb/tests/datalogger/<protocol>/`
- [ ] Write `test_<protocol>_datalogger.py` - Cocotb tests
- [ ] Write `COVERAGE.md` - Coverage tracking
- [ ] Add SDK components to the routertl package (upstream in `routertl/` repo)
  - [ ] `datalogger/<protocol>/decoder.py`
  - [ ] `datalogger/<protocol>/config.py`
  - [ ] `drivers/<protocol>.py` (if needed)
