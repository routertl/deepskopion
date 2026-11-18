"""
UART Datalogger Prescaler Sweep Test
Validates the G_TICK_PRESCALER generic at different baud rates.

Each prescaler value requires a separate simulation (different generic).
Scenarios:
  - Prescaler 4  @ 1.5 Mbps  : mid-range speed, tick-compressed widths
  - Prescaler 2  @ 3.0 Mbps  : higher speed, fewer ticks per bit
  - Prescaler 1  @ 6.0 Mbps  : max speed, no tick compression (1 tick = 1 clk)
  - Prescaler 16 @ 9600 baud  : slow speed, aggressive compression
"""

import sys
import os
from pathlib import Path
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

# Locate routertl SDK cocotb dir via the pip-installed routertl_core
# (DS-P2.29 fallout — old shim walked up the tree for an `engine/`
# folder that no longer exists post-pip-decouple).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

from engine.simulation import run_simulation
from uart_datalogger_test_utils import (
    setup_dut, apply_config, GENERICS, CUSTOM_LIBRARIES,
    UartSource, run_parameterized_test
)


# Read baud from env (set per-scenario by main())
BAUD_RATE = int(os.environ.get("TEST_BAUD", "1500000"))


@cocotb.test()
async def test_prescaler_calibration(dut):
    """Test calibration + lock at the given baud rate and prescaler."""
    await run_parameterized_test(dut, BAUD_RATE, bits=8, parity="none",
                                stop_bits=1)


# ── Scenario definitions ─────────────────────────────────────────────
SCENARIOS = [
    {"prescaler": 4,  "baud": 1_500_000, "label": "1.5M_ps4"},
    {"prescaler": 2,  "baud": 3_000_000, "label": "3M_ps2"},
    {"prescaler": 1,  "baud": 6_000_000, "label": "6M_ps1"},
    {"prescaler": 16, "baud": 9_600,     "label": "9600_ps16"},
]


def main():
    results = []
    for scenario in SCENARIOS:
        label = scenario["label"]
        print(f"\n{'='*60}")
        print(f"  SCENARIO: {label}  (prescaler={scenario['prescaler']}, "
              f"baud={scenario['baud']})")
        print(f"{'='*60}")

        gen = dict(GENERICS)
        gen["G_TICK_PRESCALER"] = scenario["prescaler"]

        env = {"TEST_BAUD": str(scenario["baud"])}

        try:
            run_simulation(
                top_level="uart_datalogger",
                module="test_uart_prescaler_sweep",
                top_level_lang="vhdl",
                generics=gen,
                waves=False,
                work_lib="tich",
                extra_env=env,
                custom_libraries=CUSTOM_LIBRARIES,
            )
            results.append((label, "PASS"))
        except Exception as e:
            results.append((label, f"FAIL: {e}"))

    # Print summary
    print(f"\n{'='*60}")
    print("  PRESCALER SWEEP SUMMARY")
    print(f"{'='*60}")
    for label, status in results:
        marker = "✓" if status == "PASS" else "✗"
        print(f"  {marker} {label:20s} {status}")

    passed = sum(1 for _, s in results if s == "PASS")
    failed = len(results) - passed
    print(f"\n  TOTAL: {passed} PASSED, {failed} FAILED")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
