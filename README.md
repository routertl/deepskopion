# Deepskopion

Deepskopion is a protocol datalogger that runs on an FPGA SoC development board. It decodes traffic from several buses at once in the FPGA fabric and stamps every record from one hardware counter, so traffic from different buses lines up on one timeline. A Linux daemon on the board streams the records over TCP to a viewer or any other program.

---

## Supported Boards

| Board | Target | Device | Toolchain |
|---|---|---|---|
| Digilent Zybo Z7-20 | [`zybo_datalogger`](targets/zybo_datalogger.yml) | AMD Zynq-7000 `xc7z020clg400-1` | Vivado |
| Terasic DE25 | [`de25_datalogger`](targets/de25_datalogger.yml) | Altera Agilex 5 | Quartus Prime Pro |
| Microchip PolarFire SoC Discovery Kit (preview) | [`pfsoc_datalogger`](targets/pfsoc_datalogger.yml) | Microchip PolarFire SoC `MPFS095T-1FCSG325E` | Libero SoC |

The build instructions below use the Zybo Z7-20. [`docs/SUPPORTED_ENVIRONMENT.md`](docs/SUPPORTED_ENVIRONMENT.md) declares the supported host, tools and board, and `tools/preflight/ds_preflight.py` checks them.

The PolarFire SoC Discovery Kit target is a preview: its self-test capture is not yet verified on silicon. The kit boots Linux and runs the capture daemon. It requires Libero SoC, and its MSS component is generated locally from the committed configuration ([`hw/libero/pfsoc/mss/README.md`](hw/libero/pfsoc/mss/README.md)). A cold boot can stop after DDR training because of an SD-card clock setting; a warm reset of the microprocessor subsystem recovers it.

---

## Supported Protocols

