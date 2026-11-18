# Driving I2C and SPI from Linux userspace

Reference for how the deepskopion PS-side self-test harness
(`sw/stream/testbench/`) drives the Zynq EMIO-routed I2C0 and SPI0
controllers from a busybox + Python3 rootfs. The harness itself
uses the idiomatic libraries — `pyserial`, `smbus2`, `spidev`
(included in the buildroot overlay). This doc walks
through what those libraries are doing underneath: the kernel
char-device API, the ioctl numbers, the struct layouts, and
the stdlib-only path you'd take if you ever needed to avoid the
deps.

## Big picture

The Zynq PS exposes its I2C / SPI / UART controllers as character
devices when they're enabled in the device tree:

| Bus  | Linux node         | Kernel driver           |
|------|--------------------|-------------------------|
| I2C0 | `/dev/i2c-0`       | `i2c-cadence`           |
| SPI0 | `/dev/spidev0.0`   | `spidev` over `spi-zynq`|
| UART0| EMIO UART0 tty (base 0xE0000000) | `xuartps` |

For deepskopion the EMIO-routing decision happens in the bitstream
(the `ds_emio_uart_i2c_spi.tcl` PS7 hook), but Linux doesn't know or
care — to userspace it's just three character devices.

The UART0 tty number is not fixed. `/dev/ttyPS<N>` follows the device
tree's `serialN` aliases, whose order comes from the device-tree generator
(with the SDTGen + Lopper output of `rr linux dtb`, UART0 is `/dev/ttyPS1`
and the USB console UART1 is `/dev/ttyPS0`). The EMIO UART0 tty is the one
whose device sits at 0xE0000000; `emio_uart_dev()` in
`sw/stream/testbench/emio_inject.py` returns it.

The kernel drivers do the protocol shifting; userspace just opens a
char device, configures it via a handful of `ioctl`s, and reads/writes
bytes. **No special library is required** — `os`, `fcntl`, and
`struct` from the Python stdlib are enough — but the libraries
do the same job with less boilerplate and they handle the edge
cases (CS auto-toggle, repeated-START, baud clamping) without
the caller having to re-derive them.

For deepskopion's self-test the call is "use the libs": they're
well-trodden, maintained, and the buildroot overlay rebuild to add
them (~1 MB) is rounding error against the maintenance saving.
The stdlib path remains a known fallback for environments where
the libs aren't available.

## I2C

### Programming model

The kernel `i2c-dev` driver lets you talk to any slave on a configured
bus without writing a kernel driver of your own. The flow:

1. `open("/dev/i2c-0", O_RDWR)` — get a file descriptor for the bus.
2. `ioctl(fd, I2C_SLAVE, 0x50)` — tell the kernel "the address byte
   for upcoming read/write should target 0x50". The kernel auto-
   prepends this to every transaction.
3. `write(fd, b"\x01\x02")` — sends `START + (0x50<<1|0) + 0x01 + 0x02
   + STOP` on the bus.
4. `read(fd, 4)` — sends `START + (0x50<<1|1) + read-4-bytes + STOP`
   and returns the 4 bytes the slave clocked out on SDA.

Or for a combined write-then-read (the common "register read" pattern
for I²C peripherals):

5. `ioctl(fd, I2C_RDWR, struct of two i2c_msg)` — issues a single
   transaction with a repeated-START between the write phase (sets
   the register pointer) and the read phase (slurps the value).

### From Python stdlib

```python
import os, fcntl, struct

I2C_SLAVE = 0x0703   # uapi/linux/i2c-dev.h
I2C_RDWR  = 0x0707

fd = os.open("/dev/i2c-0", os.O_RDWR)
fcntl.ioctl(fd, I2C_SLAVE, 0x50)         # target address
os.write(fd, b"\x01")                     # write 1 byte
data = os.read(fd, 4)                     # read 4 bytes
os.close(fd)
```

That's it. No external dep, runs on busybox + python3.

The `ioctl` numbers (`0x0703`, `0x0707`) are encoded — `_IO('I', 0x03)`
and `_IOWR('I', 0x07, struct)` per the kernel macros. They're
architecturally stable across Zynq-7 / ZynqMP / x86 — safe to
hardcode in our harness.

### Higher-level: smbus2

