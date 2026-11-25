# rr unknown-llm (AI-generated)
"""can_bit_timing DUAL-RATE / forced-resync test (DS-P2.64 step 1, CAN-REQ-14/-13).

Exercises the NEW B2 capability directly on the production can_bit_timing entity:
the CAN-FD rate switch. RATE_SEL='0' samples at the nominal pair (PRESCALER×TQ);
RATE_SEL='1' samples at the directly-supplied data pair
(DATA_CYC_PER_BIT / DATA_SAMPLE_CYC). On the RATE_SEL edge the block does a FORCED
resync (cnt:=0 + load the new pair) anchored to that bit boundary — because at the
BRS→data (and data→nominal) switch there is NO falling edge to hard-sync to.

CAN-REQ-14's sharp corner = an EDGE-STARVED data run: a run of identical recessive
bits in the fast phase, where the forced resync is the ONLY timing anchor (no
hard-sync possible). The block must free-run at the data rate with zero slip.

Hard-coded expected values (ROUTERTL-002): the test CHOOSES the driven bits and
the rates, and asserts the recovered stream + the sample SPACING match those
choices — never derived from the DUT at runtime.

Run: python test_can_bit_timing_dualrate.py
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

DS_ROOT = Path(__file__).resolve().parents[6]
RTL = DS_ROOT / "hw" / "rtl" / "datalogger" / "can" / "can_bit_timing.vhd"

NUM_TQ = 16
PRESC = 4                      # nominal: cyc_per_bit=64, sample=48
NOM_BIT = NUM_TQ * PRESC       # 64
DATA_CYC = 20                  # fast data-phase bit period (clk cycles)
DATA_SAMPLE = 15               # fast sample point (75% of 20)


class Recorder:
    """Single-threaded clock stepper that records (global_cycle, value) per sample."""
    def __init__(self, dut):
        self.dut = dut
        self.cyc = 0
        self.samples = []          # (cycle, value)

    async def step(self, n, rx=None, rate_sel=None):
        if rx is not None:
            self.dut.RX.value = rx
        if rate_sel is not None:
            self.dut.RATE_SEL.value = rate_sel
        for _ in range(n):
            await RisingEdge(self.dut.CLK)
            self.cyc += 1
            if str(self.dut.BIT_VALID.value) == '1':
                self.samples.append((self.cyc, int(self.dut.BIT_VALUE.value)))


async def _setup(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.RST.value = 1
    dut.ENABLE.value = 0
    dut.RX.value = 1
    dut.RATE_SEL.value = 0
    dut.PRESCALER.value = PRESC
    dut.DATA_CYC_PER_BIT.value = DATA_CYC
    dut.DATA_SAMPLE_CYC.value = DATA_SAMPLE
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    dut.ENABLE.value = 1
    await RisingEdge(dut.CLK)
    return Recorder(dut)


@cocotb.test()
async def edge_starved_data_run_no_slip(dut):
    """Forced resync anchors an edge-starved fast run; zero slip (CAN-REQ-14)."""
    rec = await _setup(dut)

    # --- Arbitration (nominal) phase: SOF + a few bits, ending recessive ---
    nominal_bits = [0, 1, 0, 0, 1, 1]      # SOF=0 first → lock; last bit recessive=1
    await rec.step(2 * NOM_BIT, rx=1)      # idle: no SOF yet, not synced
    for b in nominal_bits:
        await rec.step(NOM_BIT, rx=b)

    n_nom = len(rec.samples)
    assert n_nom == len(nominal_bits), (
        f"nominal phase: got {n_nom} samples, expected {len(nominal_bits)}")
    assert [v for _, v in rec.samples] == nominal_bits, (
        f"nominal recovered {[v for _, v in rec.samples]} != {nominal_bits}")

    # --- Switch to data rate with RATE_SEL=1; drive a PURE edge-starved recessive
    #     run. No falling edge anywhere → forced resync is the only anchor. ---
    data_bits = [1, 1, 1, 1, 1, 1]
    for i, b in enumerate(data_bits):
        # raise RATE_SEL exactly at the first data bit boundary
        await rec.step(DATA_CYC, rx=b, rate_sel=(1 if i == 0 else None))

    data_samples = rec.samples[n_nom:]
    assert len(data_samples) == len(data_bits), (
        f"data phase: got {len(data_samples)} samples, expected {len(data_bits)} "
        f"(fast rate not engaged?)")
    assert [v for _, v in data_samples] == data_bits, (
        f"data recovered {[v for _, v in data_samples]} != {data_bits}")

    # Exact spacing = DATA_CYC between every consecutive data sample → no slip.
    deltas = [data_samples[k + 1][0] - data_samples[k][0]
              for k in range(len(data_samples) - 1)]
    assert all(d == DATA_CYC for d in deltas), (
        f"edge-starved data run SLIPPED: sample deltas {deltas} != all {DATA_CYC}")

    # Sanity: the data rate really is faster than nominal (would be ~3 samples if
    # the block had stayed nominal over the same 6*20=120 clks).
    assert DATA_CYC < NOM_BIT
    dut._log.info(f"CAN-REQ-14 PASS — forced resync + edge-starved fast run: "
                  f"{len(data_samples)} data bits, exact {DATA_CYC}-clk spacing, no slip")


@cocotb.test()
async def rate_switch_and_back(dut):
    """Nominal → data (hard-sync still works fast) → back to nominal (CAN-REQ-13/-22)."""
    rec = await _setup(dut)

    nominal_bits = [0, 1, 1, 0]
    await rec.step(2 * NOM_BIT, rx=1)
    for b in nominal_bits:
        await rec.step(NOM_BIT, rx=b)
    n_nom = len(rec.samples)

    # Data phase WITH edges (hard-sync exercised at the fast rate).
    data_bits = [1, 0, 1, 0, 0, 1]
    for i, b in enumerate(data_bits):
        await rec.step(DATA_CYC, rx=b, rate_sel=(1 if i == 0 else None))
    n_data = len(rec.samples) - n_nom

    # Switch back to nominal (RATE_SEL→0, forced resync re-anchors nominal grid).
    tail_bits = [1, 0, 1]
    for i, b in enumerate(tail_bits):
        await rec.step(NOM_BIT, rx=b, rate_sel=(0 if i == 0 else None))

    vals = [v for _, v in rec.samples]
    expected = nominal_bits + data_bits + tail_bits
    assert n_data == len(data_bits), (
        f"data phase sample count {n_data} != {len(data_bits)}")
    assert vals == expected, f"recovered {vals} != driven {expected}"
    dut._log.info(f"CAN-REQ-13/-22 PASS — nominal→data→nominal round trip, "
                  f"{len(expected)} bits recovered exact across both rate switches")


def main():
    run_simulation(
        top_level="can_bit_timing",
        module="test_can_bit_timing_dualrate",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [str(RTL)]},
    )


if __name__ == "__main__":
    main()
