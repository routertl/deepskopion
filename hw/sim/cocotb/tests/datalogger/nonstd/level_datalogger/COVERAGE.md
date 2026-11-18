# Level Datalogger Test Coverage

Coverage for `level_datalogger` (8-line parallel level-change capture). Test file:
`hw/sim/cocotb/tests/datalogger/nonstd/level_datalogger/test_level_datalogger.py` (4 tests).
See `LEVEL_DATALOGGER_PROTOCOL.md` for the frame layout.

## Coverage

| Scenario | Test | REQ | Status |
|---|---|---|---|
| Reset determinism — VLD low, ERRORS clear (hard-zero) post-reset | `test_reset_determinism` | LEVEL-REQ-50 | ✅ |
| Lifecycle — symmetric ENABLED(`0x03`)/DISABLED(`0x00`), silent when steady | `test_lifecycle_enable_disable` | LEVEL-REQ-51 | ✅ |
| Per-datalogger reset — disable flushes state; re-enable re-emits current level | `test_capture_rst_reconfig` | LEVEL-REQ-52 | ✅ |
| Capture-time stamp — change-time `(ts, pins)` survives `MSG_RDY` backpressure | `test_timestamp_is_change_time_not_delivery` | LEVEL-REQ-10 | ✅ (discriminator-verified) |

## Event coverage

| Event | Code | Test | Status |
|---|---|---|---|
| ENABLED | `0x03` | `test_lifecycle_enable_disable` | ✅ |
| DISABLED | `0x00` | `test_lifecycle_enable_disable` | ✅ |

## Payload coverage

| Payload | Description | Test | Status |
|---|---|---|---|
| Level snapshot | 8-pin value `[7:0]` on a change | `test_capture_rst_reconfig`, `test_timestamp_is_change_time_not_delivery` | ✅ |
| Capture-time stamp | `[23:8]` = `TIMESTAMP` at the change instant | `test_timestamp_is_change_time_not_delivery` | ✅ |

## Summary

| Category | Tests | Status |
|---|---|---|
| Reset / determinism | 1 | ✅ |
| Lifecycle | 1 | ✅ |
| Per-datalogger reset | 1 | ✅ |
| Capture-time stamping | 1 | ✅ |
| **Total** | **4** | **✅ all pass** |

## Notes / gaps

- The capture-time test is **discriminator-verified**: it fails against
  the un-fixed RTL (delivery-time stamp), so it has teeth.
- `level_datalogger` capture-time stamping strategy (`latch`) is pinned by the
  structural guard (`test_datalogger_timestamp_capture_contract.py`).
- Not yet covered: multi-line simultaneous change, rapid back-to-back transitions
  (coalescing behaviour), and the device-id filter on the level instance.

Run: `rr sim run test_level_datalogger`.
