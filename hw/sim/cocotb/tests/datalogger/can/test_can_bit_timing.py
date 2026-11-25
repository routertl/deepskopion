# rr unknown-llm (AI-generated)
"""can_bit_timing unit test (DS-P3.100, Block 1).

Drives a known raw CAN-bit sequence onto RX at a configured prescaler and asserts
the recovered (sampled-at-75%) bit stream matches — exercising:
  * lock on the first recessive->dominant (SOF) edge,
  * free-running across runs of identical bits (no edge to resync on — the
    bit-stuffing-bounded case), and
  * resync on later falling edges.

bit time = G_NUM_TQ(16) * PRESCALER clk_sys cycles; sample at TQ 12 (75%).
Hard-coded expected bits (ROUTERTL-002). Run: python test_can_bit_timing.py
(rm -rf hw/sim/work first after an RTL edit, RTL-P2.671).
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

DS_ROOT = Path(__file__).resolve().parents[6]   # deepskopion/ (tests/datalogger/can → 6 up)
RTL = DS_ROOT / "hw" / "rtl" / "datalogger" / "can" / "can_bit_timing.vhd"

PRESC = 4            # clk_sys cycles per TQ
NUM_TQ = 16
BIT_CLKS = NUM_TQ * PRESC   # clk_sys cycles per CAN bit

# A raw bit sequence (NOT stuffed — Block 1 just samples whatever is on the wire).
# Starts with SOF=0 (the first falling edge → lock). Contains a 1,1 run and a
# 0,0,0 run (free-run, no resync) and both edge directions.
SEQ = [0, 1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 1, 0]


async def drive_bit(dut, level):
    dut.RX.value = level
    await ClockCycles(dut.CLK, BIT_CLKS)


@cocotb.test()
async def bit_timing_recovers_sequence(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())

    dut.RST.value = 1
    dut.ENABLE.value = 0
    dut.RX.value = 1                      # recessive idle
    dut.PRESCALER.value = PRESC
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0
    dut.ENABLE.value = 1
    await RisingEdge(dut.CLK)

    sampled = []

    async def collect():
        while True:
            await RisingEdge(dut.CLK)
            if int(dut.BIT_VALID.value) == 1:
                sampled.append(int(dut.BIT_VALUE.value))
    cocotb.start_soon(collect())

    # Idle recessive for 2 bit-times: no falling edge → not synced → no samples.
    for _ in range(2):
        await drive_bit(dut, 1)
    assert not sampled, f"sampled during idle before any SOF edge: {sampled}"

    # Drive the sequence (SOF first). The 75% sample of the LAST bit fires
    # before its drive_bit() returns, so just flush the collector a few cycles —
    # a full extra bit-time would itself be sampled (Block 1 samples every bit
    # while synced; frame-end is Block 3's job, not bit-timing's).
    for b in SEQ:
        await drive_bit(dut, b)
    await ClockCycles(dut.CLK, 8)

    assert sampled == SEQ, (
        f"recovered bit stream != driven: got {sampled}, expected {SEQ}")
    dut._log.info(f"can_bit_timing PASS — recovered {len(sampled)} bits {sampled} "
                  f"@ prescaler={PRESC} (sample point TQ12/75%), lock + free-run + resync OK")


def main():
    run_simulation(
        top_level="can_bit_timing",
        module="test_can_bit_timing",
        top_level_lang="vhdl",
        work_lib="tich",
        custom_libraries={"tich": [str(RTL)]},
    )


if __name__ == "__main__":
    main()
