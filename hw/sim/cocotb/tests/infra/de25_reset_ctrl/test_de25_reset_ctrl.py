"""Adversarial cocotb test for the DE25 fabric reset (DS-P1.80).

de25_reset_ctrl synchronises ninit_done and pll_locked into clk_i on separate
chains (G_STAGES = 3) and registers their combination, so rst_o:
  - is '1' while either source reports not ready;
  - falls exactly G_STAGES + 1 = 4 rising edges after the LATER source
    reports ready;
  - rises exactly 4 rising edges after either source reports not ready.

Inputs change halfway between rising edges, so the edge count is exact.
Expected latencies are hard-coded. A failure here is a real defect in the
reset path: diagnose it, do not soften the check.

Mutants this test kills (checked when it was written):
  - combine with AND instead of OR NOT (releases while the PLL is unlocked);
  - a chain that drops a stage, or a combination taken before the chains
    (latency differs from 4);
  - power-up value '0' on rst_r or the init chain (reset low at power-up).
"""

from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge, Timer

import routertl_core as _rrc

sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: Could not import engine. Run via `rr sim run`.")
    sys.exit(1)


def _find_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "project.yml").exists() and (p / "hw" / "rtl").is_dir():
            return p
    raise RuntimeError("deepskopion root (project.yml + hw/rtl) not found")


DEEPSKOPION_ROOT = _find_root(Path(__file__).resolve())

CLK_PERIOD_NS = 10
LATENCY_EDGES = 4  # G_STAGES (3) + the combining register
IDLE_EDGES = 20    # longer than any chain, to prove a held value stays held


async def _edges_until(dut, value: int, limit: int) -> int:
    """Count rising edges until rst_o reads `value` after an edge."""
    for n in range(1, limit + 1):
        await RisingEdge(dut.clk_i)
        await ReadOnly()
        if int(dut.rst_o.value) == value:
            return n
    raise AssertionError(f"rst_o did not reach {value} within {limit} edges")


async def _hold(dut, value: int, edges: int, why: str) -> None:
    for n in range(edges):
        await RisingEdge(dut.clk_i)
        await ReadOnly()
        assert int(dut.rst_o.value) == value, f"{why}: rst_o={dut.rst_o.value} at edge {n + 1}"


async def _between_edges(dut) -> None:
    await FallingEdge(dut.clk_i)


async def _power_up(dut) -> None:
    """Sources not ready; rst_o must read '1' from the first edge on."""
    dut.ninit_done_i.value = 1
    dut.pll_locked_i.value = 0
    await Timer(1, "ns")
    assert int(dut.rst_o.value) == 1, "rst_o must power up asserted"
    cocotb.start_soon(Clock(dut.clk_i, CLK_PERIOD_NS, unit="ns").start())
    await _hold(dut, 1, IDLE_EDGES, "both sources not ready")


@cocotb.test()
async def test_release_pll_first_then_init(dut):
    """PLL locks, then the SDM finishes: release is timed from ninit_done."""
    await _power_up(dut)
    await _between_edges(dut)
    dut.pll_locked_i.value = 1
    await _hold(dut, 1, IDLE_EDGES, "PLL locked, SDM still configuring")
    await _between_edges(dut)
    dut.ninit_done_i.value = 0
    n = await _edges_until(dut, 0, IDLE_EDGES)
    assert n == LATENCY_EDGES, f"release took {n} edges after ninit_done fell, expected {LATENCY_EDGES}"
    await _hold(dut, 0, IDLE_EDGES, "both sources ready")


@cocotb.test()
async def test_release_init_first_then_pll(dut):
    """The SDM finishes, then the PLL locks: release is timed from pll_locked."""
    await _power_up(dut)
    await _between_edges(dut)
    dut.ninit_done_i.value = 0
    await _hold(dut, 1, IDLE_EDGES, "SDM done, PLL not locked")
    await _between_edges(dut)
    dut.pll_locked_i.value = 1
    n = await _edges_until(dut, 0, IDLE_EDGES)
    assert n == LATENCY_EDGES, f"release took {n} edges after pll_locked rose, expected {LATENCY_EDGES}"
    await _hold(dut, 0, IDLE_EDGES, "both sources ready")


@cocotb.test()
async def test_release_both_together(dut):
    """Both sources report ready between the same two edges."""
    await _power_up(dut)
    await _between_edges(dut)
    dut.ninit_done_i.value = 0
    dut.pll_locked_i.value = 1
    n = await _edges_until(dut, 0, IDLE_EDGES)
    assert n == LATENCY_EDGES, f"release took {n} edges, expected {LATENCY_EDGES}"


@cocotb.test()
async def test_loss_of_lock_reasserts(dut):
    """After release, pll_locked falling re-asserts rst_o in 4 edges."""
    await _power_up(dut)
    await _between_edges(dut)
    dut.ninit_done_i.value = 0
    dut.pll_locked_i.value = 1
    await _edges_until(dut, 0, IDLE_EDGES)
    await _hold(dut, 0, IDLE_EDGES, "both sources ready")
    await _between_edges(dut)
    dut.pll_locked_i.value = 0
    n = await _edges_until(dut, 1, IDLE_EDGES)
    assert n == LATENCY_EDGES, f"re-assert took {n} edges after pll_locked fell, expected {LATENCY_EDGES}"
    await _hold(dut, 1, IDLE_EDGES, "PLL unlocked")


@cocotb.test()
async def test_init_reassert_reasserts(dut):
    """After release, ninit_done rising re-asserts rst_o in 4 edges."""
    await _power_up(dut)
    await _between_edges(dut)
    dut.ninit_done_i.value = 0
    dut.pll_locked_i.value = 1
    await _edges_until(dut, 0, IDLE_EDGES)
    await _between_edges(dut)
    dut.ninit_done_i.value = 1
    n = await _edges_until(dut, 1, IDLE_EDGES)
    assert n == LATENCY_EDGES, f"re-assert took {n} edges after ninit_done rose, expected {LATENCY_EDGES}"
    await _hold(dut, 1, IDLE_EDGES, "SDM reports not ready")


def main():
    sources = [str(DEEPSKOPION_ROOT / "hw" / "rtl" / "de25" / "de25_reset_ctrl.vhd")]
    run_simulation(
        top_level="de25_reset_ctrl",
        module="test_de25_reset_ctrl",
        custom_libraries={"tich": sources},
        waves=False,
        verbose=False,
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