If you don't mind the dependency, `smbus2.SMBus(0)` gives you
`write_byte_data` / `read_word_data` / `read_i2c_block_data` etc.
that wrap the same ioctls. Useful in dev box scripts; overkill
for the on-target self-test where stdlib is fine.

### Gotchas

- **Root required** on most busybox distros — `/dev/i2c-0` is owned
  by `root:i2c`. Our daemon already runs as root (start-stop-daemon
  in init.d), so this isn't a barrier.
- **7-bit vs 8-bit addresses**: `I2C_SLAVE` takes the 7-bit address.
  The R/W bit is added by the kernel based on whether the next op
  is a `read()` or `write()`.
- **No clock-stretch tuning** via userspace — the Cadence I2C driver
  handles SCL low automatically.
- **Bus errors** show up as `OSError(EREMOTEIO)` or `OSError(ENXIO)`
  on `write()` / `read()` — wrap in try/except.

### Shell tools (debug)

`i2c-tools` ships with busybox-compatible binaries:

```sh
i2cdetect -y 0           # probe all addresses, show which ack'd
i2cget   -y 0 0x50       # read 1 byte from addr 0x50
i2cset   -y 0 0x50 0xab  # write 0xab to addr 0x50
i2ctransfer -y 0 w2@0x50 0x01 0x02 r4@0x50  # combined write+read
```

We use these only for `i2cdetect` smoke during board bring-up; the
self-test goes through the ioctl path directly for deterministic
timing.

## SPI

### Programming model

The kernel `spidev` driver exposes one node per CS line on a bus
(`/dev/spidevB.C` = bus B, chip-select C). The flow:

1. `open("/dev/spidev0.0", O_RDWR)` — bus 0, CS 0.
2. Configure with three ioctls (one-time):
   - `SPI_IOC_WR_MODE` — CPOL/CPHA bits + LSB-first + 3-wire + CS-high.
     Modes 0-3 mirror the standard SPI table.
   - `SPI_IOC_WR_BITS_PER_WORD` — usually 8.
   - `SPI_IOC_WR_MAX_SPEED_HZ` — Hz; kernel clamps to the SoC's
     allowed list.
3. `ioctl(fd, SPI_IOC_MESSAGE(1), spi_ioc_transfer)` — issues one
   full-duplex transfer: TX bytes go out on MOSI, RX bytes come in on
   MISO simultaneously. CS is asserted at the start, deasserted at
   the end (unless `cs_change` is set in the transfer struct).

### The `spi_ioc_transfer` struct

This is the core data type the kernel expects. From
`uapi/linux/spi/spidev.h`:

```c
struct spi_ioc_transfer {
    __u64  tx_buf;      // pointer to TX bytes (or NULL → send zeros)
    __u64  rx_buf;      // pointer to RX bytes (or NULL → discard)
    __u32  len;         // transfer length in bytes (same for TX + RX)
    __u32  speed_hz;    // override the WR_MAX_SPEED_HZ for this xfer
    __u16  delay_usecs; // hold CS asserted this long after last bit
    __u8   bits_per_word;
    __u8   cs_change;   // toggle CS between this xfer and the next
    __u8   tx_nbits;    // 1=SINGLE, 2=DUAL, 4=QUAD (for QSPI)
    __u8   rx_nbits;
    __u16  word_delay_usecs;
    __u8   pad[2];
};
```

Same size on Zynq-7 and ZynqMP (32 bytes total).

### From Python stdlib

```python
import os, fcntl, struct, ctypes

# Magic numbers from linux/spi/spidev.h
SPI_IOC_WR_MODE          = 0x40016B01
SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
SPI_IOC_WR_MAX_SPEED_HZ  = 0x40046B04

def _spi_ioc_message(n):
    """SPI_IOC_MESSAGE(n) — runtime-built, encodes payload size."""
    return 0x40006B00 | ((n * 32) << 16)   # 32 = sizeof(spi_ioc_transfer)

fd = os.open("/dev/spidev0.0", os.O_RDWR)

# One-time setup.
fcntl.ioctl(fd, SPI_IOC_WR_MODE,          struct.pack("B", 0))         # mode 0
fcntl.ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
fcntl.ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ,  struct.pack("I", 1_000_000)) # 1 MHz

# One full-duplex transfer: send 4 bytes, capture 4 bytes.
tx = ctypes.create_string_buffer(b"\xDE\xAD\xBE\xEF", 4)
rx = ctypes.create_string_buffer(4)
xfer = struct.pack(
    "QQIIHBBBBHBB",                          # spi_ioc_transfer layout
    ctypes.addressof(tx),                    # tx_buf
    ctypes.addressof(rx),                    # rx_buf
    4,                                       # len
    0,                                       # speed_hz (use default)
    0,                                       # delay_usecs
    8,                                       # bits_per_word
    0,                                       # cs_change
    0, 0,                                    # tx_nbits, rx_nbits
    0,                                       # word_delay_usecs
    0, 0,                                    # pad
)
fcntl.ioctl(fd, _spi_ioc_message(1), xfer)
print("RX:", rx.raw)
os.close(fd)
```

