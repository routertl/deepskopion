# I2C Datalogger Test Coverage Plan

This document tracks test coverage for the `i2c_datalogger` module.

## Event Coverage

| Event | Code | Test | Status |
|-------|------|------|--------|
| ENABLED | `0x03` | `test_enable_event` | ✅ Pass |
| DISABLED | `0x00` | `test_disable_event` | ❌ Not implemented in DUT |
| START | `0x09` | `test_start_event` | ✅ Pass |
| STOP | `0x0A` | `test_stop_event` | ✅ Pass |
| REPEATED_START | `0x0B` | `test_repeated_start` | ❌ Not implemented in DUT |
| GLITCH_ERROR | `0x08` | `test_glitch_error` | 🔲 Not tested |
| STRETCH_ERROR | `0x06` | `test_scl_timeout` | 🔲 Not tested |
| NACK | `0x0C` | `test_nack_detection` | 🔲 Not tested |

## Payload Coverage

| Payload Type | Description | Test | Status |
|--------------|-------------|------|--------|
| ADDRESS | 7-bit address byte | `test_write_transaction` | ✅ Pass |
| DATA | Data bytes | `test_write_transaction` | ✅ Pass |
| READ_DATA | Read transaction bytes | `test_read_transaction` | 🔲 Not tested |

## Transaction Coverage

| Scenario | Test | Status |
|----------|------|--------|
| Single write byte | `test_write_transaction` | ✅ Pass |
| Multi-byte write | `test_write_transaction` | ✅ Pass |
| Read transaction | `test_read_transaction` | 🔲 Not tested |
| Combined read/write | `test_repeated_start` | ❌ DUT feature missing |
| 10-bit address | `test_10bit_address` | 🔲 Not tested |
| Clock stretching | `test_clock_stretching` | 🔲 Not tested |

## Error Injection Coverage

| Error Type | Injection Method | Test | Status |
|------------|------------------|------|--------|
| Bus glitch | `I2cMasterDriver.inject_glitch()` | `test_glitch_error` | 🔲 Not tested |
| SCL timeout | `I2cMasterDriver.stretch_scl()` | `test_scl_timeout` | 🔲 Not tested |
| Arbitration loss | Manual SDA contention | - | 🔲 Not tested |

## Summary

| Category | Passing | Failing | Not Tested | Total |
|----------|---------|---------|------------|-------|
| Events | 3 | 2 | 3 | 8 |
| Payloads | 2 | 0 | 1 | 3 |
| Transactions | 2 | 1 | 3 | 6 |
| Error Injection | 0 | 0 | 3 | 3 |
| **Total** | **7** | **3** | **10** | **20** |

## Next Steps

1. **Implement DISABLED event** in DUT - Generate message when datalogger transitions from enabled to disabled
2. **Implement REPEATED_START detection** in DUT - Distinguish START after data from initial START
3. **Add GLITCH_ERROR test** - Use `inject_glitch()` driver method
4. **Add STRETCH_ERROR test** - Use `stretch_scl()` driver method
5. **Add read transaction tests** - Verify master-read payloads are captured
