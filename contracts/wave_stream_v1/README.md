# `wave_stream_v1` — the producer/consumer stream contract

**Contract revision:** 2.8.2. **Wire version:** `wave_stream_v1`.

> **2.8.2 — the UART event vocabulary gains `enabled` and `disabled`.** The UART
> data logger's lifecycle frames (STATUS codes 0x05 and 0x06) are decoded as
> `uart_enabled` and `uart_disabled`. Before this revision they carried the
> codes of `start_error` and `calibration_done` and were decoded as those.
> Vocabulary addition only: frames, fixtures and verdicts are unchanged, and a
> 2.8.1 consumer renders the two kinds generically (`COMPAT["unknown_kind"]`).

> **2.8.1 — the edge-lane check reads edge words only when it can fire.** The
> 2.8.0 rule concerns a HELO channel with width > 0 at an id of 31 or less, so
> the checker notes those ids at HELO and scans `EDGE_EVENTS` words only when
> there is one. Verdicts and fixtures are unchanged. On a Cortex-A9 the 2.8.0
> per-word scan added 824 ns per edge to the producer's live guard.

> **2.8.0 — a HELO channel names the edge lane of its id.** Each `EDGE_EVENTS`
> word carries a 5-bit `device_id` in bits [31:27]. A consumer names the lane of
> a device_id after the HELO channel whose `id` equals it, and shows `edge_<id>`
> when there is none. That entry describes a lane whose data arrives as
> `EDGE_EVENTS`, so its `width` is 0. A channel with `width` > 0 at an id that
> the session's `EDGE_EVENTS` words carry is now a rejection: a SAMPLES lane at
> that id either lends the edge lane its name or leaves it unnamed (a daemon with
> `level_pin[1]`/`[2]` at ids 1/2 showed its I2C edge lanes as `edge_1`/`edge_2`).
> A producer that streams edges keeps its SAMPLES and annotation channels above
> id 31. `contract.json` carries the rule as `edge_lane_ids`. A session without
> `EDGE_EVENTS` words is not constrained, and the bytes of every 2.7.0 fixture
> are unchanged. A 2.7.0 consumer reads a 2.8.0 stream unchanged.
> `check_session(frames, revision=...)` and `check_stream(..., revision=...)`
> judge a recorded session by the rules of the revision it was recorded under;
> the default is the current revision. The edge-lane rule applies from 2.8.0
> (`EDGE_LANE_RULE_SINCE`), so committed evidence recorded earlier stays
> conformant under its own revision.

> **2.7.0 — an `EDGE_EVENTS` frame bounds how far its words lie from its
> anchor.** A word's `ts_us` is the low 16 bits of a µs count, and a consumer
> places it in the 65.536 ms window nearest `anchor_ns`. That window is the
> right one only while the word is within half a window of the anchor, and
> until 2.7.0 nothing said so: `anchor_ns` was the time of the frame's first
> edge, and the producer put a whole poll period of edges (50 ms by default,
> more after a stall or a backlog drain) behind one anchor. Every word of a
> frame now lies STRICTLY within `EDGE_ANCHOR_REACH_US` (32 768 µs) of
> `anchor_ns // 1000`, in whole µs. A producer with a longer edge span sends
> several `EDGE_EVENTS` frames, each with its own anchor. `contract.json`
> carries the bound as `edge_anchor_reach_us`. The checker cannot see a
> violation on the wire (the word decodes to a wrong time, not to an invalid
> one), so the bound binds the producer. No shape change: a 2.6.0 consumer
> reads a 2.7.0 stream unchanged and places every edge correctly.

> **2.6.0 — `edge_maxfreq` is a registered annotation kind.** The edge
> datalogger reports the fastest activity it saw on a line as an in-band
> `EVT_EDGE_MAXFREQ` frame, and the daemon decodes it to an `edge_maxfreq`
> annotation with `{channel, min_interval_ticks, freq_hz}`. The kind was not
> registered, so a renamed key would have reached the consumer as an empty
> column with nothing raised, and its two time-like keys had no unit row (the
> unit walk only reaches registered kinds). It is now registered: `channel` and
> `min_interval_ticks` are uint, and `freq_hz` is `num?` — `null` is the
> all-ones "no edge yet / re-armed" sentinel, which tells a consumer to clear
> the maximum it shows. `TIME_UNITS` gains both rows, and `session_golden`
> carries one measured and one sentinel annotation. Additive: a 2.5.0 consumer
> renders the kind generically (COMPAT `unknown_kind`).

