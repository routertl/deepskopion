# UART Datalogger Verification Guide

> Last Updated: 2026-02-06

This document tracks the status of the UART datalogger test suite.

---

## Completed Work

| Item | Status | Notes |
| ---- | ------ | ----- |
| Driver Verification | ✅ Done | `tb.drivers.uart_master.UartSource` validated against `cocotbext-uart` |
| Driver Migration | ✅ Done | All tests use native driver (except comparison test) |
| 9-bit Protocol | ✅ Done | Type 0x2/0x3 encoding verified |
| Payload Format Test | ✅ Done | Added to parameterized suite |
| Submodule Tests | ✅ Done | All 4 submodules have isolation tests |

---

## Calibration Principles

> [!IMPORTANT]
> **Randomized Data Requirement**
> The UART datalogger uses an auto-baud and auto-format detection engine. For calibration to succeed and lock reliably:
>
> 1. Use **RANDOMIZED** data (e.g., `random.randint`).
> 2. Avoid fixed patterns like `0x55` or `0x00`, as they may cause false locks or prevent parity detection.
> 3. Provide at least 20-30 characters of traffic for high-confidence lock.

---

## Test File Inventory

| # | File | Target DUT | Status | Test Cases |
| - | ---- | ---------- | ------ | ---------- |
| 1 | [test_uart_datalogger_parameterized.py](test_uart_datalogger_parameterized.py) | `uart_datalogger` | ✅ Primary | 78 param + 5 payload |
| 2 | [test_uart_datalogger.py](test_uart_datalogger.py) | `uart_datalogger` | ✅ Primary | Full sweep |
| 3 | [test_payload_log.py](test_payload_log.py) | `uart_datalogger` | ✅ Functional | 1 |
| 4 | [test_uart_frame_gen_isolation.py](test_uart_frame_gen_isolation.py) | `uart_frame_gen` | ✅ Functional | 7 |
| 5 | [test_uart_baudrate_gen.py](test_uart_baudrate_gen.py) | `uart_baudrate_gen` | ✅ Functional | 5 |
| 6 | [test_uart_frame_gen.py](test_uart_frame_gen.py) | `uart_frame_gen` | ⚠️ Redundant | Covered by #4 |
| 7 | [test_uart_playback.py](test_uart_playback.py) | `uart_playback` | ✅ Functional | 4 |
| 8 | [test_uart_stop_finder.py](test_uart_stop_finder.py) | `uart_stop_finder` | ✅ Functional | 4 |

---

## Test Details

### Integration Tests

#### test_uart_datalogger_parameterized.py

- Baud sweep: 460800 → 3000000
- Bit width: 5-9
- Parity: None, Even, Odd
- Payload verification with 9-bit type encoding

#### test_uart_datalogger.py

- Full protocol sweep with message reassembly
- FastSignalCollector for efficient capture

### Submodule Isolation Tests

#### test_uart_baudrate_gen.py

1. TICK disabled when not enabled
2. TICK at half BR_CYCLES period
3. Full period tick timing
4. PAUSE freezes counter
5. RST clears counter

#### test_uart_stop_finder.py

1. MATCH after valid frames
2. No match on invalid stop
3. Enable/disable reset behavior
4. Parity detection

#### test_uart_playback.py

1. FIFO empty after reset
2. Write/read operations
3. FULL condition
4. WAVE output format

#### test_uart_frame_gen_isolation.py

1. Enable sends config message
2. 8N1 payload structure
3. Multiple bytes in order
4. Even/odd parity modes
5. Word lengths 5-8
6. 9-bit mode type encoding
7. Stop bit modes

---

## Execution

