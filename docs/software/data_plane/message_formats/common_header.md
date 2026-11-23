# zs_msg Common Header — Wire Format v1

**Status:** **frozen** (2026-05-19). This document is the
single source of truth for the 20-byte `zs_msg_header_t` that the PL
prepends to every protocol event it streams into the PS DDR ring.

Anything that disagrees with this page — including the comment block
at the top of `hw/rtl/deepskopion_top.vhd`, the constants in
`hw/rtl/pkg/zs_msg_pkg.vhd`, or the parser in `sw/stream/zs_msg.py` —
is a drift bug; do not silently patch one
mirror without the others.

The format binds RouteWave host-side ingest and is consumed
today by the in-tree PS daemon via `sw/stream/zs_msg.py`.

## 1. Binary Layout

20 bytes, 32-bit aligned, little-endian throughout.

| Offset | Size | Endian | Field          | Notes |
| :---:  | :--: | :----: | :------------- | :---- |
|  0     | 2    | LE     | `magic`        | `0x5A53` (`'Z'`, `'S'`). Resync anchor. |
|  2     | 1    | —      | `version`      | `0x01` today (v1). |
|  3     | 1    | —      | `header_len`   | Header length in 32-bit words. `0x05` (= 5 × 4 = 20 bytes). |
|  4     | 1    | —      | `datalogger_id`   | Per-protocol device_id, 5b zero-extended to 8b (see §3). |
|  5     | 1    | —      | `msg_class`    | `DATA`=0, `STATUS`=1, `TRIGGER`=2. |
|  6     | 2    | LE     | `msg_type`     | `Severity(15:12) ǀ Domain(11:8) ǀ EventID(7:0)` (see §3). |
|  8     | 8    | LE     | `timestamp`    | 64-bit free-running `clk_sys`-tick counter (see §3). |
| 16     | 2    | LE     | `payload_len`  | Bytes of payload following the header. |
| 18     | 2    | LE     | `flags`        | 16-bit reserved bitmask (see §3, currently all zero). |

**Total header size: 20 bytes.** Payload immediately follows. Today's
PoC framer carries one 32-bit payload word per frame
(`G_PAYLOAD_WORDS=1`), so the on-wire frame is 24 bytes; widening
`G_PAYLOAD_WORDS` keeps the header shape and only `payload_len` changes.

### C struct

```c
typedef struct __attribute__((packed)) {
    uint16_t magic;        // 0x5A53 ('Z','S')
    uint8_t  version;      // 0x01
    uint8_t  header_len;   // 5 (in 32-bit words)
    uint8_t  datalogger_id;   // per-protocol device_id, zero-ext to 8b
    uint8_t  msg_class;    // DATA=0, STATUS=1, TRIGGER=2
    uint16_t msg_type;     // Sev(15:12)|Domain(11:8)|EventID(7:0)
    uint64_t timestamp;    // 64-bit clk_sys-tick counter
    uint16_t payload_len;  // bytes of payload after this header
    uint16_t flags;        // reserved (0)
} zs_msg_header_t;
```

### Python `struct` format

```python
HEADER_STRUCT = struct.Struct("<HBBBBHQHH")  # 20 bytes
ZS_MAGIC      = 0x5A53
```

## 2. PL Constants (mirror)

The VHDL package `tich.zs_msg_pkg` exports the wire constants the
framer emits. Any change here must land in three files atomically
(see §6).

| Constant                  | Value     | Field      |
| :------------------------ | :-------- | :--------- |
| `C_ZS_MAGIC`              | `X"5A53"` | `magic`    |
| `C_ZS_VERSION`            | `X"01"`   | `version`  |
| `C_ZS_HEADER_LEN_WORDS`   | `X"05"`   | `header_len` |
| `C_ZS_NUM_HDR_WORDS`      | `5`       | (FSM size) |
| `C_ZS_CLASS_DATA`         | `X"00"`   | `msg_class` |
| `C_ZS_CLASS_STATUS`       | `X"01"`   | `msg_class` |
| `C_ZS_CLASS_TRIGGER`      | `X"02"`   | `msg_class` |

Severity / Domain enums (4b each, used to assemble `msg_type`) are
documented in [`event_and_datalogger_id_spec.md`](event_and_datalogger_id_spec.md).
The helper `make_msg_type(sev, dom, evt_id) → std_logic_vector(15:0)`
returns `sev & dom & evt_id`.

## 3. Per-field Semantics

### `magic` — resync anchor

