# UART Datalogger IP — Auto-Calibrating Protocol Analyzer

> **Version**: 2.0 · **Protocol**: v2.0 (Single-Packet Optimized)
> **Author**: Daniel J. Mazure · **License**: MIT
> **Status**: Production · **Last Updated**: February 2026

---

## 1. Overview

The UART Datalogger is a zero-configuration, fully autonomous UART protocol
analyzer IP core. Unlike conventional UART receivers that require
pre-configured baud rates and frame formats, this IP performs **Blind Discovery**
— automatically detecting baud rate, data width (5–9 bits), parity, and stop
bits from live traffic. Once locked, the IP transitions seamlessly to
**Continuous Streaming**, delivering timestamped 32-bit messages to the system
bus with zero software intervention.

### Key Differentiators

- **Zero-Touch Calibration** — No software configuration required; all
  UART parameters are auto-discovered from raw traffic
- **5–9 Bit Support** — Full coverage of standard and extended UART
  configurations in a single IP instance
- **Single-Packet 9-bit Encoding** — Protocol v2.0 eliminates message
  fragmentation by encoding the 9th data bit in the 3-bit type field
- **Calibration Replay** — Data captured during the discovery phase is
  buffered and replayed after lock, ensuring zero data loss
- **Parallel Discovery** — Five stop-finder engines run concurrently,
  one per supported word length, for fastest possible convergence
- **Passive, Non-Intrusive** — Monitors the UART line without driving
  or loading it; suitable for in-system protocol analysis
- **Portable** — Designed for Xilinx (7-Series, UltraScale) with
  planned support for Intel and Lattice FPGA families

---

## 2. Block Diagram

