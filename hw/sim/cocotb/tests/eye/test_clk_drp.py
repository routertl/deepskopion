"""clk_drp DRP-sequencer test (DS-P2.66 / DS-P3.115 / DS-P3.118).

Proves the eye's FREQUENCY axis: the DRP reconfiguration sequencer drives the
real XAPP888 read-modify-write burst to the MMCM, waits for each DRDY, holds
BUSY through the relock, and only then asserts LOCKED — the signal the host
waits on before trusting a fresh BER sweep at the new bit-rate.

WHAT CHANGED (DS-P3.118 dual-clock): the eye now sweeps TWO MMCM outputs at the
same divide — CLKOUT0 (eye_clk, TX) and CLKOUT1 (sample_clk, RX, phase-shifted).
For sample_clk to stay FREQUENCY-LOCKED to eye_clk, BOTH outputs' counter
registers are reprogrammed every reconfig, so the XAPP888 burst grew from 3 to 5
accesses: PowerReg(0x28), CLKOUT0 Reg1/2 (0x08/0x09), CLKOUT1 Reg1/2 (0x0A/0x0B).
This test models a DRP slave with a real register file + seeded reserved bits and
asserts the FULL 5-step contract — derived from XAPP888, NOT from the DUT
(ROUTERTL-002) — including that CLKOUT1's divide MATCHES CLKOUT0's (the
frequency-lock that makes the phase sweep meaningful).

NB: the full clk_mmcm_xilinx wrapper (real MMCME2_ADV) is NOT simulated here —
the Xilinx MMCM primitive is a silicon-only checkpoint (no nvc/ghdl model). This
test proves the vendor-neutral SEQUENCER against the DRP contract; the wrapper's
`LOCKED <= drp_locked and mmcm_lock` adds the real-relock gate.

Run (ROUTERTL-001):  rr sim run test_clk_drp
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")

import sim.cocotb  # noqa: E402
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation  # noqa: E402

LOCK_CYCLES = 8

# ── The XAPP888 DRP contract (derived independently of the DUT) ──────────────
ADDR_POWER = 0x28
ADDR_CLKREG1 = 0x08          # CLKOUT0 ClkReg1
ADDR_CLKREG2 = 0x09          # CLKOUT0 ClkReg2
ADDR_CLKREG1_O1 = 0x0A       # CLKOUT1 ClkReg1
ADDR_CLKREG2_O1 = 0x0B       # CLKOUT1 ClkReg2
MASK_CLKREG1 = 0x1000        # reserved bit [12]   — must be preserved
MASK_CLKREG2 = 0xFC00        # reserved bits[15:10] — must be preserved
POWER_VALUE = 0xFFFF

# Seed the slave's reserved bits NON-ZERO so a clobber is detectable: if the DUT
# wrote 0 to them (the old bug) these assertions fail. Distinct seeds per output
# so a CLKOUT0↔CLKOUT1 mix-up is also caught.
SEED_CLKREG1 = 0x1000        # reserved bit 12 = 1
SEED_CLKREG2 = 0xFC00        # reserved [15:10] = all ones

# VCO fixed at 1000 MHz; CLKOUT_DIVIDE D sets clk = 1000/D. sel 0..3 are PINNED
# (the baked-in 4-freq silicon image); 4..9 are the denser grid; sel>=10 clamps
# to the safe 100 MHz base (D=10) in the RTL ROM.
DIVIDERS = {0: 10, 1: 8, 2: 6, 3: 5, 4: 9, 5: 7, 6: 12, 7: 16, 8: 20, 9: 40}


def _clkreg1(d):
    """ClkReg1 data (pre-mask): HIGH_TIME[11:6] & LOW_TIME[5:0], 50% duty."""
    high = (d + 1) // 2      # ceil(D/2)
    low = d // 2             # floor(D/2)
    return ((high & 0x3F) << 6) | (low & 0x3F)


def _clkreg2(d):
    """ClkReg2 data (pre-mask): EDGE bit7 = D odd; NO_COUNT/DELAY/MX = 0."""
    return (d & 1) << 7


def _drp_slave(dut, ack_latency=1, dead=False):
    """Model the MMCME2 DRP slave: a 7-bit-addressed register file that acks each
    DEN after `ack_latency` cycles. Reads drive DO from the file; writes commit DI
    into it. Reserved bits are seeded so a read-modify-write that drops them is
    caught. `regs` is returned for post-run inspection."""
    regs = {
        ADDR_POWER: 0x0000,
        ADDR_CLKREG1: SEED_CLKREG1, ADDR_CLKREG2: SEED_CLKREG2,
        ADDR_CLKREG1_O1: SEED_CLKREG1, ADDR_CLKREG2_O1: SEED_CLKREG2,
    }

    async def run():
        dut.DRDY.value = 0
        dut.DO.value = 0
        pending = -1
        op_read_addr = None
        while True:
            await RisingEdge(dut.CLK)
            dut.DRDY.value = 0
            if int(dut.DEN.value) == 1:
                addr = int(dut.DADDR.value)
                if int(dut.DWE.value) == 1:          # write: commit DI
                    regs[addr] = int(dut.DI.value)
                    op_read_addr = None
                else:                                 # read: serve DO on ack
                    op_read_addr = addr
                pending = ack_latency
            if not dead and pending == 0:
                dut.DRDY.value = 1
                if op_read_addr is not None:
                    dut.DO.value = regs.get(op_read_addr, 0)
            if pending >= 0:
                pending -= 1
    cocotb.start_soon(run())
    return regs


async def _reset(dut):
    dut.RECONFIG.value = 0
    dut.FREQ_SEL.value = 0
    dut.RST.value = 1
    await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


async def _reconfig(dut, profile, budget=200):
    dut.FREQ_SEL.value = profile
    dut.RECONFIG.value = 1
    await RisingEdge(dut.CLK)
    dut.RECONFIG.value = 0
    for _ in range(budget):
        await RisingEdge(dut.CLK)
        if int(dut.LOCKED.value) == 1 and int(dut.BUSY.value) == 0:
            return True
    return False


def _check_counter_pair(regs, addr_r1, addr_r2, d, label):
    """Assert one CLKOUT's Reg1/Reg2 carry divide D with reserved bits preserved."""
    r1 = regs[addr_r1]
    assert (r1 & MASK_CLKREG1) == (SEED_CLKREG1 & MASK_CLKREG1), \
        f"{label} Reg1 reserved bit clobbered: {r1:#06x}"
    assert (r1 & ~MASK_CLKREG1 & 0xFFFF) == (_clkreg1(d) & ~MASK_CLKREG1 & 0xFFFF), \
        f"{label} Reg1 data wrong for D={d}: {r1:#06x} vs expect {_clkreg1(d):#06x}"
    r2 = regs[addr_r2]
    assert (r2 & MASK_CLKREG2) == (SEED_CLKREG2 & MASK_CLKREG2), \
        f"{label} Reg2 reserved bits clobbered: {r2:#06x}"
    assert (r2 & ~MASK_CLKREG2 & 0xFFFF) == (_clkreg2(d) & ~MASK_CLKREG2 & 0xFFFF), \
        f"{label} Reg2 data wrong for D={d}: {r2:#06x} vs expect {_clkreg2(d):#06x}"


