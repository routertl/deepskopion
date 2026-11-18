# Event Specifications: Domains vs Datalogger IDs

To maintain stability across builds while allowing flexible FPGA configurations, the system distinguishes between the **Type** of a datalogger and its **Instance** ID.

## 1. Concepts

### Datalogger ID (Dynamic)
*   **Definition:** A build-dependent, auto-generated unique identifier for a concrete instance (e.g., `UART0` = 1, `I2C1` = 5).
*   **Change Frequency:** Changes whenever the FPGA build configuration changes (e.g., adding a new UART).
*   **Use Case:** Routing specific data streams, checking "Is `UART0` active?".

### Domain / Event Code (Static)
*   **Definition:** A stable, build-independent enum that identifies the *kind* of protocol or event (e.g., `DOMAIN_UART`, `EVENT_UART_PARITY_ERROR`).
*   **Change Frequency:** Static. Defined once in the source code and rarely modified.
*   **Use Case:** Generic logic and Triggers (e.g., "Trigger on *any* UART Parity Error").

## 2. Event Code Structure

The **Event Code** is key to the Trigger Engine. In the new **Robust Common Header**, the event code is constructed or found as follows:

### Mapping to Header
*   **Domain (Bits 11:8):** Derived from the `msg_type` or fixed by the datalogger's configured domain (e.g., UART).
*   **Severity (Bits 15:12):** Derived from `msg_class` (`DATA`=Info, `STATUS`=Info/Error depending on payload, `ERROR`=Error).
*   **Event ID (Bits 7:0):** contained in `msg_type` for Control/Status messages.

*Note: For pure DATA messages (like UART Frame), the `msg_type` serves as the Event ID (e.g., `UART_FRAME_OK`).*

### Bit Layout

| Bits | Width | Field | Description |
| :--- | :--- | :--- | :--- |
| **[15:12]** | 4 | **SEVERITY** | Classification (Info, Error, Debug). |
| **[11:8]** | 4 | **DOMAIN** | Datalogger Type (UART, I2C, SPI...). |
| **[7:0]** | 8 | **EVENT_ID** | Specific event index (Start Bit, Parity Error...). |

### Severity Codes (Bits 15:12)

| Value | Name | Description |
| :--- | :--- | :--- |
| **0x0** | `INFO` | Normal protocol events (Frame OK, Start, Stop). |
| **0x1** | `WARN` | Non-fatal anomalies (Glitch detected). |
| **0x2** | `ERROR` | Violations (Parity error, NACK). |
| **0x3** | `DEBUG` | Verbose debug trace. |
| **0x4** | `CONTROL` | System control (Trigger Armed/Disarmed). |

### Domain Codes (Bits 11:8)

| Value | Name | Datalogger Block Type |
| :--- | :--- | :--- |
| **0x0** | `SYS` | System / Generic (FIFO, DMA). |
| **0x1** | `UART` | UART dataloggers. |
| **0x2** | `I2C` | I2C dataloggers. |
| **0x3** | `SPI` | SPI dataloggers. |
| **0x4** | `I2S` | I2S / Audio. |
| **0x5** | `QSPI` | Quad-SPI. |
| **0x6** | `EDGE` | Edge / Movement Detector. |
| **0x7** | `TRIG` | Trigger Engine Logic. |

## 3. Concrete Event Codes

This list serves as the **Global API** reference for VHDL and C headers.

### System / Generic (Domain 0x0)
| Code | Name | Description |
| :--- | :--- | :--- |
| **0x0001** | `SYS_HEARTBEAT` | Periodic alive signal. |
| **0x0002** | `SYS_START_CAPTURE` | Capture session started. |
| **0x2001** | `SYS_FIFO_OVERFLOW` | Data loss due to full Burst FIFO. |
| **0x2004** | `SYS_TIMESTAMP_WRAP` | 64-bit timer rollover. |

### UART (Domain 0x1)
| Code | Name | Severity | Description |
| :--- | :--- | :--- | :--- |
| **0x0100** | `UART_FRAME_OK` | INFO | Valid character received. |
| **0x0102** | `UART_BREAK_DETECTED` | INFO | Line held low > frame time. |
| **0x2100** | `UART_PARITY_ERROR` | ERROR | Parity calculation mismatch. |
| **0x2101** | `UART_FRAMING_ERROR` | ERROR | Missing stop bit. |
| **0x2102** | `UART_OVERRUN_ERROR` | ERROR | Internal datalogger buffer full. |

