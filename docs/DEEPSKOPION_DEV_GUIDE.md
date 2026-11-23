# Deepskopion — Developer's Guide

> *An extensible FPGA datalogger for proprietary protocols.* This guide is the
> map a new developer needs to understand the whole machine — from a wire
> wiggling on an EMIO pin to a decoded packet arriving in RouteWave. It is
> written to **teach**, not just to reference: every section explains *why*
> the piece exists, not only *what* it does.
>
> Status: **silicon-validated** (2026-06-02). On a Zybo Z7-20 (xc7z020), bit
> `0x6F48C22D`, all dataloggers (UART / I2C / SPI individually, UART+I2C paired,
> and all three together) frame correctly into the DDR ring with correct
> identity, zero drops, and zero overflow.

---

## 0. The one-paragraph version

Deepskopion turns an FPGA into a **protocol logic analyzer**. Tiny RTL
"dataloggers" watch real bus pins (UART, I2C, SPI today; the architecture is
*extensible* to anything), decode bus activity into self-describing **`zs_msg`
frames**, merge them through an arbiter, wrap them in a portable wire format,
and DMA them into a **circular buffer in DDR**. On the PS (the ARM cores
running Linux), a Python **daemon** drains that ring, decodes the frames into
human-meaningful annotations, and streams them over TCP (`wave_stream_v1`) to
**RouteWave**, the waveform/packet viewer. The whole thing is built, simulated,
and deployed through the **RouteRTL (`rr`) flow** — no vendor TCL.

---

## 1. The big picture — the capture pipeline

```
        PL (gateware, clk_sys / clk_ddr)                    PS (ARM, Linux)
 ┌────────────────────────────────────────────────┐   ┌──────────────────────┐
 │  EMIO pins ──► EMIO mux ──► per-protocol         │   │  deepskopion daemon  │
 │   (UART/I2C/SPI bus)        dataloggers             │   │  (sw/stream)         │
 │                              │  zs_msg events    │   │                      │
 │                              ▼                   │   │  ring_oracle ◄─ mmap │
 │                        datalogger_gather  ◄─ burst  │   │     │  (DDR ring)    │
 │                          (arbiter)     arbitration│   │     ▼                │
 │                              │                    │   │  zs_msg parse        │
 │                              ▼                    │   │     ▼                │
 │                        zs_msg_framer              │   │  decoders            │
 │                          (24-byte ZS frames)      │   │     ▼                │
 │                              │  S_AXIS            │   │  data_source         │
 │                              ▼   (back-pressured) │   │     ▼                │
 │                        ddr_stream_axi  ───────────┼───┼─► DDR ring (HP port) │
 │                          (DMA, ring pointers)     │   │     ▲ RING_READ_PTR  │
 │                              ▲                    │   │     │  (SW drain)    │
 │                        regbank (AXI-Lite GP0) ◄───┼───┼──► server :7475 ────►│ RouteWave
 └────────────────────────────────────────────────┘   └──────────────────────┘
```

Two clock domains live in the gateware: **`clk_sys`** (the datalogger/regbank
side) and **`clk_ddr`** (the DMA/DDR side). Everything that crosses between
them is a deliberate, constrained CDC — remember that when you read the RTL.

---

## 2. Part I — The Gateware (PL)

### 2.1 EMIO + the mux

The PS exposes UART/I2C/SPI controllers to the PL fabric via **EMIO**. On real
silicon the **EMIO mux** routes *one* protocol's bus to the datalogger fabric at a
time (`set_emio_mux(uart=…/i2c=…/spi=…)`). This is the single most important
HW-vs-sim difference to internalize:

- **In simulation** each protocol has its own dedicated pins, so the testbench
  drives all three *simultaneously*.
- **On silicon** the mux time-multiplexes the bus, so "two dataloggers at once"
  really means *both dataloggers enabled, traffic alternated*. The on-board health
  tool (`ds_datalogger_health.py`) models exactly this.

### 2.2 The per-protocol dataloggers

Each datalogger (`hw/rtl/datalogger/{uart,i2c,qspi,...}`) is a small FSM that watches
its bus and emits a **`zs_msg`** on every meaningful event (a decoded byte, a
START/STOP, a framing error). Device IDs are fixed: **I2C = 4, SPI = 8,
UART = 12**. The datalogger is *gated* by an enable bit (see `config_control`)
and, for UART, by an **auto-baud calibration** that must lock first (drive a
≥150-byte burst before expecting clean frames).