def _assert_xapp888(regs, profile):
    """The whole contract: PowerReg written, both CLKOUT counter pairs RMW'd with
    the SAME divide (frequency-locked), reserved bits preserved."""
    d = DIVIDERS.get(profile, 10)        # sel>=10 clamps to the 100 MHz base
    assert regs[ADDR_POWER] == POWER_VALUE, \
        f"PowerReg(0x28) must be {POWER_VALUE:#06x}, got {regs[ADDR_POWER]:#06x}"
    _check_counter_pair(regs, ADDR_CLKREG1, ADDR_CLKREG2, d, "CLKOUT0")
    _check_counter_pair(regs, ADDR_CLKREG1_O1, ADDR_CLKREG2_O1, d, "CLKOUT1")
    # The frequency-lock invariant: sample_clk (CLKOUT1) MUST get the same divide
    # data as eye_clk (CLKOUT0), else the checker samples a mismatched rate.
    assert (regs[ADDR_CLKREG1] & ~MASK_CLKREG1 & 0xFFFF) == \
           (regs[ADDR_CLKREG1_O1] & ~MASK_CLKREG1 & 0xFFFF), \
        "CLKOUT1 divide must equal CLKOUT0 divide (sample_clk must be frequency-locked)"


@cocotb.test()
async def reconfig_writes_power_and_rmw_both_outputs(dut):
    """The XAPP888 sequence: PowerReg=0xFFFF + read-modify-write of BOTH CLKOUT0
    and CLKOUT1 counter registers, reserved bits preserved, then LOCKED."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    regs = _drp_slave(dut, ack_latency=1)
    await _reset(dut)

    profile = 3       # D=5 → 200 MHz, odd divide → EDGE=1 (exercises ClkReg2 bit7)
    assert await _reconfig(dut, profile), "reconfig must finish (BUSY low, LOCKED high)"
    _assert_xapp888(regs, profile)
    dut._log.info(
        f"✓ profile {profile}: PowerReg={regs[ADDR_POWER]:#06x} "
        f"CLKOUT0 R1={regs[ADDR_CLKREG1]:#06x} CLKOUT1 R1={regs[ADDR_CLKREG1_O1]:#06x} → LOCKED")


@cocotb.test()
async def freq_sel_steers_both_outputs(dut):
    """Different FREQ_SEL must change BOTH outputs' divide data together — proves
    the axis is live and that sample_clk tracks eye_clk."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    regs = _drp_slave(dut, ack_latency=2)
    await _reset(dut)

    assert await _reconfig(dut, 1), "profile 1 (125 MHz) reconfig must complete"
    _assert_xapp888(regs, 1)
    r1_p1, r1o1_p1 = regs[ADDR_CLKREG1], regs[ADDR_CLKREG1_O1]

    assert await _reconfig(dut, 2), "profile 2 (166.7 MHz) reconfig must complete"
    _assert_xapp888(regs, 2)
    r1_p2, r1o1_p2 = regs[ADDR_CLKREG1], regs[ADDR_CLKREG1_O1]

    assert r1_p1 != r1_p2, \
        f"different profiles must yield different CLKOUT0 data ({r1_p1:#06x} == {r1_p2:#06x})"
    assert r1o1_p1 != r1o1_p2, \
        f"CLKOUT1 must also follow the profile ({r1o1_p1:#06x} == {r1o1_p2:#06x})"
    dut._log.info(f"✓ profile1 {r1_p1:#06x} ≠ profile2 {r1_p2:#06x}; CLKOUT1 tracks both")