```text
                        CONFIG[15:0]    TIMESTAMP[15:0]
                             │                │
  ┌──────────────────────────┼────────────────┼──────────────────────────┐
  │ uart_datalogger             ▼                ▼                         │
  │                    ┌───────────┐                                    │
  │   UART_IN ────────▶│  edge     │                                    │
  │                    │ detector  │                                    │
  │                    └─────┬─────┘                                    │
  │                     ┌────┴────┐                                     │
  │                     ▼         ▼                                     │
  │              ┌──────────┐ ┌──────────┐                              │
  │              │Edge FIFO │ │Edge BRAM │                              │
  │              │ (1K dep) │ │ (mirror) │                              │
  │              └──────────┘ └────┬─────┘                              │
  │                                ▼                                    │
  │                         ┌─────────────┐                             │
  │                         │uart_playback│                             │
  │                         └──────┬──────┘                             │
  │           ┌────────────────────┼────────────────────┐               │
  │           ▼         ▼         ▼         ▼          ▼               │
  │     ┌──────────┬──────────┬──────────┬──────────┬──────────┐       │
  │     │stop_find │stop_find │stop_find │stop_find │stop_find │       │
  │     │  5-bit   │  6-bit   │  7-bit   │  8-bit   │  9-bit   │       │
  │     └────┬─────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┘       │
  │          │ MATCH     │ MATCH    │ MATCH    │ MATCH    │ MATCH       │
  │          └───────────┴──────┬───┴──────────┴──────────┘             │
  │                             ▼                                       │
  │                      ┌──────────────┐                               │
  │                      │uart_frame_gen│                               │
  │                      │ (reassembler)│                               │
  │                      └──────┬───────┘                               │
  │                             ▼                                       │
  │                     UART_MSG_OUT[31:0]  ──▶  UART_MSG_VLD           │
  │                                              UART_ERRORS[1:0]      │
  └─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Generics Reference

| Generic | Type | Default | Description |
| :--- | :--- | :---: | :--- |
| `G_SYSTEM_CLK_FREQ` | integer | 100,000,000 | System clock frequency in Hz |
| `G_TICK_PRESCALER` | integer | `C_TICK_PRESCALER` | Tick prescaler (see §5.1) |
| `G_UART_ID_WIDTH` | integer | 5 | Width of the device ID field |
| `G_UART_DEV_ID` | natural | 28 | Instance device ID (0–31) |
| `G_TIMESTAMP_WIDTH` | natural | 16 | Timestamp field width (16–32) |
| `G_CONFIG_WIDTH` | integer | 16 | Configuration port width |
| `G_ERROR_WIDTH` | integer | 2 | Error output port width |
| `G_ILA_EN` | integer | 1 | ILA debug insertion (0=off, 1=on) |

### Package Constants (`uart_pkg.vhd`)

| Constant | Value | Description |
| :--- | :---: | :--- |
| `C_SYSTEM_CLK_FREQ` | 100 MHz | Reference clock frequency |
| `C_UART_MAX_BAUDRATE` | 3 Mbps | Maximum supported baud rate (for prescaler calc) |
| `C_UART_MIN_BAUDRATE` | 9,600 | Minimum supported baud rate |
| `C_MAX_UART_BITS` | 12 | Maximum UART frame width (1S+9D+1P+2St) |
| `C_TICK_PRESCALER` | 8 | Default edge distance compression factor |
| `C_POSSIBLE_UART_CONFS` | 5 | Parallel stop-finder instances (5–9 bits) |
| `C_FIFO_DEPTH` | 10 | Edge FIFO depth (2¹⁰ = 1024 entries) |
| `C_TIMESTAMP_WIDTH` | 16 | Global timestamp width |

### Tick-Based Width Functions (`uart_pkg.vhd`)

FIFO and BRAM widths are computed dynamically from `G_TICK_PRESCALER`
using pure functions in the package body:

| Function | Formula | ps=8 | ps=4 | ps=1 |
| :--- | :--- | :---: | :---: | :---: |
| `f_dist_width(ps)` | ⌈log₂(max_cycles/ps)⌉+1 | 14 | 16 | 18 |
| `f_bram_width(ps)` | 1 + dist_width | 15 | 17 | 19 |
| `f_fifo_width(ps)` | 16 + 1 + dist_width | 31 | 33 | 35 |

---

## 4. Port Reference

| Port | Direction | Width | Description |
| :--- | :---: | :---: | :--- |
| `CLK` | in | 1 | System clock |
| `RST` | in | 1 | Synchronous reset (active high) |
| `UART_IN` | in | 1 | UART receive line (directly from pin) |
| `CONFIG` | in | 16 | Configuration register (see §8) |
| `TIMESTAMP` | in | 16 | Global 1 µs timestamp counter |
| `UART_MSG_OUT` | out | 32 | Output message bus |
| `UART_MSG_VLD` | out | 1 | Message valid strobe |
| `UART_MSG_RDY` | in | 1 | Downstream ready (flow control) |
| `UART_ERRORS` | out | 2 | Runtime error flags `[1:0]` |

### Error Flags

| Bit | Name | Description |
| :---: | :--- | :--- |
| `[0]` | Frame Error | Stop bit violation detected |
| `[1]` | Parity Error | Parity mismatch detected |

---

## 5. Functional Description

The UART Datalogger operates in three sequential phases after the `ENABLE` bit
is asserted via the `CONFIG` port.

### Phase 1 — Edge Capture & Baud Rate Detection

Upon enable, the IP begins capturing **transition events** on the `UART_IN`
line. Instead of oversampling at a fixed rate, it records the **distance
between edges** (in compressed ticks) and the **signal level** at each
transition.

- Edges are stored simultaneously in a **synchronous FIFO** (1024 entries)
  and a **BRAM mirror** for offline analysis
- The minimum observed transition distance (`baudRateMin_r`) converges to the
  bit period of the incoming UART traffic
- A configurable ticker prescaler (`G_TICK_PRESCALER`, default 8) compresses
  distances to reduce counter and FIFO/BRAM data widths
- Width savings scale with prescaler: ps=8 saves 3 bits vs. cycle-based
  storage (14 vs. 17 bits per distance)

### Phase 2 — Blind Discovery (Parallel Stop-Finder Array)

Facts in this section, the FSM diagram and the calibration tables verified
against `dce1f3d0` on 2026-11-09.

The edge buffer closes when the line is idle for 16 bit-times
(`relativeDistCnt_r >= baudRateMin_r & "0000"`, `uart_datalogger.vhd:590`) or
when the FIFO holds `2**(C_FIFO_DEPTH-3)-1` = 127 edges (`uart_datalogger.vhd:598`,
`C_FIFO_DEPTH` = 10 in `uart_pkg.vhd:60`). A burst longer than 127 edges is
therefore cut inside a frame. Calibration then runs:

1. `uart_playback` replays the buffered edges as a waveform, one recorded
   distance tick per clock cycle (`PB_CLK => '1'`, `uart_datalogger.vhd:816`).
   The distances are counted in prescaled ticks, so one replayed bit lasts
   `baudRateMin_r` clock cycles and the replay runs faster than the line by the
   prescaler factor. Measured at 9600 baud, 100 MHz: adjacent stop-finder
   instances step 26.04 µs (2604 cycles = `baudRateMin_r`) against a 104.17 µs
   line bit (`test_integration_9600_8N1` trace, seed 1794146657, on `97073018`
   and `df1d12c4`).
2. Five `uart_stop_finder` instances, one per word length 5 to 9, check every
   replayed frame: start bit, data bits, stop bit, parity consistency.
3. An instance reports `MATCH` when it has seen `G_MIN_MATCHES` = 7 frames
   with a valid stop bit (`uart_datalogger.vhd:837`) and no frame has refuted
   it (`uart_stop_finder.vhd:233`).
4. A refutation counts only while the replay still plays recorded edges
   (`JUDGE = not pbEmpty`, `uart_datalogger.vhd:828`). After the replay has
   drained, the playback holds its last level; a frame read from that level
   can neither refute an instance nor clear its stop-bit result
   (`uart_stop_finder.vhd:141`, `:174`, `:206`).
5. The format is decided after the replay has drained, plus two bit-times
   (`settleCnt_r >= baudRateMin_r & '0'`, `uart_datalogger.vhd:728`), not on
   the first instance to match.
6. Of the instances that report `MATCH`, the shortest word is taken
   (`pick_format`, `uart_datalogger.vhd:133`). No instance matching is a
   calibration failure (`uart_datalogger.vhd:734`).
7. The chosen instance's word length, parity and stop bits are latched
   (`uart_datalogger.vhd:748`) and one Config Message is emitted.

Word-length aliases. On the wire, an n-bit frame with its top data bit 1 and
one stop bit is the same waveform as an (n-1)-bit frame with two stop bits,
and a gapped n-bit frame is the same waveform as an (n+1)-bit frame with its
top data bit 1. A buffer in which every frame fits both readings resolves to
the shorter word (`pick_format`). `test_uart_autosync_aliases_ds_p2_242.py`
pins the three cases.

> [!NOTE]
> `NOT_A_MATCH` is absorbing: an instance refuted on recorded data stays out
> until the logger is disabled and enabled again.

### Phase 3 — Playback & Continuous Streaming

After a successful lock:

1. The calibration buffer is **replayed** through `uart_frame_gen`, recovering
   all data captured during the blind phase (**Calibration Replay**)
2. The FIFO then seamlessly switches to forwarding **new live edges** from
   `UART_IN`
3. `uart_frame_gen` samples the reconstructed waveform using the locked baud
   rate, extracts data bits, verifies parity, and emits 32-bit
   **Payload Messages** on `UART_MSG_OUT`
4. Streaming continues indefinitely until the IP is disabled

### Internal FSM Architecture

Calibration FSM states (`t_fsm_calibrate_uart`, `uart_datalogger.vhd:158`):

```text
┌─────────────┐  buffer closed   ┌───────────────────┐
│  DISABLED   │─────────────────▶│  DETECT_STOP_BIT  │  replay the buffer
│             │                  │  (5 finders)      │  through the finders
└─────────────┘                  └─────────┬─────────┘
                                           │ last buffered edge played
                                 ┌─────────▼─────────┐
                                 │WAIT_FOR_DETECTION │  replay drained (pbEmpty)
                                 │                   │  + 2 bit-times, then
                                 └─────────┬─────────┘  pick_format
                                           │
                                 ┌─────────▼─────────┐
                                 │ CALIBRATION_DONE  │  format latched,
                                 │                   │  Config Message, stream
                                 └───────────────────┘