> **2.5.0 — an annotation names its event and carries two resolutions of its
> label.** These are the two fields the HWTX transcript reads from an annotation.
> `event_id` was already an optional string. It is now checked: within a
> session one id names one event, as `(channel_id, ts_start, ts_end, kind)`. The
> same event may be sent again under its id, because a late joiner's keyframe
> resends the last annotation of each lane. A second, different event under the
> same id is a rejection, because a verdict that points at that id could then
> mean either event. The new optional `text_res` is `{long, short}`. `short` is
> the inline label and is at most `TEXT_SHORT_MAX` (32) characters. `long` reads
> on its own outside the timeline. `text` stays the required string it was. A
> 2.4.x consumer ignores both keys and reads a 2.5.0 stream unchanged. A 2.4.x
> checker accepts the 2.5.0 golden session; it does not see the new rejections.

> **2.4.2 — the unit walk reaches map shapes.** The shapes of
> `DIGEST.verdict`, `DIGEST.verdict.anomalies[]`, `DIGEST.protocols.<name>`,
> `STATS.proto_stats.<name>` and `STATS.ring_alarm` were literals inside the
> validators, so a time-like key added to one of them carried no unit and passed
> the gate. They are now module constants that the validators and the walk both
> read, with the same keys. Every map-typed field declares how its keys are
> shaped (`_map_shapes()`), and the gate fails on a map field with no
> declaration. No time-like key is in any of them today. No wire change.

> **2.4.1 — the unit walk reaches list items.** 2.4.0 walked only the keys of
> top-level shapes, so `eye_summary.curve[].eye_width_ps` had no unit. Every
> list-typed field now declares its item shape (`eye_summary.curve` as
> `EYE_CURVE_POINT_SPEC`), the walk descends into it, and the gate fails on a
> list field with no declared item shape. The curve item shape is not yet
> checked on received payloads. No wire change.

> **2.4.0 — every time on the wire states its unit.** RouteWave keeps time in
> ps internally, while every time on this wire is whole ns, and no part of the
> contract said which was which. `TIME_UNITS` names the unit of each time-like
> key (`ts`, `ts_*`, `*_ns`, `*_ps`, `*_us`, `*_ms`, `*_s`, `*_ticks`, `*_hz`,
> `*_mhz`), and `contract.json` carries it as `time_units`. The gate fails on a
> time-like key with no row, and on a row that names no key. SAMPLES,
> ANNOTATIONS and DIGEST times are whole ns since HELO, so two samples closer
> than 1 ns share a `ts`. EDGE_EVENTS place edges in ticks of the edge clock.
> The wire does not change: a 2.3.0 consumer reads a 2.4.0 stream unchanged.
> The shape of `DIGEST.recent` is now the module constant `DIGEST_RECENT_SPEC`,
> with the same keys.

> **2.3.0 — `STATS.proto_stats` pins the vocabulary, not a build.**
> Up to 2.2.0 the key set had to EQUAL one fixed tuple. That tuple is a
> per-build selection (`datalogger_config.yml`), so when the CAN data
> logger became opt-in, deepskopion hand-edited its copy to drop `can` — no
> revision bump, no `emit` — while RouteWave kept `can`. Both copies said 2.2.0
> and each rejected the other's honest stream: RW-WS1 rejected the live CAN-less
> daemon (`field=proto_stats`), and WS-C0 rejected the committed DE25 captures of
> a CAN build. From 2.3.0 a key must be a **registered** protocol
> (`REGISTERED_PROTOCOLS`: `can, edge, i2c, jtag, spi, uart`), the map must be
> non-empty, and the key set must not change within a session. A retired name
> (`i2s`) is still a rejection; "a live logger is missing" is a producer fact,
> gated by each producer against its own build. `contract.json` renames
> `active_protocols` → `registered_protocols`. **Every vendored copy must
> re-vendor this revision**; a consumer still enforcing the 2.2.0 equality
> rejects any build whose selection differs from its hard-coded one.


This directory pins the seam between a **producer** (the deepskopion capture
daemon, the routertl REA bridge, the mock) and a **consumer** (RouteWave).

## Why it exists

The seam was implemented twice and pinned nowhere. deepskopion encodes the
protocol in `sw/stream/protocol.py`; RouteWave decodes it in
`logic-trace/include/logic/wave_stream/protocol.hpp`; the normative prose lives
in a third repo (`routertl/docs/protocol/wave_stream_v1.md`); and the only thing
that checked the three against each other was a test in a super-repo that ships
in none of the products.

Two sides can each be internally consistent and still disagree — and when they
do, nothing raises:

- **A tag collision.** deepskopion shipped `SYNC` on tag 7 while RouteWave had
  frozen tag 7 as `EDGE_EVENTS`. One side's "catch-up finished" marker decoded
  as the other side's edge batch. `SYNC` was renumbered to 9 (D.14).
