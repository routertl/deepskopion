# RouteRTL SDK User Guide

Welcome to the RouteRTL SDK! This framework provides a unified build system, simulation engine, and verification tools for FPGA development.

The SDK is designed to be a lightweight dependency for your hardware projects, installed via pip.

```bash
# Install from PyPI (release)
pip install routertl

# Or install in editable mode for local development
pip install -e /path/to/routertl
```

> [!TIP]
> This architecture separates your proprietary RTL code and testbenches from the shared SDK build infrastructure. Use `rr sdk-path` to locate the installed SDK root.

## 1. System Requirements

The SDK is designed to work with your local tools.

- **Essential**: `python3` (3.8+), `make`
- **Recommended**:
  - **Simulation**: `ghdl` (open-source) or `nvc`
  - **Waveforms**: `gtkwave`
  - **Synthesis**: `vivado` (Xilinx), `quartus` (Intel/Altera)

The initialization script (`init_project.py`) checks your environment for these tools and **warns** you if they are missing. It does **not** stop execution, allowing you to install them later.

## 2. Quick Start: Day-1 Onboarding

Follow this step-by-step guide to go from a fresh clone to running simulations.

### Step 0: System Prerequisites

On a fresh Ubuntu/Debian system, ensure Python 3 and the build tools are installed:

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv git make build-essential
```

### Step 1: Clone Your Project

Clone your project repository:

```bash
git clone git@bitbucket.org:your-org/your-project.git
cd your-project
```

### Step 2: Bootstrap the Project

> [!IMPORTANT]
> On a fresh clone, install the RouteRTL SDK first to get the `routertl` CLI.

```bash
pip install routertl
# Or for local development:
# pip install -e /path/to/routertl

routertl workspace init
```

This will:

- Check for tools (`ghdl`, `nvc`, `make`, `gtkwave`).
- Create a Python virtual environment (`.venv`) if needed.
- Create your project structure (`src`, `sim`, `project.yml`) if it doesn't exist.

### Step 3: Install Dependencies & Activate

```bash
# Install all Python dependencies (creates .venv if needed)
make install

# Activate the environment (required for every new terminal session)
source .venv/bin/activate
```

After activation, the `routertl` command becomes available.

### Step 4: Verify Your Environment

```bash
# Check that all SDK tools are detected
routertl workspace check-env

# Verify the CLI is working — you should see all command groups
routertl --help
```

### Step 5: Run Your First Simulation

```bash
# Run all discovered tests
routertl sim --all

# Or run a specific testbench
routertl sim test_my_module
```

> [!TIP]
> Use `routertl sim` (no arguments) for an interactive test selection menu.

## 3. Project Setup

### Option A: Automatic Initialization

Use the included helper script to scaffold a new project:

```bash
routertl workspace init --name "my_project" --vendor xilinx
```

The command performs the following **SDK Automation**:

1. **Environment Check**: Verifies `ghdl`, `gtkwave`, and `make` are in PATH.
2. **Python Setup**: Creates a local `.venv` and installs dependencies.
3. **Project Scaffold**: Creates directories and configuration files.

The command will **interactively prompt** for:

- Top module name
- FPGA part number
- **Source directory** (default: `src`)
- **Simulation directory** (default: `sim`)
- **Constraints directory** (default: `xdc`)

**For non-interactive mode**, provide all arguments:

```bash
routertl workspace init \
    --name "my_project" \
    --vendor xilinx \
    --part "xc7z020clg400-1" \
    --language vhdl \
    --src-dir hw/rtl \
    --sim-dir hw/sim
```

### Option B: Manual Setup

1. **Create `project.yml`**: This is the heart of your project configuration.
2. **Create `Makefile`**: Include the SDK master makefile.

#### Minimal Makefile

```makefile
ROOT_DIR := $(shell pwd)
SDK_ROOT := $(shell rr sdk-path)
include $(SDK_ROOT)/tools/Common.mk
```

The `project.yml` file is the "single source of truth" for your project.

```yaml
project:
  name: my_datalogger_project
  top_module: uart_datalogger      # Synthesis top-level
  top_sim: tb_uart_datalogger      # Simulation top-level

hardware:
  vendor: xilinx                # xilinx, lattice, or altera
  part: "xc7z020clg400-1"
  language: vhdl                # vhdl, verilog, or systemverilog
  platform: my_custom_board

# Automatically find sources in src/ and the SDK
auto_sources: true

features:
  regbank: true                 # Enable automated register bank generation

simulation:
  test_dir: sim/cocotb/tests    # Custom location for tests

# Custom directory paths (for non-standard project structures)
paths:
  sources: hw/rtl               # Default: src
  simulation: hw/sim            # Default: sim
  constraints: hw/constraints   # Default: xdc

# Tool-specific tweaks
vendor_overrides:
  xilinx:
    enable_pblocks: true
  lattice:
    synthesis_tool: lse

# Quality control for Hooks
hooks:
  pre_commit:
    enabled: true
    lint: true
    check_yaml: true
    tests:
      - tb_uart_datalogger

