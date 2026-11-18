# Datalogger Builder Tool

Tools for configuring and building the Deepskopion datalogger aggregation subsystem.

## Tools

### 1. CLI Configurator (`configurator.py`)

Interactively or via command-line flags, selects target boards and datalogger counts, validates against board pin budgets and datalogger ID limits, reports estimated resource utilization (LUTs/ALMs, FFs, BRAM) calibrated from real Vivado/Quartus fit reports, and emits configuration YAML.

#### Non-Interactive Usage (Flags)

```bash
# Configure Zybo Z7-20 PoC mix and output to config file
python3 tools/datalogger-builder/configurator.py \
    --board zybo-z7-20 \
    --i2c 2 --uart 2 --spi 1 --jtag 1 --can 1 --edge 4 \
    -o my_datalogger_config.yml

# Check calibration against a Vivado utilization report within 5% tolerance
python3 tools/datalogger-builder/configurator.py \
    --board zybo-z7-20 \
    --i2c 2 --uart 2 --spi 1 --jtag 1 --can 1 --edge 4 \
    --check-fit reports/utilization.rpt \
    --tolerance 0.05
```

`hier_calibration.py <compile_netlist_hier_resources.csv> <prefix>` sums a Libero hierarchy report under a prefix and exits 2 when the prefix matches no row.

#### Interactive Usage (Wizard)

Run without arguments (or with `--interactive` / `-i`):
```bash
python3 tools/datalogger-builder/configurator.py --interactive
```

#### Supported Boards

| Board ID | Platform / Part | Pin Budget | Logic Capacity | BRAM Capacity | Provenance |
| -------- | --------------- | ---------- | -------------- | ------------- | ---------- |
| `zybo-z7-10` | `xc7z010clg400-1` | 32 pins | 17,600 LUTs | 60 Tiles | In-tree silicon scoping (`STEP7_SILICON_SCOPING.md`: base 22,504 LUTs) |
| `zybo-z7-20` | `xc7z020clg400-1` | 40 pins | 53,200 LUTs | 140 Tiles | Vivado 2024.1 postRoute report (Zybo CAN FD bench build) |
| `de25-standard` | `A5ED013BB32AE4SCS` | 36 pins | 46,800 ALMs | 358 M20Ks | Quartus Prime Pro 25.3.1 Fitter report (de25_datalogger build) |
| `polarfire-soc-discovery` | `MPFS095T-1FCSG325E` | 14 pins | 93,516 4LUTs | 308 LSRAMs (20 Kb) | Libero SoC 2025.2 compile netlist report (pfsoc_datalogger build of the default mix, 2026-11-16); the JTAG cost is from the 2026-11-05 build |

On `polarfire-soc-discovery`, QSPI is refused: neither calibration build has a QSPI instance, so no PolarFire cost exists for it yet. The DMA capacity line reports demand only, because no PolarFire DMA/FIC bandwidth figure is recorded in the repository.

#### CAN / CAN-FD

The CAN and CAN-FD data logger instances (`--can` / `--canfd` > 0) are project code; the configurator prints no third-party notice for them.
- The three product targets (`zybo_datalogger`, `de25_datalogger`, `pfsoc_datalogger`) build without the CTU CAN FD IP core.
- The CTU CAN FD IP core is used only by bench targets, which are not part of the distribution. Its licence is listed in [`LICENSE.md`](../../LICENSE.md).

### 2. Datalogger Gather Generator (`datalogger_gather_gen.py`)

Generates `datalogger_config_pkg.vhd` and `datalogger_gather.vhd` from a YAML configuration file.

```bash
python3 tools/datalogger-builder/datalogger_gather_gen.py my_datalogger_config.yml \
    --pkg-dir hw/rtl/pkg \
    --rtl-dir hw/rtl/datalogger/infra
```

#### Capture wiring: logical pins and board pins

A config numbers **logical** capture pins freely (the configurator counts them
`0..N`). `deepskopion_core` can capture only from the sources listed in
`board_pins.yml`: PMOD `JA0`-`JA7` / `JB0`-`JB7` (some carry an EMIO or
self-test mux), the REA JTAG tap (`REA_TCK`..`REA_TDO`) and the fabric CAN bus
(`CAN_BUS`). The generator owns the map between the two. It gives every
configured signal one board pin and emits the map as `C_CAPTURE_BOARD_PIN` in
`datalogger_config_pkg.vhd`. The core routes `capture_pin` from that map.

Allocation fills each board pin's preferred signal first (I2C on `JA0`-`JA3`,
SPI on `JA4`-`JA7`, UART on `JB0`-`JB1`, EDGE on `JB2`-`JB5`, JTAG on the REA
tap, CAN on the bus). It then fills the remaining general pins, `JB6`/`JB7`
first. A top-level `BoardPins: {<logical pin>: <board pin>}` map pins a signal
by hand. The configurator prints the resulting wiring table.

The configurator never places a CAN RX on the CAN bus tap: that source carries
the core's own CAN controllers, not the bus a user wires to the header. It gives
each CAN RX an external general pin and writes that choice into the mix as a
`BoardPins` entry, so the build wires the same pin. The shipped default mix
puts CAN RX on `JB5`.

The pin budget counts header pins on this allocation. A signal on an internal
source (the REA tap) uses no header pin, so the first JTAG datalogger takes
none; further instances take general pins. Every CAN datalogger takes a header
pin. Because of that, a mix that used to fit by putting CAN on the internal tap
can now be refused: on `polarfire-soc-discovery` the default mix with a fourth
EDGE datalogger asks for 15 header pins of 14 and is refused.

A JTAG datalogger reads the REA tap. The `de25-standard` and
`polarfire-soc-discovery` tops tie the tap inputs to constants
(`hw/rtl/de25/deepskopion_de25_top.vhd:822`,
`hw/rtl/pfsoc/deepskopion_pfsoc_fabric.vhd:793`), so on those boards a JTAG
datalogger records no live traffic and the configurator prints a warning.

A config that needs more capture sources than the core has is refused, by the
configurator and by the generator. Rather than build signals that read a
constant `'0'`, both stop. Set `Constraints.AllowUnwiredPins: true` to build
the extra signals unwired on purpose; the package then lists each one.

## Files

| File | Description |
| ---- | ----------- |
| `configurator.py` | CLI configurator and resource utilization estimator |
| `datalogger_gather_gen.py` | Main RTL and VHDL package generator script |
| `protocols.yml` | Protocol metadata registry (ports, generics, conditioning) |
| `board_pins.yml` | `deepskopion_core` capture sources and their allocation preferences |
| `config/datalogger_config.yml` | Tracked default configuration |
| `templates/` | Jinja2 templates for gather generation |
| `tests/` | Pytest test suite and test fixtures |
