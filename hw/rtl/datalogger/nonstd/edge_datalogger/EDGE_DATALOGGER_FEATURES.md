# Edge Detector Datalogger — Features & Roadmap

> **Module**: `edge_datalogger.vhd`
> **Category**: Non-standard (`nonstd/`)

---

## Implementation Status

| Feature | Status | Version | Notes |
| :------ | :----: | :-----: | :---- |
| Both-edge detection | ✅ | v1.0 | Rising + falling on single pin |
| Sub-µs timestamp extension | ✅ | v1.0 | 8-bit counter (10 ns resolution at 100 MHz) |
| CONFIG enable/disable | ✅ | v1.0 | Via `config_control` shared infra |
| SW-triggered readback | ✅ | v1.0 | Read + Capture / Read + Stop modes |
| FIFO buffering (FWFT) | ✅ | v1.0 | 256 words, portable `fifo_sync` boundary |
| Phase accumulator test path | ✅ | v1.0 | Performance testing via `datalogger_gather` |
| Standard type field | 🔲 | v2.0 | Phase 3: Replace S/F/E with `0b010`/`0b011`/`0b001` |
| Auto initial-state readback | 🔲 | v2.0 | Phase 3: Emit initial state on enable |
| Stream-RLE mode | ✅ | v1.2 | CONFIG[4:3]=`10`; SYNC + 3 × 8-bit deltas per PACK word, LONG/IDLE for long gaps, re-SYNC after a lost word. `ED_MSG_STREAM` flag; framed as `EVT_EDGE_RLE` (0x0609); daemon decodes to EDGE_EVENTS (`sw/stream/edge_capture.py` `EdgeRleDecoder`) |
| Stream-1 (sampled) mode | 🔲 | — | CONFIG[4:3]=`01` reserved. Dropped by the v2 FDD §7.2 in favour of differential mode |
| Input filter (debounce) | 🔲 | — | `G_ENABLE_INFILTER` generic exists but unused |
| Max-frequency tracker | ✅ | v1.1 | `ED_MAX_INTERVAL`/`ED_MAX_NEW`: smallest validated edge-to-edge interval (= max freq); glitch floor `G_MAXFREQ_MIN_INTERVAL`; confirmation gate `G_MAXFREQ_CONFIRM` (largest of the last N intervals, so one runt wider than the floor cannot raise the max at N >= 4; EDGE-REQ-64, default 1 = off); host re-arm via `CONFIG[2]`. Surfaced **in-band** on the edge stream as `EVT_EDGE_MAXFREQ` (DOMAIN_EDGE \| event_id 0x02; see `edge_datalogger_protocol.yml` and `sw/stream/zs_msg.py`), NOT via a regbank CSR — so RouteWave can correlate it in time with the live edges. (D.27/D.28 are RouteWave sigrok-decoder decisions and do not govern this split; the formal decision for the in-band choice is recorded super-repo-side.) |

## Roadmap

### Phase 1 — Documentation (Conv18, Feb 2026) ✅

- [x] YAML spec corrected to match RTL
- [x] PROTOCOL.md created
- [x] FEATURES.md created (this file)
- [x] Compliance checklist created
- [x] Architecture spec for v2 evolution

### Phase 2 — Centralized Error Reporting

- [ ] New `infra/error_reporter.vhd` entity
- [ ] Monitor arbiter FIFO overflow across all dataloggers
- [ ] Emit timestamped STATUS messages for overflow events
- [ ] Remove `F` bit from Edge Detector word format
- [ ] Cocotb test: `test_error_reporter.py`

### Phase 3 — Standard Type Field (Option B)

- [ ] Replace S/F/E flags with 3-bit `type` discriminator
- [ ] Type codes: `0b010` rising, `0b011` falling, `0b001` initial state
- [ ] Auto-emit initial state message on `configEnable` rising edge
- [ ] Remove ReadEvent logic and associated CONFIG codes
- [ ] Simplify CONFIG to single enable bit
- [ ] Cocotb test: `test_edge_datalogger_v2.py`

### Phase 4 — Adaptive Streamer Mode

- [x] Mode selector at CONFIG[4:3] (CONFIG[2] is the max-freq re-arm): `00`
      Event, `10` Stream-RLE, `01`/`11` reserved (Event)
- [ ] Stream-1 (24 samples per word): not built; FDD §7.2 reserves `01`
- [x] Stream-RLE: 3 × 8-bit deltas per PACK word, plus SYNC / LONG / IDLE
      words (`edge_datalogger_protocol.yml` `rle_*`)
- [x] Routing: the stream arbiter no longer exists (D.4). RLE words take the
      lifecycle frame's route instead: `ED_MSG_STREAM` → gather tag
      `EVT_EDGE_RLE` (0x09) → framed alone as 0x0609, never batched
- [x] Daemon: `DdrRingDataSource` decodes 0x0609 frames to EDGE_EVENTS words
- [x] Cocotb test: `test_edge_streamer.py`, plus
      `test_datalogger_to_ddr.py::test_edge_rle_to_ddr`
- [ ] Silicon capture of a Stream-RLE line (not done yet)

## Dependencies

- **RouteRTL SDK**: All verification uses NVC simulator + Cocotb 2.0+
- **Arbiter**: Phase 2 requires new input slot in burst arbiter
- **datalogger_gather.vhd**: Phases 2 and 4 require gathering topology changes
- **datalogger_config_pkg.vhd**: Phase 2 requires `C_NUM_ARB_INPUTS` update

## Known Limitations (v1.0)

1. **Bandwidth**: 32 bits per edge event for 1 bit of data (3.125% efficiency)
2. **FIFO depth**: **256 entries** — `fifo_sync`'s `G_FIFO_DEPTH=8` is an
   **exponent** (2^8 = 256), not a literal count. Under a sustained 1 MHz toggle
   the gather drains it continuously (silicon-proven: 100 M+ edges captured with
   zero overflow); it overflows only when the gather/ring backpressures.
3. **Error messages**: ✅ **driven** — `ED_ERRORS` bit[0] =
   internal-FIFO-overflow → `ERR_CNT_EDGE`, reported in-band as a `SEV_ERROR`
   loss frame (silicon-proven). See `EDGE_DATALOGGER_PROTOCOL.md` §5.
4. **No initial state**: First edge polarity must be inferred by SW
5. **Non-standard format**: S/F/E flags prevent standard type-based routing