```

The type also declares `IDLE` and `DETECT_BAUDRATE`; the baud rate
(`baudRateMin_r`, the shortest edge distance) is measured while the buffer
fills, in the buffer FSM (`uart_datalogger.vhd:580`).

---

## 6. Protocol Specification (v2.0)

### 6.1 Message Structure (32-bit)

All messages follow a consistent 32-bit layout:

```text
 31      27 26   24 23                 8 7              0
┌──────────┬───────┬────────────────────┬────────────────┐
│ DEVICE_ID│  TYPE │     TIMESTAMP      │      DATA      │
│  (5-bit) │(3-bit)│     (16-bit)       │    (8-bit)     │
└──────────┴───────┴────────────────────┴────────────────┘
```

### 6.2 Message Types

| Type | Code | Name | Description |
| :--- | :---: | :--- | :--- |
| **Config** | `0b001` | CONFIG | Post-calibration configuration report |
| **Payload** | `0b010` | PAYLOAD | Captured data (standard or 9-bit MSB=0) |
| **Payload 9B** | `0b011` | PAYLOAD_B9 | Captured 9-bit data with MSB=1 |
| **Status** | `0b100` | STATUS | Calibration events and errors |

### 6.3 Payload Message (Types `0b010` / `0b011`)

Data bits are packed into a single message. For 9-bit words, the MSB
is encoded in the type field — **no fragmentation required**.

| Mode | Behavior |
| :---: | :--- |
| **5–8 bits** | Type = `0b010`. Data in `[7:0]`, natural LSB-first alignment |
| **9 bits, bit[8]=0** | Type = `0b010`. Lower 8 bits in `[7:0]` |
| **9 bits, bit[8]=1** | Type = `0b011`. Lower 8 bits in `[7:0]` |

**Decoding (all modes):**

```python
def decode_payload(msg: int) -> int:
    msg_type = (msg >> 24) & 0x07
    data = msg & 0xFF
    bit9 = 1 if msg_type == 0b011 else 0
    return (bit9 << 8) | data
