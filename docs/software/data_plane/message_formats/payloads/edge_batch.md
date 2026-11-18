# EDGE_BATCH Payload — Bulk Edge Events in One zs_msg Frame

**Status:** **frozen** (2026-05-19). Layered on top of the
v1 zs_msg header defined in
[`../common_header.md`](../common_header.md).

This payload exists because edge events are the **worst case** for the
20-byte zs_msg header overhead. A pure edge detector emits one frame
per signal transition — 8 bytes of information (timestamp, channel,
direction) wrapped in a 20-byte header gives ~83% framing overhead.
At burst rates (>100 K edges/s) this saturates DDR bandwidth and the
host drain on framing bytes alone.

`EDGE_BATCH` exploits the v1 wire format's variable `payload_len` to
amortise the header across **N edge events per frame**, at the cost of
a small batching FSM in the framer path. At N=32 the per-event cost
drops from 24 bytes to ~4.6 bytes — roughly a 5× efficiency win — with
no protocol-version bump.

## 1. Frame Shape

```
┌────────────────────────────────────────────────────────────┐
│  zs_msg_header_t  (20 bytes)                               │
│    msg_class    = DATA  (0x00)                             │
│    msg_type     = SEV_INFO | DOMAIN_EDGE | EVT_EDGE_BATCH  │
│                 = 0x0601                                   │
│    timestamp    = 64-bit clk_sys-tick anchor (see §3)      │
│    payload_len  = N × 4   (N = events in this batch)       │
├────────────────────────────────────────────────────────────┤
│  edge_event_t[0]   (4 bytes)                               │
│  edge_event_t[1]   (4 bytes)                               │
│  ...                                                       │
│  edge_event_t[N-1] (4 bytes)                               │
└────────────────────────────────────────────────────────────┘
```

`N` is determined at flush time by the framer (see §4). The PoC bound
is `1 ≤ N ≤ 32`; nothing in the wire format constrains N other than
`payload_len` fitting in 16 bits (max 16384 events per frame).

## 2. `edge_event_t` Layout

4 bytes, little-endian throughout.

| Offset | Size | Field      | Notes |
| :---:  | :--: | :--------- | :---- |
|  0     | 2    | `ts_delta` | Clock-cycle count from the header's `timestamp` (LE u16). |
|  2     | 1    | `channel`  | Per-instance device_id (5b zero-extended to 8b; matches `datalogger_id` in the header for the multiplex case). |
|  3     | 1    | `flags`    | `[7:3]` reserved=0; `[2]` S; `[1]` F; `[0]` E. See §3.3. |

### C struct

```c
typedef struct __attribute__((packed)) {
    uint16_t ts_delta;   // cycles after batch anchor (LE)
    uint8_t  channel;    // device_id, zero-extended to 8b
    uint8_t  flags;      // [2]=S, [1]=F, [0]=E
} edge_event_t;
```

### Python `struct` format

```python
EDGE_EVENT_STRUCT = struct.Struct("<HBB")  # 4 bytes
```

## 3. Per-Field Semantics

### 3.1 Batch anchor timestamp

The header's 64-bit `timestamp` field anchors the batch. It is captured
by the framer at the moment the **first** event of a batch arrives at
the FSM, in `clk_sys` ticks (10 ns at 100 MHz). Subsequent events in
the same batch carry only the cycle-count delta from this anchor.

Global correlation across batches is preserved because the anchor is a
full 64-bit `clk_sys`-tick value — the same time base used elsewhere
in zs_msg framing. The 64-bit width gives ~5800 years of monotonic
range.

### 3.2 `ts_delta`

Unsigned 16-bit clock-cycle count from the batch anchor. Reconstructed
absolute timestamp:

```
event_ts_64b = header.timestamp + edge_event.ts_delta
```

Range: 65535 cycles = ~655 µs at 100 MHz. Sized to comfortably cover
even pathological dense bursts (32 edges across 655 µs = avg 50 kHz
edge rate per channel; real bursts are typically faster and tighter).
**Overflow of `ts_delta` is a flush condition** — the framer never
emits an entry whose delta would wrap (see §4).

### 3.3 `flags`

Per-event status nibble — same semantics as the legacy 32-bit
`edge_event` message's S/F/E bits (defined in
[`edge_detector.yaml`](edge_detector.yaml)):

| Bit | Name | Meaning |
| :-: | :--- | :------ |
| `[0]` E | Edge polarity / level | When S=0 (edge): **inverted** current input level (1 → falling, 0 → rising). When S=1 (read): direct sample (1 → high, 0 → low). |
| `[1]` F | FIFO pressure | 0 = capacity OK; 1 = framer side-FIFO almost full. SW uses this as a "drop risk soon" hint. |
| `[2]` S | Send / ReadEvent | 0 = edge-triggered event. 1 = software-triggered readback (changes E semantics). An S=1 entry is always a readback: lifecycle markers travel as `EDGE_LIFECYCLE` (0x0608) frames, and an edge that coincides with a read is an S=0 entry. |
| `[7:3]` | reserved | MUST be 0 in v1. Forward-compat space. |

### 3.4 `channel`

Per-instance device_id, 5 bits zero-extended to 8. Identical to the
header's `datalogger_id` field when the batch comes from a single
datalogger. **All events in one batch SHOULD share a `channel`** — this
keeps the consumer-side per-channel demux trivial. A future v2 could
mix channels in one batch by relaxing this, at the cost of one extra
byte per entry.

