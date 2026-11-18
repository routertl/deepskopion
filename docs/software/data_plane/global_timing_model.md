# Global Timing Model

This document defines the time-keeping strategy for the entire capture system.

## 1. Parameters

*   **F_SYS_CLK_HZ:** 100 MHz (Typical FPGA Fabric Clock).
*   **TIMESTAMP_TICK_US:** 1 Microsecond (Default resolution).
    *   *Logic:* The fast system clock is pre-scaled to generate this tick.

## 2. Timestamp Source

*   **Mechanism:** A single global free-running counter in the FPGA (PL).
*   **Synchronization:** This counter is broadcast to all datalogger blocks. All dataloggers sample this value simultaneously when they detect a relevant event (Start bit, Edge, etc.).

## 3. Data Format

The timestamp is transmitted in the **Common Message Header**.

*   **Container Size:** 64 bits (Fixed in header).
*   **Effective Width:** 48 bits (Recommended default).
    *   **Range:** 2^48 microseconds ≈ 8.9 years. This prevents aliasing issues during long capture sessions.
    *   **Upper Bits:** Bits [63:48] are reserved (zero).
*   **Optimization Note:** While the container is 64-bit to maintain 16-byte header alignment, the effective logic width can be reduced to 32 bits (Range ≈ 71 minutes) if FPGA resource minimization is critical.