### I2C (Domain 0x2)
| Code | Name | Severity | Description |
| :--- | :--- | :--- | :--- |
| **0x0200** | `I2C_START` | INFO | Start condition detected. |
| **0x0202** | `I2C_STOP` | INFO | Stop condition detected. |
| **0x0204** | `I2C_WRITE_DATA` | INFO | Byte written to slave. |
| **0x2200** | `I2C_NACK` | ERROR | Slave did not acknowledge byte. |
| **0x2201** | `I2C_ARB_LOST` | ERROR | Master lost arbitration. |

### SPI (Domain 0x3)
| Code | Name | Severity | Description |
| :--- | :--- | :--- | :--- |
| **0x0300** | `SPI_CS_ASSERT` | INFO | CS goes low. |
| **0x0302** | `SPI_XFER_COMPLETE` | INFO | Full word transferred. |
| **0x2300** | `SPI_CLK_GLITCH` | ERROR | Noise on SCK line. |

### Edge Detector (Domain 0x6)
| Code | Name | Severity | Description |
| :--- | :--- | :--- | :--- |
| **0x0600** | `EDGE_EVENT` | INFO | Single-event payload — per-frame edge transition (legacy / framer default). Payload is one 32-bit `edge_event` word from the datalogger (see [`payloads/edge_detector.yaml`](payloads/edge_detector.yaml)). |
| **0x0601** | `EDGE_BATCH` | INFO | Batched payload — N × `edge_event_t` packed in one zs_msg frame. Header `timestamp` anchors the batch; per-entry `ts_delta` is a 16-bit cycle count. See [`payloads/edge_batch.md`](payloads/edge_batch.md). |
| **0x0602** | `EDGE_MAXFREQ` | INFO | FPGA-authoritative max-frequency datum for one line — the smallest validated edge-to-edge interval the `edge_datalogger` latched. 8-byte payload: `u32 min_interval` (CLK ticks; all-ones in `ival_width` bits = no edge yet), `u8 channel` (== header `datalogger_id`), `u8 ival_width`, `u16 flags`. `freq = clk / (2 * interval)`. In-band replacement for the reverted `MAXFREQ_IVAL_EDGE<k>` regbank CSR slots. See `sw/stream/zs_msg.py` pack/parse_edge_maxfreq. |
| **0x0608** | `EDGE_LIFECYCLE` | INFO | An `edge_datalogger` was enabled or disabled (lifecycle frame). Payload: the 32-bit lifecycle word, byte 0 = code (`0x03` ENABLED, `0x00` DISABLED). Framed on its own, never as an `EDGE_BATCH` entry: its type `100` equals S=1/F=0/E=0, which a batch entry would carry as a readback of level 0. `0x08` is outside the `0x00`–`0x07` S/F/E tags the gather puts on edge data words. |
| **0x0609** | `EDGE_RLE` | INFO | One `edge_datalogger` Stream-RLE word (CONFIG[4:3] = `10`). Payload: the 32-bit word, LE. Bits [26:24] are the RLE word type: `000`/`001` SYNC (line low/high, [23:0] = TIMESTAMP[23:0]), `101` PACK (three 8-bit edge deltas, delta_0 oldest, `0x00` = empty slot), `110` LONG (one edge, [23:0] = delta), `111` IDLE (no edge, [23:0] ticks elapsed). Framed on its own, never as an `EDGE_BATCH` entry, where the word type would read as S/F/E. Decoder: `sw/stream/edge_capture.py` `EdgeRleDecoder`. |

### Trigger Engine (Domain 0x7)
| Code | Name | Severity | Description |
| :--- | :--- | :--- | :--- |
| **0x0700** | `TRIG_ARMED` | INFO | Trigger logic active. |
| **0x4700** | `TRIG_MATCH` | CONTROL | **Trigger Condition Fired.** |

## 3. Generation Strategy

### Static (Repo Committed)
The Domain and Event Enums are fixed constant files:
*   **VHDL:** `C_DOMAIN_UART = x"1"`, `C_EVENT_UART_PARITY = x"2100"`
*   **C/Python:** Matching header files.
This is the **Global API**.

### Dynamic (Build Generated)
From the project YAML configuration, a script generates the Instance IDs:
*   **Input:** `dataloggers: [uart0, uart1, i2c0]`
*   **Output (C/VHDL):**
    *   `#define DATALOGGER_ID_UART0 1`
    *   `#define DATALOGGER_ID_UART1 2`
    *   `#define DATALOGGER_ID_I2C0 3`

This ensures that while IDs float, the event logic in software remains stable content.