dependencies:
  xpm_vhdl:
    url: https://github.com/Xilinx/XilinxPowerAndPerformance.git
    fallback_url: https://github.com/another-mirror/xpm_vhdl.git
    path: libs/xpm_vhdl
    library: xpm
```

> [!TIP]
> The `paths:` section allows you to use a non-standard directory structure. If omitted, defaults (`src`, `sim`, `xdc`) are used.

## 4. Quality Control

The SDK provides robust tools to ensure code quality and consistency.

### Git Hooks

The SDK provides a pre-configured git hook that runs linting and tests automatically before every commit.

**To Install:**

```bash
routertl workspace install-hooks
```

*This copies the hook script to `.git/hooks/pre-commit`.*

### Pre-Commit Configuration Rules

When configuring the `hooks.pre_commit` section in `project.yml`, follow these critical rules:

1. **Tests are Modules, NOT Paths**:
    The SDK's test runner expects **module names** (or patterns) to search for recursively within your `simulation.test_dir`.
    - **Correct**: `- test_my_block`
    - **Incorrect**: `- hw/sim/cocotb/tests/test_my_block.py`
    *(Providing full paths will cause the test runner to fail silently, resulting in a false positive pass.)*

2. **Wildcards & Batches**:
    You can use wildcards to run batches of tests.
    - `- "*"`: Runs all tests in the test directory.
    - `- "test_uart_*"`: Runs all tests matching the pattern.

3. **Exclusions**:
    Use the `exclude` section to skip specific tests or lint checks.

    ```yaml
    hooks:
      pre_commit:
        tests:
          - "*"               # Run everything by default
        exclude:
          tests:
            - test_heavy_integration  # Skip slow tests
          lint:
            - vendor/legacy/file.vhd  # Skip linting for legacy/vendor files
    ```

### Smart Linting

The SDK uses a "Smart Linter" that automatically detects hierarchy roots (forest detection) to optimize the linting process.

**Usage:**

```bash
# Auto-detect hierarchies and lint everything (default)
routertl linting

# Target a specific top-level module (resolves dependencies automatically)
routertl linting my_module
```

**How it works:**

1. **Forest Detection**: The linter scans your source files to identify independent hierarchies.
2. **Dependency Resolution**: For each hierarchy (or the specified `TOP`), it calculates the exact dependency tree.
3. **Result Summary**: At the end of the process, it prints a summary table listing each hierarchy and its Pass/Fail status.

> [!NOTE]
> The linter is language-aware. For pure Verilog/SystemVerilog projects (`hardware.language: systemverilog`), VHDL pre-compilation is automatically skipped.

## 5. Dependency Management

The SDK includes a dependency manager (`tools/project-manager/manage_dependencies.py`) to handle external libraries automatically.

**Configuration:**
Define dependencies in your `project.yml`:

```yaml
dependencies:
  library_name:
    url: https://git.example.com/repo.git
    path: libs/local_path  # Where to clone it
    library: lib_name      # VHDL library name
```

**Usage:**
Dependencies are automatically checked and cloned when you run implementation or simulation commands. You can also trigger it manually:

```bash
routertl workspace update
```

## 6. Development Workflow

- **Run Simulation**:
  - **Single test**: Set `SIM` (optional) and run the python script:

      ```bash
      export SIM=ghdl
      python3 sim/cocotb/tests/test_my_feature.py
      ```

  - **Regression (All tests)**: Auto-discover and run all `test_*.py` files:

      ```bash
      routertl sim --all
      ```

      *Configured via `simulation.test_dir` in `project.yml`.*

### 6.1 Simulation API

The SDK exposes a stable Python API for testbenches to ensure portability and ease of use.

```python
from routertl.sim import Tb, run_simulation, UartSource, SignalCollector

@cocotb.test()
async def my_test(dut):
    # Standardized testbench environment (handles Clock, Reset, Logging)
    tb = Tb(dut, clk="CLK", rst="RST")
    await tb.start_clock()
    await tb.reset()
    
    # Use standardized drivers
    uart = UartSource(dut.UART_RX, baud=115200)
    await uart.write_bytes([0xDE, 0xAD, 0xBE, 0xEF])
