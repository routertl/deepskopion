# Protocol Dataloggers

This directory contains protocol datalogger modules for the Deepskopion logic analyzer.

## Documentation Status

| Datalogger | RTL | Protocol Spec | YAML | Features Doc | Tests | Coverage |
| ------- | --- | ------------- | ---- | ------------ | ----- | -------- |
| **I2C** | ✅ | ✅ | ✅ | ✅ | ✅ 4/6 | ✅ |
| **QSPI** | ✅ | ✅ | ✅ | ✅ | ⚠️ | 🔲 |
| **UART** | ✅ | ✅ | ✅ | ✅ | ✅ 12/14 | ✅ |
| **Edge** | ✅ | ✅ | ✅ | ✅ | ✅ | 🔲 |
| **Level** | ✅ | 🔲 | 🔲 | 🔲 | ✅ | 🔲 |

### Legend

- ✅ Complete
- ⚠️ Partial
- 🔲 Not started

## Directory Structure

```text
datalogger/
├── i2c/                    # I2C datalogger (reference implementation)
│   ├── i2c_datalogger.vhd
│   ├── I2C_DATALOGGER_PROTOCOL.md
│   ├── i2c_datalogger_protocol.yml
│   └── I2C_DATALOGGER_FEATURES.md
├── qspi/                   # QSPI flash datalogger
│   ├── qspi_datalogger.vhd
│   ├── QSPI_DATALOGGER_PROTOCOL.md
│   ├── qspi_datalogger_protocol.yml
│   └── QSPI_DATALOGGER_FEATURES.md
├── uart/                   # UART datalogger (auto-calibrating)
│   ├── uart_datalogger.vhd
│   ├── uart_baudrate_gen.vhd
│   ├── uart_frame_gen.vhd
│   ├── uart_playback.vhd
│   ├── uart_stop_finder.vhd
│   ├── UART_DATALOGGER_FEATURES.md
│   └── UART_DATALOGGER_PROTOCOL.md
├── nonstd/                 # Non-standard protocol dataloggers
│   ├── edge_datalogger.vhd
│   ├── EDGE_DATALOGGER_PROTOCOL.md
│   ├── edge_datalogger_protocol.yml
│   ├── EDGE_DATALOGGER_FEATURES.md
│   └── level_datalogger.vhd
├── docs/                   # Cross-datalogger architecture docs
│   └── edge_detector_v2_architecture.md
└── infra/                  # Shared infrastructure
```

## Documentation Guide

See [docs/standards/datalogger_documentation_guide.md](../../../docs/standards/datalogger_documentation_guide.md) for templates and requirements.