```

### 6.4 Config Message (Type `0b001`)

Sent once after successful auto-calibration. The `timestamp` field carries
`baud_cycles` instead of the global timestamp.

| Bits | Field | Description |
| :--- | :--- | :--- |
| `[23:8]` | `baud_cycles` | Detected baud rate in clock cycles per bit |
| `[7]` | Reserved | — |
| `[6]` | `stop_bits` | `1` = 1 stop bit, `0` = 2 stop bits |
| `[5]` | `parity_type` | `0` = Even, `1` = Odd |
| `[4]` | `parity_enable` | `0` = None, `1` = Parity enabled |
| `[3:0]` | `word_length` | Detected data bits (5–9) |

**Decoding:**

```python
def decode_config(msg: int) -> dict:
    baud_cycles = (msg >> 8) & 0xFFFF
    return {
        "baud_rate": clk_freq / baud_cycles,
        "word_length": msg & 0x0F,
        "parity": "odd" if (msg >> 5) & 1 else "even"
                  if (msg >> 4) & 1 else "none",
        "stop_bits": 1 if (msg >> 6) & 1 else 2,
    }
```

### 6.5 Status Message (Type `0b100`)

| Code | Name | Description |
| :---: | :--- | :--- |
| `0x00` | `CALIBRATION_DONE` | Auto-calibration completed successfully |
| `0x01` | `CALIBRATION_TIMEOUT` | No valid traffic detected within timeout |
| `0x02` | `PARITY_ERROR` | Runtime parity mismatch |
| `0x03` | `START_ERROR` | Framing error: invalid start bit |
| `0x04` | `STOP_ERROR` | Framing error: invalid stop bit |

---

## 7. Software API Reference

The following Python API provides a reference decoder for integrating
with the UART Datalogger output stream. It is derived from the formal
protocol definition in
[uart_datalogger_protocol.yml](uart_datalogger_protocol.yml).

```python
"""UART Datalogger Protocol Decoder — Reference Implementation"""

# Message type codes
TYPE_CONFIG     = 0b001
TYPE_PAYLOAD    = 0b010
TYPE_PAYLOAD_9B = 0b011
TYPE_STATUS     = 0b100

# Status codes
STATUS_NAMES = {
    0x00: "CALIBRATION_DONE",
    0x01: "CALIBRATION_TIMEOUT",
    0x02: "PARITY_ERROR",
    0x03: "START_ERROR",
    0x04: "STOP_ERROR",
}


def decode_message(raw: int, clk_freq: int = 100_000_000) -> dict:
    """Decode a 32-bit UART datalogger message.

    Args:
        raw: Raw 32-bit message word from the DMA stream.
        clk_freq: System clock frequency for baud rate calculation.

    Returns:
        Dictionary with decoded fields.
    """
    device_id = (raw >> 27) & 0x1F
    msg_type  = (raw >> 24) & 0x07
    timestamp = (raw >>  8) & 0xFFFF
    data      = raw & 0xFF

    result = {"device_id": device_id, "type": msg_type}

    if msg_type in (TYPE_PAYLOAD, TYPE_PAYLOAD_9B):
        bit9 = 1 if msg_type == TYPE_PAYLOAD_9B else 0
        result.update({
            "name": "payload",
            "timestamp": timestamp,
            "data": (bit9 << 8) | data,
        })

    elif msg_type == TYPE_CONFIG:
        baud_cycles = timestamp  # Config reuses timestamp field
        result.update({
            "name": "config",
            "baud_cycles": baud_cycles,
            "baud_rate": clk_freq // baud_cycles if baud_cycles else 0,
            "word_length": data & 0x0F,
            "parity_enable": bool((data >> 4) & 1),
            "parity_type": "odd" if (data >> 5) & 1 else "even",
            "stop_bits": 1 if (data >> 6) & 1 else 2,
        })

    elif msg_type == TYPE_STATUS:
        result.update({
            "name": "status",
            "timestamp": timestamp,
            "status_code": data,
            "status_name": STATUS_NAMES.get(data, f"UNKNOWN_0x{data:02X}"),
        })

    return result