```

- **Tb**: The main testbench class wrapper.
- **run_simulation**: Python entry point for launching simulations.
- **Drivers/Monitors**: Common BFMs (UART, I2C, SPI) available directly from `routertl.sim`.
- **Build Bitstream**: `routertl bitstream`
- **Clean**: `routertl workspace clean`
- **Update SDK**: `routertl workspace update-sdk`
  - Pulls the latest version of the RouteRTL submodule, shows a changelog of new commits, and re-installs the CLI package.
- **Update Source Lists**:
  - `routertl workspace update --src`: Scans `src/` for new RTL files and appends them to `project.yml`.
  - `routertl workspace update --sim`: Scans `sim/` for new testbenches (`tb_*.vhd`) and appends them to `project.yml`.
  - `routertl workspace update --xdc`: Scans `xdc/` for new constraint files (`*.xdc`) and appends them to `project.yml`.
  - `routertl workspace update --tcl`: Scans `tcl/` for new Tcl scripts and hooks.
  - `routertl workspace update --all`: Runs all updates.
- **View Project Sources**: `make sources`
  - Displays a categorized list of all files currently tracked by the build system.
- **View Hierarchy**: `routertl hierarchy module_name`
  - Displays the dependency tree for the specified module.
- **Waveform Viewing**:
  - `make waves TOP=tb_name`: Opens the waveform for a previous simulation.
  - `routertl sim tb_name --waves`: Runs simulation and immediately opens the waveform.
  - *Simulations generate waveforms by default. To disable: `routertl sim`.*
- **RTL Schematics**: `routertl schematic module_name`
  - Generates an SVG schematic of the specified module (using Yosys/Netlistsvg) in `schematics/`.
- **Non-Project Mode Synthesis**: `routertl npm-synthesis module_name`
  - Runs Vivado synthesis for the specified module **without a full project file**.
  - Automatically resolves dependencies using `dependency_resolver.py`.
  - Outputs a `.dcp` checkpoint to `dcp/<module>/`.
  - Ideal for quick iteration on sub-modules or IP packaging.

## 7. Directory Structure

Recommended structure for your project:

```text
my-project/
├── project.yml
├── Makefile
├── src/                # Your RTL (VHDL/Verilog) code
├── sim/
│   └── cocotb/tests/   # Your Python tests
├── xdc/                # Constraints
└── .venv/              # Virtual environment (contains routertl SDK)
```

## 8. Build System Variables

The RouteRTL build system populates several variables from your `project.yml`. These are available in your `Makefile` after including `Common.mk`:

| Variable | Description |
|----------|-------------|
| `SYN_FILES` | List of synthesis source files |
| `SIM_FILES` | List of simulation-specific source files |
| `XDC_FILES` | List of constraint files |
| `XCI_FILES` | List of Xilinx IP core files |
| `PKG_FILES` | List of VHDL package files |
| `HDL_LANGUAGE` | HDL language from `project.yml` (`vhdl` or `systemverilog`) |
| `HDL_EXT` | File extension derived from language (`.vhd` or `.sv`) |

You can inspect the current values of these variables at any time by running:

```bash
make sources
```

## 9. How to Remove the SDK

To uninstall the SDK from your environment:

```bash
pip uninstall routertl
```

### Re-Initialize Project

If you need to wipe all generated artifacts (Makefile, project.yml, build folders) and start fresh:

```bash
make reinit-project
```

*WARNING: This deletes your `project.yml`. Use with caution.*

## 10. Register Bank & Versioning

The SDK handles register bank creation split into two parts: **Static Schema** and **Dynamic Build Info**.

### How It Works

1. **Static Schema**: You define `project_regbank.yml`. This dictates the memory map.
    - Generated to: `src/pkg/system_regbank_pkg.vhd` (Address constants, Types).
    - Status: **Always Created** (if YAML exists).
2. **Dynamic Build Info**: The SDK calculates version, git hash, and build date at **build time**.
    - Generated to: `hw/rtl/pkg/rom_info_pkg.vhd` (Constants: `C_GIT_HASH`, `C_BUILD_DATE`).
    - Status: **Always Updated** on every `routertl synthesis`.
    - The file is a gitignored build output, not committed. It is written by the `regbank-regen` pre_build hook, the `rom-info-sim` pre_sim hook, and `tools/ci/materialize_lint_packages.sh` before lint.
    - Within one build, `C_BUILD_DATE` and the creation line keep the value of the first stage. The `regbank-regen` hook keeps a copy of what it wrote in `.rom_info_stage_stamp`, in the `project.output_dir` of the active target (for example `build/zybo_datalogger/` after `rr target set zybo_datalogger`), and the next stage of the same build reuses its date. `rr bitstream run` stops when a pre_build hook output differs from what `rr synth run` used, so this copy is what lets the two stages run one after the other. A new commit, or a deleted build directory, gives the next build a new date. The active target is resolved as `rr` resolves it: `$RR_TARGET`, then the target `rr target set` selected, then `target:` in `project.yml` (`tools/active_target.py`).

> [!NOTE]
> The SDK does **NOT** modify your `project_regbank.yml`. It injects build values directly into the VHDL via `rom_info_pkg`.
> Regbank and ROM-info generators currently emit **VHDL only**. For SystemVerilog projects, the `system_config_pkg.sv` stub is generated but the full regbank pipeline requires porting.

### Managing Registers

You can add or remove registers interactively using Make targets:

```bash
# Interactive wizard to add a new register
routertl regbank add

# Remove a specific register by name
routertl regbank remove my_reg
```

### Manual Generation

While generation happens automatically during build, you can verify your schema manually:

```bash
# Generate VHDL packages from YAML
routertl regbank parse