> **Educational aside — why a calibration burst?** UART has no clock; the
> receiver must *infer* the bit period from edge timing. The datalogger measures
> inter-edge gaps over a random burst to lock the baud, then frames cleanly.
> Skip the burst and you get garbage or silence — a classic "the bus works but
> nothing frames" trap (a classic bring-up symptom).

### 2.3 `config_control` — the enable latch

`hw/rtl/datalogger/infra/config_control/config_control.vhd` is the per-datalogger
enable. There is **one instance per datalogger**, each matching its own device ID
out of the shared 16-bit `DATALOGGER_CONFIG` broadcast word
(`[15:11]=device_id`, `[10:0]=config`, bit0=enable). Key subtlety (line 54):
*the config bus is not guaranteed to return to 0*, so each instance **latches**
its config when it sees its own ID and a change event, and **holds it** until
the same ID is written again. Consequence: you enable N dataloggers with N
sequential `DATALOGGER_CONFIG` writes, and the *register read-back only shows the
last write* — that is not "only the last one is enabled." (Misreading this
sent one investigation down a wrong path; don't repeat it.)

### 2.4 `datalogger_gather` — the burst arbiter

`hw/rtl/datalogger/infra/datalogger_gather/datalogger_gather.vhd` merges the per-protocol
`zs_msg` streams into one. Each protocol feeds a small per-input FIFO; a
**`tich.arbiter`** round-robins them onto a single `DOUT_BURST_*` output with a
`DOUT_BURST_VALID` / `REQ_BURST` (valid/ready) handshake. It exposes
`FIFO_BURST_CSR` (level + **`overflowCnt`**, saturating): if a datalogger out-runs
its input FIFO, the lost word is *counted* (visible to the PS), never silent.

> **Educational aside — valid/ready and held valid.** `DOUT_BURST_VALID` is a
> FIFO-arbiter output: it **stays high until `REQ_BURST`**. If you count
> *valid-high cycles* you over-count one frame as many; the true frame count is
> *valid AND ready* **handshakes**. A common pitfall in a
> test monitor is reporting a "drop" that wasn't. When you instrument a valid/ready
> interface, **count handshakes.**

### 2.5 `zs_msg_framer` — the ZS wire format

`hw/rtl/datalogger/infra/zs_msg_framer/zs_msg_framer.vhd` serializes each gathered
event into a fixed **24-byte `zs_msg` frame** (magic `0x5A53` = "ZS" = *Zynq
Datalogger*). One word per cycle in `S_EMIT`, **gated by `OUT_READY`** — the FSM
holds when the sink can't accept, so the framer is fully back-pressure-safe.
`OUT_READY` is wired to `ddr_stream_axi`'s real `s_axis_ready_o`, so genuine
front-end-FIFO back-pressure propagates all the way up
to the gather. (Why a wire format at all? See §3.)

### 2.6 `ddr_stream_axi` — the DDR ring DMA

`rr-ddr-stream`'s `ddr_stream_axi` (fetched by `rr pkg install` into
`libs/routertl/rr_ddr_stream/` at the commit `ip.lock` pins) is the DMA engine. It takes the 32-bit framer
word stream on `S_AXIS`, packs it into 128-bit AXI4 bursts, and writes them to
a **circular buffer in DDR** over an HP port. The ring is `2^buffer_width_g`
bytes (256 MB on this board). Two facts to burn in:

1. **The ring wraps at `2^buffer_width_g`, not `2^ddr_addr_width_g`.** Getting
   this wrong lets `AWADDR` escape the reserved window on silicon and
   scribble kernel RAM → kernel panic, while sim (which set the two generics
   equal) stayed green. The fix wraps + masks at the *buffer* width.
2. **The ring is a single-producer/single-consumer (SPSC) queue.** The PL is
   the producer (write pointer); the PS is the consumer. With no consumer the
   ring fills once and stalls. The drain is §4.

### 2.7 The regbank — the PS↔PL contract

`hw/rtl/regbank/` (generated from `project_regbank.yml` by
`tools/regen_regbank.sh`) is the AXI-Lite register file at **`0x4000_0000`**
(M_AXI_GP0). It is the *entire* contract surface between the daemon and the
gateware. The slots you will actually use (offsets):