```

---

## 8. Configuration Register

The `CONFIG` input (16-bit) controls the datalogger instance. All UART
parameters are auto-discovered — only enable/disable is software-controlled.

| Bits | Name | Description |
| :--- | :--- | :--- |
| `[15:11]` | `UART_DEV_ID` | 5-bit device ID (must match `G_UART_DEV_ID` to apply) |
| `[10:1]` | Reserved | Must be `0` (reserved for future manual override) |
| `[0]` | `ENABLE` | `1` = Start auto-calibration and capture |

---

## 9. Timing & Characterization

### Verified Baud Rate Limits (100 MHz System Clock)

| Baud Rate | Prescaler | Ticks/Bit | Status | Notes |
| :--- | :---: | :---: | :---: | :--- |
| 9,600 | 16 | 651 | ✅ PASS | Aggressive compression |
| 9,600 | 8 | 1,302 | ✅ PASS | Default prescaler |
| 115,200 | 8 | 109 | ✅ PASS | Standard reference |
| 460,800 | 8 | 27 | ✅ PASS | Recommended high-speed |
| 921,600 | 8 | 14 | ✅ PASS | Sweep-verified |
| 1,000,000 | 8 | 13 | ✅ PASS | Sweep-verified |
| 1,500,000 | 4 | 17 | ✅ PASS | Reduced prescaler |
| 1,843,200 | 8 | 7 | ✅ PASS | Sweep-verified |
| 3,000,000 | 8 | 4 | ✅ PASS | Nyquist limit at ps=8 |
| 3,000,000 | 2 | 17 | ✅ PASS | Prescaler=2 sweep |
| 6,000,000 | 1 | 17 | ✅ PASS | No compression |

> [!IMPORTANT]
> **Maximum baud rate depends on prescaler**: With `G_TICK_PRESCALER=1`,
> the IP supports up to **6 Mbps** at 100 MHz. The default prescaler
> (8) supports up to 3 Mbps. Use a lower prescaler for higher baud rates.

### Calibration Requirements

| Parameter | Value | Source |
| :--- | :---: | :--- |
| `G_MIN_MATCHES` | 7 | valid frames per instance before `MATCH`; `uart_datalogger.vhd:837` (the `uart_stop_finder` generic default, 16, is overridden) |
| Buffer threshold | 127 edges | `2**(C_FIFO_DEPTH-3)-1`, `uart_datalogger.vhd:598`; `C_FIFO_DEPTH` = 10, `uart_pkg.vhd:60` |
| Buffer idle timeout | 16 bit-times | `baudRateMin_r & "0000"`, `uart_datalogger.vhd:590` |
| Format settle | 2 bit-times after the replay drains | `uart_datalogger.vhd:728` |
| `C_BUFFER_TIMEOUT_LIMIT` | not used | dead code (`uart_datalogger.vhd:585-591`); only a NOTE assert reads it (`:327`) |


### Calibration data limits

An instance with parity checking reads the bit after its data bits as a parity
bit. Its parity check records only a change of parity between replayed frames
(`parityFail_r`, `uart_stop_finder.vhd:193`; `PARITY(0) <= not parityFail_r`,
`:218`). On an 8N1 line the 8-sample instance can therefore read d0..d6 as data
and d7 as parity.

| Calibration buffer on an 8N1 line | autoSync result | Measured by |
| :--- | :--- | :--- |
| At least one byte whose number of ones differs in parity from the others | 8 data bits, no parity; `parityFail_r` rises at the first such byte | `test_uart_autosync_parity_ambiguity_ds_p2_244.py`, cases 1 and 2 (first such byte at frame 11) |
| Every byte has an odd number of ones | 7 data bits, odd parity (CONFIG `nbits` 7, `parity_enable` 1, `parity_type` 1); each decoded byte is d0..d6 | same file, case 3 |

When every calibration byte has the same parity, an 8N1 line and a line with 7
data bits and that parity produce the same waveform, and autoSync cannot tell
them apart. A calibration burst of one repeated byte (for example 0x55 or 0x00)
meets this condition. Explicit configuration (`autoSync` = 0) avoids it.

### Streaming Latency

| Metric | Value | Source |
| :--- | :---: | :--- |
| Edge → MSG_OUT latency | not characterized | see Items to confirm |
| Buffer closed → format locked | 7.27 ms at 9600 baud, 30-byte burst | simulation trace on `df1d12c4`, `test_uart_initial_idle_repro`, seed 1794154559 (bufferFinish 29.5635 ms, calibrationDone 36.8335 ms); one replayed bit lasts `baudRateMin_r` clock cycles, so this scales with the bit time and the buffer length |
| Replay drained → format locked | 52 cycles at 1 Mbaud (bound 2 × baudRateMin_r + 3) | `test_uart_autosync_msb_race_ds_p2_242.py` |
| Post-calibration idle flush | 16 bit-times of line idle | `pr_timeout_idle`, `relativeDistCnt_r >= baudRateMin_r & "0000"`, `uart_datalogger.vhd:477` |
| Line end → last byte decoded | 1.50–5.46 ms at 9600 baud, 36-byte burst, 20 seeds | `test_integration_9600_8N1.py` on `822b36ac`, seeds 201–220; the test bounds it by max(lock + E/R + 10/R, E) + 16 + 10 bit-times, with E the line length in bit-times from the first start bit and R = 4 |

### Items to confirm

- autoSync result for a calibration buffer in which every byte has an even
  number of ones: not measured (expected by the same rule: 7 data bits, even
  parity).
- Edge → MSG_OUT latency.
- Minimum traffic for a lock: the earlier "70–100 bytes" and "500 bytes for
  9-bit" figures are not traced to a measurement on `dce1f3d0`.
- `G_TICK_PRESCALER` default: §Phase 1 states 8; the `test_uart_initial_idle_repro` trace at
  100 MHz measured `baudRateMin_r` = 2604 ticks for a 10417-cycle bit, i.e. a
  prescaler of 4.

---

## 10. Integration Guide

### Instantiation Template

```vhdl
i_uart_datalogger : entity work.uart_datalogger
  generic map (
    G_SYSTEM_CLK_FREQ => 100_000_000,
    G_TICK_PRESCALER  => 8,       -- Default; use 4 for >1.5M, 1 for >3M
    G_UART_DEV_ID     => 0,
    G_ILA_EN          => 0        -- Disable ILA for production
  )
  port map (
    CLK          => sys_clk,
    RST          => sys_rst,
    UART_IN      => uart_rx_pin,  -- Direct from IO buffer
    CONFIG       => datalogger_config,
    TIMESTAMP    => global_timestamp(15 downto 0),
    UART_MSG_OUT => msg_data,
    UART_MSG_VLD => msg_valid,
    UART_MSG_RDY => msg_ready,
    UART_ERRORS  => uart_err
  );