# Generate ROM info package (Git hash, Build date)
routertl rom
```

## 11. Regression Testing

To ensure your project remains compatible with SDK updates, we recommend running the Integration Test suite.

**Should I add this to Pre-Commit?**
No. The full integration test (which runs `routertl synthesis`) is too heavy (minutes) for a pre-commit hook.

**Recommendation**:

- **Pre-Commit**: Run `routertl linting` (fast).
- **CI / Nightly**: Run the SDK integration test suite (comprehensive).

## 12. Test Generation Tools

The SDK includes `cocotb-testgen`, a tool to automatically generate Cocotb test stubs from your VHDL sources.

**Usage:**

**Option A: Make Target (Recommended)**

```bash
routertl testgen
```

*(Creates stubs in `sim/cocotb/tests` for sources in `src/`)*

**Option B: Manual Execution**

```bash
python3 "$(rr sdk-path)/tools/cocotb-testgen/gen_cocotb_structure.py" \
    --src src \
    --out sim/cocotb/tests
```

*(Use this for custom paths or source directories)*

See [tools/cocotb-testgen/README.md](https://github.com/routertl/deepskopion/blob/main/tools/cocotb-testgen/README.md) for full documentation.

## 13. VUnit: Native VHDL Testbenches

### Usage

```bash
# Run all VUnit tests
make vunit

# List available tests
make vunit-list

# Run with waveform viewer
make vunit-gui
```

See [sim/vunit/README.md](https://github.com/routertl/deepskopion/blob/main/sim/vunit/README.md) for full documentation on writing testbenches, adding DUT sources, and using the check library.

## 14. Docker Development Environment

The SDK includes pre-configured Docker environments for consistent, reproducible builds across different machines.

### Commands

| CLI Command | Make Equivalent | Description |
|---|---|---|
| `routertl docker build <env>` | `make docker-build DOCKER_ENV=<env>` | Build the Docker image for the specified environment |
| `routertl docker shell <env>` | `make docker-shell DOCKER_ENV=<env>` | Start an interactive shell inside the container |
| `routertl docker run <env> "<cmd>"` | `make docker-run CMD="<cmd>" DOCKER_ENV=<env>` | Run a single command inside the container |
| `routertl docker status` | — | Dashboard: volumes, images, containers, health checks |
| `routertl docker uninstall <env>` | — | Remove vendor tool Docker volume |
| `routertl docker config <env>` | — | Show/edit install configuration |
| `routertl docker setup` | — | Interactive wizard to configure license and environment |

### Available Environments

| Environment | Purpose | Base Image | Key Tools |
|---|---|---|---|
| `sim` | Open-source simulation | Ubuntu 22.04 | NVC 1.18.2, Verilator 5.036, GHDL, Cocotb 2.0.1 |
| `vivado` | Xilinx FPGA builds | `sim` | Extends sim + Vivado (requires local installer mount) |
| `quartus` | Intel/Altera FPGA builds | **`sim`** | Extends sim + Quartus Prime (unattended install) |
| `questa` | Siemens verification | **`sim`** | Extends sim + Questa (unattended install) |
| `riviera` | Aldec verification | **`sim`** | Extends sim + Riviera-PRO (unattended install) |
| `buildroot` | Embedded Linux cross-compilation | Ubuntu 22.04 | ARM/AArch64 toolchains, bootgen, dtc |

> [!WARNING]
> **Image Dependency Chain**: The `quartus`, `questa`, and `riviera` images all extend `routertl-sim:latest`. You **must** build the `sim` image first before building any of these:
> ```bash
> routertl docker build sim      # Build base image first
> routertl docker build quartus   # Then build the dependent image
> ```

### Usage Examples

```bash
# Build the base simulation image
routertl docker build sim

# Start an interactive shell in the sim container
routertl docker shell sim

# Run the full regression suite inside Docker
routertl docker run sim "routertl sim --all"

# Use the Vivado environment
routertl docker shell vivado