| Reg | Off | Meaning |
|---|---|---|
| `GIT_HASH` | `0x000` | **bitstream provenance** — git short-hash baked into a ROM. Read it back to prove what's running. |
| `BUILD_DATE` | `0x008` | build timestamp |
| `CONTRACT_ABI_VER` | `0x028` | shared RTL↔SW ABI version (catches *breaking* drift) |
| `DATALOGGER_CONFIG` | `0x020` | per-datalogger enable broadcast (§2.3) |
| `FIFO_OVERFLOW_CNT` | `0x0C4` | gather input-FIFO drops (saturating) |
| `FIFO_OVERFLOW_CNT_RSVD` | `0x0C8` | framer-side FIFO drops |
| `DROP_CNT_{I2C,SPI,UART}` | `0x120/124/128` | per-domain ring drops |
| `RING_READ_PTR` | `0x154` | **SW read pointer** (the drain, §4) |
| `RING_BASE_LOW/HIGH` | `0x158/15C` | ring base address in DDR |
| `RING_BYTES` | `0x160` | ring size |
| `RING_COMMIT_PTR` | `0x164` | **where the DMA has actually committed** — walk the ring up to *this*, never the raw write counter |

---

## 3. Part II — The wire formats

### 3.1 `zs_msg` — the in-ring frame (24 bytes)

Every frame in the DDR ring is a little-endian `zs_msg` (see `sw/stream/zs_msg.py`
and `hw/rtl/pkg/zs_msg_pkg.vhd`):

```
 w0: magic(LE 0x5A53) | version=1 | header_len=5
 w1: datalogger_id | msg_class | msg_type
 w2: timestamp[31:0]
 w3: timestamp[63:32]
 w4: payload_len | flags
 w5: payload (the raw 32-bit datalogger message)
```

`msg_type` is bit-packed: **`[15:12]=severity | [11:8]=domain | [7:0]=event_id`**.
Domains: `UART=0x1, I2C=0x2, SPI=0x3, QSPI=0x5`. So a frame's protocol identity
is `(datalogger_id, domain_of(msg_type))` — and a healthy capture has every frame
carrying the *expected* pair for the protocol that produced it (the "ID health"
the soak tool checks).

> **Why a self-describing frame and not raw samples?** A raw 32-bit word with a
> bare timestamp is ambiguous (which protocol? which event? severity?) and the
> 32-bit timestamp wraps in ~43 s at 100 MHz — useless for long captures. The
> ZS frame carries a 64-bit timestamp and full identity, so the PS can decode
> and order frames captured hours apart with no out-of-band state.

### 3.2 `wave_stream_v1` — the egress protocol

The daemon streams to RouteWave over TCP using `wave_stream_v1`
(`sw/stream/protocol.py`): msgpack `[tag, payload]` frames. Tags:
`HELO=0, HELO_OK=1, SAMPLES=2, RATE_HINT=3, EOF=4, ERR=5, ANNOTATIONS=6,
EDGE_EVENTS=7, CONTROL=8, SYNC=9, CONTROL_ACK=10` (the canonical spec lives in
`routertl/docs/protocol/wave_stream_v1.md`; `protocol.py` mirrors it). The
server sends a **HELO** (channel manifest + clock), waits for the consumer's
**HELO_OK**, then streams `SAMPLES`/`ANNOTATIONS`/`EDGE_EVENTS` until EOF or
disconnect, with a one-shot **SYNC** marking the catch-up→live boundary for a
late joiner. Deepskopion is *annotation-only* for decoded protocols, but emits
**EDGE_EVENTS** (tag 7) for raw Edge Detector edges → RouteWave sigrok decode.
`SYNC` was renumbered 7→9 (D.14) to free tag 7 for `EDGE_EVENTS`.

A consumer sends `CONTROL` (tag 8) and the daemon answers each with one
`CONTROL_ACK` (tag 10). Every `CONTROL` carries `op`, `datalogger_id` and
`cfg_word`.

| `op` | Builder (`protocol.py`) | Effect | ACK extra keys |
|---|---|---|---|
| `set_config` | `make_control` | `cfg_word` written to data logger `datalogger_id` | none |
| `stim` | `make_stim_control(protocol)` | on-board stimulus armed for `uart`, `i2c`, `spi` or `edge`; EMIO_MUX_SEL is rewritten whole | `op`, `protocol`; for `uart`/`i2c`/`spi` also `ps_emio_taken`, and `warning` when it is true |
| `edge_route` | `make_edge_route_control(route)` | EMIO_MUX_SEL bits [7:3] set to `i2c` (bit 4), `spi` (3), `uart` (5), `can` (6), `stim` (7) or `pmod` (none); bits [2:0] and [11:8] kept; the edge data loggers of the route enabled | `op`, `route`, `emio_mux_sel`, `edge_channels` |