The only synchronisation primitive in the wire format. Stored as the
little-endian uint16 view of bytes `'Z'`(`0x5A`), `'S'`(`0x53`); in
memory the bytes are `[0x53, 0x5A]`, and
`struct.unpack('<H', ...)` decodes to `0x5A53`. No per-frame
alignment marker beyond this. See §5 for the resync algorithm.

### `version` — protocol version

`0x01` today. Bump on any layout-breaking change. The parser MUST
reject (or downgrade-handle) unknown versions; today it does not
gate on this field, so future updates that change layout MUST also
update both parsers.

### `header_len` — header words

Header length in **32-bit words**. `0x05` (= 20 bytes) today. Lets
the parser advance past unknown future trailing-header fields without
having to know v2/v3 layout. Computed by the parser as
`header_len * 4` to get the byte advance to payload.

### `datalogger_id` — per-instance identifier

Per-protocol 5-bit `device_id` zero-extended to 8 bits, sourced from
`payload[31:27]` at the `datalogger_gather` block (landed
2026-05-17). The 5-bit width comes from the gather block's per-channel
ID field; the zero-extension to a full byte gives room for future
expansion without an on-wire layout change.

Frames now land on per-protocol RouteWave channels (`uart_byte`,
`i2c_frame`, …) rather than the generic `pl_heartbeat` lane. The
build-time mapping from instance name (e.g. `UART0`) to `datalogger_id`
is generated from the project YAML — see
[`event_and_datalogger_id_spec.md`](event_and_datalogger_id_spec.md) §3.

### `msg_class` — coarse routing

Three values:

| Value  | Name      | Meaning |
| :----: | :-------- | :------ |
| `0x00` | `DATA`    | Protocol payload — UART byte, I2C frame, SPI word, … |
| `0x01` | `STATUS`  | Datalogger status / counter snapshot. |
| `0x02` | `TRIGGER` | Trigger-engine event (armed, matched, …). |

Coarse enough for top-level SW dispatch without decoding `msg_type`.

### `msg_type` — fine-grained type

16-bit field: `Severity(15:12) | Domain(11:8) | EventID(7:0)`.

For v1 PoC frames:
```
msg_type = (SEV_INFO << 12) | (DOMAIN_<PROTO> << 8) | payload[26:24]
```

Severity is `INFO` for every frame in v1; per-event `ERROR` severity
(UART parity, I2C NACK, …) is a follow-on. Full enum tables for
Severity, Domain, and per-Domain EventID values are in
[`event_and_datalogger_id_spec.md`](event_and_datalogger_id_spec.md).

### `timestamp` — global time

Full 64-bit free-running counter clocked by `clk_sys` (landed
2026-05-17). Wraparound at 2^64 / 100 MHz ≈ 5800 years —
treat as never-wrapping. Resolution is one `clk_sys` tick (10 ns at
100 MHz).

The per-protocol datalogger chain still consumes a 32-bit slice via the
same counter's low 32 bits; the framer header carries the full 64.
Documented by the [Global Timing Model](../global_timing_model.md).

### `payload_len` — payload byte count

Bytes following the header. The PoC always emits 4 (one 32-bit
datalogger word). The consumer MUST advance its read cursor by
`header_len * 4 + payload_len` to reach the next header.

### `flags` — reserved bitmask

16-bit field, **reserved 0 in v1**. Reserved bits for future use:

| Bits   | Meaning (future)                |
| :----- | :------------------------------ |
| `[0]`  | `OVERFLOW`  — PL FIFO dropped sample upstream of framer. |
| `[1]`  | `TRUNCATED` — payload was clipped to fit ring window.    |
| `[2]`  | `RESYNC`    — preceded by a torn frame (set by SW; here for documentation). |
| `[15:3]` | reserved.                     |

PL never sets these today. SW must not assert on them being zero
(forward-compatibility), but should surface unknown bits to the
operator if they ever appear.

## 4. SW Locator — DDR Ring Discovery

The PS reads the regbank slots below to find the ring and walk it, and writes
`RING_READ_PTR` to drain it. Slot addresses are stable per the regbank package
(`hw/rtl/pkg/system_regbank_pkg.vhd`); the byte offsets below are the AXI-side
addresses in `project_regbank.yml` (the `Regs` mirror is `sw/stream/regbank.py:422-429`).

