# User Space Daemon (`dataloggerd`)

The user space daemon (`dataloggerd`) is the long-running process responsible for acting as the central manager of the Deepskopion acquisition system.

## Responsibilities

1.  **Hardware Initialization:**
    *   Read the FPGA Manifest (BRAM).
    *   Configure default dataloggers and routing.
    *   Start and reset the DMA channels.

2.  **Core Capture Loop:**
    *   Owns the DMA Ring and DDR Buffers.
    *   Waits on DMA completion (polling or interrupt-wait).
    *   Parses completed buffers into structured messages.
    *   Dispatches messages to connected clients or local storage.

3.  **IPC Interface:**
    *   Exposes a Unix Domain Socket (or TCP) for control.
    *   Implements a Request/Response API for clients (CLI, GUI).

## API Outline

The daemon exposes a high-level command API to clients:

*   `CAPTURE_START`: Begin data acquisition.
*   `CAPTURE_STOP`: Halt acquisition.
*   `SUBSCRIBE {burst | stream | both}`: Request real-time data feed for a specific channel type.
*   `GET_STATS`: Query packet counters, buffer usage, and dropped packet counts.
*   `SET_CONFIG {key=value}`: Update a register or parameter (e.g., `voltage=1.8`, `i2c0_baud=400k`).

*Note: The daemon internally translates these high-level commands into the specific Register Bank writes.*

## Why a Daemon?

Using a dedicated daemon simplifies the architecture:
*   **Decoupling:** Tools (CLI, GUI) do not need root privileges or direct access to physical memory/registers.
*   **Multiplexing:** Multiple clients can monitor status or receive data simultaneously.
*   **Remoting:** Future support for remote capture (over Ethernet) is trivial as the IPC interface can be bridged to TCP.