`stim i2c` followed by `edge_route i2c` gives EMIO_MUX_SEL 0x11: the stimulus
I2C bus on EDGE0 (SCL, device 1) and EDGE1 (SDA, device 2). `edge_channels`
holds the HELO edge-lane entries for the new routing, since the HELO a consumer
holds names the lanes as routed at connect. `sw/stream/testbench/live_capture_client.py`
sends both after `SYNC` with `--stim i2c --edge-route i2c`.

On a Zynq-7000 image the top sets G_STIM_DRIVES_EMIO, so while a
protocol's STIM_*_CONTROL bit 0 is set the stimulus generator drives that
protocol's PS EMIO legs in place of the PS controller, and PS-driven traffic on
that protocol does not reach the bus. A stim arm sets the bit and leaves it set.
No register reports the generic; `sw/stream/stim_emio.py` infers it from the
device-tree root compatible `xlnx,zynq-7000`. The ACK then carries
`ps_emio_taken: true` and a `warning` text naming the inference, and the daemon
logs the warning. On other platforms `ps_emio_taken` is false. Optional keys;
consumers ignore unknown keys.

| Path | Behaviour while STIM_I2C_CONTROL bit 0 is set on a Zynq-7000 image |
|---|---|
| `emio_inject.I2CMaster(bus, regbank=...)` | each transfer is refused with RuntimeError before any byte is sent |
| `emio_inject.I2CMaster(bus)` (no regbank) | no check |
| `SelfTestBench` | passes its regbank to I2CMaster and clears the STIM_*_CONTROL registers on open (`RegbankExt.release_stimulus_generator`) |

#### UART autoSync calibration failure

A UART datalogger armed with autoSync (DATALOGGER_CONFIG config[1]) whose
calibration cannot complete emits one STATUS 0x01 CALIBRATION_TIMEOUT, decoded
as `uart_calibration_timeout` (`is_error` true). The stimulus generator forces
both failure paths. STIM_UART_CONTROL bit 1 selects the break pattern: each
STIM_UART_DATA write then sends DATA[15:0] breaks, each the line low for
CONTROL[15:8] bit-times and then high for one.

| Case | Armer call / writes | UART annotations after the arm |
|---|---|---|
| Buffer timeout | autoSync arm; STIM_UART_CONTROL 0x1; one STIM_UART_DATA byte; silence | uart_disabled, uart_enabled, uart_calibration_timeout |
| No format fits | `stimulus_armer.arm_uart_breaks(rb, low_bits=13, count=80)` | uart_disabled, uart_enabled, uart_calibration_timeout |
| Control (a valid 5N1 frame) | `stimulus_armer.arm_uart_breaks(rb, low_bits=6, count=80)` | uart_disabled, uart_enabled, uart_config 0x54, 80 × uart_byte 0x00 |

With 13 or more low bit-times every candidate stop bit samples low, so no format
fits. 80 breaks exceed the 70-byte calibration buffer. No STATUS
calibration_done is emitted; a completed calibration shows as the CONFIG message
and decoded bytes. All three rows were measured on the Zybo-Z7-20 at 1 Mbaud.
`arm_uart_breaks` drives the Zybo and DE25
stimulus generator only, not the PolarFire MMUART1 path.

---

## 4. Part III — The Daemon (PS software) — the heart of the PS side

The daemon lives in **`sw/stream/`** (a self-contained Python package — only
stdlib + `msgpack`; `regbank` talks to the PL via `/dev/mem`). It is staged into
the rootfs at `/usr/local/share/deepskopion/sw/stream` and launched by
`/etc/init.d/S99deepskopion-stream`, serving `wave_stream_v1` on **`:7475`**.

### 4.1 Package map

| File | Role |
|---|---|
| `regbank.py` | PS↔PL register access. `MmioRegbank` (/dev/mem + mmap) + a fake backend for host tests. `Regs` = the offset map. |
| `ddr_mmap.py` | `RealDdrMmap` — mmaps the DDR ring region for direct PS reads. |
| `zs_msg.py` | The `zs_msg` parser: `parse_header`, `domain_of`/`severity_of`/`event_id_of`, the domain enum. |
| `decoders.py` | Per-protocol decoders: turn a `zs_msg` event into human text (e.g. a UART byte → `"0x41 'A'"`). |
| `data_source.py` | `DdrRingDataSource` — the bridge: reads the ring, parses frames, emits **annotations** on declared channels. Owns the pointer drain (writes `RING_READ_PTR`) and the overrun guard. |
| `channels.py` | The channel manifest sent in HELO (sample channels + annotation channels per protocol). |
| `edge_route.py` | `CONTROL op=edge_route`: EMIO_MUX_SEL edge-input routing (bits [7:3]) and the edge data logger enables. |
| `protocol.py` | `wave_stream_v1` wire codec (`pack_frame`, `make_helo`, tags). |
| `server.py` | The socket server: bind (AF_INET/AF_UNIX), accept one consumer, HELO/HELO_OK handshake, drain the data source into frames. |
| `__main__.py` | Daemon entry point (wires a `DdrRingDataSource` to `serve_one`). |
| `testbench/` | On-target test kit (below). |

