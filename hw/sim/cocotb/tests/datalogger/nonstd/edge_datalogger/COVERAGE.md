# Edge Datalogger Test Coverage

Coverage for `edge_datalogger` (the raw edge-capture front-end that feeds the
`edge_batcher` / EDGE_BATCH pipeline). Test file:
`hw/sim/cocotb/tests/datalogger/nonstd/edge_datalogger/test_edge_datalogger.py` (3 tests).

## Message format (32-bit `ED_MSG_OUT`)

| Field | Bits | Source | Notes |
|---|---|---|---|
| device_id | `[31:27]` | `G_ED_DEV_ID` (default 28) | datalogger instance id |
| s_bit | `[26]` | `ReadEvent or ReadEvent_r` | read-event marker |
| f_bit | `[25]` | `fifoEdAlmostFull` | FIFO almost-full backpressure flag |
| e_bit | `[24]` | `not EDGE_IN` (inverted at an edge) | captured line polarity |
| timestamp | `[23:8]` | `timeStamp_r` | low 16 bits of `TIMESTAMP` |
| ts_ext | `[7:0]` | `timeStampExt_r` | sub-µs extension counter (EDGE-specific, T4) |

The `(timestamp, ts_ext)` tuple is captured **into the FIFO at the edge instant**
(`fifoEdDin <= s & f & e & timeStamp_r & timeStampExt_r`), so the embedded
timestamp is capture-time even under `ED_MSG_RDY` backpressure (§T5 —
edge is `fifo`-strategy compliant).

## Coverage

| Scenario | Test | Status |
|---|---|---|
| Reset determinism — VLD low + errors clear post-reset | `test_reset_determinism` | ✅ |
| No false overflow while a consumer drains normally | `test_no_false_overflow_while_draining` | ✅ |
| Internal FIFO overflow counts dropped edges (no silent loss) | `test_internal_fifo_overflow_counts_dropped_edges` | ✅ |
| event mode for CONFIG[4:3] 00/01, stamp = drive + 1 (EDGE-REQ-63) | `test_edge_streamer.py::test_event_mode_stamp_lag` | ✅ |
| Stream-RLE SYNC + PACK words (EDGE-REQ-59) | `test_edge_streamer.py::test_rle_pack_words` | ✅ |
| Stream-RLE LONG words (EDGE-REQ-60) | `test_edge_streamer.py::test_rle_long_words` | ✅ |
| Stream-RLE IDLE word (EDGE-REQ-60) | `test_edge_streamer.py::test_rle_idle_word` | ✅ |
| partial PACK flush at 255 ticks (EDGE-REQ-61) | `test_edge_streamer.py::test_rle_partial_pack_flush` | ✅ |
| partial PACK flush on disable (EDGE-REQ-61) | `test_edge_streamer.py::test_rle_disable_flush` | ✅ |
| re-SYNC after a word lost to a full FIFO (EDGE-REQ-62) | `test_edge_streamer.py::test_rle_resync_after_lost_word` | ✅ |
| Max-freq confirmation gate (N = 4): a runt above the floor cannot raise the max (EDGE-REQ-64) | `test_edge_datalogger_maxfreq_confirm.py::test_maxfreq_runt_above_floor_cannot_raise_max` | ✅ |

## REQ → test map

`EDGE-REQ-50` (reset determinism) in `requirements.yml`. The overflow-accounting
tests are the design's load-bearing contract (the edge front-end must never
silently drop edges — a dropped edge corrupts the downstream EDGE_BATCH timeline).

## Summary

| Category | Tests | Status |
|---|---|---|
| Reset / determinism | 1 | ✅ |
| FIFO overflow accounting | 2 | ✅ |
| **Total** | **3** | **✅ all pass** |

## Gaps / next steps

The current suite pins reset + the FIFO-overflow accounting. Not yet covered
(future REQs): per-edge **polarity** decode (e_bit correctness on rising vs
falling), the **sub-µs `ts_ext`** rollover vs the 16-bit timestamp wrap, and a
full **EDGE_BATCH** end-to-end (this front-end → `edge_batcher` →
`parse_edge_batch`). The EDGE_BATCH decode itself is validated on silicon (see
the edge-datalogger work and validation testbench).