```

### Clock & Reset Requirements

- **Clock**: Single clock domain. Minimum frequency determined by target
  baud rate: `f_clk >= 100 × baud_rate` for reliable discovery
- **Reset**: Synchronous, active-high. Hold for ≥2 clock cycles
- **Timestamp**: Free-running 16-bit counter at 1 µs resolution

### Flow Control

The `UART_MSG_RDY` signal implements backpressure. When deasserted:

- `uart_frame_gen` pauses its FSM (`PAUSE` state)
- Edge capture continues — no data is lost at the input FIFO
- Messages are held until `RDY` is reasserted

> [!CAUTION]
> The `ENABLE` bit in `CONFIG` must be **held high** persistently after
> initial assertion. If the bus returns to `0` after configuration, the
> edge FIFO input is gated by `configEnable`, silently dropping all
> subsequent UART edges.

### Dependencies

| Library | Source | Purpose |
| :--- | :--- | :--- |
| `tich` (project) | `hw/rtl/pkg/` | `datalogger_pkg`, `uart_pkg` |
| `xpm` | Xilinx Parameterized Macros | Synchronous FIFO primitives |

---

## 11. Submodule Architecture

### `uart_stop_finder`

Parallel framing detector — one instance per supported word length (5–9).

| Port | Dir | Description |
| :--- | :---: | :--- |
| `BR_CYCLES` | in | Bit period in clock cycles |
| `WAVE` | in | Replayed waveform from `uart_playback` |
| `MATCH` | out | `'1'` when `G_MIN_MATCHES` consecutive stop bits found |
| `PARITY` | out | `[1]` = type (0=Even, 1=Odd), `[0]` = has parity |
| `STOP_BITS` | out | `'1'` = 1 stop bit detected |

**FSM**: `WAIT_START` → `SAMPLE_START_BIT` → `CHECK_START` →
`SAMPLE_BITS` → `WAIT_STOP` → `CHECK_STOP` → `RESOLVE_PARITY` →
`CHECK_PARITY` | `NOT_A_MATCH` (absorbing)

### `uart_playback`

Reconstructs the compressed edge sequence into a continuous waveform.

| Port | Dir | Description |
| :--- | :---: | :--- |
| `PB_CLK` | in | Distance tick; tied to `'1'` (one tick per clock cycle) at `uart_datalogger.vhd:816` and `:868` |
| `LEVEL` / `DISTANCE` | in | Edge data from FIFO/BRAM |
| `WAVE` | out | Reconstructed `t_wave` record |
| `EMPTY` / `FULL` | out | FIFO status |

**FSM**: `IDLE` → `LOAD_0` → `LOAD_1` → `PLAYBACK` (distance countdown)

### `uart_frame_gen`

Extracts UART frames from the reconstructed waveform and generates
32-bit protocol messages.

| Port | Dir | Description |
| :--- | :---: | :--- |
| `WAVE` | in | Reconstructed waveform |
| `BR_CYCLES` | in | Locked baud rate (clock cycles) |
| `NBITS` / `PARITY` / `STOP_BITS` | in | Locked configuration |
| `MSG_OUT` | out | 32-bit protocol message |
| `MSG_VLD` | out | Message valid strobe |
| `READY` | in | Downstream flow control |

**FSM**: `IDLE` → `WAIT_START` → `CHECK_START` → `SAMPLE_BITS` →
`SAMPLE_PARITY` → `WAIT_STOP` → `CHECK_STOP` → `GENERATE_MSG` →
`SEND_MSG` → `WAIT_START`

### `uart_baudrate_gen`

Generates sampling ticks at mid-bit and full-bit intervals.

| Port | Dir | Description |
| :--- | :---: | :--- |
| `BR_CYCLES` | in | Bit period |
| `PAUSE` | in | Freeze counter |
| `TICK` | out | Sampling pulse (mid-bit + wrap) |

**Logic**: Uses a `PLAIN/HALF` offset rotation to compensate for
odd bit periods, ensuring stable sampling across all baud rates.

### `t_wave` Record

Shared data type for passing reconstructed waveform data:

```vhdl
type t_wave is record
  level     : std_logic;
  timestamp : std_logic_vector(15 downto 0);