### 4.2 `ring_oracle.py` — reading the ring correctly

`RingOracle` is the canonical way to read frames out of the ring. Three methods
matter, and the order is the lesson:

- **`snapshot()`** — latch the current `commit_offset()` as the read cursor.
- **`collect_new_frames(min_count, timeout_s, max_frames)`** — return frames
  committed *since the snapshot*. Re-call without re-snapshotting and you
  **re-read** the same frames (an easy way to wildly over-count — snapshot each
  round).
- **`advance_read(consumed_off)`** — write `RING_READ_PTR = consumed_off`. This
  is the **SW drain**: it tells the PL "I own everything up to here,
  free the space." You already have the data (you read it via mmap); this only
  moves a pointer — no double-read.

Always walk the ring up to **`commit_offset()`** (`RING_COMMIT_PTR`), the
DMA's *committed* write address — never a raw enqueue counter, which races
ahead of what's actually in DDR.

### 4.3 `data_source.py` — the annotation bridge

`DdrRingDataSource` is what the server drains. Each batch: it reads new frames
(via the oracle), decodes each into an **annotation** (`ts_start`, `ts_end`,
`channel_id`, `kind`, `text`, `color`, `is_error`) on the channel for its
domain, advances `RING_READ_PTR`, and tracks an **overrun guard** (warns when
the PS falls too far behind the PL producer). It also anchors PL hardware
timestamps to wall-clock so RouteWave gets monotonic times.

On the wire (`server._annotations_payload`) each annotation also carries the
two fields the HWTX transcript reads:

| Field | Value |
|---|---|
| `event_id` | `a<seq>_<i>`: the seq of the frame the annotation first went out in, and its index in that frame. Stamped once on the `Annotation`, so a keyframe resend keeps it. |
| `text_res` | `{long, short}`. `short` is `text`, clipped to 32 characters with a trailing `…`. `long` is `<kind>: <text>`, never clipped. |

### 4.4 The testbench kit (`sw/stream/testbench/`)

This is how you *drive* and *verify* the board without external instruments:

- **`emio_inject.py`** — `SelfTestBench`: drives UART/I2C/SPI over EMIO, sets
  the mux, loads injector responses, and `enable_datalogger(dev_id)`. `RegbankExt`
  wraps the raw regbank with the high-level helpers (`ring_locator()`,
  `set_emio_mux()`, `enable_datalogger()`; raw reads via `.raw.read_u32`).
- **`ring_oracle.py`** — the `RingOracle` above.
- **`run_self_test.py`** — the one-shot contract self-test (REQ-tagged).
- **`scenarios.py`** — the proven drive recipes (UART calibration burst, I2C
  injector load, SPI patterns).
- **`provenance.py`** — deploy-stamp / ABI-version helpers (`read_deploy_stamp`,
  `CONTRACT_ABI_VER`, RTC-trustworthiness for verdict ordering).
- **`ds_datalogger_health.py`** — the **sustained datalogger-health soak** used to
  validate multi-protocol captures on silicon. Modes `uart|i2c|spi|uart_i2c|all`; reports ring
  health + ID health + a PASS/FAIL verdict.

```sh
# on the board, after a fresh deploy + daemon stopped:
python3 /usr/local/share/deepskopion/sw/stream/testbench/ds_datalogger_health.py all 2
```

---

## 5. Part IV — The DDR ring drain

The ring is an SPSC queue with two pointers in `ptr_mgr`: a **write** pointer
(producer) and a **read** pointer (consumer). The gateware can be built with
either plane elided:

