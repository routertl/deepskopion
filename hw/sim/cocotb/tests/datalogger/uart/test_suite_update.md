# Test Suite Update: Timestamp Drivers

## Summary

The test suite has been updated to systematically drive the `TIMESTAMP` input on the `uart_datalogger` module. This resolves the propagation of 'U' values into the captured messages and ensures correct protocol verification.

## Updated Tests

1. **`test_uart_datalogger_parameterized.py`**
    * **Change:** Added independent `timestamp_driver` coroutine to `setup_dut`.
    * **Impact:** Ensures all parameterized config sweeps (baud/parity/bits) operate with valid timestamps.

2. **`test_uart_highspeed_debug.py`**
    * **Change:** Added `timestamp_driver` coroutine to `setup_dut`.
    * **Impact:** Validates that high-speed calibration and message capture work with proper timing references.

3. **`test_uart_integration_debug.py`**
    * **Change:** Added `timestamp_driver` coroutine to `setup_dut`.
    * **Impact:** Enables clean debugging of internal FSMs without 'U' values obscuring logic states.

## Verification Status

* **`test_uart_datalogger.py`** was already correct (uses `UartDriver`).
* **`test_uart_frame_gen_isolation.py`** was already correct (drives `WAVE.TIMESTAMP` manually).
* **`test_5bit_odd_parity.py`** was fixed in the previous step.

All primary integration tests now drive `TIMESTAMP` correctly.
