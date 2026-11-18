# RouteRTL SDK v3.0 Simulation Migration Report

This document outlines the key changes required to successfully migrate custom simulation environments to the RouteRTL SDK v3.0 standard, specifically focusing on how simulation dependencies, project structures, and module imports have evolved in the new python-driven pipeline.

## 1. Dependency Management (XPM & Third-Party Libraries)

In previous versions, testbench scripts like `test_uart_datalogger.py` often included explicit, hardcoded arrays of file paths passed directly to the simulation engine (e.g., `custom_libraries={"xpm": [...]}`).

**The SDK v3.0 Flow:**
The SDK now reads dependencies directly from `project.yml`. The `routertl sim` wrapper ensures that the VHDL components listed in the project file are correctly compiled before running the Cocotb test.

### Required Changes:
1. **Remove hardcoded library blocks** from your python testbenches. Do not call `engine.simulation.run_simulation` with manual `custom_libraries` dictionaries.
2. **Define dependencies in `project.yml`**:
   ```yaml
   dependencies:
     xpm_vhdl:
       url: https://github.com/fransschreuder/xpm_vhdl
       path: libs/xpm_vhdl
       library: xpm  
   ```
3. **Automatic Dependency Fetching**: The `routertl` CLI and underlying Makefiles will *automatically* `git clone` the dependencies from the provided `url` into the defined `path` during initialization, simulation, or linting. If a dependency exists but is an empty directory (or missing its `.git` subfolder due to dirty legacy states), this automatic clone will fail. In that case, simply delete the empty directory so the SDK can fetch it properly.

## 2. Dynamic Library Parsing

The SDK scans the dependent files recursively, grouping them by the `library` assigned in `project.yml` (e.g., `tich`, `utils`, `xpm`). During simulation, it will parse the VHDL/Verilog files to determine topological compilation order and inject these dynamically into the simulator. Testbenches no longer need explicit knowledge of library file paths.

## 2. Testbench Execution

Previously, test scripts could be executed standalone using `python test_module.py` because they contained a `main()` function invoking the simulator engine directly.

**The SDK v3.0 Flow:**
Simulations should be executed using the SDK command `routertl sim test_module` (which redirects to `make sim MODULE=test_module`). The SDK wrapper handles:
- Parsing generic overrides.
- Compiling dependencies and the `tich` library.
- Establishing the `PYTHONPATH` properly.

### Required Changes:
1. Remove the standalone `if __name__ == "__main__":` block that invokes `run_simulation()` with hardcoded configurations.
2. Rely entirely on the SDK Makefile target. The test module itself only needs to contain `cocotb.test()` decorated async functions.

## 3. Python Verification IP (VIP) Import Paths

When moving project-specific drivers and monitors out of the SDK repository and into the local project (e.g., `hw/sim/vip`), relative imports and flat `sys.path` injections can fail because of how the new SDK launches the Python process from `ROOT_DIR`.

**The SDK v3.0 Flow:**
The RouteRTL simulation wrapper sets `PYTHONPATH=$(ROOT_DIR)`. Therefore, all local python packages must be imported using their absolute path relative to the root directory.

### Required Changes:
1. **Remove intrusive `sys.path` hacks** from your testbenches:
   ```python
   # ❌ REMOVE THIS:
   sys.path.insert(0, str(DEEPSKOPION_ROOT / "hw" / "sim"))
   ```
2. **Use absolute imports explicitly** starting from the directory that resides at the project root:
   ```python
   # ❌ Old Way (Relative or broken flat import)
   from vip.drivers.uart_datalogger_driver import UartDriver
   from .base import DataloggerDriver

   # ✅ New Way (Absolute path from project root)
   from hw.sim.vip.drivers.uart_datalogger_driver import UartDriver
   from hw.sim.vip.drivers.base import DataloggerDriver
   ```
3. **Update SDK Native Imports**: Update imports pulling from the SDK to reflect the new `sim/cocotb/engine` and `sim/cocotb/tb` structure.
   ```python
   # ✅ Native RouteRTL SDK components
   from tb.env import TbEnv
   from tb.scoreboard import Scoreboard
   from tb.drivers.uart_master import UartSource
   ```

## Summary Checklist
- [ ] Dependencies (like XPM) are recorded in `project.yml`.
- [ ] No more explicit Yosys/GHDL compilation lists inside Python test scripts.
- [ ] `PYTHONPATH` hacks removed from the test scripts.
- [ ] Local VIP package references converted to absolute imports (e.g., `hw.sim.vip...`).
- [ ] Simulations launched strictly via `make sim MODULE=...` or `routertl sim...`.