end record t_wave;
```

---

## 12. Compatibility Matrix

Verified configurations (460,800 baud, 100 MHz clock):

| Word Length | No Parity | Even Parity | Odd Parity |
| :---: | :---: | :---: | :---: |
| **5 bits** | ✅ | ✅ | ✅ |
| **6 bits** | ✅ | ✅ | ✅ |
| **7 bits** | ✅ | ✅ | ✅ |
| **8 bits** | ✅ | ✅ | ✅ |
| **9 bits** | ✅ | ❌ | ❌ |

> [!NOTE]
> **9-bit + Parity** is explicitly excluded. These configurations
> correctly trigger `calibrationFail_r` and are outside the supported
> compatibility matrix.

---

## 13. Verification & Test Suite

The IP is verified using Cocotb 2.0 with the NVC VHDL simulator.

### Test Inventory

| # | Test File | Target DUT | Test Cases | Status |
| :-: | :--- | :--- | :---: | :---: |
| 1 | `test_uart_datalogger_parameterized.py` | `uart_datalogger` | 78 + 5 | ✅ |
| 2 | `test_uart_datalogger.py` | `uart_datalogger` | Full sweep | ✅ |
| 3 | `test_payload_log.py` | `uart_datalogger` | 1 | ✅ |
| 4 | `test_uart_frame_gen_isolation.py` | `uart_frame_gen` | 7 | ✅ |
| 5 | `test_uart_baudrate_gen.py` | `uart_baudrate_gen` | 5 | ✅ |
| 6 | `test_uart_playback.py` | `uart_playback` | 4 | ✅ |
| 7 | `test_uart_stop_finder.py` | `uart_stop_finder` | 4 | ✅ |
| 8 | `test_uart_precommit.py` | `uart_datalogger` | 12 (5-9 bits, parity) | ✅ |
| 9 | `test_uart_datalogger_baud_sweep.py` | `uart_datalogger` | 6 (115K–3M) | ✅ |
| 10 | `test_uart_prescaler_sweep.py` | `uart_datalogger` | 4 (ps=1,2,4,16) | ✅ |

### Pre-Commit Regression

The expanded pre-commit (`test_uart_precommit.py`) provides the
production quality gate:

- **Configurations**: 12 — all word lengths 5–9, parity (none/odd/even), 2-stop-bit anchors
- **Baud Rate**: 460,800 (reliable payload verification)
- **Calibration**: 100-byte randomized stimulus per config
- **Runtime**: ~4 minutes (12 × ~19s)
- **Coverage**: Full bit-width matrix with payload integrity verification

### Prescaler Sweep

The `test_uart_prescaler_sweep.py` validates the `G_TICK_PRESCALER`
generic across 4 scenarios (each a separate NVC elaboration):

| Scenario | Prescaler | Baud | Width |
| :--- | :---: | :---: | :---: |
| High-speed | 4 | 1.5 Mbps | 16-bit |
| Very high-speed | 2 | 3.0 Mbps | 17-bit |
| Maximum speed | 1 | 6.0 Mbps | 18-bit |
| Slow / compressed | 16 | 9,600 | 13-bit |

### Calibration Principles

1. Use **randomized data** (`random.randint`) — never fixed patterns
2. Provide **≥70 bytes** for standard modes, **≥500 bytes** for 9-bit
3. Seed sensitivity: Calibration success at 921,600 baud is
   data-dependent (Seed 100 passes, Seed 42 fails)

### Running Tests

```bash
# Pre-commit regression (from project root)
make sim MODULE=test_uart_precommit