@cocotb.test()
async def dense_grid_profiles_resolve(dut):
    """The denser grid (sel 4..9 → 111/143/83/62.5/50/25 MHz) feeds the fine
    frequency sweep (DS-P3.119). Each must reconfig cleanly with both outputs
    locked to the same divide, and odd dividers (D=9,7) must set EDGE."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    regs = _drp_slave(dut, ack_latency=1)
    await _reset(dut)

    for profile in (4, 5, 6, 7, 8, 9):
        assert await _reconfig(dut, profile), f"profile {profile} must reconfig"
        _assert_xapp888(regs, profile)
        d = DIVIDERS[profile]
        edge = (regs[ADDR_CLKREG2] >> 7) & 1
        assert edge == (d & 1), f"profile {profile} (D={d}): EDGE must be {d & 1}, got {edge}"
        dut._log.info(f"✓ profile {profile} D={d} EDGE={edge} CLKOUT0=CLKOUT1 locked")


@cocotb.test()
async def undefined_profile_clamps_to_base(dut):
    """sel>=10 is undefined → the ROM must clamp to the safe 100 MHz base (D=10),
    not emit garbage that could over-clock the link — on BOTH outputs."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    regs = _drp_slave(dut, ack_latency=1)
    await _reset(dut)

    assert await _reconfig(dut, 12), "undefined profile 12 must still complete"
    _assert_xapp888(regs, 12)      # asserts against D=10 (the clamp)
    assert (regs[ADDR_CLKREG1] & ~MASK_CLKREG1 & 0xFFFF) == _clkreg1(10), \
        "undefined profile must clamp CLKOUT0 to the 100 MHz base divide"
    assert (regs[ADDR_CLKREG1_O1] & ~MASK_CLKREG1 & 0xFFFF) == _clkreg1(10), \
        "undefined profile must clamp CLKOUT1 to the 100 MHz base divide"
    dut._log.info("✓ undefined profile 12 clamped both outputs to the 100 MHz base (D=10)")


@cocotb.test()
async def dead_drp_never_false_locks(dut):
    """Adversarial: an MMCM that never returns DRDY must hold BUSY, never LOCK —
    it parks on the very first (PowerReg) access and cannot fake a reconfig."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    _drp_slave(dut, dead=True)
    await _reset(dut)

    done = await _reconfig(dut, 3, budget=100)
    assert not done, "a dead DRP must NOT complete"
    assert int(dut.BUSY.value) == 1, "sequencer must stay BUSY waiting on DRDY"
    assert int(dut.LOCKED.value) == 0, "must NEVER assert LOCKED without DRDY"
    dut._log.info("✓ dead DRP: stuck BUSY, never false-LOCKED")


def main():
    run_simulation(
        top_level="clk_drp",
        module="test_clk_drp",
        top_level_lang="vhdl",
        generics={"G_LOCK_CYCLES": LOCK_CYCLES},
        waves=False,
        work_lib="tich",
        custom_libraries={"tich": [str(test_dir.parents[4] / "hw/rtl/eye/clk_drp.vhd")]},
    )


if __name__ == "__main__":
    main()