In v1: the framer MUST emit one batch per (`channel`) before allowing
events from a different channel to enter the FSM. The consumer MUST
treat all entries' `channel` as advisory and reject the batch if any
entry's `channel` mismatches the header's `datalogger_id`.

## 4. Batch Close Conditions

The framer closes a batch (i.e. emits one zs_msg with the accumulated
payload and starts a new one) on any of:

1. **Capacity** — `N` events queued. PoC bound `N_MAX = 32`; a
   build-time generic widens this if needed. Capacity flush is the
   common steady-state path.
2. **Delta overflow** — the next event's `ts_delta` would exceed
   `0xFFFF`. The current batch flushes; the overflow-causing event
   becomes the first event of a fresh batch with a new anchor.
3. **Timer** — no event seen for `T_FLUSH_CYCLES` consecutive cycles
   while a partial batch is open. PoC default `T_FLUSH_CYCLES = 10_000`
   (100 µs at 100 MHz); also generic-controlled. Prevents stale
   single-event "ghost" batches from sitting in the FSM indefinitely.
4. **Channel switch** — an event for a different `channel` arrives
   while a batch is open. The open batch flushes; the new event
   becomes the first of a fresh batch.
5. **Reset / shutdown** — soft reset MUST flush any open batch
   (best-effort) so the consumer never sees a "phantom" timestamp
   gap. Hard reset MAY drop the batch and the consumer recovers via
   the zs_msg resync semantics in
   [`../common_header.md` §5](../common_header.md#5-resync-overrun-semantics).

The framer MUST NOT emit a zs_msg with `payload_len == 0`. A batch
with no events is dropped, not framed.

## 5. Reference Decode (Python sketch)

```python
def decode_edge_batch(header: ZsHeader, payload: bytes) -> list[EdgeEvent]:
    """One zs_msg with msg_type=(SEV_INFO|DOMAIN_EDGE|EVT_EDGE_BATCH)
    → N reconstructed EdgeEvent records with absolute timestamps."""
    assert header.event_id == EVT_EDGE_BATCH
    assert header.payload_len % EDGE_EVENT_SIZE == 0
    n = header.payload_len // EDGE_EVENT_SIZE
    events = []
    for i in range(n):
        ts_delta, channel, flags = EDGE_EVENT_STRUCT.unpack_from(
            payload, i * EDGE_EVENT_SIZE)
        events.append(EdgeEvent(
            ts_64b=header.timestamp + ts_delta,
            channel=channel,
            s=bool(flags & 0x04),
            f=bool(flags & 0x02),
            e=bool(flags & 0x01),
        ))
    return events
```

The canonical implementation lives at `sw/stream/zs_msg.py` —
`EdgeEvent` dataclass, `EDGE_EVENT_STRUCT`, `pack_edge_batch`,
`parse_edge_batch`. Regression tests live at
`sw/stream/tests/test_zs_msg.py`. (Not linked here because the
mkdocs strict build won't resolve targets outside the docs tree.)

## 6. Header Overhead — Worked Numbers

|  Mode                          | Bytes per event | Overhead | Notes |
| :----------------------------- | :-------------: | :------: | :---- |
| Per-event (v1, no batching)    |       24        |    83%   | 20B header + 4B payload (1 × edge_event). |
| `EDGE_BATCH` N=1               |       24        |    83%   | Same — degenerate batch matches per-event cost (no penalty for fallback). |
| `EDGE_BATCH` N=8               |       6.5       |    38%   | 20 + 32 = 52 bytes per frame. |
| `EDGE_BATCH` N=32              |       4.6       |    14%   | 20 + 128 = 148 bytes per frame — the PoC target. |
| `EDGE_BATCH` N=256             |       4.08      |    1.9%  | Hypothetical upper bound — gated by FSM SRAM, not the wire format. |

At UART/I2C/SPI protocol rates (10K frames/s) the overhead is
irrelevant. At QSPI burst rates and noisy edge channels (>100 K
frames/s on a Zybo) the N=32 win — ~5× — moves the host drain
ceiling from ~3 MB/s of useful data per channel to ~17 MB/s, well
inside USB 2.0 + gigabit Ethernet headroom.

## 7. Implementation Status

| Phase | Item | Status |
| :---- | :--- | :----- |
| A     | This spec (`edge_batch.md`)                                    | shipped 2026-05-19 |
| A     | `EVT_EDGE_BATCH` allocation in `event_and_datalogger_id_spec.md`  | shipped 2026-05-19 |
| A     | `sw/stream/zs_msg.py` — `EdgeEvent` + `pack_edge_batch` + `parse_edge_batch` | shipped 2026-05-19 |
| A     | Regression tests — pack/parse round-trip, N=1, N=32, malformed payloads | shipped 2026-05-19 |
| B     | VHDL batching FSM in the framer path                           | open  (Phase B) |
| B     | cocotb test of the FSM (capacity / delta-overflow / timer flushes) | open |
| C     | End-to-end overhead measurement on a stub burst source         | open |

## 8. References

* [`edge_detector.yaml`](edge_detector.yaml) — legacy per-event datalogger-level word format (input to the batching FSM).
* [`../event_and_datalogger_id_spec.md`](../event_and_datalogger_id_spec.md) — Severity / Domain / EventID enums (`EVT_EDGE_BATCH = 0x01`).