| Slot              | AXI offset | Width | Access | Meaning |
| :---------------- | :--------- | :---- | :----- | :------ |
| `RING_WRITE_PTR`  | `0x0150`   | 32b   | RO     | Total 32-bit words accepted by the DDR stream since reset (free-running, wraps at 2^32). |
| `RING_READ_PTR`   | `0x0154`   | 32b   | RW     | Ring drain pointer: the byte offset up to which software has consumed the ring (same units as `RING_COMMIT_PTR`). The PL frees ring space against it. |
| `RING_BASE_LOW`   | `0x0158`   | 32b   | RO     | Physical base address of the ring in PS DDR, low 32 bits. Default `0x10000000`. |
| `RING_BASE_HIGH`  | `0x015C`   | 32b   | RO     | High 32 bits of the physical base. Always `0` on Zynq-7. |
| `RING_BYTES`      | `0x0160`   | 32b   | RO     | Ring size in bytes. Default `0x10000000` (256 MB = 2^28). |
| `RING_COMMIT_PTR` | `0x0164`   | 32b   | RO     | The DMA's committed DDR write address: where the ring has actually been written, not where the enqueue counter `RING_WRITE_PTR` has reached (`project_regbank.yml:428-432`). Reads 0 until the first write. |

(The regbank lives at AXI base `0x40000000` in the default PoC
`project.yml`; absolute addresses are `0x40000150` etc.)

### Walk recipe

The walk is driven by `RING_COMMIT_PTR`, as `DdrRingDataSource.next_batch` does it
(`sw/stream/data_source.py:1525-1552`):

```
commit       = RING_COMMIT_PTR mod RING_BYTES        # byte offset of the write head
bytes_unread = (commit - read_ofs) mod RING_BYTES    # read_ofs: the consumer's byte offset
# read min(bytes_unread, span limit) bytes from base + read_ofs, wrapping at
# base + RING_BYTES; walk frames; advance read_ofs by each frame's total size
# (header_len * 4 + payload_len) and by consumed pad; write read_ofs to RING_READ_PTR.
```

`RING_COMMIT_PTR mod RING_BYTES` equals the offset `RING_COMMIT_PTR - RING_BASE_LOW` when the base
is a multiple of `RING_BYTES`, as the default base `0x10000000` is for the default size `0x10000000`.

`RING_WRITE_PTR` is not a walk input. It counts 32-bit words accepted by the DDR stream; four times that count
differs from the committed byte offset because frames have variable length and the DMA commits
whole DDR words, so a cursor seeded from it lands mid-frame (`sw/stream/data_source.py:1343-1349`).
The daemon reads it for telemetry only (`sw/stream/data_source.py:1526`).

One tick copies at most `_MAX_SPAN_BYTES_PER_TICK` (2 MiB) of the ring and stops decoding at a frame
boundary after `_TICK_BUDGET_S` (0.25 s) (`sw/stream/data_source.py:1275`, `:1291`); the rest is read
on the next tick.

`RING_READ_PTR` is the ring drain pointer: `ddr_stream_axi`
gates its writes on FULL against it, so a ring whose pointer is not
advanced fills once and then backpressures every producer.
`DdrRingDataSource` (`sw/stream/data_source.py`) writes it:

* at the seed tick: the first tick sets the read head to
  `RING_COMMIT_PTR` without parsing the back-history and writes that
  value, logging one line `ring seed: RING_READ_PTR <old> -> <seed>,
  <n> bytes of back-history skipped`. Without this write a ring that is
  full when the daemon starts receives no further commit, no tick
  drains, and the pointer is never written;
* at the end of every draining tick, with the consumed byte offset.

## 5. Resync & Overrun Semantics

Real hardware drops frames — PS daemon stalls, ring wrap during a
slow consumer, partial mmap reads. The wire format gives the
consumer two recovery primitives:

### Resync (torn frame, lost framing)

There is **no per-frame alignment marker** beyond `magic`. On any of:

* short read at offset (less than 20 bytes available),
* `magic` mismatch at the cursor,
* `version` unsupported,
* `header_len * 4 + payload_len` advance overshoots the ring,

the parser MUST scan forward byte-by-byte for the next `0x5A53` LE
sequence, advance to it, and retry. The 32-bit-alignment hint is
soft: the framer always writes on 4-byte boundaries, but the consumer
should not rely on it during resync (a torn write can land mid-word).

The `magic` chosen (`0x5A53`) is a high-bit-set, asymmetric pattern
unlikely to collide with all-zero or all-one payload bytes; false
positives during resync are possible but always self-correct on the
next failed header validation.

### Zero-pad words

The PL writes the ring in DDR words of `G_DDR_DATA_WIDTH` bits (Zybo 128, DE25 256, PolarFire SoC 64), each holding
several 32-bit messages. When the producer goes idle with a word only partly filled, `rr_ddr_stream`'s
`ddr_fifo_intf` completes the word with zero messages after 128 idle cycles, so it commits. A consumer that reads
the ring directly sees those zeros.

