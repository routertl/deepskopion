# Data Plane: Capture Path

This section covers the high-bandwidth data path from FPGA to User Space.

## Detailed Topics

*   [DMA and DDR Buffers](dma_ddr_buffers.md) — Ring buffer layout, descriptor management, and zero-copy access.
*   [Global Timing Model](global_timing_model.md) — System-wide timestamp source and resolution.
*   [Trigger Engine Architecture](trigger_architecture.md) — Hardware trigger conditions and arming logic.
*   [Burst vs Stream Paths](burst_vs_stream.md) — Differences between burst and continuous capture modes.
*   [Stacked Decoders](stacked_decoders.md) — PS decode layers that consume another lane's events.
*   [Message Formats](message_formats/overview.md) — Common header, payload structure, and datalogger/event IDs.

### Real Time Constraints

Analysis of required bandwidth, max latency permitted before buffer overflow,
and strategies for deep buffering in the Zynq PS DDR.
Key considerations:
*   Maximum sustained throughput per datalogger channel.
*   Acceptable latency before ring-buffer wrap-around.

### User Space Capture API

The high-performance C/C++ interface for reading data from the `dataloggerd`
shared usage buffers or via the network socket.
Key capabilities:
*   Zero-copy buffer access via `mmap`.
*   Callback and polling modes for data consumption.
