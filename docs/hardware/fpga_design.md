# FPGA Design and Datalogger Architecture

The FPGA (PL) side of Deepskopion is responsible for real-time signal acquisition, protocol interpretation, and message encapsulation.

## The Datalogger Block Concept
To allow seamless addition of new protocols, the architecture uses a standardized "Datalogger Block" interface. Each protocol (I2C, SPI, etc.) is implemented as an independent RTL module.

### Responsibilities
1.  **Signal Sampling:** Oversampling the physical lines (SDA/SCL, MOSI/MISO, etc.).
2.  **Protocol State Machine:** Tracking the state of the bus (Idle, Start Bit, Data, ACK, Stop).
3.  **Data Extraction:** Converting serial bits into bytes/words.
4.  **Timestamping:** Tagging events with a high-precision global timestamp.
5.  **Message Packing:** Formatting the captured data into the system's standard message format.

## Clock Domains
The design operates across two primary clock domains:
1.  **Sampling Domain:** High-speed clock where dataloggers run, signal edges are detected, and timestamps are latched.
2.  **Processing Domain:** Lower-speed (or AXI-synchronous) clock for message assembly, FIFO management, and DMA output.

*Clock Synchronization:* Crossings are handled via clean synchronizers or dual-clock FIFOs to ensure metastability-free operation.

## Data Transport & Buffering

### Burst vs. Stream Separation
To optimize throughput and simplify DMA descriptors, data is split into two logical paths:
*   **Burst Path:** For unpredictable, packet-based events (I2C, UART, SPI transfers).
    *   *Why:* These events are sporadic. Independent buffering prevents a sudden burst of I2C traffic from blocking timing-critical audio data.
*   **Stream Path:** For continuous, steady-rate data (I2S, ADC streams).
    *   *Why:* These require guaranteed bandwidth and low jitter.

### FIFO Architecture
*   **Depths:** FIFO sizes are calculated based on the worst-case protocol speeds + maximum DMA latency.
*   **Arbitration:** The DMA engine services these FIFOs based on fullness and priority, ensuring neither path starves.

## Timing Guarantees
*   **Latency:** Maximum timestamp latency is deterministic, bounded by the pipeline depth of the Message Builder.
*   **Independence:** The dual-path architecture guarantees that Burst traffic cannot starve Stream traffic, and vice-versa.

## Level Shifter Control

The hardware features **Programmable Level Shifters** on the front-end to support multiple signal standards (1.2V, 1.5V, 1.8V, 2.5V, 3.3V).

*   **Mechanism:** The FPGA pins themselves utilize a single, fixed logic standard (e.g., LVCMOS33).
*   **Translation:** External level shifter ICs (controlled by the Zynq logic) translate the user-side signals to this fixed FPGA voltage.
*   **User Control:** The software selects the desired external voltage, which the Zynq converts into control signals for the shifter ICs.
*   **Benefit:** Allows the FPGA to sample diverse logic families without requiring multiple complex I/O banks or hardware reconfiguration.