# Enter the Buildroot cross-compilation container
routertl docker shell buildroot
```

> [!TIP]
> The container mounts your project directory. Changes inside the container are reflected on your host filesystem.

See [tools/docker/README.md](https://github.com/routertl/deepskopion/blob/main/tools/docker/README.md) for the full Docker documentation.

## 15. Advanced Tools

The SDK includes specialized tools for power users in `tools/`.

### ILA Generator (`tools/ila-gen`)

Automates the creation of Xilinx Integrated Logic Analyzers (ILA).

- **Input**: YAML configuration defining signals to probe.
- **Output**: VHDL/Verilog instantiation snippet + TCL debug setup.
- **Usage**: `python3 "$(rr sdk-path)/tools/ila-gen/gen_ila.py" config.yaml`

### Report Analyzer (`tools/report-analyzer`)

Scripts to parse and filter verbose Vivado reports.

- `filter_control_sets.py`: Analyzes control set reduction issues.
- `utilization_filter.py`: Extracts hierarchical utilization for specific modules.

See [tools/report-analyzer/README.md](https://github.com/routertl/deepskopion/blob/main/tools/report-analyzer/README.md) for details.

## 16. Detailed Command Reference

The sections below power `routertl help <command>`. Each heading matches a
Make target name so the deep-help engine can extract it automatically.

> [!TIP]
> You can type `help` at the end of any command instead of `--help`:
> `rr docker help`, `rr docker build help`, `rr workspace help`, etc.

### docker

**Docker Container Development Environment**

The Docker subsystem provides reproducible, containerized build and simulation
environments. All containers mount your project directory so file changes are
reflected on the host.

**Subcommands:**

| CLI | Make Equivalent | Description |
|---|---|---|
| `routertl docker build <env>` | `make docker-build DOCKER_ENV=<env>` | Build Docker image |
| `routertl docker shell <env>` | `make docker-shell DOCKER_ENV=<env>` | Interactive shell |
| `routertl docker run <env> "<cmd>"` | `make docker-run CMD="<cmd>" DOCKER_ENV=<env>` | Run one-shot command |
| `routertl docker status` | — | Show RouteRTL volumes and containers |
| `routertl docker uninstall <env>` | — | Remove vendor tool Docker volume |
| `routertl docker setup` | — | Interactive license/environment wizard |

**Environments:** `sim`, `vivado`, `quartus`, `questa`, `riviera`, `buildroot`

> **Warning**: `quartus`, `questa`, and `riviera` extend the `sim` base image.
> Build `sim` first: `routertl docker build sim`

### docker-build

Build a Docker image for a specific environment. On first run this may take
10-15 minutes as it downloads and installs all dependencies.

**Usage:** `routertl docker build <env>`

**Examples:**

```bash
routertl docker build sim        # Build base simulation image
routertl docker build vivado     # Build Vivado image (extends sim)
routertl docker build quartus    # Build Quartus image (requires sim)
```

### docker-shell

Start an interactive Bash shell inside a Docker container. Your project
directory is automatically mounted at `/home/<user>/work`.

**Usage:** `routertl docker shell <env>`

**Examples:**

```bash
routertl docker shell sim          # Enter simulation container
routertl docker shell buildroot    # Enter embedded Linux container
```

### docker-run

Run a single command inside a Docker container and exit. Useful for CI/CD
pipelines or scripted builds.

**Usage:** `routertl docker run <env> "<command>"`

**Examples:**

```bash
routertl docker run sim "routertl sim --all"
routertl docker run sim "make regression"
routertl docker run buildroot "make build-kernel"
```

### docker-status

Full Docker environment dashboard. Shows everything at a glance: what's
installed, what's running, and whether your license configuration is valid.

**Usage:** `routertl docker status`

**Output sections:**

- **Volumes** — `routertl_*_opt` named volumes with disk usage
- **Images** — `routertl-*` Docker images with size and build age
- **Containers** — running/stopped containers with status indicators
- **Health** — Docker daemon reachability, `LM_LICENSE_FILE` presence, MAC override status

### docker-uninstall

Remove the Docker volume for a vendor tool installation. Prompts for
confirmation before deletion. The volume can be recreated by rebuilding
the Docker environment.

**Usage:** `routertl docker uninstall <env>`

**Arguments:**

- `ENV` — Target environment: `vivado` or `quartus`.

**Examples:**

```bash
routertl docker uninstall quartus   # Remove Quartus installation volume
routertl docker uninstall vivado    # Remove Vivado installation volume
```

> After uninstalling, reinstall with: `routertl docker build <env>`

### docker-setup

Interactive wizard that configures license and environment variables
for Docker EDA containers. Prompts for each value and writes them to
a `.env` file in the project root, which Docker Compose reads
automatically.

**Usage:** `routertl docker setup`

**Variables configured:**

| Variable | Purpose | Example |
|---|---|---|
| `LM_LICENSE_FILE` | FlexLM license server | `2100@lic-server.company.com` |
| `MGLS_LICENSE_FILE` | Siemens/Mentor license server | `1717@lic-server.company.com` |
| `EDA_MAC_ADDRESS_OVERRIDE` | Node-locked license MAC | `00:1A:2B:3C:4D:5E` |

**Behavior:**

- Reads existing `.env` values as defaults for each prompt
- Preserves existing `.env` content (comments, other variables)
- Updates existing keys in-place; appends new keys at the end
- Skips writing if no values are entered

> Verify the configuration with: `routertl docker status`

### docker-config

Show or edit the install configuration for an EDA tool (Vivado or
Quartus). These config files control unattended installer options.

**Usage:** `routertl docker config <env> [--edit]`

**Examples:**

```bash
routertl docker config vivado          # Show config
routertl docker config vivado --edit   # Open in $EDITOR
routertl docker config quartus         # Show Quartus config
```

### sim

Run a Cocotb simulation testbench. If no testbench name is provided, an
interactive menu appears letting you select from discovered tests.

**Usage:** `routertl sim [TESTBENCH] [--waves] [--all] [--seed N] [--tag NAME]`

**Options:**

- `--waves` — Open GTKWave after simulation completes
- `--all`   — Run all discovered tests (full regression)
- `--seed N` — Fixed random seed for reproducible runs
- `--history` — Show recent simulation history
- `--tag NAME` — Run a named group of tests from `simulation.tags` in project.yml

**Examples:**

```bash
routertl sim                       # Interactive test picker
routertl sim test_my_module        # Run specific test
routertl sim --all                 # Full regression
routertl sim test_my_module --waves  # Run and open waveform
routertl sim --tag smoke           # Run the 'smoke' tag group
routertl sim --tag uart --seed 42  # Run 'uart' tag with fixed seed
```

**Tag Configuration** (`project.yml`):

```yaml
simulation:
  test_dir: sim/cocotb/tests
  tags:
    smoke: [test_edge_counter, test_lfsr]
    uart:  [test_uart_datalogger, test_uart_master]
    full:  [test_edge_counter, test_lfsr, test_uart_datalogger]