The daemon (`DdrRingDataSource._pad_run`, `sw/stream/data_source.py`) consumes them as pad, not as a resync:

* A pad is a run of zero bytes from a position that is not on a DDR-word boundary up to the next boundary.
* It is taken only right after a parsed frame, because the PL completes the word after its last real message.
* It is taken before the 20-byte header check, so an idle ring drains to the commit pointer.
* A zero run cut by the end of the read span is left for the next read.

Zero bytes that start on a boundary, stop before it, or follow a resync slide are resynced and reported as
`zs_resync`, as before. A whole zero DDR word on a boundary is therefore reported as `zs_resync`; its known source
is a producer write on the flush-trigger cycle, present in `rr_ddr_stream` releases v0.2.0 to v0.3.7 and fixed
at `rr_ddr_stream` commit 19a383c8, after v0.3.7.

The word width is declared per target as the kernel argument `ds_ring_word_bytes=` (the top's
`G_DDR_DATA_WIDTH` / 8: Zybo 16, DE25 32, PolarFire SoC 8). `S99deepskopion-stream` passes it as
`--ring-word-bytes`. Without it the daemon keeps the old walk, which reports every pad as `zs_resync`, and logs that
once.

The native EDGE walk (`sw/stream/native`) stops at a zero word that directly follows a frame it walked and hands
it back to the daemon's walk, which applies the pad rule there. Both walks therefore give the same records and the
same `zs_resync` count.

### Ring full and producer loss

The ring cannot be overrun. `ddr_stream_axi` frees ring space only up to `RING_READ_PTR`, which
the regbank feeds to its `csr_rd_ptr_addr_i` (`project_regbank.yml:408`); when the ring is full
the DMA write side stops and the producers are backpressured. Unread bytes are never overwritten,
so a large commit advance in one tick means the consumer was slow, not that data was lost. The
daemon does not skip ahead or snap its read offset to the commit
(`sw/stream/data_source.py:1562-1571`).

What is reported instead:

| Quantity | Where |
|---|---|
| Ring fill, `(RING_COMMIT_PTR - RING_READ_PTR) mod RING_BYTES` | STATS `ring_used_bytes` (`sw/stream/data_source.py:693`, `:701`) |
| Fill alarm: bits [30:0] count excursions at or above 75 %, bit 31 is the live alarm | `RING_ALARM_CNT` `0x016C`, STATS `ring_alarm` (`sw/stream/regbank.py:429`, `sw/stream/data_source.py:703`) |
| Producer loss: ring-full backpressure cycles | `DDR_BACKPRESSURE_CNT` `0x0168` (`sw/stream/regbank.py:428`), polled every tick by `LossProvenanceWatcher` (`sw/stream/data_source.py:1157`) |

`RING_READ_PTR` reads 0 after reset, so a ring that no consumer drains fills once and then holds
every producer (`project_regbank.yml:408`).

## 6. Mirror Artefacts — Drift Surface

Three places must change in lockstep when extending the wire format.
List is exhaustive; if you find a fourth, add it here in the same PR.

| Artefact                                        | Role |
| :---------------------------------------------- | :--- |
| `hw/rtl/pkg/zs_msg_pkg.vhd`                     | PL-side constants (`C_ZS_MAGIC`, `C_ZS_VERSION`, `C_ZS_HEADER_LEN_WORDS`, severity/domain enums, `make_msg_type`). |
| `sw/stream/zs_msg.py`                           | PS-side parser (`HEADER_STRUCT`, `ZS_MAGIC`, `parse_header`, `pack_header`). |
| `hw/rtl/deepskopion_top.vhd` (header block)     | Project-level overview comment block — must mirror §1 layout. |
| `docs/software/data_plane/message_formats/common_header.md` | **This document — single source of truth.** |

When extending:

1. Bump `version` (and `C_ZS_VERSION`) — never silently change
   semantics under v1.
2. Update all four artefacts in the same commit.
3. Add a regression test under `sw/stream/tests/` that packs+parses
   a frame at the new version and asserts byte-exact wire bytes.
4. Record an architectural decision citing the new layout and pointing at this doc.

## 7. References

* [`event_and_datalogger_id_spec.md`](event_and_datalogger_id_spec.md) — Severity / Domain / EventID enums.
* [`overview.md`](overview.md) — Message-format big picture.
* [`../global_timing_model.md`](../global_timing_model.md) — Timestamp clock domain & resolution.
