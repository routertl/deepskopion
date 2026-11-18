# Efficient FPGA Edge Capture and Bandwidth Optimization

## 1. Don't Stream Raw Samples if You Only Need Edges

If your goal is to represent **edges + timestamps**, the most
bandwidth-efficient representation is:

-   **(Δt, new_level)** per transition\
-   or **timestamp of edge** with edge polarity

This is essentially **run-length encoding (RLE)** of the waveform and is
close to optimal for sparse transitions.

------------------------------------------------------------------------

## 2. The Hard Bandwidth Floor at 40 MHz

For a 40 MHz square wave:

-   Period = 25 ns\
-   Edges per second = 2 × 40 MHz = **80 million edges/s**

At this rate, compression gains are limited because the signal changes
constantly.

------------------------------------------------------------------------

## 3. Bandwidth Comparison of Encoding Schemes

### A) Raw Sampling (1 bit @ 100 MHz)

-   100M samples/s × 1 bit = **100 Mb/s = 12.5 MB/s** per channel

Packing multiple channels into words improves efficiency.

------------------------------------------------------------------------

### B) Edge List with Full Timestamps

Example record: - 32-bit timestamp\
- 8-bit edge/channel info

\~40--64 bits per edge.

At 80M edges/s: - 48 bits × 80M = **3.84 Gb/s = 480 MB/s** - 64 bits ×
80M = **5.12 Gb/s = 640 MB/s**

Worse than raw sampling for continuous clocks.

------------------------------------------------------------------------

### C) Edge List with Δt Compression

At 40 MHz sampled at 100 MHz: - Δt ≈ 1 or 2 cycles typically

If encoded in \~3 bits/edge: - 3 × 80M = **240 Mb/s = 30 MB/s**

Better than full timestamps, but still more than raw sampling for a
single high-speed clock.

------------------------------------------------------------------------

## 4. Practical Rule of Thumb

-   **Bursty / mostly-idle signals** → Edge + Δt encoding wins big\
-   **Continuous high-frequency clocks** → Packed raw sampling is often
    better

------------------------------------------------------------------------

## 5. Practical Transport Formats

### Option 1: Packed Samples

-   Sample every cycle\
-   Pack N channels into N-bit words\
-   Transmit continuously

Bandwidth:

    BW = f_clk × N_channels / 8

Example: 16 channels @ 100 MHz → **200 MB/s**

------------------------------------------------------------------------

### Option 2: Transition Stream with Variable-Length Δt

On edge detection: - Compute `dt = now - last_change_time` - Output
`(dt, level, channel)`

Use variable-length encoding: - 1 byte if dt \< 256\
- 2 bytes if dt \< 65536\
- 4 bytes otherwise

Very efficient for sparse signals.

------------------------------------------------------------------------

### Option 3: Block RLE over Packed Samples (Hybrid)

-   Pack samples per cycle into a word\
-   Compare with previous word\
-   If unchanged, increment counter\
-   When changed, emit `(count, word)`

Advantages: - Huge compression during idle\
- Falls back to near-raw throughput during fast toggling\
- Simple RTL implementation

------------------------------------------------------------------------

## 6. Sampling Limits at 100 MHz

With 100 MHz sampling: - Nyquist limit ≈ 50 MHz\
- A 40 MHz square wave can be represented\
- Edge placement uncertainty ≈ ±10 ns\
- Duty cycle distortion possible

If higher timing precision is required: - Increase sampling clock\
- Use IDDR/ISERDES\
- Use faster PLL-derived clock

------------------------------------------------------------------------

## 7. Recommended Approach

For a flexible FPGA datalogger system:

-   Use **block RLE over packed samples** as a universal format\
-   Optionally implement **Δt edge encoding** for sparse channels\
-   Allow fallback to raw mode for high toggle rates

Simple RLE-style compression provides excellent bandwidth reduction with
minimal LUT cost. No complex algorithms like LZ are required in FPGA
fabric.

------------------------------------------------------------------------

## 8. Final Guidance

-   Bursty protocols → Δt-based edge encoding\
-   Continuous clock-like signals → Packed sampling\
-   Mixed workloads → Hybrid RLE approach

The best solution depends on signal characteristics, not just frequency.
