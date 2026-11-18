# Deepskopion Documentation

Documentation index for the Deepskopion FPGA-accelerated protocol datalogger and logic analyzer.

## Getting Deepskopion running

| Doc | Description |
|-----|-------------|
| [SUPPORTED_ENVIRONMENT.md](SUPPORTED_ENVIRONMENT.md) | What we support — host, toolchain, board, JTAG, network, power, PMOD, traffic source |
| [DEPLOYMENT_CONTRACT.md](DEPLOYMENT_CONTRACT.md) | How the capture daemon reaches a board and starts there — the autostart mechanism, what is tracked vs staged |

## Architecture

| Doc | Description |
|-----|-------------|
| [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md) | System architecture and data flow |
| [hardware/fpga_design.md](hardware/fpga_design.md) | FPGA design and datalogger blocks |
| [software/README.md](software/README.md) | Software architecture overview |
| [hardware/datalogger_builder_flow.md](hardware/datalogger_builder_flow.md) | YAML → VHDL code generation |

## Datalogger Standards

| Doc | Description |
|-----|-------------|
| [datalogger_documentation_guide.md](standards/datalogger_documentation_guide.md) | Templates and requirements |
| [datalogger_compliance_checklist.md](standards/datalogger_compliance_checklist.md) | 9-category validation matrix |

## Project Management

| Doc | Description |
|-----|-------------|
| [REQUIREMENTS_ROADMAP.md](REQUIREMENTS_ROADMAP.md) | System requirements and verification |
| [GITHUB_SYNC.md](GITHUB_SYNC.md) | GitHub synchronization architecture and bidirectional sync workflow |

## Design Documents

| Doc | Description |
|-----|-------------|
| [pdl_architecture.md](design/pdl_architecture.md) | Protocol Description Language |

## Migration & History

| Doc | Description |
|-----|-------------|
| [sdk_v3_migration_report.md](migration/sdk_v3_migration_report.md) | RouteRTL SDK v3.0 migration |

## Conventions

| Scope | Convention | Example |
|-------|-----------|---------|
| Top-level reference docs | `SCREAMING_SNAKE.md` | `REQUIREMENTS_ROADMAP.md` |
| Subdirectory content docs | `lower_snake.md` | `hardware/fpga_design.md` |
| Directory index files | `README.md` | `software/README.md` |

**Top-level** = files directly in `docs/` (guides, standards, roadmaps, reports).
**Subdirectory** = files under `docs/*/` (architecture details, flows, specs).

## See Also

- Protocol datalogger docs: [hw/rtl/datalogger/README.md](https://github.com/routertl/deepskopion/blob/main/hw/rtl/datalogger/README.md)
- RouteRTL SDK: [routertl](https://github.com/djmazure/routertl) (pip-installed; use `rr sdk-path` to locate)
