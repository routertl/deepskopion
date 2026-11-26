# Release Notes

## 0.1.0

Facts verified against `9228d526` on 2026-11-24.

Deepskopion decodes traffic from several buses at once in the FPGA fabric, stamps every record from one hardware counter, and streams the records over TCP from a Linux daemon on the board.

### Targets

One target per FPGA vendor:

| Board | Target | Device | Toolchain | Status |
|---|---|---|---|---|
| Digilent Zybo Z7-20 | `zybo_datalogger` | AMD Zynq-7000 `xc7z020clg400-1` | Vivado 2024.1 | Supported |
| Terasic DE25 | `de25_datalogger` | Altera Agilex 5 | Quartus Prime Pro 25.3.1 | Supported |
| Microchip PolarFire SoC Discovery Kit | `pfsoc_datalogger` | Microchip PolarFire SoC `MPFS095T-1FCSG325E` | Libero SoC 2025.2 | Preview |

### Default data logger mix

Each target builds the same nine data loggers, read on the board's external header pins.

| Data logger | Count | Instances |
|---|---|---|
| UART | 2 | UART 12, UART 13 |
| I2C | 2 | I2C 4, I2C 5 |
| SPI | 1 | SPI 8 |
| CAN | 1 | CAN 20 |
| Edge | 3 | Edge 1, Edge 2, Edge 3 |

Other mixes, including QSPI and JTAG data loggers, are built with the configurator ([Data logger combination](getting_started/datalogger_mix.md)).

### Board-alone self-test

The SPI stimulus also arms the on-chip SPI responder, which answers each SPI byte on MISO. Requested together with the SPI stimulus, the SPI edge route is written before the first byte, so Edge 1, Edge 2 and Edge 3 see the transfer.

The default targets generate bus traffic for six of the nine data loggers, so those six capture with the board alone: Edge 1, Edge 2, Edge 3, I2C 4, SPI 8 and UART 12. I2C 5, UART 13 and CAN 20 have no board-alone source and need external traffic on their header pins. The reach is the same on all three boards and is measured in simulation by `test_selftest_coverage.py`; on the Zybo Z7-20 it is also measured on the board (see Tested on the board).

### Requirements

| Item | Version |
|---|---|
| RouteRTL | `routertl 5.7.0`, installed from PyPI |
| Python | `>= 3.11, < 3.15` |
| RouteWave | `2.4.0` |
| Vivado (Zybo Z7-20) | `2024.1`, ML Standard or ML Enterprise |
| Quartus Prime Pro (DE25) | `25.3.1` with patch 1.02 |
| Libero SoC (PolarFire SoC Discovery Kit) | `2025.2` |
| Host | x86_64 Linux, 24 GB RAM, 60 GB free disk |

### Tested on the board

| Board | Tested |
|---|---|
| Zybo Z7-20 | The image built by the README steps at `87d88829` (2026-11-24) boots from the microSD card, runs Python 3.12 and the capture daemon, and streams `wave_stream_v1`. The board-alone traffic of all six reachable data loggers decodes exactly: UART 12, I2C 4 and SPI 8 annotations; Edge 1 (the 4-edge vector, 200 / 320 / 280 ns), Edge 2 (the I2C write A0 AB CD 3E with four ACKs) and Edge 3 (SPI MOSI DE AD BE EF, MISO A5 5A C3 3C), decoded from the edge words. Evidence `79ad423a`. The same captures passed on the image built at `8b9dc7b5` (2026-11-18), evidence `b23c67ed`. |
| Zybo Z7-20 | The FPGA build follows the README with `routertl 5.7.0` from PyPI in a fresh virtual environment: every step through the bitstream exits 0 and the host check passes 13 of 13. The Linux image and board steps were not part of that run. |
| DE25 | A fresh clone at `8ce1c03d` with `routertl 5.6.0` from PyPI in a clean virtual environment builds the image, programs the board, boots Linux from the microSD card and streams `wave_stream_v1` from the capture daemon. The board-alone UART, I2C, SPI and Edge captures decode exactly and RouteWave 2.5.3 accepts each stream. The run sets `RR_NO_CONFINE_HINT=1`, because without it `rr` under an unconfined board lease prints a hint on standard output ahead of its `--json` output. Evidence `ea429091`. |
| PolarFire SoC Discovery Kit | Linux boots, the capture daemon runs, and the self-test traffic of all six board-alone data loggers decodes exactly: UART, I2C and SPI annotations, and the three edge vectors, including SPI MOSI DE AD BE EF and MISO A5 5A C3 3C (2026-11-19). |

### Known limitations

| Area | Limitation |
|---|---|
| Self-test reach | Three of the nine default data loggers need external traffic (see Board-alone self-test). |
| PolarFire SoC Discovery Kit | Preview target. At the first edge-logger enable after a cold power-on, the edge loggers recorded extra edges on the SPI MISO and MOSI lines around the transfer (2026-11-19 and 2026-11-24); the decoded SPI bytes were correct, and the nine repeat captures of 2026-11-19 showed none. A design change that clears the edge detector at enable is built and is not yet measured on the kit. |
| PolarFire SoC Discovery Kit | On the kit used for testing, 2 of 6 cold boots stopped before the Linux shell; after a few minutes powered off, a new cold boot reached it. |
| PolarFire SoC Discovery Kit | A build from a clone of the public repository is not tested. |
| DE25 | A fresh clone built by the README steps with `routertl 5.7.0` from PyPI stopped after synthesis on 2026-11-23: the memory gate reported the 4 declared block RAMs as not confirmed for Altera. A fix in RouteRTL is pending. The board, programmed with an image the maintainers built, boots and streams (see Tested on the board). |
| DE25 | Over a 2 s window the daemon drains the DDR ring at 19.4e6 to 20.1e6 B/s against a 20.0e6 B/s ring write (image built at `d2447f7e`, 2026-11-23). Over 65 s an earlier image (`2104e288`, 2026-11-11) drained 1.4e6 to 2.1e6 B/s less than the write, so a sustained full-rate write fills the 256 MiB ring after about 2 to 3 minutes; the 65 s measurement has not been repeated on the current drain code. |
| JTAG data logger | Records no live traffic on the DE25 and the PolarFire SoC Discovery Kit, whose tops tie the tap it reads to constants. Not part of the default mix. |