The ctypes `create_string_buffer` matters — it keeps the TX/RX
buffers alive (the kernel reads from + writes back at those
pointers). Plain `bytes` won't work; the GC could move them.

### Higher-level: spidev

`pip install spidev` gives you `SpiDev().xfer2([0xDE, 0xAD])` which
wraps exactly the above into one line. Same caveat as smbus2 — fine
on dev box, overkill on busybox.

### Gotchas

- **Full-duplex**: every SPI transfer reads N bytes when it writes
  N bytes. The slave's MISO is always sampled, even if you don't
  care. For our injector test this is the *point* — we drive MOSI
  and read what the slave drove on MISO.
- **CS auto-toggle**: each `ioctl(SPI_IOC_MESSAGE)` asserts CS at
  the start, deasserts at the end. To keep CS asserted across
  multiple ioctls, set `cs_change=0` on each transfer in a single
  ioctl with multiple `spi_ioc_transfer` structs. We don't need
  this; one ioctl per byte-batch is the natural shape.
- **MODE bits**: bit 0 = CPHA, bit 1 = CPOL, bit 2 = CS active-high,
  bit 3 = LSB-first, bit 6 = 3-wire. We use mode 0 (CPOL=0, CPHA=0)
  to match the deepskopion `spi_slave` v1.
- **No spidev0.0 → check device tree**: if the node doesn't appear,
  the kernel's spi-zynq driver isn't binding to a `spidev` child.
  The Zybo Z7-20 device tree we ship adds a `compatible = "spidev"`
  child under the EMIO SPI controller.

### Chip-select: the native EMIO `SPI0_SS_O` is dead — drive CS from an EMIO GPIO

**The trap.** On Zynq-7000, the PS SPI controller (`xlnx,zynq-spi-r1p6`,
Cadence) asserts its chip-select *internally* (`CONFIG.SSCTRL`) and its EMIO
`SCLK`/`MOSI` reach the PL fabric — but the native EMIO **`SPI0_SS_O` pin never
carries the chip-select out**. A fabric SPI slave keyed off `SPI0_SS_O` never
sees a transaction start: the build passes, but on hardware the slave never
responds (the `ETIMEDOUT`).

Proven on silicon: the on-chip REA showed `SPI0_SCLK_O` toggling but
`SPI0_SS_O==0` *never* triggering during a `/dev/spidev1.0` transfer, while live
sampling of the SPI `CONFIG` register (`0xE0006000`) showed the cadence driver
*does* drive native CS (`SSCTRL=0xE`, ~96% of the transfer). The EMIO pin
silently drops it. This is **chip-select-specific** — I2C (no CS, open-drain
SCL/SDA) round-trips fine on the same EMIO path.

**The fix — `cs-gpios`.** Don't rely on `SPI0_SS_O`. Drive the chip-select from
an **EMIO GPIO** bit and couple it to spidev via the SPI core's `cs-gpios`. The
Linux SPI core then toggles that GPIO around every transfer — **spidev and the
cadence driver are untouched**; you only change the device tree and the fabric.

1. **PS7 / BD**: enable EMIO GPIO (`PCW_GPIO_EMIO_GPIO_ENABLE`), route
   `GPIO_O(0)` to the fabric, and have the SPI slave/injector watch *that* line
   as its `CS_N` (see `hw/bd/ds_emio_uart_i2c_spi.tcl`, `deepskopion_bd.tcl`,
   and `spi0_cs_gpio_w` in `hw/rtl/deepskopion_top.vhd`).