- **A silent enum fallback.** A channel descriptor with an unregistered `radix`
  is not rejected by the consumer; it falls back to hexadecimal. A producer
  asking for decimal gets hex and is never told. (Found by this contract on
  landing — deepskopion's eye and stats lanes.)
- **A renamed field.** `ERR` carried its human explanation under `detail` while
  the consumer read `message`, so the one frame whose entire job is to say *why*
  a session died arrived empty. (Also found by this contract on landing.)

None of those is a crash. Each is a product that looks like it is working.

## Who owns it

**Neither product.** The normative prose spec is routertl's. This directory is
its *executable* form, and it is **vendored** — the identical directory is
copied into every repo that implements the protocol, identified by
`contract_revision` plus the digests in `manifest.json`. "Are we on the same
contract?" is then a digest comparison, not a reading of two headers.

A repo **must not** edit its local copy to make its own implementation pass.
A change goes into the spec, then into this directory, then re-vendors.
Correspondingly, this directory imports nothing from deepskopion; deepskopion
declares itself *subject* to it in
`sw/stream/tests/test_wave_stream_contract_conformance.py`.

## What is in here

| Path | What it is |
|---|---|
| `wave_stream_contract.py` | the rules and the checker; the only executable |
| `contract.json` | the same rules as language-neutral data (generated) |
| `fixtures/*.json` | the fixtures — **the source of truth**, readable and reviewable |
| `fixtures/*.p2c.mp` | the exact bytes a **consumer** reads off the socket (generated) |
| `fixtures/*.c2p.mp` | the exact bytes a **producer** reads off the socket (generated) |
| `manifest.json` | every fixture: digest, expected verdict, and the field a rejection must name (generated) |
| `tests/` | the contract's own gate |

Fixtures are written as JSON and **compiled** to MessagePack. Committing only
the bytes would make them unreviewable; committing only the JSON would make them
useless to a C++ consumer; committing both invites drift — so
`wave_stream_contract.py emit --check` regenerates and requires byte equality,
and the test suite runs it.

## How each side consumes it

**A consumer (no Python).** Read `manifest.json`, feed each `streams.p2c.file`
to your parser, and require the recorded `expect`. `session_golden` must parse
completely, including the frame carrying an unknown tag; every `reject_*` must
be refused. `contract.json` carries the tag registry, the per-tag payload
shapes, the role-key tables and the compatibility policy as data.

**A producer.** Same, with `streams.c2p.file` for the inbound direction. A
Python producer can import `wave_stream_contract` and call
`check_session(frames)` on its own builders' output — that is what deepskopion's
conformance test does.

```
python3 wave_stream_contract.py verify      # every fixture, verdict AND reason
python3 wave_stream_contract.py check <session.json | stream.mp>
python3 wave_stream_contract.py emit --check
```

## The compatibility policy

Stated once, here, and enforced by the checker. It is authoritative over the
prose spec, which currently says two contradictory things: its forward-
compatibility note says an unknown tag MUST be skipped, while its versioning
section still says adding a tag is breaking and an old consumer MUST answer
`ERR{FRAME_DECODE}` and close. That ambiguity is not academic — "adding a tag is
breaking" makes renumbering look no worse than appending, which is the soil the
D.14 collision grew in.

| Situation | What the receiver MUST do |
|---|---|
| **Unknown frame tag** | Skip the frame, keep the session alive. Never `ERR`, never close. A tag you do not know is a peer speaking a newer revision, not a corrupt stream. |
| **Publishing a tag** | The registry is **append-only**. A published tag number is frozen to its meaning forever — never reused, never renumbered. Adding one is not a breaking change; renumbering one is, and it is the change no consumer can detect. |
| **Unknown map key** | Ignore it. Optional keys are additive. The required-key set in `contract.json` is the whole of what a receiver may rely on. |
| **Unknown enum member** (channel `type` / `role` / `radix`) | Fall back to the documented default (`Signal` / `Wire` / `Hexadecimal`). Because this is survivable *and silent*, an unregistered member is a violation on the **producer** side — the checker names it there. |
| **Unknown annotation `kind`** | Render it generically. `kind` is a free-form producer string. A kind **registered** in the contract must carry its registered role keys; those keys are the cross-repo field contract. |
| **Unknown body schema** (`DIGEST.schema`) | Skip that frame only. A body versions independently of its wire tag. |
| **`HELO.ver` not implemented** | **Fatal — the only fatal incompatibility.** Answer `ERR {code: "VERSION_MISMATCH"}` and close. Guessing at an unknown wire version is how a green session stops meaning what it says. |

## What the golden fixture covers

`session_golden` is one conformant session carrying everything the end-to-end
self-test path depends on:

- the `HELO`/`HELO_OK` handshake and its channel-descriptor negotiation;
- `SAMPLES` with monotonic timestamps, and the `SYNC` catch-up boundary;
- decoded annotations for **every protocol the recorded session carries** (it was
  recorded on the Zybo CAN FD bench target) —
  CAN (including a stuff error), UART (including a 9-bit char and a status
  error), I2C (ACK *and* NACK), SPI (MOSI/MISO pairing), JTAG (IR shift, TAP
  state) — each with its role-tagged `payload`;
- `EDGE_EVENTS` carrying both a packed batch and legacy words, with the absolute
  `anchor_ns` that makes them placeable in time at all, and the instance
  `datalogger_id`;
- loss counters in **both** provenance flavours (precise in-band, and a marker
  synthesized from an out-of-band counter delta) plus an `error_run` storm
  collapse;
- the `edge_maxfreq` max-frequency datum of a captured line, both measured and
  as the re-armed `null` sentinel;
- a `STATS` frame with the full counter set, including the `null`s that mean
  "this protocol has no regbank counter slot" rather than a fabricated zero;
- a `DIGEST` with a `CRITICAL` verdict;
- the `CONTROL` / `CONTROL_ACK` back-channel and a `RATE_HINT`;
- annotations carrying `event_id` and `text_res` (one long label clipped to its
  32-character `short`), one annotation without either key, and one event sent
  a second time under its own `event_id`, as a keyframe resends it;
- an **unknown tag**, which must be skipped without ending the session;
- a graceful `EOF`.

`session_datalogger_subset` is the second accepted session: a build that does
**not** carry every registered data logger — the CAN-less `proto_stats` the live
daemon streams since CAN became opt-in. It is what keeps the contract from pinning one
build's selection again.

`session_edge_lane_names` is the third: width-0 HELO entries at ids 1 and 2,
named `i2c_scl` and `i2c_sda`, beside `EDGE_EVENTS` words of device_ids 1 and 2,
with the SAMPLES channels at ids 40 to 45.

## What the rejections cover

One fixture per defect class, each recording the field the checker must name —
a rejection that fires for an incidental reason stops guarding its defect the
moment the incidental reason changes.

| Fixture | The defect |
|---|---|
| `reject_tag_body_collision` | tag 9 carrying an `EDGE_EVENTS` body — **D.14, reproduced** |
| `reject_stats_stale_proto_set` | `proto_stats` with a retired `i2s` and no `can`/`jtag` — **reproduced** |
| `reject_stats_proto_set_changes` | `proto_stats` loses `jtag` between two STATS of one session — a row vanishing under the viewer |
| `reject_edge_anchor_missing` | edge words with no `anchor_ns` beside absolute annotation time — **reproduced** |
| `reject_edge_lane_on_sample_channel` | a SAMPLES lane (`level_pin[2]`, width 1) at the id of an edge device_id — **reproduced** |
| `reject_unregistered_channel_enum` | `type: "Annotation"` / `radix: "Decimal"` — the silent-fallback drift |
| `reject_payload_role_key` | `i2c_byte` keyed `value` instead of `data` — the emptied packet column |
| `reject_version_mismatch` | a `HELO.ver` this side does not implement |
| `reject_no_helo` | data before the handshake |
| `reject_channel_total_bits` | the slice width disagrees with the channel widths |
| `reject_duplicate_channel_id` | two buses merged into one lane |
| `reject_annotation_channel_ref` | an annotation anchored to a channel that was never declared |
| `reject_annotation_ts_inverted` | `ts_end` before `ts_start` |
| `reject_samples_ts_regress` | non-monotonic sample timestamps |
| `reject_seq_regress` | non-monotonic `seq` — the drop detector turned into noise |
| `reject_loss_unknown_stage` | a loss outside the provenance taxonomy |
| `reject_event_id_names_two_events` | one `event_id` on two different annotations — a verdict pointing at it is ambiguous |
| `reject_text_res_short_too_long` | a `text_res.short` over the 32-character inline label |
| `reject_text_res_missing_long` | a `text_res` with no `long` resolution |
| `reject_missing_annotation_text` | an event with no label to draw |
| `reject_wrong_direction` | `STATS` arriving from the consumer |
| `reject_frame_after_eof` | a producer still sending after the close |
| `reject_not_a_frame` | a one-element array on the wire |
| `reject_truncated_frame` | the tail a dropped connection leaves in the buffer |

## Scope

This contract judges the **wire**. It does not judge whether a capture is
*correct* — that a CAN frame on the bus produced the right bytes is the golden
path's evidence contract (GP-C0). Producer conformance of
a live end-to-end session is **DS-WS1**; viewer conformance is
**RW-WS1**. Both depend on this.
