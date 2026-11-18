# DMA and DDR Buffers

This page describes how datalogger messages travel from the FPGA fabric into Linux user space through DMA and DDR.

## Goals
1.  **Continuous Capture:** Zero data loss during sustained high-bandwidth events.
2.  **Bounded Latency:** Predictable time from PL event to User Space processing.
3.  **Minimal Overhead:** Zero-copy operations where possible.

## Buffer Model

The system uses a **Ring Buffer** in DDR, managed by the AXI DMA engine in Scatter-Gather mode.

### Structure
*   **Descriptors (N):** The ring consists of `N` Buffer Descriptors (BDs).
*   **Buffer Size (B):** Each descriptor points to a contiguous memory block of `B` bytes.
*   **Total Window:** `N * B` bytes of capture history.

### Operation
1.  **Write:** The DMA engine fills buffers in order. When a buffer is full (or a timeout occurs), it updates the descriptor status and moves to the next.
2.  **Interrupt:** An interrupt is fired (per buffer or per batch) to notify software.
3.  **Read:** User space checks for completed descriptors, processes the data, and then **returns the descriptor to the free pool** by clearing the status bit.

## Kernel vs User Space Control

There are two primary architectural choices for managing the DMA engine:

### Option A: Kernel Driver
*   **Mechanism:** Uses the Linux `DMAengine` subsystem.
*   **Interface:** Exposes a character device (`/dev/capture0`). User space calls `read()` or `mmap()`.
*   **Pros:** Standard Linux integration, better interrupt latency hiding.
*   **Cons:** Higher complexity in kernel space, potential copy overhead if not careful.

### Option B: User Space Control (Recommended Initial Approach)
*   **Mechanism:** Uses **UIO** (Userspace I/O) or **VFIO** to map the DMA Control Registers and the DDR Buffers directly into the process address space.
*   **Interface:** Direct register writes from C++ code.
*   **Pros:** Simpler to develop/debug (no kernel rebuilds), Zero-copy by definition.
*   **Cons:** Requires root privileges, less secure.

**Decision:** The project will start with **User Space Control** for simplicity and agility, moving to a Kernel Driver only if interrupt latency becomes a bottleneck.