```

Each test in the tag is run sequentially. A pass/fail summary is printed at
the end, and the command exits non-zero if any test fails.

> Set `WAVES=0` to disable waveform generation for faster simulation.

### scan-tests

List all available Cocotb test modules discovered in the configured simulation
directory. Searches for `test_*.py` files recursively.

**Usage:** `routertl scan-tests`

The output shows module names that can be passed to `routertl sim <name>`.

### status

Display a project health dashboard showing system state at a glance.

**Usage:** `routertl status`

Provides:

- **Project identity** — name, top module, FPGA part, HDL language
- **SDK version** — with commit hash and remote-behind warnings
- **Discovered tests** — count and test directory location
- **Source files** — categorized counts (syn, sim, constraints, IP, TCL)
- **Dependencies** — clone status of external libraries
- **Embedded Linux** — memory map presence and device count
- **Features & Hooks** — enabled feature flags and pre-commit config
- **Warnings** — stale `build_env.mk`, outdated SDK, missing tests

### waves

Open GTKWave to inspect waveform files (`.ghw`, `.vcd`, `.fst`) from a
previous simulation run.

**Usage:** `routertl waves <module>`

**Example:** `routertl waves test_uart_datalogger`

### linting

Run the RouteRTL Smart Linter, which auto-detects design hierarchies and
checks them using GHDL `--std=08` analysis. Supports incremental mode to
skip unchanged files.

**Usage:** `routertl linting [TOP]`

**Examples:**

```bash
routertl linting               # Lint all hierarchies
routertl linting my_top        # Lint specific hierarchy
```

### hierarchy

Display the VHDL/Verilog dependency tree for a given top-level module.
Useful for understanding source file ordering and missing dependencies.

**Usage:** `routertl hierarchy <TOP>`

**Example:** `routertl hierarchy uart_datalogger`

### schematic

Generate an SVG schematic from RTL using Yosys and netlistsvg. Requires
`yosys` (with GHDL plugin for VHDL) and `netlistsvg` to be installed.

**Usage:** `routertl schematic <TOP>`

**Example:** `routertl schematic my_top`

### workspace

Project workspace and configuration management. Groups several subcommands
for maintaining the project lifecycle.

**Subcommands:**

| Command | Description |
|---|---|
| `workspace update [--all\|--src\|--sim\|--xdc\|--tcl]` | Scan and update project.yml |
| `workspace install` | Create venv and install Python dependencies |
| `workspace install-hooks` | Install git pre-commit hooks |
| `workspace check-env` | Verify SDK tool availability |
| `workspace clean` | Remove build artifacts |
| `workspace sources` | Show current source file lists |
| `workspace update-config` | Regenerate build_env.mk |
| `workspace update-sdk` | Update SDK package |
| `workspace reinit` | Wipe and re-initialize project |
| `workspace xdc-template <TOP>` | Generate XDC constraint template |
| `workspace rebrand` | Re-create CLI aliases (rr + branded name) |
| `workspace repair` | Nuke and rebuild .venv from scratch |
| `workspace cicd` | Update CI/CD config |
| `workspace pre-build` | Run pre-build code generation hooks |

### workspace-rebrand

Re-create CLI shortcut aliases from the current `project.yml` branding
configuration. Always creates the `rr` shortcut, and optionally creates a
custom-branded alias from `branding.cli_name`.

**Usage:** `routertl workspace rebrand`

Safe to re-run whenever `project.yml` changes. Requires an active virtualenv.

**Configuration** (`project.yml`):

```yaml
branding:
  cli_name: mysdk       # Creates: mysdk → routertl
