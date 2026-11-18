# Deepskopion — Session Summary (demo config #1)

**Capture window:** 25.050 s &nbsp;·&nbsp; **3863 decoded events** &nbsp;·&nbsp; single shared µs timebase (all events on one 100 MHz sample clock)

## Protocols observed

| Protocol | Transactions | Window | Errors | Event kinds |
|---|---|---|---|---|
| **EDGE (logic-analyzer)** | 2322 | 25.000 s | 0 | edge×2322 |
| **SPI (4-wire)** | 600 | 11.869 s | 0 | spi_mosi×240, spi_miso×240, spi_event×120 |
| **UART** | 518 | 6.575 s | 0 | uart_byte×518 |
| **I2C** | 300 | 5.823 s | 30 | i2c_byte×210, i2c_start×30, i2c_repeated_start×30, i2c_stop×30 |

## Shared timebase (the cross-protocol proof)

Every protocol is stamped against the same monotonic sample clock, so transactions across protocols are directly time-orderable:

| t (µs from start) | Protocol | First event |
|---|---|---|
| 0.0 | EDGE (logic-analyzer) | edge word=0x10db7b32 |
| 54452.2 | UART | 0x58 'X' |
| 7035508.3 | I2C | START |
| 13108756.1 | SPI (4-wire) | CS↓ |

## Anomalies (observed facts)

| Channel | Kind | Count | Note |
|---|---|---|---|
| zs_resync | zs_resync | 123 | capture-side: daemon zs_msg stream resync at EMIO-mux switches during self-injection — NOT a bus error |
| i2c_frame | i2c_byte | 30 | I2C read terminated by the master NACK on the last byte (normal end-of-read); verify against decoder semantics |