```bash
cd hw/sim/cocotb/tests/datalogger/uart

# Integration tests
SIM=nvc python test_uart_datalogger_parameterized.py
SIM=nvc python test_uart_datalogger.py

# Submodule tests
SIM=nvc python test_uart_baudrate_gen.py
SIM=nvc python test_uart_stop_finder.py
SIM=nvc python test_uart_playback.py
SIM=nvc python test_uart_frame_gen_isolation.py

---

## Troubleshooting & Architectural Characterization

### 1. Architecture: Continuous Streaming (Confirmed)

**Behavior**: The datalogger architecture supports **Continuous Streaming**.
- **Calibration Phase**: Captures edges to determine baud rate.
- **Playback Phase**: Replays the calibration buffer first, then seamlessly switches to streaming new input data.
- **Verification**: Confirmed via `test_payload_verify.py`. Payload bytes sent *after* calibration are received at the output.

**Limitations**:
- **High-Speed Sampling**: At **921,600 baud** (100MHz clock), the sampling precision is marginal, leading to **Bit Errors** in the payload (observed ~3 errors per 8 bytes).
- **Recommendation**: Use **460,800 baud** or lower for error-free payload reliability until sampling logic is improved.

### 2. Baud Rate Limits (Verified at 100MHz)

| Baud Rate | Status | Notes |
| :--- | :--- | :--- |
| **115,200** | ✅ PASS | Standard reference. |
| **460,800** | ✅ PASS | High reliability. |
| **921,600** | ⚠️ MARGINAL| **Functionally works** (Messages Flow), but has **Bit Errors**. |
| **1,000,000** | ❌ FAIL | bit timing errors. |

**Root Cause**: The prescaler (`C_TICK_PRESCALER`) at 100MHz / 3Mbps max baud results in ~8 cycles per tick. At >1Mbps, the quantization error in `uart_stop_finder` becomes too large for reliable stop bit detection.

### 3. Waveform Generation

**Critical Fix**: The simulation framework defaults `WAVES_DIR` to `vendor/routertl/...`.
- **Solution**: Tests must explicitly set `os.environ["ROUTERTL_WAVES_DIR"]` to a project-local path (e.g., `hw/sim/waves`) to ensure FST files are accessible.

### 4. Calibration Data Sensitivity (Seed Analysis)

Calibration reliability is data-dependent at high baud rates (921600).

- **Seed 100**: ✅ PASS
- **Seed 42**: ❌ FAIL (`calibrationFail_r = 1`)

**Failing Input Pattern (Seed 42)**:
The following 150-byte sequence at 921600 baud causes calibration failure. This sequence can be used for debugging the RTL stop bit detector.

```text
0x39, 0x0C, 0x8C, 0x7D, 0x72, 0x47, 0x34, 0x2C, 0xD8, 0x10, 0x0F, 0x2F, 0x6F, 0x77, 0x0D, 0x65,
0xD6, 0x70, 0xE5, 0x8E, 0x03, 0x51, 0xD8, 0xAE, 0x8E, 0x4F, 0x6E, 0xAC, 0x34, 0x2F, 0xC2, 0x31,
0xB7, 0xB0, 0x87, 0x16, 0xEB, 0x3F, 0xC1, 0x28, 0x96, 0xB9, 0x62, 0x23, 0x17, 0x74, 0x94, 0x28,
0x77, 0x33, 0xC2, 0x8E, 0xE8, 0xBA, 0x53, 0xBD, 0xB5, 0x6B, 0x88, 0x24, 0x57, 0x7D, 0x53, 0xEC,
0xC2, 0x8A, 0x70, 0xA6, 0x1C, 0x75, 0x10, 0xA1, 0xCD, 0x89, 0x21, 0x6C, 0xA1, 0x6C, 0xFF, 0xCA,
0xEA, 0x49, 0x87, 0x47, 0x7E, 0x86, 0xDB, 0xCC, 0xB9, 0x70, 0x46, 0xFC, 0x2E, 0x18, 0x38, 0x4E,
0x51, 0xD8, 0x20, 0xC5, 0xC3, 0xEF, 0x80, 0x05, 0x3A, 0x88, 0xAE, 0x39, 0x96, 0xDE, 0x50, 0xE8,
0x01, 0x86, 0x5B, 0x36, 0x98, 0x65, 0x4E, 0xBF, 0x52, 0x00, 0xA5, 0xFA, 0x09, 0x39, 0xB9, 0x9D,
0x7A, 0x1D, 0x7B, 0x28, 0x2B, 0xF8, 0x23, 0x40, 0x41, 0xF3, 0x54, 0x87, 0xD8, 0x6C, 0x66, 0x9F,
0xCC, 0xBF, 0xE0, 0xE7, 0x3D, 0x7E
```

**Passing Input Pattern (Seed 100)**:
This sequence passes calibration at 921600 baud, useful for comparing edge density or bit transitions against the failing case.

```text
0x4A, 0xEB, 0xE8, 0x59, 0xC9, 0xB3, 0xDD, 0x38, 0x3E, 0x29, 0xE9, 0x86, 0x18, 0x68, 0xAB, 0x75,
0x9E, 0x68, 0x5B, 0x48, 0x60, 0xB1, 0xBD, 0xD1, 0x6B, 0xCE, 0xEC, 0x8D, 0xC0, 0x52, 0x3F, 0x5C,
0x02, 0xCA, 0x4B, 0x53, 0x62, 0x55, 0x0D, 0x79, 0xE5, 0xC6, 0x40, 0x1A, 0x7E, 0x94, 0xAD, 0xAF,
0xB9, 0xF7, 0xCD, 0x1F, 0x33, 0x99, 0xDE, 0x81, 0x7A, 0xDF, 0xFC, 0x82, 0x4B, 0x1D, 0xA7, 0x54,
0x1B, 0x8C, 0x3D, 0x19, 0x70, 0x9E, 0x6E, 0xB6, 0xAA, 0xE9, 0x06, 0x3A, 0xF3, 0x62, 0x3C, 0x6B,
0x16, 0xC7, 0x35, 0xCD, 0x4D, 0xF4, 0x13, 0xE3, 0x52, 0x19, 0xF2, 0x18, 0x65, 0x7F, 0x50, 0x30,
0x13, 0x47, 0x6C, 0x2E, 0x3E, 0x80, 0x22, 0xCD, 0x12, 0xDA, 0xF2, 0xE6, 0xAF, 0xE8, 0x63, 0x80,
0xD5, 0x4A, 0x6A, 0xD3, 0x27, 0x89, 0xCD, 0x85, 0x30, 0x61, 0x74, 0x18, 0xC7, 0x8A, 0x01, 0x8A,
0x9B, 0x13, 0x0D, 0x96, 0x76, 0xCE, 0x04, 0x28, 0x70, 0x98, 0x0B, 0x2C, 0xB4, 0xA5, 0xE3, 0x11,
0x5E, 0x2A, 0x0B, 0xB4, 0x49, 0x51
```
