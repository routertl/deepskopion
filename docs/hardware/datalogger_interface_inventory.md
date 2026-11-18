# Datalogger Interface Inventory — Bandwidth & Priority Analysis

**Last updated**: Feb 28 2026

This document inventories every digital interface worth capturing,
calculates the per-channel bandwidth cost, and prioritizes
implementation.

---

## 1. Bandwidth Model

Every protocol datalogger emits **one 32-bit message per decoded
frame/byte**. The bus bandwidth cost per channel is:

```text
Step 1: bytes/sec = protocol_clock ÷ bits_per_frame
Step 2: msg/sec   = bytes/sec  (1 msg per decoded unit)
Step 3: bus_bw    = msg/sec × 32 bits/msg
```

The edge detector is protocol-agnostic — it emits **one 32-bit
message per toggle**, so its cost is simply `toggle_rate × 32`.

**System transport reference:**

| Transport | Sustained BW | 32-bit Messages/sec |
|:----------|:-------------|:--------------------|
| USB 2.0 (current) | ~35 MB/s (280 Mbps) | ~8.75M msg/s |
| **GbE (planned)** | **~100 MB/s (800 Mbps)** | **~25M msg/s** |
| HP AXI (burst) | ~284 MB/s (2,272 Mbps) | ~71M msg/s |

---

## 2. Implemented Dataloggers

| Interface | N | Pins | Max Clock | Msg Rate/ch | × 32 bits | BW/ch | Status |
|:----------|:-:|:----:|:----------|:------------|:----------|:------|:-------|
| UART | 6 | 1 | 3 Mbaud | 300,000 × 32 = 9,600,000 bps | 9.6 Mbps (1.2 MB/s) | ✅ Done |
| I2C | 4 | 2 | 400 kHz SCL | 44,444 × 32 = 1,422,208 bps | 1.4 Mbps (0.18 MB/s) | ✅ Done |
| SPI | 4 | 4 | 20 MHz CLK | 2,500,000 × 32 = 80,000,000 bps | 80 Mbps (10 MB/s) | ✅ Done |
| Edge | 5 | 1 | 50 MHz (Nyquist) | varies (1 msg/toggle) | varies | ✅ Done |
| Timestamp | 1 | — | — | 61 × 32 = 1,952 bps | ≈0 | ✅ Done |

---

## 3. Planned Dataloggers

| Interface | N | Pins | Max Clock | Msg Rate/ch | × 32 bits | BW/ch | Priority |
|:----------|:-:|:----:|:----------|:------------|:----------|:------|:---------|
| QSPI | 1 | 6 | 50 MHz CLK | 25,000,000 × 32 = 800M bps | 800 Mbps (100 MB/s) | 🔵 High |
| I2S | 2 | 3 | 12.288 MHz | ~1,000,000 × 32 = 32M bps | 32 Mbps (4 MB/s) | 🟡 Medium |

---

## 4. Candidate Interfaces — Prioritized

### Tier 1 — High Priority (🔵)

#### MDIO (Management Data I/O)

- **Use case**: Ethernet PHY register access (read/write PHY
  configuration). Essential when debugging Ethernet bring-up.
- **Wires**: 2 (MDC clock + MDIO data)
- **Max clock**: 2.5 MHz
- **Frame format**: 32-bit preamble + 2-bit start + 2-bit opcode
  + 5-bit PHY addr + 5-bit reg addr + 2-bit turnaround + 16-bit
  data = 64 bits per frame
- **Bandwidth**:

  ```text
  2,500,000 CLK ÷ 64 bits/frame = 39,063 frames/sec
  39,063 × 32 = 1,250,000 bps = 1.25 Mbps (0.16 MB/s)
  ```

- **vs GbE**: 0.16% — trivial
- **Complexity**: Low. Similar to I2C (2-wire, synchronous).

#### JTAG (IEEE 1149.1)

- **Use case**: ARM/FPGA boundary scan, debug access. Anyone
  debugging a multi-chip board needs to see JTAG traffic.
- **Wires**: 4 (TCK + TMS + TDI + TDO), optionally +1 (TRST)
- **Max clock**: 1–20 MHz (typically 10 MHz)
- **Frame format**: Variable-length shift registers. Protocol
  datalogger emits one message per decoded DR/IR scan.
- **Bandwidth** (at 10 MHz TCK, ~50 bits avg scan):

  ```text
  10,000,000 CLK ÷ 50 bits/scan = 200,000 scans/sec
  200,000 × 32 = 6,400,000 bps = 6.4 Mbps (0.8 MB/s)
  ```

- **vs GbE**: 0.8% — trivial
- **Complexity**: Medium. TAP state machine is well-defined (16
  states). IR/DR length detection requires some intelligence.

#### SWD (Serial Wire Debug)

- **Use case**: ARM Cortex-M/A/R debug. This is how 90% of
  embedded ARM debugging happens. Capturing SWD traffic lets you
  see what a debugger is doing to the target.
- **Wires**: 2 (SWCLK + SWDIO)
- **Max clock**: Up to 50 MHz (typically 1–10 MHz)
- **Frame format**: 8-bit request + 3-bit ACK + 32-bit data +
  1-bit parity = 46 bits per transaction
- **Bandwidth** (at 10 MHz):

  ```text
  10,000,000 CLK ÷ 46 bits/txn = 217,391 txn/sec
  217,391 × 32 = 6,956,512 bps = 7.0 Mbps (0.87 MB/s)
  ```

- **vs GbE**: 0.87% — trivial
- **Competitive note**: No commercial logic analyzer does SWD
  protocol decode well. This would be a strong differentiator.