```

> The WSL setup wizard automatically calls `workspace rebrand` on first run.

### workspace-repair

Desperate recovery for when the CLI or virtual environment is broken.
Diagnoses common corruption issues, deletes the old `.venv`, creates
a fresh one, re-installs the SDK, and rebuilds CLI shortcuts.

**Usage:** `routertl workspace repair [--force]`

**Options:**

- `--force` — Force repair even if `.venv` appears healthy.

**What it fixes:**

- Broken Python symlinks (e.g. after OS upgrade or `brew update`)
- Missing or corrupted pip
- Stale entry points (interrupted `pip install`)
- Missing SDK package (no `routertl` command)

> [!TIP]
> If the CLI itself is completely broken, reinstall from scratch:
> ```bash
> rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate && pip install routertl
> ```

### linux

Embedded Linux generation and build tools. Manages the full pipeline from
BSP generation to SD card image assembly.

**Subcommands:**

| Command | Description |
|---|---|
| `linux build` | Full build pipeline (BSP → DTB → FSBL → U-Boot → Kernel → SD) |
| `linux gen` | Generate all software bindings |
| `linux dtbo` | Generate Device Tree Overlay |
| `linux c-headers` | Generate C UIO headers |
| `linux python` | Generate Python UIO drivers |
| `linux uboot` | Cross-compile U-Boot |
| `linux kernel` | Cross-compile Linux Kernel |
| `linux bsp` | Generate Board Support Package |
| `linux sdcard` | Assemble BOOT.bin and SD card |
| `linux bootscr` | Compile boot script |
| `linux crosscheck` | Cross-check memory map vs hardware |
| `linux fetch-sw` | Fetch custom software repos |
| `linux mmap` | Update Vivado address map from YAML |

### diff

Analyze `git diff` output and suggest which tests to re-run based on the
reverse dependency tree. Parses changed files, filters for RTL sources,
and walks the dependency graph upward to find all affected entities.

**Usage:** `routertl diff [REF]`

**Arguments:**

- `REF` — *(optional)* Git ref to diff against (`HEAD~1`, `main`, a commit
  hash, etc.). When omitted, the unstaged working-tree diff is used.

**Options:**

- `--src` — Source directory to scan (default: from `project.yml` or `src`).

**Examples:**

```bash
routertl diff                # Show impact of unstaged changes
routertl diff HEAD~1         # Last commit
routertl diff main           # Diff against main branch
```

**Output:**

A Rich table mapping each affected entity/module to its matching test
module, with ready-to-copy `routertl sim <test>` commands.

> The matching is heuristic: entity `uart_datalogger` maps to
> `test_uart_datalogger`. If no match is found the entity is still listed
> for manual review.

### completion

Generate shell completion scripts for the RouteRTL CLI. Supports Bash,
Zsh, and Fish. The generated scripts use Click's `_COMPLETE` environment
variable protocol for dynamic completions.

**Usage:** `routertl completion <SHELL>`

**Arguments:**

- `SHELL` — Target shell: `bash`, `zsh`, or `fish`.

**Installation:**

```bash
# Bash — add to ~/.bashrc
eval "$(routertl completion bash)"

# Zsh — add to ~/.zshrc
eval "$(routertl completion zsh)"

# Fish — add to config.fish
routertl completion fish | source
```

**Persist to a file (recommended):**

```bash
# Bash
routertl completion bash > ~/.local/share/bash-completion/completions/routertl

# Zsh
routertl completion zsh > ~/.zfunc/_routertl

# Fish
routertl completion fish > ~/.config/fish/completions/routertl.fish
```

> The `sim` command's TESTBENCH argument includes a tab-completion
> callback that returns discovered test names from `project.yml`.


### report

Generate a unified project report that aggregates lint, simulation, and
synthesis results into a single view. Supports Rich console output (default)
or CI-friendly markdown.

**Usage:** `routertl report [--format rich|md] [-o FILE]`

**Options:**

- `--format rich` — Rich-formatted console output (default)
- `--format md`   — Markdown output, suitable for CI artifacts
- `-o FILE`       — Write report to a file instead of stdout

**Data Sources:**

| Source | Location | What's Reported |
|---|---|---|
| Simulation | `sim/results/<module>/latest.json` | Per-module pass/fail/duration |
| Lint | `sim/lint.log` | PASS / FAIL status |
| Synthesis | `logs/regression_summary.txt` | Build status |

The report also includes **performance regression warnings** if any module's
latest run exceeds its 10-run rolling average by more than 40%.

**Examples:**

```bash
# Rich console report
routertl report

