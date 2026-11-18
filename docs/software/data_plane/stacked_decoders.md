# Stacked decoders

Facts verified against `bc692605` plus `sw/stream/stacked.py`, on 2026-11-07.
Tested against simulated ring frames and exercised in RouteWave (`routewave` live attach + `routewave_cli stream-conformance`). Not exercised on silicon.

A stacked decoder consumes the decoded events of one annotation lane and emits higher-level events on the same timeline.

## Placement

| Item | Decision |
|---|---|
| Location | PS daemon, `sw/stream/stacked.py` |
| Not in the fabric | Reassembly costs LUTs and gives no latency benefit. No hardware trigger uses it. |
| Hardware trigger on a stacked predicate | Separate ticket and separate cost. Not provided here. |

## Registration and chaining

| Item | Rule |
|---|---|
| Declaration | `StackedChain.register(decoder, source)`. `source` is a lane id or name. |
| Daemon option | `--stack SOURCE:DECODER`, repeatable, for example `--stack uart_byte:nmea0183` |
| Valid source | An annotation lane (width 0), or an earlier stacked lane |
| Refused | Unknown lane, SAMPLES lane, same decoder twice on one lane |
| Output lane | One per registration, ids from 400, name `<decoder>@<source>` |
| Order | Registration order is dependency order. A cycle cannot be declared. |
| Default | No `--stack` option: no stacked lanes, channel table unchanged |

Decoders available: `nmea0183`.

## Event shape

Stacked events are ANNOTATIONS frames. Every stacked event payload carries:

| Key | Meaning |
|---|---|
| `source_channel` | Id of the consumed lane |
| `layer_depth` | 1 over a datalogger lane, source depth + 1 over a stacked lane |
| `decoder` | Decoder name |

The HELO channel descriptor of a stacked lane also carries `source_channel` and `layer_depth`. On `wave_events_v1` both are in the event `body` and the HELO manifest.

## Timestamps

| Field | Value |
|---|---|
| `ts_start` | `ts_start` of the first source event of the PDU |
| `ts_end` | `ts_end` of the last source event of the PDU |

Reassembly time is not used.

## Lossy input

| Input | Position in the source stream | Result |
|---|---|---|
| Source event with `is_error` | Exact | Decoder reset, `stacked_gap` at that event |
| `loss` on the source lane | Unknown | Discard window |
| `loss` with stage `ring_full`, on any lane | Unknown | Discard window |
| `zs_resync` | Unknown within its batch | Discard window |
| `error_run` with `ongoing` true on the source lane | Unknown within its batch | Discard window |
| End of stream with a partial PDU | End | `stacked_gap`, reason `end_of_stream` |
| End of stream with source events still held (last batch had `Batch.ring_drained` false) | End | No held PDU is emitted. One `stacked_gap`, reason `ring_not_drained_at_eof`, spans the held events, with `withheld_pdus`, `withheld_events` and `partial_events`. Committed ring data is still unread, so a coalesced in-band `loss` frame may follow. |

Discard window:

| Step | Behaviour |
|---|---|
| Backlog hold | While `Batch.ring_drained` is false and no marker has opened a window, source events are buffered in `_pending` without feeding the decoder. This prevents a PDU completed between a drop and a coalesced in-band `loss` frame further back in the ring from being emitted before that `loss` frame is read. |
| Open | When a batch carries an out-of-band marker (`in_band` false, `zs_resync`, `error_run`), the decoder is reset, `stacked_gap` is emitted with `reason` and `partial_events`, and `_pending` plus the whole batch is discarded. When an in-band coalesced `loss` frame (`in_band` true) is reached, `stacked_gap` and `stacked_resync` are emitted at that frame and every event buffered in `_pending` ahead of the `loss` frame is discarded and counted. |
| During | Source events are discarded and counted. No PDU is emitted. |
| Close | For out-of-band markers, at the end of a later batch with no out-of-band marker and `Batch.ring_drained` true (`stacked_resync` with `discarded_events`). For in-band `loss` frames, at the `loss` frame itself once the preceding interval is discarded, with subsequent backlog events held until `Batch.ring_drained` is true. |

The loss counters are read after the commit pointer of the same tick. Only the commit pointer of a later tick is known to be past the drop. Bytes after that pointer were written after the drop. In-band `loss` frames are coalesced in the PL (`error_event_emitter`, `G_FLUSH_CYCLES`), so they sit in the ring after data written behind the drop; holding source events until `Batch.ring_drained` is true ensures any PDU completed in that interval is caught and discarded when the coalesced `loss` frame is reached.

A drained batch releases every held event, so held events remain at end of stream only when the last batch left the ring undrained. Their PDUs are reported as one `ring_not_drained_at_eof` gap, never emitted and never dropped silently.

The window discards clean PDUs near a loss. This is the accepted cost of not emitting a joined PDU.

A gap on a stacked lane has `is_error` true, so the layer above resets on it.

## wave_stream_v1 contract

| Item | Decision |
|---|---|
| FrameTag | None allocated |
| Annotation kinds | `stacked_gap`, `stacked_resync`, `nmea0183_*`. Unregistered kinds are legal: a consumer renders them generically. |
| Channel descriptor keys | `source_channel`, `layer_depth`. Unknown map keys are ignored by a consumer. |
| `contract_revision` | Unchanged. The vendored directory is not edited. |

## RouteWave presentation

| Item | Decision |
|---|---|
| Layout | One row per stacked lane, not mixed into the source row |
| Reason | Rates differ by layer, and a consumer can hide a layer by its row |
| Current consumer | Verified against RouteWave (`stream attach` WCP and `routewave_cli stream-conformance`): HELO descriptors with `source_channel` and `layer_depth` are accepted with zero parse errors, `nmea0183@uart_byte` (channel 400) is materialised as its own signal row, and stacked annotations (`STACKED_GAP`, `STACKED_RESYNC`, `NMEA0183_SENTENCE`) render on `nmea0183@uart_byte`. |
| Nesting under the source row | Consumer work, from `source_channel` and `layer_depth` in HELO |

## Items to confirm

- CAN and JTAG have no drop counter.