### Tier 2 — Medium Priority (🟡)

#### CAN (Controller Area Network)

- **Use case**: Automotive, industrial. Standard CAN runs at
  1 Mbps, CAN-FD at 8 Mbps.
- **Wires**: 1 (CAN_H – CAN_L is differential, datalogger taps one
  logic-level signal after the transceiver)
- **Max clock**: 1 Mbps (CAN 2.0) / 8 Mbps (CAN-FD)
- **Frame format**: ~130 bits per frame (with 8-byte payload)
- **Bandwidth** (CAN 2.0):

  ```text
  1,000,000 bps ÷ 130 bits/frame = 7,692 frames/sec
  7,692 × 32 = 246,144 bps = 0.25 Mbps (0.03 MB/s)
  ```

- **vs GbE**: 0.03% — negligible
- **Complexity**: Medium-high. Bit-stuffing, CRC, error frames
  require non-trivial FSM. CAN-FD doubles complexity.
- **Alternative**: Edge datalogger + SW CAN decoder for basic use.

#### 1-Wire (Dallas/Maxim)

- **Use case**: Temperature sensors (DS18B20), iButtons, serial
  numbers.
- **Wires**: 1
- **Max clock**: ~16 kHz (time-slot based, 60 µs slots)
- **Bandwidth**: 0.03 Mbps — negligible
- **Alternative**: Edge datalogger captures this perfectly.
  SW decoder extracts bytes from timing.

### Tier 3 — Low Priority (⚪)

#### LIN (Local Interconnect Network)

- **Use case**: Automotive sub-bus, very slow.
- **Wires**: 1
- **Max clock**: 20 kbps
- **Bandwidth**: 0.06 Mbps
- **Alternative**: UART datalogger handles LIN natively (LIN is
  UART 8N1 with a specific framing layer on top).

#### SDIO / SD Card

- **Wires**: 6 (CLK + CMD + 4× DAT)
- **Max clock**: 50 MHz
- **Bandwidth**: ~800 Mbps — saturates GbE alone
- **Verdict**: Too much BW for practical live streaming. Would
  require dedicated high-speed capture mode.

#### RMII (Ethernet PHY)

- **Wires**: 4+2 (TXD[1:0], RXD[1:0], TX_EN, CRS_DV) + REF_CLK
- **Max clock**: 50 MHz
- **Bandwidth**: ~200 Mbps
- **Verdict**: High BW. Useful for PHY-level Ethernet debugging but
  niche. MDIO datalogger covers the more common use case.

---

## 5. Full Fleet BW Budget

### Worst Case — All Interfaces at Max Speed

```text
6× UART 3M:     6 × 9.6    =    57.6 Mbps
4× I2C 400K:    4 × 1.4    =     5.6 Mbps
4× SPI 20M:     4 × 80     =   320   Mbps
1× QSPI 50M:   1 × 800    =   800   Mbps  ← saturates GbE alone
5× Edge 10K:    5 × 0.6    =     3.2 Mbps
2× I2S:         2 × 32     =    64   Mbps
2× MDIO:        2 × 1.25   =     2.5 Mbps
1× JTAG 10M:   1 × 6.4    =     6.4 Mbps
1× SWD 10M:    1 × 7.0    =     7.0 Mbps
──────────────────────────────────────────────────────
TOTAL:                        ≈ 1,266.3 Mbps
```

> [!WARNING]
> QSPI at 50 MHz alone consumes 100% of GbE. In practice, QSPI
> captures would be burst-mode (DDR-buffered), not sustained.

### Realistic Debug Session (No QSPI)

```text
2× UART 3M:     2 × 9.6    =    19.2 Mbps
2× I2C 400K:    2 × 1.4    =     2.8 Mbps
1× SPI 20M:     1 × 80     =    80   Mbps
1× MDIO:        1 × 1.25   =     1.25 Mbps
1× JTAG 10M:   1 × 6.4    =     6.4 Mbps
3× Edge (IRQ):  3 × 0.6    =     1.8 Mbps
──────────────────────────────────────────────────────
TOTAL:                        ≈  111.5 Mbps  (14% GbE)
```

**Plenty of headroom** for the full debug interface suite.

---

## 6. Edge Detector Coverage

Several low-speed or simple protocols can be captured by the
**edge detector + SW decoder** instead of a dedicated HW datalogger:

| Protocol | Why Edge Datalogger Works | Limitation |
|:---------|:-----------------------|:-----------|
| 1-Wire | 16 kHz max toggle, timing-based | SW must measure slot widths |
| LIN | 20 kbps = slow UART | UART datalogger is even better |
| CAN (basic) | 1 Mbps toggle | No error frame detection |
| PWM | Pure timing capture | — |
| GPIO / IRQ | Sporadic events | — |
| Chip select lines | Level monitoring | — |

This means the 5 edge detector instances cover at least 6 additional
"protocols" with zero additional HW cost.

---

## 7. Implementation Roadmap

| Phase | Interfaces | BW Added | Pins Added |
|:------|:-----------|:---------|:-----------|
| **Done** | UART, I2C, SPI, Edge, Timestamp | ~420 Mbps | 34 |
| **Phase 3** | QSPI, I2S | +832 Mbps | 12 |
| **Phase 4** | MDIO, JTAG | +7.7 Mbps | 6 |
| **Phase 5** | SWD, CAN | +7.25 Mbps | 3 |
| **Future** | SDIO, RMII (burst mode) | +1,000 Mbps | 13 |