# Save markdown report for CI
routertl report --format md -o report.md
```


## 17. Project Reporting

The `routertl report` command provides a single-command overview of your
project's build and verification status. It aggregates data from three sources:

1. **Simulation results** — scans `sim/results/<module>/latest.json` for every
   module that has been simulated.
2. **Lint status** — reads `sim/lint.log` to determine PASS/FAIL.
3. **Synthesis status** — reads `logs/regression_summary.txt`.

### Console Output

```bash
routertl report
```

Produces a Rich-formatted table showing per-module pass/fail counts, durations,
and an overall summary. Performance regression warnings appear inline.

### Markdown Output

```bash
routertl report --format md -o build_report.md
```

Generates a CI-friendly markdown file. This is useful for:

- Attaching to pull requests
- Archiving in CI pipelines
- Embedding in GitHub/Bitbucket comments

## 18. Performance Regression Detection

The SDK automatically tracks simulation duration history and flags tests that
show unexpected slowdowns. This feature is built into `result_collector.py`
and runs as part of every `collect_results()` call.

### How It Works

1. **History tracking** — each simulation run appends a timestamped entry to
   `sim/results/history.jsonl`, including per-test durations.
2. **Rolling average** — the detector computes a 10-run rolling average for
   the module's total duration and for each individual test.
3. **Threshold check** — if the latest run exceeds the average by more than
   **40%**, a warning is emitted to the console.

### Configuration

| Constant | Default | Description |
|---|---|---|
| `REGRESSION_WINDOW` | 10 | Number of prior runs used for the rolling average |
| `REGRESSION_THRESHOLD` | 0.40 | Fraction above average that triggers a warning |

### Example Output

```text
⚠️  Performance regression detected for test_uart_datalogger:
    Latest: 4.20s  |  Avg (10 runs): 2.80s  |  +50.0%
    └─ test_sweep_baud: 3.1000s (avg 1.8000s, +72.2%)
```

> [!NOTE]
> Regression detection is **advisory only** — it never fails the build or
> blocks a commit. It provides early visibility into slowdowns that might
> indicate an RTL or testbench issue.

## 19. Plugin System

The SDK supports external CLI plugins that extend `routertl` with new
commands. Plugins are discovered automatically via Python entry points.

### Discovery Mechanism

At startup, the CLI calls:

```python
importlib.metadata.entry_points(group="routertl.plugins")
```

Each entry point must resolve to a `click.BaseCommand` (command or group).
The entry point's name becomes the CLI subcommand name.

### Creating a Plugin

1. Create a Python package with a Click command:

    ```python
    # my_plugin/cli.py
    import click

    @click.command()
    @click.argument("name")
    def greet(name):
        """Say hello from a plugin."""
        click.echo(f"Hello, {name}!")
    ```

2. Register the entry point in `pyproject.toml`:

    ```toml
    [project.entry-points."routertl.plugins"]
    greet = "my_plugin.cli:greet"
    ```

3. Install and use:

    ```bash
    pip install -e ./my-routertl-plugin
    routertl greet World
    ```

### Plugin Contract

| Requirement | Details |
|---|---|
| Entry point group | `routertl.plugins` |
| Return type | `click.BaseCommand` (command or group) |
| Naming | Entry point name = CLI subcommand name |
| Failures | Logged as warnings; never crash the CLI |

> [!TIP]
> For full examples including command groups and debugging tips, see
> See the Plugin Development guide for details.


### watch

Auto-rerun a simulation whenever source or test files change. Uses a
polling file watcher with **no external dependencies** — monitors `src/`
and `sim/` directories for RTL and Python file changes.

**Usage:** `routertl watch [TESTBENCH] [--interval SECS] [--src DIR] [--sim DIR]`

**Options:**

- `--interval, -i` — Polling interval in seconds (default: 1.5)
- `--src` — Override source directory to watch
- `--sim` — Override simulation directory to watch

**Monitored extensions:** `.vhd`, `.vhdl`, `.v`, `.sv`, `.vh`, `.svh`, `.py`

**Examples:**

```bash
routertl watch test_uart_datalogger       # Watch and rerun on change
routertl watch                         # Rerun full simulation suite
routertl watch test_edge_counter -i 2  # Custom poll interval
```

**How it works:**

1. Takes an initial snapshot of file modification times.
2. Polls at the configured interval.
3. On change detection, runs `make sim MODULE=<test>` (or `make simulation`).
4. After the simulation completes, takes a new snapshot and continues watching.
5. Press `Ctrl+C` to stop.

> Watch mode reads `paths.sources` and `simulation.test_dir` from
> `project.yml` for directory discovery.

### deps

Inspect project dependencies or export the full dependency graph.
Without `--graph`, prints the dependency tree for a given entity.
With `--graph`, exports the entire dependency forest.

**Usage:** `routertl deps [ENTITY] [--graph FORMAT] [-o FILE] [--src DIR]`

**Options:**

- `--graph FORMAT` — Export graph (choices: `mermaid`, `html`, `json`)
- `-o, --output FILE` — Write graph to a file instead of stdout
- `--src` — Override source directory to scan

**Examples:**

```bash
routertl deps                              # Show forest summary
routertl deps uart_datalogger                 # Print tree for entity
routertl deps --graph mermaid              # Export mermaid to stdout
routertl deps --graph html -o deps.html    # Save interactive HTML
routertl deps --graph json                 # JSON graph to stdout
```

**Output formats:**

| Format | Description |
|---|---|
| `mermaid` | Mermaid `graph TD` block with colored nodes (green=root, blue=package, purple=module) |
| `html` | Self-contained HTML page with mermaid.js CDN — open in any browser |
| `json` | Structured JSON with nodes, edges, roots, packages, and orphans |

> The graph is built from the `DependencyResolver.export_graph()` method,
> which can also be called programmatically from Python.
