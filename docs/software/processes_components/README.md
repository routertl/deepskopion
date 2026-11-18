# Processes and Components

The implementation details of the software stack running on the Zynq ARM Core.

*   [Overview / Architecture](overview.md)
*   [User Space Daemon](user_space_daemon/README.md)

### Kernel Driver

Documentation for the custom Linux kernel module or UIO driver that exposes the AXI DMA engine and physical memory to user space.

### Library API (`libdatalogger`)

A shared library that abstracts the daemon protocol.

*   **Configuration API:** `datalogger_set_voltage()`, `datalogger_enable_protocol()`...
*   **Capture API:** `datalogger_read_frame()`...
*   **Callback Interface:** `datalogger_register_callback(ON_DATA_frame)`...

### CLI Tools

CommandLine Interface tools for debugging and headless operation.

*   `dataloggerctl`: Configuration tool (set voltages, enable dataloggers).
*   `dataloggerdump`: Packet logger (like tcpdump but for logic data).
*   `dataloggerstats`: View packet counters and buffer usage.
*   `dataloggerreplay`: Replay captured data back into the analysis pipeline.

### Integration Points

*   **PCAP Export:** Converting Burst traffic (like Ethernet or WiFi Datalogger data) to Standard PCAP for Wireshark.
*   **CSV/JSON:** Exporting decoded frames for spreadsheets or web dashboards.
