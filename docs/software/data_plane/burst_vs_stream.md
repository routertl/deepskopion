# Burst vs Stream Paths

Burst and stream messages originate from different datalogger types in the PL but ultimately reside in DDR for software consumption.

## DDR Region Separation
To handle the unique traffic variances, these two message categories are routed to **Separate DDR Regions**:

1.  **Burst Buffer:** Optimized for sporadic, high-intensity events (I2C Frames, UART).
2.  **Stream Buffer:** Optimized for steady-state, isochronous data (I2S, ADC).

## Implementation Strategy
There are two ways to achieve this separation in hardware:

*   **Option 1: Dual DMA Channels (Preferred)**
    *   Two independent AXI DMA instances (or a multi-channel DMA).
    *   One channel targets the Burst Ring, the other targets the Stream Ring.
    *   **Pros:** Complete isolation. A flood of I2C interrupts cannot delay the I2S DMA write.
*   **Option 2: Single Channel with Tags**
    *   A single stream where software parses headers to sort data.
    *   **Cons:** Higher software overhead; risk of head-of-line blocking.

**Decision:** The architecture assumes **Dual DMA Channels** to enforce physical isolation of the streams in memory.

## Software Abstraction
Despite the physical separation, the user space software abstracts these as two logical sources:

```c
// Blocking or polling calls to retrieve the next available message
int capture_next_burst(struct message *msg);
int capture_next_stream(struct message *msg);
```

This ensures the analysis logic remains decoupled from the physical buffering strategy.
