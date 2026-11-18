# QSPI Datalogger Test Coverage

Coverage for `qspi_datalogger` (the canonical SPI / Dual-SPI / Quad-SPI datalogger).
Landed by the adversarial campaign. **25 tests across 4 elaboration
builds**; `rr sim coverage-map` 11/12. The campaign found and fixed **3 DUT
defects** + 1 testability fix.

## Builds (each re-elaborates with different generics)

| Build / file | Generics | What it covers |
|---|---|---|
| `test_qspi_capture.py` (14) | `G_QSPI_PINS=6`, `G_CLK_FREQ_HZ=100k` | Quad payload format (A/B both type 001), all 4 modes, framing, padding, half-duplex, no-CS, CPOL, device-id, timeout |
| `test_qspi_spi2pin.py` (6) | `G_QSPI_PINS=4` | 2-data-pin format (type-tagged A=001/B=010 + embedded timestamp), capture-time stamping |
| `test_qspi_infilter.py` (2) | `G_ENABLE_INFILTER=1` | Input glitch-filter (clean capture + spurious-SCLK rejection) |
| `test_qspi_datalogger.py` (2) | defaults | Reset determinism + lifecycle self-reset |

## SPI mode coverage (CPOL/CPHA, CPOL auto-detected from idle SCLK)

| Mode | CPOL | CPHA | Sample edge | Test | Status |
|---|---|---|---|---|---|
| 0 | 0 | 0 | rising (1st) | `test_mode0_single_byte_io0` | ✅ |
| 1 | 0 | 1 | falling (2nd) | `test_mode1_single_byte_io0` | ✅ |
| 2 | 1 | 0 | falling (1st) | `test_mode2_single_byte_io0` | ✅ |
| 3 | 1 | 1 | rising (2nd) | `test_mode3_single_byte_io0` | ✅ |
| CPOL flip mid-stream | — | — | re-latch + NEW_POL event | `test_cpol_change_between_frames` | ✅ |

## Payload coverage

| Payload | Description | Test | Status |
|---|---|---|---|
| Quad A/B | `{Q1,Q0}` then `{Q3,Q2}`, both type 001, order-distinguished | `test_quad_four_lines` | ✅ |
| Multi-byte | back-to-back bytes in one CS frame | `test_multibyte_backtoback_io0` | ✅ |
| 2-pin A (001) | MOSI/IO0 byte + embedded timestamp | `test_spi2pin_payloadA_io0` | ✅ |
| 2-pin B (010) | MISO/IO1 byte (type-tagged) | `test_spi2pin_payloadB_io1` | ✅ |
| 2-pin A+B distinct | both lines, distinguished by type | `test_spi2pin_both_lines_distinct` | ✅ |
| Embedded timestamp | `[23:8]` carries `TIMESTAMP` at **capture** time | `test_spi2pin_timestamp_embedded`, `test_timestamp_is_capture_not_send_time` | ✅ |
| Partial / padded | CS up mid-byte → MSB-justified + zero-pad | `test_partial_byte_padding` | ✅ |

## Event coverage

| Event | Code | Test | Status |
|---|---|---|---|
| START / STOP | `0x03` / `0x00` | `test_capture_rst_reconfig` | ✅ |
| CS down / up | `0x09` / `0x0A` | `test_cs_framing_events` | ✅ |
| CS down new-polarity | `0x10` / `0x11` | `test_cpol_change_between_frames` | ✅ |
| CS up incomplete | `0x0B` | `test_partial_byte_padding` | ✅ |
| Event capture-time stamp | — | `test_event_timestamp_is_capture_not_send_time` | ✅ |
| SCLK-stuck timeout | `0x04` | `test_sclk_stall_timeout` | ✅ (reachable via scaled `G_CLK_FREQ_HZ`) |

## Feature / config coverage

| Feature | Test | Status |
|---|---|---|
| Half-duplex (3-wire) — payloadB suppressed | `test_half_duplex_no_b_payload` | ✅ |
| No-CS (2-wire) — self-start sampling | `test_no_cs_2wire_capture` | ✅ |
| Device-id filter (`CONFIG[15:11]`) | `test_device_id_filter` | ✅ |
| Input glitch filter (`G_ENABLE_INFILTER`) | `test_infilter_clean_capture`, `test_infilter_rejects_sclk_glitch` | ✅ |

## Adversarial coverage

| Scenario | Test | Status |
|---|---|---|
| Idle bus — SCLK toggling with CS HIGH captures nothing | `test_idle_bus_cs_high_no_payload` | ✅ |
| Backpressure — `QSPI_MSG_RDY=0` holds the payload intact | `test_backpressure_holds_payload` | ✅ |
| Spurious 1-cycle SCLK glitch rejected (filter on) | `test_infilter_rejects_sclk_glitch` (discriminator-verified) | ✅ |

## REQ → test map (`requirements.yml`, enforced by `rr sim coverage-map`)

`QSPI-REQ-10/11/12/13/14/15/22/31/40/41/100` + `-50/-51` (lifecycle). 11/12 ✓
(REQ-40 shows ⊘ — the cosmetic signal-name heuristic, same as the i2c/uart
injector REQs).

## Defects found & fixed by this campaign

| Item | Defect | Fix |
|---|---|---|
| **Padding** | partial-byte padding was dead logic (`qspiSrQxPad_r` written, never read) → right-aligned garbage | left-justify `qspiSrQx_r` during PAD_PACKET |
| **No-CS Mode** | no-CS (2-wire) mode could never start (FSM only left IDLE on a CS edge) | self-start IDLE→SAMPLING when `noChipSelectMode='1'` |
| **Timestamps** | timestamps stamped at **delivery**, not capture → unbounded error under backpressure | latch `tsPayload_r`/`tsEvent_r` at capture |
| **Timeout Scaling** | SCLK-stuck timeout was a hard 1s constant — sim-unreachable + mis-scaled | derive from `G_CLK_FREQ_HZ` generic |

## Summary

| Category | Tests | Status |
|---|---|---|
| SPI modes (0-3 + CPOL flip) | 5 | ✅ |
| Payloads (quad + 2-pin + timestamp + padding) | 8 | ✅ |
| Events (framing + timeout + capture-time) | 4 | ✅ |
| Features (half-duplex, no-CS, device-id, glitch filter) | 5 | ✅ |
| Adversarial (idle, backpressure) | 2 | ✅ |
| Lifecycle / reset | 2 | ✅ |
| **Total** | **25** (incl. multi-purpose) | **✅ all pass** |

Run: `rr sim run test_qspi_capture` / `test_qspi_spi2pin` / `test_qspi_infilter` /
`test_qspi_datalogger`.
