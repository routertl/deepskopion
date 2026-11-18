# Control Plane: Configuration

This section details how the software configures the FPGA dataloggers and manages the system state.

*   [Register Bank Access](register_bank_access.md)
*   [YAML Manifest and Discovery](yaml_manifest.md)

### Pin and Datalogger Configuration Flow

Typical configuration sequence for initializing the system:

1.  **Discovery:** Read manifest and version registers to identify available capabilities.
2.  **Global Setup:** Configure global parameters (timestamp scaling, master clocks).
3.  **Routing:** Configure pin mapping (Pin Muxing) or datalogger routing logic (which pins go to which datalogger).
4.  **Datalogger Enable:** Enable selected dataloggers (e.g., Enable I2C Datalogger #0).
5.  **Reset:** Clear counters and status flags if needed to ensure a clean state.

This sequence can be wrapped into a single "apply configuration" API for user tools.

### System Status and Counters

*   **Heartbeats:** FPGA alive signals.
*   **Packet Counters:** Total packets captured, dropped packets (FIFO overflow).
*   **Error Flags:** Protocol violations (e.g., I2C missing ACK).

### Error Handling and Validation

Strategies for recovering from:
*   DMA Desynchronization.
*   FIFO Overflows (Burst traffic exceeding bandwidth).
*   Invalid Configuration requests.
