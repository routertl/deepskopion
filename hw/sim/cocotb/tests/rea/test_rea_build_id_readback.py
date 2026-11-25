# rr unknown-llm (AI-generated)
"""REA BUILD_ID (0xD4) design-identity readback — DS-T2.75 RTL guard.

Proves the on-chip mechanism `rr hw bit-sha` depends on: rr_rea_regbank must
surface the build-flow-GENERATED design identity (C_REA_BUILD_ID, from
rr_rea_build_id_pkg) at the frozen JTAG register offset 0xD4. That register is
what a real Agilex REA readback reads and compares to the baked design stamp.

WHY THIS TEST EXISTS (the defect it would have caught): the DS-T2.75 hook +
sidecar + build-free guard all pass while asserting only at the `rr hw bit-sha`
RESOLUTION surface (expected value + sidecar filename). None of them touch the
RTL, and the deepskopion local rr_rea fork (v0.4 lineage) had NO BUILD_ID
register — 0xD4 fell through the read mux's `when others => (others => '0')`, so
the on-chip stamp read 0x00000000 (UNSTAMPED) and identity could NEVER reach
CONFIRMED. The generated package was compiled but consumed by nobody: a working
detector nobody called.

The check is value-AGNOSTIC on purpose: it asserts "0xD4 reads whatever design
identity the package carries", comparing rd_data against the DUT-exported
C_REA_BUILD_ID rather than a hard-coded number that would rot every commit. The
non-zero assertion is what makes it fail on the pre-fix RTL (0x00000000) and on
the mutation that deletes the read-mux case.

Run (ROUTERTL-001):  rr sim run test_rea_build_id_readback
NOTE: rm -rf hw/sim/work before re-running after any RTL edit (RTL-P2.671).
"""

import sys
import os
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer

test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")

import sim.cocotb
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation

# Frozen JTAG register offsets (host SW contract — rr_rea_pkg / bit_sha.py).
ADDR_VERSION  = 0x0000
ADDR_BUILD_ID = 0x00D4
ADDR_UNMAPPED = 0x00D8   # a hole between BUILD_ID (0xD4) and DATA_BASE (0x100)
REA_VERSION   = 0x5245410F   # 'REA' + v1.11 tier (rr_rea_pkg C_REA_VERSION)


async def _settle(dut, addr):
    """Drive the combinational read address and let it propagate."""
    dut.rd_addr_i.value = addr
    await Timer(20, units="ns")   # >> comb delay; read port has no clock edge


@cocotb.test()
async def build_id_register_surfaces_design_identity(dut):
    # The read port is combinational, but keep a clock running so the DUT's
    # clocked ports are well-defined and any registered debug logic settles.
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    await Timer(5, units="ns")

    expected = int(dut.pkg_build_id_o.value)
    assert expected != 0x00000000, (
        f"C_REA_BUILD_ID is 0x{expected:08X} — the generated design-identity "
        f"package baked a ZERO stamp; a zero identity is UNSTAMPED and can never "
        f"reach `rr hw bit-sha` CONFIRMED. Regenerate rr_rea_build_id_pkg."
    )

    # Positive control: VERSION (0x00) reads its magic. Proves the read datapath
    # and address decode work at all, so a BUILD_ID miss is a real hole, not a
    # dead DUT.
    await _settle(dut, ADDR_VERSION)
    ver = int(dut.rd_data_o.value)
    assert ver == REA_VERSION, (
        f"VERSION (0x00) read 0x{ver:08X}, expected 0x{REA_VERSION:08X} — "
        f"the regbank read datapath itself is broken (positive control failed)."
    )

    # The register under test: BUILD_ID (0xD4) must read the design identity.
    await _settle(dut, ADDR_BUILD_ID)
    got = int(dut.rd_data_o.value)
    assert got == expected, (
        f"BUILD_ID (0xD4) read 0x{got:08X}, expected the baked design identity "
        f"0x{expected:08X} (C_REA_BUILD_ID). If 0x00000000, the regbank has no "
        f"BUILD_ID register and `rr hw bit-sha` will read UNSTAMPED → never "
        f"CONFIRMED — this is the DS-T2.75 RTL defect."
    )

    # Negative control: an unmapped offset reads 0 (the `when others` default),
    # so the positive result above is a real decode hit, not a stuck bus.
    await _settle(dut, ADDR_UNMAPPED)
    hole = int(dut.rd_data_o.value)
    assert hole == 0x00000000, (
        f"unmapped offset 0x{ADDR_UNMAPPED:04X} read 0x{hole:08X}, expected 0 — "
        f"the read mux is not defaulting cleanly, so the BUILD_ID hit above is "
        f"not trustworthy."
    )

    dut._log.info(
        f"✓ BUILD_ID (0xD4) = 0x{got:08X} == C_REA_BUILD_ID; VERSION=0x{ver:08X}; "
        f"unmapped=0 — the design identity reaches the JTAG register `rr hw "
        f"bit-sha` reads (DS-T2.75)."
    )


def main():
    ds_root = test_dir.parents[4]   # deepskopion/
    rea_rtl = ds_root / "libs/routertl/rea/rtl"
    run_simulation(
        top_level="tb_rea_build_id",
        module="test_rea_build_id_readback",
        top_level_lang="vhdl",
        waves=False,
        work_lib="rr_rea",
        # Hand-curated, dependency-ordered compile set (mirrors
        # test_glitch_filter.py): scope to just the regbank + its two packages +
        # the tb, so the project's other testbenches are not pulled in. The
        # generated design-identity package is a build-flow output under
        # generated/, placed into library rr_rea, the library the package's
        # generated output is locked to.
        custom_libraries={"rr_rea": [
            str(rea_rtl / "rr_rea_pkg.vhd"),
            str(ds_root / "generated/rr_rea_build_id_pkg.vhd"),
            str(rea_rtl / "rr_rea_regbank.vhd"),
            str(ds_root / "hw/sim/tb_rea_build_id.vhd"),
        ]},
    )


if __name__ == "__main__":
    main()
