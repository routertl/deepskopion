# Default data logger mix: header pins per board

Facts verified against deepskopion a47af5a9 and the RouteRTL board registry (`boards/<board>/pinout.yml`) on 2026-11-15.

The three product targets (`zybo_datalogger`, `de25_datalogger`, `pfsoc_datalogger`) build one data logger mix,
`tools/datalogger-builder/config/datalogger_config.yml`: 2 UART, 2 I2C, 1 SPI, 1 CAN FD and 3 edge data loggers, 14
inputs. Each input sits on an external header pin. The board pin names JA0..JA7 and JB0..JB7 are the core's `ja` and
`jb` inputs; each product top wires them to the package pins below.

Every header input has a weak pull-up, so an input with nothing attached reads high. UART RX, SPI CS and I2C all idle
high. Without a pull-up an undriven input reads low, and an enabled I2C data logger reports one stretch error per
25 ms.

`tools/tests/test_default_mix_pin_doc.py` holds these tables equal to the mix, the product targets and the board
registry that RouteRTL emits the location constraints from.

## Inputs

| Data logger | Input | Board pin |
|---|---|---|
| I2C 4 | SCL | JA0 |
| I2C 4 | SDA | JA1 |
| I2C 5 | SCL | JA2 |
| I2C 5 | SDA | JA3 |
| SPI 8 | CS | JA4 |
| SPI 8 | MOSI | JA5 |
| SPI 8 | MISO | JA6 |
| SPI 8 | SCLK | JA7 |
| UART 12 | TX | JB0 |
| UART 13 | TX | JB1 |
| EDGE 1 | LINE | JB2 |
| EDGE 2 | LINE | JB3 |
| EDGE 3 | LINE | JB4 |
| CAN 20 | RX | JB5 |

## Zybo Z7-20 (`zybo_datalogger`)

Headers: Pmod JA and Pmod JB. Pull-up: `PULLUP true` in `hw/xdc/zybo_header_pullups.xdc`.

| Board pin | Port | Package pin | Schematic net |
|---|---|---|---|
| JA0 | `ja[0]` | N15 | JA1_P |
| JA1 | `ja[1]` | L14 | JA2_P |
| JA2 | `ja[2]` | K16 | JA3_P |
| JA3 | `ja[3]` | K14 | JA4_P |
| JA4 | `ja[4]` | N16 | JA1_N |
| JA5 | `ja[5]` | L15 | JA2_N |
| JA6 | `ja[6]` | J16 | JA3_N |
| JA7 | `ja[7]` | J14 | JA4_N |
| JB0 | `jb[0]` | V8 | jb_p[1] |
| JB1 | `jb[1]` | W8 | jb_n[1] |
| JB2 | `jb[2]` | U7 | jb_p[2] |
| JB3 | `jb[3]` | V7 | jb_n[2] |
| JB4 | `jb[4]` | Y7 | jb_p[3] |
| JB5 | `jb[5]` | Y6 | jb_n[3] |

## DE25-Standard (`de25_datalogger`)

Header: JP1 (2×20). Pin 1 = GPIO[0] (silk). The pin numbers follow the standard DE 2×20 layout: pins 11 and 29
power, 12 and 30 ground. Pull-up: `WEAK_PULL_UP_RESISTOR ON` in
`hw/quartus/de25/de25_project_options.tcl`; the fitter report lists Weak Pull Up On for all 16 `GPIO_D` inputs.

| Board pin | Port | Package pin | JP1 pin |
|---|---|---|---|
| JA0 | `GPIO_D[0]` | BK31 | 1 |
| JA1 | `GPIO_D[1]` | BE43 | 2 |
| JA2 | `GPIO_D[2]` | BF29 | 3 |
| JA3 | `GPIO_D[3]` | BF40 | 4 |
| JA4 | `GPIO_D[4]` | BK28 | 5 |
| JA5 | `GPIO_D[5]` | BM31 | 6 |
| JA6 | `GPIO_D[6]` | BM28 | 7 |
| JA7 | `GPIO_D[7]` | BP31 | 8 |
| JB0 | `GPIO_D[8]` | BR31 | 9 |
| JB1 | `GPIO_D[9]` | BU28 | 10 |
| JB2 | `GPIO_D[10]` | BU31 | 13 |
| JB3 | `GPIO_D[11]` | BW28 | 14 |
| JB4 | `GPIO_D[12]` | BR22 | 15 |
| JB5 | `GPIO_D[13]` | BU19 | 16 |

## PolarFire SoC Discovery Kit (`pfsoc_datalogger`)

Header: the 40-pin Raspberry Pi header. Pull-up: `set_io -RES_PULL Up` in `hw/pdc/pfsoc_header_pullups.pdc`. The
post-layout pin report lists Resistor Pull Up for all 14 inputs; a build without that file reports the same, as Up is
Libero's default for these inputs.

| Board pin | Port | Package pin | RPi header pin |
|---|---|---|---|
| JA0 | `rpi_gpio4` | E12 | 7 |
| JA1 | `rpi_gpio17` | G18 | 11 |
| JA2 | `rpi_gpio18` | E10 | 12 |
| JA3 | `rpi_gpio27` | F21 | 13 |
| JA4 | `rpi_gpio22` | F20 | 15 |
| JA5 | `rpi_gpio23` | E21 | 16 |
| JA6 | `rpi_gpio24` | F19 | 18 |
| JA7 | `rpi_gpio25` | A19 | 22 |
| JB0 | `rpi_gpio5` | D17 | 29 |
| JB1 | `rpi_gpio6` | D18 | 31 |
| JB2 | `rpi_gpio12` | B20 | 32 |
| JB3 | `rpi_gpio13` | A15 | 33 |
| JB4 | `rpi_gpio19` | B19 | 35 |
| JB5 | `rpi_gpio16` | B15 | 36 |

## Items to confirm

- Zybo Pmod pin numbers for each schematic net (the board registry records the nets, not the Pmod pin numbers).
- DE25 JP1 pins 5 to 16: the numbering follows the standard DE 2×20 layout; only pins 1 to 4 are silk-confirmed.