- **Write-only integration** (deepskopion's mode: `enable_read_plane_g=false`).
  No HW downloader, so the *read* pointer would be pinned to 0 and the ring
  fills once and wedges FULL. Deepskopion provides `csr_rd_ptr` so the PS advances
  the read pointer in software (`RING_READ_PTR`) after consuming via mmap —
  re-instating the SPSC drain. This is exactly what `advance_read()` drives.
- **Read-only integration** (`enable_write_plane_g=false`). The symmetric case:
  `csr_wr_ptr` allows a PS producer to advance the write pointer
  in software, giving the HW downloader something to drain.

Both pointers cross `clk_sys → clk_ddr` with a coherent **multi-bit MCP CDC**
(toggle + `sync_cdc` + edge-detect capture) — because the pointer moves *during*
traffic, a naive free-running 2-FF could latch an incoherent old/new mix and
momentarily report extra free space (→ overwrite un-drained data). The
crossing is constrained by `ddr_stream_cdc_scoped.xdc`.

> **What "ring health" means in practice:** `RING_COMMIT_PTR` advances as the
> PL writes; `RING_READ_PTR` should track it as the PS drains; the overflow and
> `DROP_CNT_*` counters stay at 0. (Note: a 256 MB ring won't *wrap* in a
> minutes-long slow soak — the drain is still exercised by read tracking
> commit; force a wrap with a much longer or faster run.)

---

## 6. Part V — Provenance & deploy (read this before you bring up a board)

### 6.1 The bitstream identity loop

On a Zynq-7 **already booted into Linux**, a JTAG reconfig (`rr program run`)
prints "Programming successful" but the PS keeps running the **old** PL design
(PCAP owns configuration). The *only* reliable redeploy is:

```
rr bitstream run            # build the .bit (use --force to defeat synth cache!)
rr linux sdcard             # bootgen → BOOT.bin embedding the new .bit
python3 tools/check_zybo_console.py --target zybo_datalogger   # console = USB UART 0xE0001000 in every stage
# TFTP BOOT.bin → SD FAT → reboot → VERIFY:
devmem 0x40000000 32        # GIT_HASH must equal the new build's short hash
```

The skill `zynq-bitstream-redeploy-tftp` codifies the full TFTP dance. **Always
verify the stamp flipped** — a cached synthesis (`Synthesis duration 0.0s`) will
re-impl the *old* netlist and the stamp won't change; `rr bitstream run --force`
invalidates the cache so the new stamp is actually synthesized in.

### 6.2 The provenance gap you must respect

The bitstream carries a readable identity. **The PS software does not** — and it
rides in the **rootfs**, a *different partition* that a bitstream-only redeploy
**never touches**. So the board can silently run a weeks-old daemon while you
keep redeploying bits. (For example, a stale `sw/stream` lacking the
drain helper.) To ensure SW provenance:

```sh
# refresh the on-board SW live (rootfs is writable) — no full rootfs rebuild:
tar czf <tftp-root>/dssw.tgz -C <deepskopion> sw/stream     # host
# board: tftp dssw.tgz; tar xzf /tmp/dssw.tgz -C /usr/local/share/deepskopion
```

**Rule of thumb:** after deploying a new bit, ask "is the board's `sw/stream`
also current?" There is (today) no automatic check.

### 6.3 Zybo-Z7-20 network identity (MAC address, U-Boot networking)

Facts verified against deepskopion `2ddba4ec` and U-Boot `a87a5c15`
(`xilinx_zynq_virt_defconfig`, lwIP network stack) on 2026-11-11.

| Fact | Value |
|---|---|
| MAC with no environment file on the SD FAT | random on every boot: U-Boot prints `using random MAC address - <mac>` and Linux eth0 inherits it |
| Effect on the host | the host's neighbour entry still names the previous boot's MAC; the first connection after a reboot waits for re-resolution (Appendix B) |
| Carrier for a fixed MAC | a complete U-Boot environment in `uboot.env` and `uboot-redund.env` on the SD FAT (`CONFIG_ENV_IS_IN_FAT`, `CONFIG_ENV_REDUNDANT`) |
| Measured result | 3 cold boots: `Loading Environment from FAT... OK`, no random-MAC line, `ethaddr` and Linux eth0 equal the file's value, the host sees that MAC from U-Boot and from Linux; with both files removed the random MAC returns |
| Environment size | `CONFIG_ENV_SIZE=0x20000`; U-Boot reports `Environment size: N/131067 bytes` (0x20000 minus the 5-byte redundant header) |
| `CONFIG_ENV_OVERWRITE` | not set: once `ethaddr` is set from the loaded environment, a later `setenv ethaddr` (for example in `boot.scr`) does not change it |

The file replaces the default environment completely, so it must hold every
default variable. `board_late_init` adds `modeboot` and `boot_targets` only when
the default environment is in use; a partial file therefore boots without them.
The file is composed from the `env print -a` output of a boot with no
environment file, with only `ethaddr` replaced. A captured `env print -a` is
complete when the text between its echoed command line and the
`Environment size: N/...` line is exactly N - 1 bytes long (one NUL per
variable plus a final NUL in U-Boot's count).

The MAC value is per board. It is kept with the other deployment values outside
the repository (`runtimes/hw.local.yml`), never in a tracked file. Until the
factory MAC can be read, the value is a locally administered address (first
byte `02`), different for each board on a shared segment.

Manual procedure (Zybo-Z7-20, original SD image):

| Step | Action | Expected |
|---|---|---|
| 1 | Boot with no `uboot.env` on the card; stop autoboot; capture `env print -a` | the size check above holds |
| 2 | Replace the `ethaddr=` line with the per-board value; nothing else changes | same variable count |
| 3 | `mkenvimage -r -s 0x20000 -o uboot.env env.txt`, and the same for `uboot-redund.env` | two 131072-byte files; CRC32 of bytes 5..end equals the first 4 bytes |
| 4 | From the Linux shell: mount the SD FAT read-write, copy both files, `sync`, unmount | the listing adds exactly the two files; BOOT.bin, uImage and system.dtb md5 unchanged |
| 5 | Cold boot | `Loading Environment from FAT... OK`; no `using random MAC address` line; `printenv ethaddr` and `/sys/class/net/eth0/address` show the value |
| Rollback | From Linux: delete both files. At `Zynq>`: `fatrm mmc 0:1 uboot.env`, `fatrm mmc 0:1 uboot-redund.env`, `reset` | the next boot prints `using random MAC address` again |

U-Boot networking:

| Fact | Value |
|---|---|
| `ipaddr`, `netmask`, `serverip` in the default environment | absent |
| U-Boot `ping` with no `ipaddr` | no frame leaves the board (0 frames from the board on a host capture, 4 of 4 boots); prints `host ... is not alive` |
| U-Boot `ping` after `setenv ipaddr <board-ip>` and `setenv netmask <mask>` | answered at the first attempt, 4 of 4 boots |
| First network use after a cold boot | may print `Waiting for PHY auto negotiation to complete... TIMEOUT !`; a second attempt completes |

U-Boot network commands (`ping`, `tftpboot`) therefore need `ipaddr` (and
`serverip` for TFTP) set first. The SD boot path (`boot.scr`, default
`bootsrc=sd`) uses no network in U-Boot.

Items to confirm:

- The factory MAC. The board manual places it in the QSPI flash OTP region,
  bytes 0x20-0x25 (OUI `00:18:3e`). On the current image U-Boot reports
  `Flash: 0 Bytes` and `sf probe 0` fails with `Invalid chip select 0:0 (err=-19)`,
  and Linux lists no MTD device, so it is not read yet.
- Linux: a host ping within a few seconds of `Link is Up` was unanswered in some
  boots (0/3) while later traffic in the same boots was answered. Cause not
  determined.

### 6.4 Restoring the Zybo SD card on a host

A Linux desktop host (udisks) mounts both partitions of the Zybo SD card as soon
as the card is inserted: the FAT boot partition (p1) and the ext4 rootfs
partition (p2). The ext4 mount is read-write, so the kernel replays the p2
journal and rewrites its superblock (mount count, mount time). p2 changes even
when nothing on it is touched.

Measured on 2026-11-15: a restore that replaced only `BOOT.bin` on p1 changed the
p2 sha256 from `81e494ca…` to `bd4c48c9…`. The host journal showed
`EXT4-fs (sda2): recovery complete`, then `mounted filesystem … r/w`, for the
two minutes the card was in the reader.

| Step | Action | Expected |
|---|---|---|
| 1 | Card into the host reader | p1 and p2 appear as `/dev/sdX1`, `/dev/sdX2` |
| 2 | p2 unmounted at once: `udisksctl unmount -b /dev/sdX2` | `/proc/mounts` lists no `sdX2` |
| 3 | p1 updated (copy, `sync`), then `udisksctl unmount -b /dev/sdX1` | no `sdX` entry in `/proc/mounts` |
| 4 | If a p2 checksum is part of a recorded baseline: p2 hashed again on the board (`sha256sum /dev/mmcblk0p2`, read-only) and the new value recorded before any test relies on it | value recorded |

A p2 checksum recorded before a host restore is not valid after it, unless the
host log shows that p2 was never mounted.

The board's USB UART (the Digilent FT2232 `if01`) enumerates only when the board
has power. A console reader opened before a cold power-on therefore starts
about 3 s after power is applied, and the FSBL banner of that boot is not in its
log. A reboot from Linux (`reboot -f`) with the port already open shows the
banner, when the FSBL prints one.

---

## 7. Part VI — Build, simulate, bring up

Everything goes through `rr` (pure-RouteRTL flow — never vendor TCL):

```sh
rr bitstream run [--force]          # synth + impl + bitstream
rr linux sdcard                     # BOOT.bin (+ sdcard tree)
python3 tools/check_zybo_console.py # Zybo only: refuse a BOOT.bin whose console is not the USB UART
rr sim run test_datalogger_to_ddr      # the full-path adversarial sim (5 tests)
```

The Zybo console check reads the built `system.dtb` and `uboot_control.dtb`
and refuses unless U-Boot's `stdout-path` and the kernel's `console=ttyPS<N>`
and `earlycon` address all resolve to the USB UART at 0xE0001000. The serial
alias order comes from the device-tree generator, so a correct-looking
`console=` can point at the EMIO UART0 at 0xE0000000.

`hw/sim/cocotb/tests/datalogger/infra/full_data_path/test_datalogger_to_ddr.py` is the
golden integration test: it drives real UART/I2C/SPI through the *entire* pipe
into a cocotbext AxiRam DDR sink and asserts hard-coded domains/datalogger_ids/
payload bytes. Five sub-tests: each protocol alone, all-three combined, and the
UART+I2C pair. **Simulate the exact sequence you'll run on silicon** — that
de-risks issues before touching hardware.

### 7.1 Targets and the `ctu_can_fd_rtl` library

`hw/rtl/deepskopion_core.vhd` (line 16) references `library ctu_can_fd_rtl` on every target.

| Target yml carries the package `ctu-fee/ctucanfd_ip_core` | `sources.libraries.ctu_can_fd_rtl` |
|---|---|
| yes (the CTU CAN FD bench targets, not distributed) | not set; the package supplies the library |
| no (every other target that compiles `deepskopion_core`) | `[tools/sim/vendor_stubs/ctu_can_fd/can_top_apb_stub.vhd]` |

A target compiles `deepskopion_core` when its top reaches it in the `hw/` entity graph or it declares
simulation tags. Enforced by
`tools/tests/test_de25_core_baseline_contract.py::test_ctu_library_is_the_stub_exactly_when_a_core_target_lacks_the_ctu_package`.

The silicon bring-up sequence (reproducible with `ds_datalogger_health.py`):
`uart` → `i2c` → `spi` → `uart_i2c` → `all`, each a sustained soak, watching ring
+ ID health. All PASS on `0x6F48C22D`.

---

## 8. Part VII — RouteWave integration

The daemon is a `wave_stream_v1` producer; RouteWave is the consumer
(`stream attach tcp:<board>:7475`). Deepskopion ships **annotations**, so frames
land as decoded protocol events. Note: live
annotations currently feed RouteWave's *wave panel* (decoded overlays), not its
Wireshark-style *datalogger packet list* — that bridge is the next RouteWave step.

---

## Appendix A — Glossary

- **`zs_msg`** — the 24-byte in-ring frame (magic `0x5A53`).
- **EMIO** — PS peripheral I/O routed to the PL fabric.
- **SPSC ring** — single-producer/single-consumer circular buffer (PL writes,
  PS reads).
- **MCP CDC** — multi-cycle-path clock-domain crossing for a coherent multi-bit
  value (toggle-synchronized capture).
- **commit pointer** — the DMA's actually-written DDR address; the safe
  high-water mark to read up to.

## Appendix B — Troubleshooting quick table

| Symptom | Likely cause | Check |
|---|---|---|
| "Bus works but 0 frames" | datalogger not enabled / UART not calibrated / **stale SW** | `DATALOGGER_CONFIG` writes; 150-byte UART burst; `sw/stream` vintage (§6.2) |
| Board runs old design after deploy | JTAG reconfig on booted Linux, or cached synth | `devmem 0x40000000` (§6.1); `rr bitstream run --force` |
| Frame count >> bytes written | monitor counting valid-cycles not handshakes | count valid AND ready (§2.4) |
| Kernel panic under sustained capture | ring wrap at wrong width | wrap at `2^buffer_width_g` |
| Datalogger panel empty in RouteWave | annotations feed wave panel, not packet list | see annotation bridge notes |
| First TCP connect to `:7475` right after a Zybo reboot times out; later connects work (card without a `uboot.env`, §6.3) | eth0 takes a random MAC on every boot; the host's neighbour entry still names the previous boot's MAC until it is re-validated (about 8 s with Linux defaults: 5 s delay, then 3 probes 1 s apart) | `ip neigh show <board-ip>` names a MAC other than the console's `using random MAC address` line, in STALE, DELAY or PROBE |

---

*Deepskopion is real. Now go capture something nobody else can.* 🔬
