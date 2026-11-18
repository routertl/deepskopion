# `wave_events_v1` — decoded-events stream specification

**Status:** Implemented (2026-10-30). Not yet exercised on silicon.
**Producer:** deepskopion-daemon (`sw/stream/__main__.py --events-listen ...`).
**Consumers:** AI agents, log scrapers, Grafana exporters, RouteWave, CI regression scripts.

## Why this exists

`wave_stream_v1` (the existing protocol) is a binary msgpack stream of lane
transitions optimised for RouteWave's GUI ingest. It carries:

  * Channel manifest (signals + annotations, IDs, widths).
  * Time-ordered transitions per channel.
  * Sample-clock metadata.

That format is excellent for human visualisation — the GUI re-aggregates
transitions into the wave display, runs translator passes to decode UART/I2C/
SPI/etc., and renders the decoded frames in the Protocol Analyzer panel.

`wave_events_v1` is the **agent-facing peer** of that stream. It carries
**already-decoded events** as newline-delimited JSON, one event per line,
each line independently parseable. Properties chosen for agent
consumption:

  * **Line-delimited.** Stream-parseable by any tool that can iterate lines
    (`nc | jq`, `tail -f | grep`, Python `for line in sock.makefile()`,
    `node split2`).
  * **Pure JSON.** stdlib parseable in every language; no msgpack
    dependency.
  * **Pre-decoded.** Producer (Deepskopion's PS-side daemon) runs the
    protocol decoders next to the data source, so it knows which lane is
    UART vs I2C vs custom-zs. Agents consume the answers, not the substrate.
  * **Same channel IDs as `wave_stream_v1`** for every annotation channel,
    so consumers that bridge both streams can cross-reference. Edge channels
    are the exception: `wave_stream_v1` carries edges by device id outside its
    channel table, so ids 300-331 (`edge[n]`, device id n) exist only here.

`wave_events_v1` does NOT replace `wave_stream_v1`. They're two views of
the same in-PL truth, serialised for different consumers. Producers ship
both simultaneously on separate ports.

## Transport

  * **TCP** by default. Producer listens; consumer connects.
  * Default port: **7476** (`wave_stream_v1` lives at 7475). With `--listen
    HOST:PORT` the lane is on by default at `HOST:7476`. With a Unix-socket
    `wave_stream_v1` it is off by default, so a local daemon opens no TCP port.
  * **UNIX socket** accepted as alternate transport:
    `--events-listen unix:/tmp/rr_wave_events.sock`.
  * `--events-listen ""` disables the lane. If the port cannot be bound the
    daemon logs an error and keeps serving `wave_stream_v1`.
  * **Many concurrent consumers** in the default fan-out mode, fed by the same
    single ring drainer as `wave_stream_v1`. A consumer leaving does not end the
    lane. Consumers see events from when they connect: there is no backfill.
  * `--serve-once --events-listen ...` serves one consumer on this lane instead
    of `wave_stream_v1`, then exits. The ring has one owner, so `--serve-once`
    cannot serve both.

## Wire format

Newline-delimited JSON. Each line is one independent JSON object. No
multi-line objects, no pretty-printing, no leading/trailing array
brackets. UTF-8, `\n` line terminator (LF only — never CRLF, even on
Windows producers; consumers SHOULD also accept stray `\r` defensively
but producers MUST NOT emit them).

A session has three phases:

  1. **HELO** — producer's first line. Identifies version + producer +
     channel manifest.
  2. **(optional) SUBSCRIBE** — consumer's first line. Filters
     server-side. Default behaviour with no SUBSCRIBE: emit all
     `frame` + `annot` events for all channels.
  3. **Events** — producer streams `frame` / `annot` / `edge` lines
     until the consumer disconnects or the source ends.

## HELO (server → client, first line)

```json
{
  "v": "wave_events_v1",
  "producer": "deepskopion-daemon/0.1.0",
  "started_at": "2026-05-22T08:30:00Z",
  "sample_clk_hz": 100000000,
  "session_id": "ds-2026-05-22T08-30-00-abc123",
  "channels": [
    {"id": 100, "name": "uart_byte", "kind": "frame",
     "schema": "uart_byte_v1"},
    {"id": 101, "name": "i2c_frame:4", "kind": "frame",
     "schema": "decoded_v1"},
    {"id": 200, "name": "pl_heartbeat", "kind": "annot",
     "schema": "counter_v1"},
    {"id": 302, "name": "edge[2]", "kind": "edge",
     "schema": "edge_v1"}
  ]
}
```

`channels[].kind` is the channel's main event kind. On the `regbank` source a
protocol channel also carries `annot` lines for its packet and error counters.

| kind  | Meaning |
|-------|---------|
| `frame` | Decoded protocol message (UART byte, I2C frame, custom zs_msg). |
| `annot` | Counter, state change, alert — periodic or event-driven annotation. |
| `edge`  | Raw signal transition. Suppressed by default (volume); only emitted on explicit `SUBSCRIBE`. |

`channels[].schema` is a short stable name for the `body` shape (see below).

A stacked lane (`--stack`, see `stacked_decoders.md`) has kind `frame`, and its
manifest entry and `body` carry `source_channel` and `layer_depth`.
Schemas are forward-compatible: producers may add fields, consumers MUST
ignore unknown fields. Removing or repurposing a field requires a version
bump (`uart_byte_v2`).

## SUBSCRIBE (client → server, first line, optional)

```json
{"subscribe": {
   "channels": [100, "i2c_frame:4"],
   "kinds": ["frame"]
}}
```

| Field | Type | Meaning |
|-------|------|---------|
| `channels` | (int or string)[] / `"*"` | Channel IDs or names to include. `"*"` = all. Default `"*"`. |
| `kinds` | string[] / `"*"` | Event kinds to include. `"*"` = all. Default `["frame", "annot"]` (excludes `edge`). |

Other keys inside `subscribe` are ignored. If the client doesn't send one
complete line within 500 ms of HELO, the producer applies defaults and ignores
anything sent later. To explicitly accept defaults, the client may send the
empty object `{}` as its first line.

A first line that is not valid JSON, has the wrong shape, or names an unknown
channel or kind is refused with a fatal `error` line and the socket closes. A
filter that matched nothing would look like a quiet bus.

## Event lines (server → client, after HELO)

Common envelope (every event line has these fields):

```json
{
  "ts_ns": 12345678,
  "ch": 100,
  "kind": "frame",
  "name": "uart_byte",
  "body": { ... }
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `ts_ns` | int (unsigned 64-bit) | Same timebase as the `wave_stream_v1` annotation `ts_start`. |
| `ts_end_ns` | int | Annotation end time, same as `ts_end`. Absent on `edge` lines. |
| `ch` | int | Channel ID matching one in the HELO manifest. |
| `kind` | string | `"frame"` \| `"annot"` \| `"edge"`. |
| `name` | string | Human-friendly channel name (duplicates manifest for grep-ability). |
| `body` | object | Kind-specific schema; see below. |

### Schemas for `body`

Every `frame` and `annot` body holds the same three fields, plus the decoder's
structured fields (`decoders.DecodedFrame.fields`) unchanged:

| Field | Type | Meaning |
|-------|------|---------|
| `decoded_as` | string | Decoder event kind (`uart_byte`, `i2c_byte`, `spi_mosi`, `uart_parity_error`...). |
| `text` | string | The label RouteWave renders. |
| `errors` | string[] | `[decoded_as]` if the decoder flagged an error, else `[]`. |

#### `decoded_v1`

The common fields and the decoder's fields, for example an I2C byte:

```json
{"data": 85, "ack": true, "datalogger_id": 4, "decoded_as": "i2c_byte",
 "text": "0x55 ACK", "errors": []}
```

Events are per decoded message as the PL frames them: one per I2C or SPI byte
and per START/STOP/CS event. They are not reassembled into transactions.

#### `uart_byte_v1`

`decoded_v1`, plus these on a received character:

| Field | Type | Meaning |
|-------|------|---------|
| `value` | int (0-255) | Decoded byte value. |
| `char` | string | The character if printable, or tab, LF or CR; otherwise the text `\xNN`. |

UART status and config events share the channel and carry `status` or `config`
instead. `baud` is not emitted: the daemon has no live baud measurement today.

#### `counter_v1`

`decoded_v1`, plus `counter` (int), for `pl_heartbeat`.

#### `edge_v1`

```json
{"value": 1, "prev_value": 0}
```

Raw signal transition, emitted only when subscribed. `prev_value` is null on a
state readback (S=1). `ts_window_relative: true` is added when the source gave
no time anchor: `ts_ns` is then an offset inside a 65.536 ms window and MUST NOT
be compared with annotation times.

## Error and end-of-stream

  * Producer-side fatal error (lost connection to PL, daemon dying):
    final line is

    ```json
    {"kind": "error", "ts_ns": 9999, "body": {"message": "...", "fatal": true}}
    ```

    followed by socket close.

  * Producer-side non-fatal warning (overflow, dropped frames):

    ```json
    {"kind": "warning", "ts_ns": 9999, "body": {"message": "...", "dropped": 42}}
    ```

  * Producer-side clean end-of-session (test-mode finite stream):

    ```json
    {"kind": "end", "ts_ns": 9999, "body": {"reason": "source_exhausted"}}
    ```

    followed by close.

  * Session lines (`error`, `warning`, `end`, `ping`) have no `ch` or `name`
    and are never filtered by a subscription.

  * Consumers MUST tolerate long idle periods (FPGA may be quiescent).

  * After a consumer shuts down its write side (`nc </dev/null`), an idle
    session gets `{"ts_ns":0,"kind":"ping","body":{}}` every 2 s. TCP cannot tell
    a half-close from a full close, so this send is how a gone consumer is
    found on a quiet bus. Other sessions never see `ping`.

## Versioning

  * `wave_events_v1`: this document.
  * Schema additions to `body` are backwards-compatible (consumers ignore
    unknown fields).
  * Schema removals / repurposing require a new `wave_events_v2` version
    string in HELO.
  * Producer MUST emit the version string it implements in HELO; consumer
    MUST validate and disconnect on mismatch (with a clear log line).

## Example session

```
$ nc 198.51.100.50 7476
{"v":"wave_events_v1","producer":"deepskopion-daemon/0.1.0","started_at":"2026-05-22T08:30:00Z","sample_clk_hz":100000000,"session_id":"ds-2026-05-22T08-30-00-abc123","channels":[{"id":100,"name":"uart_byte","kind":"frame","schema":"uart_byte_v1"}, ...]}
{"ts_ns":12345678,"ts_end_ns":12395678,"ch":100,"kind":"frame","name":"uart_byte","body":{"byte":85,"ascii":"U","value":85,"char":"U","decoded_as":"uart_byte","text":"0x55 'U'","errors":[]}}
^C
```

## Agent recipes

### Tail the first 5 UART frames

```bash
nc 198.51.100.50 7476 | jq -c 'select(.name == "uart_byte" and .kind == "frame")' | head -5
```

### Subscribe to one channel from a Python client

```python
import json, socket
s = socket.create_connection(("198.51.100.50", 7476))
f = s.makefile("rb")
helo = json.loads(f.readline())
s.sendall(json.dumps({"subscribe": {"channels": [100], "kinds": ["frame"]}}).encode() + b"\n")
for line in f:
    ev = json.loads(line)
    if ev["kind"] == "frame":
        print(f"{ev['ts_ns']}: {ev['body']['char']!r}")
```

### Anomaly hook (any error event)

```bash
nc 198.51.100.50 7476 | jq -c 'select(.body.errors? and (.body.errors | length > 0))'
```

## Relationship to `wave_stream_v1`

| Aspect | `wave_stream_v1` | `wave_events_v1` |
|--------|------------------|------------------|
| Encoding | msgpack (binary) | NDJSON (text) |
| Granularity | per-transition | per-decoded-event |
| Decoder side | RouteWave's translator chain (consumer) | Deepskopion's PS daemon (producer) |
| Default port | 7475 | 7476 |
| Primary consumer | RouteWave GUI | Agents, scripts, exporters |
| Same channel IDs? | yes | yes, for annotation channels |

Both streams emit from the same source, encode the same in-PL truth from
the same `zs_msg` frames flowing through the DDR ring. They differ only
in serialisation + decoding side. A consumer that wants both (e.g.
RouteWave with trust-source mode) connects to both ports.

## Implementation notes

  * `sw/stream/protocol.py` — `WaveEventsEncoder` next to the `wave_stream_v1`
    helpers. Pure stdlib `json.dumps(..., separators=(',',':'))` + `b'\n'`.
  * `sw/stream/server.py` — `_EventHub` is fed by the one `_drainer_loop` with
    exactly what went out on `wave_stream_v1`, so a batch the contract guard
    refuses is on neither lane. `serve_one_events` is the `--serve-once` form.
  * `sw/stream/__main__.py` — `--events-listen`. The board's init script passes
    only `--listen`, so it gets the lane without changing.
  * `sw/stream/decoders.py` — unchanged. The encoder reads the same `Annotation`
    `wave_stream_v1` sends. Single source of truth.
  * Tests: `sw/stream/tests/test_wave_events.py`.

## Open questions for v2

  * Compression (gzip wrap on the socket)? Today: no. Agents handle JSON
    text fine over 100 Mbps; compression complicates `nc | jq` recipes.
    Revisit if we ship over WAN.
  * Backpressure semantics. Today each consumer has a bounded queue of 4096
    lines: a slow consumer loses its oldest events, gets a `warning` line with
    the `dropped` count, and is disconnected after 10 s of blocked sends. It
    never stalls the drain. Long-term: a bounded ring per channel server-side.
  * Schema registry — should `schema` names resolve to a versioned JSON
    Schema file? Probably overkill for v1. The names are stable
    identifiers; consumer-side validation is consumer's choice.
  * Authentication — none in v1 (single-consumer, LAN scope). v2 may add
    a token handshake when WAN exposure becomes a real use case.
