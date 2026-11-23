# Release Notes

## Unreleased

### Board-alone self-test

The SPI stimulus also arms the on-chip SPI responder, which answers each SPI byte on MISO. Requested together with the SPI stimulus, the SPI edge route is written before the first byte, so Edge 1, Edge 2 and Edge 3 see the transfer.

The default targets generate bus traffic for six of the nine data loggers: Edge 1, Edge 2, Edge 3, I2C 4, SPI 8 and UART 12. I2C 5, UART 13 and CAN 20 need external traffic on their header pins. The reach is the same on all three boards and is measured in simulation by `test_selftest_coverage.py`.

## 0.1.0

Facts verified against `c574f0d5` on 2026-11-17.

Deepskopion decodes traffic from several buses at once in the FPGA fabric, stamps every record from one hardware counter, and streams the records over TCP from a Linux daemon on the board.

### Targets

One target per FPGA vendor:

| Board | Target | Device | Toolchain | Status |
|---|---|---|---|---|
| Digilent Zybo Z7-20 | `zybo_datalogger` | AMD Zynq-7000 `xc7z020clg400-1` | Vivado 2024.1 | Supported |
| Terasic DE25 | `de25_datalogger` | Altera Agilex 5 | Quartus Prime Pro 25.3 | Supported |
| Microchip PolarFire SoC Discovery Kit | `pfsoc_datalogger` | Microchip PolarFire SoC `MPFS095T-1FCSG325E` | Libero SoC 2025.2 | Preview |

### Default data logger mix

Each target builds the same nine data loggers, read on the board's external header pins.

| Data logger | Count | Instances |
|---|---|---|
| UART | 2 | UART 12, UART 13 |
| I2C | 2 | I2C 4, I2C 5 |
| SPI | 1 | SPI 8 |
| CAN, CAN FD | 1 | CAN 20 |
| Edge | 3 | Edge 1, Edge 2, Edge 3 |

Other mixes, including QSPI and JTAG data loggers, are built with the configurator (README section "Choosing a Datalogger Mix").

### Board-alone self-test

The default targets generate bus traffic for five of the nine data loggers, so those five capture with the board alone: Edge 1, Edge 2, I2C 4, SPI 8 and UART 12.

The other four need external traffic on their header pins:

| Data logger | Reason |
|---|---|
| Edge 3 | The SPI stimulus is master-only, so the SPI MISO line that Edge 3 reads under the SPI edge route carries no data. |
| I2C 5 | No board-alone source. |
| UART 13 | No board-alone source. |
| CAN 20 | No board-alone source. Under the SPI edge route this input receives the SPI chip-select, which is not CAN traffic. |

The reach is the same on all three boards and is measured in simulation by `test_selftest_coverage.py`.

### Requirements

| Item | Version |
|---|---|
| RouteRTL | `routertl 5.6.0`, installed from PyPI |
| Python | `>= 3.11, < 3.15` |
| RouteWave | `2.4.0` |
| Vivado (Zybo Z7-20) | `2024.1`, ML Standard or ML Enterprise |
| Quartus Prime Pro (DE25) | `25.3` |
| Libero SoC (PolarFire SoC Discovery Kit) | `2025.2` |
| Host | x86_64 Linux, 24 GB RAM, 60 GB free disk |

### Verified on silicon

| Board | Verified |
|---|---|
| Zybo Z7-20 | The image built by the README steps boots from the microSD card, runs Python 3.12 and the capture daemon, and streams `wave_stream_v1`. Board-alone records verified per data logger: UART 12, I2C 4, SPI 8, Edge 1 and Edge 2. |
| Zybo Z7-20 | CAN FD decoding, against CAN FD traffic generated inside the FPGA. |
| DE25 | A fresh clone at `8ce1c03d` with `routertl 5.6.0` from PyPI in a clean virtual environment builds the image, programs the board, boots Linux from the microSD card and streams `wave_stream_v1` from the capture daemon. The board-alone UART, I2C, SPI and Edge captures decode exactly and RouteWave 2.5.3 accepts each stream. The run sets `RR_NO_CONFINE_HINT=1`, because without it `rr` under an unconfined board lease prints a hint on standard output ahead of its `--json` output. Evidence `ea429091`. |
| PolarFire SoC Discovery Kit | Linux boots and the capture daemon runs. The self-test capture is not verified on silicon. |

### Known limitations

| Area | Limitation |
|---|---|
| CAN FD | Capture of a CAN FD signal from the external header pins is not tested. Decoding is verified only against traffic generated inside the FPGA. |
| Self-test reach | Four of the nine default data loggers need external traffic (see Board-alone self-test). |
| PolarFire SoC Discovery Kit | Preview target: the self-test capture is not verified on silicon. |
| PolarFire SoC Discovery Kit | A cold boot can stop after DDR training because of an SD-card clock setting. A warm reset of the microprocessor subsystem recovers it. |
| DE25 | The daemon drains the DDR ring at about 13.1e6 B/s. A sustained ring write at 20.0e6 B/s fills the ring in about 39 s, after which records are lost upstream. |
| JTAG data logger | Records no live traffic on the DE25 and the PolarFire SoC Discovery Kit, whose tops tie the tap it reads to constants. Not part of the default mix. |