# Full parameterized sweep
make sim MODULE=test_uart_datalogger_parameterized

# Prescaler sweep (4 scenarios, ~15 min with 9600)
SIM=nvc python test_uart_prescaler_sweep.py

# Individual submodule tests
make sim MODULE=test_uart_baudrate_gen
make sim MODULE=test_uart_stop_finder
make sim MODULE=test_uart_playback
make sim MODULE=test_uart_frame_gen_isolation
```

---

## 14. Resource Utilization

Synthesized with Vivado 2024.1 targeting **xc7z020clg400-1** (Zynq-7020).

| Resource | Used | Available | Util% |
| :--- | :---: | :---: | :---: |
| **Slice LUTs** | 983 | 53,200 | 1.85% |
| **Slice Registers** | 885 | 106,400 | 0.83% |
| **Block RAM (36Kb)** | 4 | 140 | 2.86% |
| **DSP** | 0 | 220 | 0.00% |
| **Carry Logic** | 234 | — | — |

### Primitives Breakdown

| Primitive | Count | Category |
| :--- | :---: | :--- |
| FDRE | 799 | Flip-Flop |
| FDSE | 86 | Flip-Flop (Set) |
| LUT1–LUT6 | 1,131 | Look-Up Table |
| CARRY4 | 234 | Carry Logic |
| RAMB36E1 | 4 | Block RAM |

> [!NOTE]
> Post-optimization LUT count may be lower after `opt_design`.
> No DSP slices or distributed RAM are consumed.

---

## 15. Known Limitations

| # | Limitation | Impact | Status |
| :-: | :--- | :--- | :---: |
| 1 | **9-bit + Parity not supported** | Correctly rejected by calibration | By design |
| 2 | **Stop-finder is one-shot** | `NOT_A_MATCH` state is absorbing; requires reset to retry | Planned improvement |
| 3 | **Seed-dependent calibration at edge speeds** | Stimulus pattern affects lock success at > 460,800 | Known |
| 4 | **Streaming latency uncharacterized** | Edge → MSG_OUT delay not formally measured | Pending |
| 5 | **9600 baud simulation is slow** | ~15 min at ps=16 due to 156M ns sim time | By design |

---

## 16. Revision History

| Date | Version | Change |
| :--- | :---: | :--- |
| 2025-11 | 1.0 | Initial IP: Blind Discovery, fragmented 9-bit protocol |
| 2026-01 | 1.1 | 8x oversampling fix (`BR_CYCLES sll 3`) |
| 2026-02 | 2.0 | Protocol v2.0: Single-packet 9-bit, natural LSB-first ordering |
| 2026-02 | 2.0.1 | Pre-commit regression (46s, Minimal Anchor Set) |
| 2026-02 | 2.0.2 | SDK de-coupling: VIPs migrated to `hw/sim/vip/` |
| 2026-02 | 2.1.0 | `G_TICK_PRESCALER` generic: tick-based widths, 6 Mbps support |

---

## License

```text
MIT License

Copyright (c) 2025-2026 Daniel J. Mazure

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