2. **Device tree** (`project.yml` `linux.dt_nodes`, injected by `rr linux dtb`).
   EMIO GPIO bit *n* = PS GPIO controller line `54 + n` (MIO 0–53, EMIO 54+).
   `cs-gpios` is a phandle cell-array, so `dt_nodes` writes it as raw cells
   `[<gpio0-phandle>, <line>, <flags>]` (no `&label` — fdtput patches the binary
   DTB). On our build gpio0 (`gpio@e000a000`) has phandle **9**, EMIO bit 0 =
   line **54**, `GPIO_ACTIVE_LOW` = **1**:

   ```yaml
   linux:
     dt_nodes:
       - path: /axi/spi@e0006000
         properties:
           cs-gpios: [9, 54, 1]      # == <&gpio0 54 GPIO_ACTIVE_LOW>
   ```

   Equivalent DTS: `&spi0 { cs-gpios = <&gpio0 54 GPIO_ACTIVE_LOW>; };`.
   Re-verify the gpio0 phandle after any `rr linux dtb` regen
   (`dtc -I dtb -O dts system.dtb | grep -A1 gpio@e000a000` → `phandle`) — it's
   stable in practice but the cell-array hard-codes it.

**Verified end-to-end** (deepskopion `694dbd5`): REA `SPI0_CS_GPIO==0` fires;
preload the injector (`SPI_INJ_DATAIN`), drive `/dev/spidev1.0`, and the master
reads the preloaded bytes back on MISO exactly. Captured as rr rule
**`ZYNQ7-SPI-EMIO-CS-001`**.

### Shell tools (debug)

```sh
spi-pipe -d /dev/spidev0.0 -s 1000000 -n 4   # full-duplex 4 bytes
spidev_test -D /dev/spidev0.0 -p "\xDE\xAD\xBE\xEF"
```

`spi-pipe` is small; `spidev_test` is the canonical example shipped
with the kernel under `tools/spi/`. Both bypass our self-test loop
— useful for bare-hardware bring-up but not what the harness uses.

## UART (for completeness)

UART is the simplest of the three — once `xuartps` binds, the EMIO UART0
tty (base 0xE0000000; resolve with `emio_uart_dev()`) behaves like a serial
port. Configure baud via `termios`:

```python
import os, termios
from sw.stream.testbench.emio_inject import emio_uart_dev

fd = os.open(emio_uart_dev(), os.O_RDWR | os.O_NOCTTY)   # the EMIO UART0 tty
attrs = termios.tcgetattr(fd)
# attrs is [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
attrs[4] = attrs[5] = termios.B9600                # 9600 baud
attrs[2] = (attrs[2] & ~termios.CSIZE) | termios.CS8 | termios.CREAD | termios.CLOCAL
attrs[2] &= ~termios.PARENB                        # no parity
attrs[2] &= ~termios.CSTOPB                        # 1 stop bit
termios.tcsetattr(fd, termios.TCSANOW, attrs)
os.write(fd, b"U")
os.close(fd)
```

That's the entire stdlib UART path. `pyserial` is nicer but we don't
need it.

## Why this matters for the self-test

The harness in `sw/stream/testbench/` uses these three patterns to
drive the EMIO peripherals deterministically. Each scenario:

1. Pre-loads the I2C / SPI injector FIFO via regbank writes
   (`/dev/mem` + mmap — see `sw/stream/regbank.py`).
2. Flips `EMIO_MUX_SEL` to route the EMIO bus into the datalogger slot.
3. Drives the master from the EMIO UART0 tty (base 0xE0000000; resolve with
   `emio_uart_dev()`) / `/dev/i2c-0` / `/dev/spidev0.0`
   using exactly the snippets above.
4. Taps the `wave_events_v1` NDJSON stream on TCP 7476 and asserts
   the expected event arrived.

Reading `emio_inject.py` next to this doc, every line maps to one of
the sections above. No magic.

## Further reading

- `Documentation/i2c/dev-interface.rst` in the Linux source tree —
  authoritative reference for `/dev/i2c-N`.
- `Documentation/spi/spidev.rst` — same for `/dev/spidev*`.
- `tools/spi/spidev_test.c` — minimal C example of the ioctl flow.
- `man 3 termios` — UART configuration knobs.
