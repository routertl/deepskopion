# Software Architecture Overview

This section describes the software side of the Deepskopion project. The goal is to provide a clear view of how Linux, DMA, configuration, and user space tools interact with the FPGA fabric.

## Objectives

The software stack has three main goals:

1.  **Configure the hardware**
    *   Discover capabilities from the FPGA, configure dataloggers, pins, and thresholds, and expose a clean API for doing so.
2.  **Capture and deliver data**
    *   Efficiently move datalogger messages from PL DDR buffers into user space, with minimal CPU overhead, preserving timestamps and ordering.
3.  **Provide usable tools**
    *   Offer command line tools and libraries that allow:
        *   Live viewing of captured traffic
        *   Logging to disk (PCAP or custom formats)
        *   Integration with higher-level analysis scripts or GUIs

## High Level Components

The software side consists of:

*   **Kernel Interface** (optional, depending on implementation)
    *   Either a custom kernel driver using DMAengine.
    *   Or a user space driver using UIO / VFIO style access.
*   **User Space Daemon**
    *   Owns the DMA ring and DDR buffers.
    *   Polls or receives interrupts from DMA.
    *   Parses raw messages into structured records.
    *   Exposes data via sockets, shared memory, or a simple API.
*   **Configuration Library**
    *   Talks to the FPGA register bank.
    *   Reads the YAML-like manifest exposed from BRAM.
    *   Provides typed accessors to configuration fields and status counters.
*   **Tools and Utilities**
    *   CLI tools for start / stop capture.
    *   Tools to dump traffic to files (PCAP, CSV, JSON).
    *   Tools to query status, version, build information.

## Control Plane vs Data Plane

The architecture is split into two distinct paths:

### 1. [Control Plane](control_plane/README.md)
Low bandwidth, side-band operations:
*   Setting datalogger enable bits.
*   Mapping pins to dataloggers.
*   Reading counters and movement detectors.
*   Querying firmware version and build.

### 2. [Data Plane](data_plane/README.md)
High bandwidth continuous path:
*   PL writes messages into DDR through DMA.
*   Software consumes messages from a ring buffer.
*   Messages are parsed and forwarded to tools.

This separation keeps the critical capture path simple and predictable while allowing flexible configuration on the side.

## Key Interfaces

*   **PL to PS Data:** AXI DMA writes to DDR buffers, described by DMA descriptors.
*   **PS to FPGA Control:** Memory mapped registers, described in the YAML register bank schema.
*   **User Space API:** A small library that wraps configuration read/write, capture session control, and the message iterator/callback interface.

## Deployment

How this software reaches a board and starts there — the one autostart
mechanism, where the daemon source lives, what is tracked versus staged — is
the [deployment contract](../DEPLOYMENT_CONTRACT.md) (SW-C0). It is declared in
`sw/linux/deployment_contract.yml` and enforced against the overlay at image
build time, so it is the authoritative answer rather than one description among
several.

## Detailed Documentation

*   [Deployment Contract](../DEPLOYMENT_CONTRACT.md)
*   [Control Plane](control_plane/README.md)
*   [Data Plane](data_plane/README.md)
*   [Processes and Components](processes_components/README.md)

### Testing and Tooling

*   **Synthetic Traffic Generator:** Logic to generate fake I2C/SPI patterns within the FPGA for self-test.
*   **Capture Replay Tools:** Software to inject recorded files back into the decoder stack.
*   **Python Scripts:** Rapid prototyping and test automation.
*   **Profiling:** Tools to measure bandwidth and CPU usage.