| Protocol | Records | Status |
|---|---|---|
| UART | Baud rate calibration, frames, parity and framing errors | Default targets |
| I2C | START and STOP, 7-bit address, ACK and NACK, data bytes | Default targets |
| SPI | Chip-select framing, MOSI and MISO bytes | Default targets |
| QSPI | Flash command, address and data | Selected with the configurator |
| JTAG | Passive TAP capture | Selected with the configurator; records no live traffic on the DE25 and the PolarFire SoC Discovery Kit, whose tops tie the tap it reads to constants |
| Edge | Timed transitions, batched | Default targets |
| CAN, CAN FD | IDs, BRS, ESI, DLC, CRC and stuff errors | Default targets; see [CAN FD licensing](#appendix-can-fd-licensing) |

---

## When Another Tool Fits

Deepskopion decodes protocols inside a running FPGA SoC. Other tools fit other jobs:

* Logic analyzer hardware and its decoders: [sigrok / PulseView](https://sigrok.org/).
* Analog signals, and board bring-up before Linux runs: a mixed-signal oscilloscope.

See [`ATTRIBUTIONS.md`](ATTRIBUTIONS.md).

---

## Architecture

![Deepskopion architecture: dataloggers in the FPGA fabric, an arbiter and framer, the DDR writer, a ring buffer in board DDR, the capture daemon on Linux, and clients on the host](docs/images/architecture.svg)

| Block | Function |
|---|---|
| Dataloggers | Tap bus lines, reconstruct frames and add hardware timestamps |
| Arbiter and framer | Merge datalogger messages into `zs_msg` records (`datalogger_gather.vhd`) |
| DMA | Writes records to a ring buffer in board DDR through `rr_ddr_stream` |
| Capture daemon | Drains the ring on Linux and streams records over TCP |
| Readers | RouteWave, or any program that reads the stream |

---

## In-System Self-Test

On the Zybo Z7-20, the DE25 and the PolarFire SoC Discovery Kit, the default targets generate bus traffic for five of the nine data loggers of the default mix, so those loggers capture with the board alone. The other four need external traffic on their header pins. The reach is the same on all three boards; it is measured in simulation by [`test_selftest_coverage.py`](hw/sim/cocotb/tests/datalogger/infra/default_mix/test_selftest_coverage.py). On the PolarFire SoC Discovery Kit this path is not yet verified on silicon.

| Data logger | Board-alone source |
|---|---|
| Edge 1 | The edge stimulus, or the I2C edge route; on the Zybo Z7-20 also the CAN edge route, and on the PolarFire SoC Discovery Kit also the UART edge route |
| Edge 2 | The I2C edge route |
| Edge 3 | None: not reached by the self-test, whose SPI stimulus is master-only. MISO is driven by an external SPI device or by the on-chip SPI responder (spi_injector), which the self-test does not arm. |
| I2C 4 | The I2C stimulus |
| I2C 5 | None: external traffic only |
| SPI 8 | The SPI stimulus |
| UART 12 | The UART stimulus (on the PolarFire SoC Discovery Kit, MMUART_1) |
| UART 13 | None: external traffic only |
| CAN 20 | None: external traffic only. The SPI edge route also drives this input with the SPI chip-select, which is not CAN traffic. |


| Board | Traffic source | Sources |
|---|---|---|
| Zybo Z7-20 | Linux drivers on the PS peripherals, routed to the fabric over EMIO. The `EMIO_MUX_SEL` register selects the input. | [`hw/bd/ds_emio_uart_i2c_spi.tcl`](hw/bd/ds_emio_uart_i2c_spi.tcl), [`hw/bd/ds_emio_can.tcl`](hw/bd/ds_emio_can.tcl), [`hw/rtl/emio/ds_emio_mux.vhd`](hw/rtl/emio/ds_emio_mux.vhd), [`hw/rtl/emio/glitch_filter.vhd`](hw/rtl/emio/glitch_filter.vhd) |
| DE25 | A stimulus block in the fabric, controlled from Linux through the `STIM_*` registers. | [`hw/rtl/stimulus/ds_stimulus_gen.vhd`](hw/rtl/stimulus/ds_stimulus_gen.vhd), [`project_regbank.yml`](project_regbank.yml) |
| PolarFire SoC Discovery Kit | Linux writes UART traffic on MMUART_1 (`/dev/ttyS1`), which the MSS routes to the fabric. `EMIO_MUX_SEL` bits [11:8] select the MSS I2C, UART, SPI and CAN; this image arms the UART bit only. I2C, SPI and edge traffic come from the fabric stimulus block through the `STIM_*` registers. | [`hw/rtl/pfsoc/deepskopion_pfsoc_top.vhd`](hw/rtl/pfsoc/deepskopion_pfsoc_top.vhd), [`hw/libero/pfsoc/pfsoc_mss_subsys.tcl`](hw/libero/pfsoc/pfsoc_mss_subsys.tcl), [`hw/rtl/stimulus/ds_stimulus_gen.vhd`](hw/rtl/stimulus/ds_stimulus_gen.vhd) |

![Self-test data flow on the Zybo Z7-20, the DE25 and the PolarFire SoC Discovery Kit: a traffic source, the source select, the dataloggers, then the gather, DDR ring and daemon](docs/images/self_test.svg)

---

## Build This with RouteRTL

Deepskopion uses the [RouteRTL](https://routertl.dev) build system (`rr`) to manage IP dependencies, run VHDL simulations, build bitstreams and program boards.

> Disclosure: RouteRTL is the author's own tool, written by the same person as Deepskopion. RouteRTL has a free tier, free forever, and a paid Pro tier.
> Everything in this README runs on the free tier: `rr` installs free from PyPI (`pip install routertl`), and no command in this README requires a paid licence.
> Registration is optional and never blocks a build. The Pro tier adds advanced-verification commands (`rr loop`, `rr formal`, `rr verify`, coverage) that this project does not need.
> Deepskopion itself is open hardware and software; see [Licensing Summary](#licensing-summary).

### Requirements

| Item | Version |
|---|---|
| RouteRTL | `routertl >= 5.6.0` |
| Python | `>= 3.10, < 3.15` |
| RouteWave | `2.4.0` |
| Vivado (Zybo Z7-20) | `2024.1`, ML Standard or ML Enterprise |
| Quartus Prime Pro (DE25) | `25.3` |
| Libero SoC (PolarFire SoC Discovery Kit) | `2025.2` |
| NVC (simulation only) | `>= 1.19.3` |
| Host | x86_64 Linux, 24 GB RAM as the OS sees it (under WSL2: the `.wslconfig` limit), 60 GB free disk |

> On a host with 16 GB of RAM or less, a Vivado target can need `RR_EDA_ALLOW_OVERSUBSCRIBE=1` to pass the build's memory estimate.

### Try it without a board

The datalogger RTL runs in simulation on an x86_64 Linux host with Python, RouteRTL and the free NVC simulator. No board, vendor tool or licence is needed.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install routertl
git clone https://github.com/routertl/deepskopion.git deepskopion
cd deepskopion
rr eda install nvc
rr pkg install
pip install -r requirements.txt
rr sim run test_i2c_datalogger
```

`rr eda install nvc` builds NVC from source with the system package manager; on a fresh container image, `apt-get update` comes first. The run drives an I2C bus model into the I2C datalogger and checks the messages it emits: nine tests, about 36 seconds of simulation. Each test writes an FST waveform to `hw/sim/waves/test_i2c_datalogger/`, which RouteWave opens:

```bash
routewave -c "open hw/sim/waves/test_i2c_datalogger/tb_i2c_datalogger.test_i2c_datalogger.test_write_transaction.fst; show --tree tb_i2c_datalogger; zoom fit"
```

![The I2C write-transaction test in RouteWave: SCL, SDA and the datalogger's configuration and timestamp](docs/images/sim_i2c_datalogger.png)

The other datalogger testbenches run the same way; `rr sim run --all` runs every test.

### Build and run on the Zybo Z7-20

The build runs Vivado 2024.1, so put it on `PATH` and set `XILINX_VIVADO` first, in the shell that runs the steps below. After a standard install, the installer's script does both:

```bash
source <install-dir>/Vivado/2024.1/settings64.sh
```

An install copied to another directory, rather than installed there, keeps the original directory inside `settings64.sh`, so that script points at the wrong place. Set the two variables directly instead; no vendor file needs editing:

```bash
export XILINX_VIVADO=<install-dir>/Vivado/2024.1
export PATH="$XILINX_VIVADO/bin:$PATH"
```

`vivado -version` then prints `vivado v2024.1`, and step 4 checks the same version. RouteRTL finds the pinned 2024.1 release through `XILINX_VIVADO`.

```bash
# 1. Install RouteRTL in a virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install routertl

# 2. Clone the repository
git clone https://github.com/routertl/deepskopion.git deepskopion
cd deepskopion

# 3. Select the board, then fetch the packages its committed lock names
rr target set zybo_datalogger
rr pkg install
pip install -r requirements.txt

# 4. Check the host against the supported environment
python3 tools/preflight/ds_preflight.py --stage build

# 5. Build the bitstream
rr synth run
rr implementation run
rr bitstream run

# 6. Build the Linux image, which embeds the bitstream, and write it to a microSD card.
#    The Python runtime of the image is built once from upstream Buildroot (1-3 h).
sw/linux/build_buildroot.sh && sw/linux/extract_python_overlay.sh
rr linux build
rr linux flash-sd --device <sd-card-device>

# 7. Boot the board from the card with Ethernet connected, then attach RouteWave
routewave -c "stream attach <board-ip>:7475"
```

RouteWave shows edge records in its Edge Scope. `stream status --json` in the RouteWave console reports the connection state.

In the RouteWave console, `export <file.vcd>` saves the session, and `open <file>` opens a saved capture (VCD, FST or ltrace).

The bitstream reaches the fabric from the card at boot, so a new bitstream goes to the board in a new card image.

### First boot

The image ships with no network address and no SSH key. The board's USB serial console (115200 8N1) is available from
power-on and takes these steps, in this order:

1. Address. The board requests an address over DHCP. Without a DHCP server it boots with no address and prints
   `network: no DHCP lease; set ds_ip=<address>/<prefix> or create /etc/deepskopion/network.conf`. A static address
   comes from either source:
   - `ds_ip=<address>/<prefix>` (and optionally `ds_gateway=<gateway>`) on the kernel command line;
   - `/etc/deepskopion/network.conf` with the line `address=<address>/<prefix>` (and optionally `gateway=<gateway>`),
     applied by `/etc/init.d/S40network restart`. The root filesystem is held in RAM, so a file written on the board
     lasts until the next boot; the same file placed as `etc/deepskopion/network.conf` under the rootfs overlay
     directory `sw/linux/rootfs_overlay/` before `rr linux build` is part of the image.

   RouteWave needs only the address: the stream on port 7475 does not use SSH.
2. SSH key. SSH stays off until an owner key exists, and password logins are disabled. `deepskopion-authorize-key
   /path/to/key.pub` on the serial console installs a public key (`deepskopion-authorize-key -` reads it from standard
   input). A key file placed as `etc/deepskopion/authorized_keys` under `sw/linux/rootfs_overlay/` (ignored by git)
   before the build is installed at every boot instead; it becomes `/etc/deepskopion/authorized_keys` in the image.
3. SSH start. `/etc/init.d/S49dropbear start` starts the SSH server.

[`docs/SUPPORTED_ENVIRONMENT.md`](docs/SUPPORTED_ENVIRONMENT.md) has the details.

### Other targets

```bash
# Terasic DE25, built with Quartus Prime Pro
rr target set de25_datalogger
rr pkg install

# Microchip PolarFire SoC Discovery Kit, built with Libero SoC
rr target set pfsoc_datalogger
rr pkg install
```

The DE25 Linux image is built with `python3 tools/de25_linux_image.py build`. [`docs/DEPLOYMENT_CONTRACT.md`](docs/DEPLOYMENT_CONTRACT.md) describes how software reaches each board.

---

## Choosing a Datalogger Mix

The set of dataloggers in a build is declared in a YAML file, which the target names in `system_config`. [`tools/datalogger-builder/configurator.py`](tools/datalogger-builder/README.md) writes that file, checks the mix against the board's pin and ID limits, estimates resource use, and prints which board pin carries each signal.

```bash
# 1. Write the mix
python3 tools/datalogger-builder/configurator.py \
    --board zybo-z7-20 \
    --uart 2 --i2c 2 --spi 1 --edge 4 \
    -o tools/datalogger-builder/config/my_mix.yml

# 2. Make a target for it
rr target clone zybo_datalogger my_mix
#    In targets/my_mix.yml, name the mix in both places that read it:
#    system_config: tools/datalogger-builder/config/my_mix.yml
#    and, in hooks.pre_build, the first argument of the datalogger-builder hook:
#      - tools/datalogger-builder/config/my_mix.yml

# 3. Select it, lock the project for it, and build as above
rr target set my_mix
rr lock --update
```

The build regenerates the gather RTL and its packages from the mix. `configurator.py --interactive` asks for the same values one at a time.

Four named mixes ship as mix files and build the same way, from a clone of the board's datalogger target:

| Mix file (`tools/datalogger-builder/config/`) | Board target to clone | Dataloggers |
|---|---|---|
| `zybo_datalogger_config_5uart_2i2c.yml` | `zybo_datalogger` | 5 UART, 2 I2C |
| `zybo_datalogger_config_0uart_5i2c_1spi.yml` | `zybo_datalogger` | 5 I2C, 1 SPI |
| `de25_datalogger_config_5uart_2i2c.yml` | `de25_datalogger` | 5 UART, 2 I2C |
| `de25_datalogger_config_0uart_5i2c_1spi.yml` | `de25_datalogger` | 5 I2C, 1 SPI |

---

## The On-Board Software

The capture daemon runs on Linux on the processor subsystem of each board: the Zynq PS, the Agilex HPS or the PolarFire SoC MSS.

| Item | Value |
|---|---|
| Stream port | TCP `7475` |
| Stream format | `wave_stream_v1`, MessagePack framing, defined in [`contracts/wave_stream_v1/README.md`](contracts/wave_stream_v1/README.md) |
| Software documentation | [`docs/software/README.md`](docs/software/README.md) |

The telemetry exporter runs on the board and reports CPU load, memory use, device temperature, interrupt rates and kernel health as JSON. See [`docs/software/telemetry_exporter.md`](docs/software/telemetry_exporter.md).

```bash
python3 -m sw.telemetry --interval 5 --pretty
```

---

## RouteWave as a Client

RouteWave is a waveform and protocol viewer for RouteRTL designs. It is one client of the versioned `wave_stream_v1` contract. Other programs that read MessagePack can attach to the same port, such as analysis scripts or test benches that check protocol sequences.

```bash
nc <board-ip> 7475
```

---

## Documentation Index

### System Architecture
* [System Overview](docs/SYSTEM_OVERVIEW.md): Architecture, data plane flow and DDR buffer model.
* [FPGA Design](docs/hardware/fpga_design.md): Datalogger subsystems, clock domains and FIFOs.
* [Software Architecture](docs/software/README.md): Capture daemon, ring draining and kernel interfaces.
* [Deployment Contract](docs/DEPLOYMENT_CONTRACT.md): How software reaches a board.
* [Supported Environment](docs/SUPPORTED_ENVIRONMENT.md): Host OS, tools and memory requirements.
* [Redistribution](docs/REDISTRIBUTION.md): Third-party and vendor-generated files.
* [Release Notes](docs/RELEASE_NOTES.md): Targets, default mix, requirements and known limitations of each release.

### Datalogger Specifications
* [Protocol Dataloggers Overview](hw/rtl/datalogger/README.md): Subsystem status and directory map.
* [UART Datalogger Protocol](hw/rtl/datalogger/uart/UART_DATALOGGER_PROTOCOL.md): Wire framing and error status.
* [I2C Datalogger Protocol](hw/rtl/datalogger/i2c/I2C_DATALOGGER_PROTOCOL.md): Bus events and byte messages.
* [QSPI Datalogger Protocol](hw/rtl/datalogger/qspi/QSPI_DATALOGGER_PROTOCOL.md): Flash transactions and command decoding.
* [Edge Datalogger Protocol](hw/rtl/datalogger/nonstd/edge_datalogger/EDGE_DATALOGGER_PROTOCOL.md): Transition timestamps and batch format.

---

## Contributing

Contributions are welcome. [`CONTRIBUTING.md`](CONTRIBUTING.md) has the full workflow.

* Commits carry a `Signed-off-by:` line under the Developer Certificate of Origin.
* A pull request that changes RTL, the daemon, a device tree or a target needs a record from `rr hw run <REQ_ID> --probe <xsdb|juart> --write` on a physical board, and a clean simulation regression.
* New board targets and new protocol dataloggers are welcome, for example an Efinix Ti60 port with the Sapphire RISC-V SoC, a Zynq UltraScale+ port, or an EtherCAT datalogger.

---

## Licensing Summary

| Component | Path | License |
|---|---|---|
| Gateware and RTL | `hw/`, `sim/`, `docs/` | [CERN-OHL-W-2.0](LICENSES/CERN-OHL-W-2.0.txt) |
| Software | `sw/`, `tools/`, `contracts/` | [MPL-2.0](LICENSES/MPL-2.0.txt) |
| Vendored cores | `libs/` | Upstream licenses |

Both licenses are weakly reciprocal at the file level. Deepskopion components can go into a larger design or a commercial product, and the rest of that design keeps its own licence. Changes to Deepskopion files stay under their licenses. See [`LICENSE.md`](LICENSE.md), [`REUSE.toml`](REUSE.toml) and [`ATTRIBUTIONS.md`](ATTRIBUTIONS.md).

---

## Appendix: CAN FD licensing

The CAN FD data logger is DeepSkopion's own passive receiver (`hw/rtl/datalogger/can/`). It contains no CTU CAN FD code, and none of the three targets in `targets/` includes the CTU CAN FD core. The core appears only in bench targets, which are not part of this distribution. Its licence terms, for reference:

* CTU CAN FD core: Copyright (C) 2021-present Ondrej Ille. Free of charge for educational, research, evaluation and self-interest purposes. Commercial use requires prior agreement with the copyright holder. See [`LICENSES/LicenseRef-CTU-CAN-FD-NonCommercial.txt`](LICENSES/LicenseRef-CTU-CAN-FD-NonCommercial.txt).
* CAN protocol patents: Robert Bosch GmbH holds patents on the CAN protocol. Implementing the core on silicon requires a CAN protocol licence from Bosch.
