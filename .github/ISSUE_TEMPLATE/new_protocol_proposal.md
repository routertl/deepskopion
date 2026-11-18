---
name: New Protocol Proposal
about: Propose a new hardware protocol datalogger for Deepskopion
title: "[PROTOCOL] "
labels: enhancement, protocol
assignees: ''
---

### Protocol Overview
- **Protocol Name**: [e.g. EtherCAT, LIN, SENT, Modbus RTU]
- **Specification / Standard Reference**: [e.g. IEC 61158, SAE J2716, ISO 17987]
- **Target Application & Motivation**: Why is this datalogger needed and what multi-protocol debug scenario does it unlock?

### Physical Layer & Signaling Requirements
- **Signaling Type**: [Single-ended / Differential (e.g. LVDS, RS-485, CAN PHY, Ethernet MII/RMII)]
- **Pin Count & Roles**: [e.g. TX, RX, CLK, CS, DE]
- **Clocking & Data Rates**: [e.g. Fixed 100 Mbps, Variable 9600–115200 bps, Asynchronous with oversampling]
- **I/O Conditioning Needed**: [e.g. Glitch filter, debounce, edge detection, CDC synchronization]

### Framing, Decoding & Payload Representation
- **Packet Structure**: Preamble, header fields, address, data payload, CRC/checksum, delimiter/trailer.
- **Error Detection States**: Parity error, framing error, CRC failure, timeout, bus collision.
- **Wire Stream Output**: How events and bytes should be formatted into `wave_stream_v1` annotations or sample streams.

### Estimated FPGA Resource Budget
- **Estimated Logic**: [LUTs / ALMs]
- **Estimated Registers**: [FFs]
- **Estimated Memory**: [BRAM / M20K blocks for FIFOs or buffers]

### In-System Self-Test & Verification Plan
- **Loopback / Stimulus Source**: How will the protocol datalogger be self-tested without external instruments? (e.g. EMIO loopback from Linux driver, RTL stimulus generator in fabric).
- **Testbench Plan**: Cocotb / NVC simulation coverage.
- **Hardware Validation Board**: Which board will run the hardware contract?
