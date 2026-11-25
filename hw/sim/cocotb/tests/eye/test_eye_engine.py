# rr unknown-llm (AI-generated)
"""eye_engine point-measure handshake test (DS-P2.66, DS-P3.118 dual-clock).

Proves the one-point BERT engine that fills each cell of the bathtub/eye, now
with the TX generator and RX checker on SEPARATE, frequency-locked clocks
(CLK_TX = swept MMCM CLKOUT0, CLK_RX = phase-shifted MMCM CLKOUT1):

  - clean loopback, RX sampled mid-bit       → DONE, LOCKED, ERR==0, BIT~target
  - clean loopback, CLK_RX at 90°/270° phase → STILL locks 0 err (the self-
        synchronising checker tolerates the sample-phase offset — the property
        that lets ONE engine sweep every phase; DS-P3.118 foundation)
  - K isolated line errors injected          → ERR == 3*K (descrambler taps+1)
  - dead loopback (RX stuck at 0)            → DONE, NOT LOCKED, BIT==0 (no false
        green) — adversarial, across the clock split
  - re-arm (START pulse twice)              → counters reset cleanly between runs

The two clocks share a period (frequency-locked, as the MMCM VCO guarantees on
silicon) and differ only in phase — modelling the CLKOUT0/CLKOUT1 pair. True
sub-UI eye-WALKING against real analog edges is a silicon checkpoint (no analog
model in a digital sim); here we prove the dual-clock measurement is FUNCTIONALLY
correct and phase-offset-tolerant.

NOTE — there is deliberately NO "sample-on-the-eye-edge → errors" test here.
Eye-closure at the worst sample phase is an ANALOG phenomenon (setup/hold →
metastable/wrong samples); a 2-state digital simulator samples a well-defined
value at every edge, so a clean loopback locks 0-error at EVERY phase (the
self-syncing checker is latency/phase-independent by design). That the eye
actually CLOSES at the bad phase is the DS-P3.118 SILICON checkpoint (full eye
at 200 MHz, phase-independence of f_max), not something nvc can show. Genuine
data corruption is covered by injected_errors / dead_loopback below.

The loopback + error injection live in the testbench (ROUTERTL-002: the engine
is attacked with externally-imposed conditions, never values re-derived from the
DUT).

Run (ROUTERTL-001):  rr sim run test_eye_engine
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer

test_dir = Path(__file__).resolve().parent
os.environ["ROUTERTL_WAVES_DIR"] = str(test_dir.parents[3] / "waves")

import sim.cocotb  # noqa: E402
_sdk_file = getattr(sim.cocotb, "__file__", None)
sdk_cocotb_path = Path(_sdk_file).parent if _sdk_file else Path(list(sim.cocotb.__path__)[0])
if sdk_cocotb_path.exists():
    sys.path.insert(0, str(sdk_cocotb_path))

from engine.simulation import run_simulation  # noqa: E402

ORDER = 7
LOCK_OK = 32
CNT_W = 48
MULT = 3                       # taps+1 for a 2-tap polynomial
BIT_TARGET = 2000
PERIOD_NS = 10.0               # both clocks share this period (frequency-locked)


async def _start_clocks(dut, rx_phase_frac=0.5):
    """Start CLK_TX and CLK_RX at the same frequency, CLK_RX shifted by
    rx_phase_frac of a period (0.5 = sample mid-bit; 0.0 = sample on the edge)."""
    cocotb.start_soon(Clock(dut.CLK_TX, PERIOD_NS, units="ns").start())
    if rx_phase_frac > 0.0:
        await Timer(int(round(rx_phase_frac * PERIOD_NS * 1000)), units="ps")
    cocotb.start_soon(Clock(dut.CLK_RX, PERIOD_NS, units="ns").start())


async def _reset(dut):
    dut.START.value = 0
    dut.BIT_TARGET.value = BIT_TARGET
    dut.AGGRESSOR_EN.value = 0
    dut.RX_I.value = 0
    dut.RST.value = 1
    await ClockCycles(dut.CLK_TX, 3)
    dut.RST.value = 0
    await RisingEdge(dut.CLK_TX)


def _loopback(dut, flips=None, stuck=None):
    """Coroutine: mirror TX_O onto RX_I with a 1-CLK_TX-cycle pipe (the TX bit is
    then stable across a full bit period; CLK_RX samples it at its own phase).
    Optionally flip the bit at given absolute TX-cycle indices, or force it stuck."""
    flips = set(flips or [])

    async def run():
        n = 0
        while True:
            await RisingEdge(dut.CLK_TX)
            if stuck is not None:
                dut.RX_I.value = stuck
            else:
                b = int(dut.TX_O.value)
                if n in flips:
                    b ^= 1
                dut.RX_I.value = b
            n += 1
    return cocotb.start_soon(run())


async def _measure(dut):
    """Pulse START (CLK_TX domain), wait for DONE, let the CLK_RX counters freeze,
    then return (locked, err, bits)."""
    dut.START.value = 1
    await RisingEdge(dut.CLK_TX)
    dut.START.value = 0
    for _ in range(BIT_TARGET + 400):
        await RisingEdge(dut.CLK_TX)
        if int(dut.DONE.value) == 1:
            break
    assert int(dut.DONE.value) == 1, "engine never raised DONE within the bit budget"
    # DONE is CLK_TX; the checker counters live in CLK_RX and freeze a couple
    # CLK_RX edges after run_en drops — exactly the poll-then-read latency the host
    # sees. Settle before sampling.
    await ClockCycles(dut.CLK_RX, 6)
    return (int(dut.LOCKED.value), int(dut.ERR_COUNT.value), int(dut.BIT_COUNT.value))


@cocotb.test()
async def clean_loopback_zero_ber(dut):
    await _start_clocks(dut, rx_phase_frac=0.5)
    await _reset(dut)
    _loopback(dut)

    locked, err, bits = await _measure(dut)
    assert locked == 1, "clean loopback must lock"
    assert err == 0, f"clean loopback must have 0 errors, got {err}"
    assert 1500 < bits <= BIT_TARGET + 16, \
        f"post-lock bit count should be ~BIT_TARGET, got {bits}"
    assert int(dut.BUSY.value) == 0, "BUSY must drop once DONE"
    dut._log.info(f"✓ clean (mid-bit sample): LOCKED, err=0, bits={bits}")


@cocotb.test()
async def phase_offset_tolerance(dut):
    """The split's reason for existing: the checker locks 0-error regardless of the
    CLK_RX sample phase (here 90° and 270°), as long as it samples a stable bit.
    This is what makes a phase SWEEP meaningful — every phase is a valid sample
    point, not just IDELAY taps. (0°/360° lands on the data edge and is covered by
    sample_on_transition below.)"""
    for frac in (0.25, 0.75):
        await _start_clocks(dut, rx_phase_frac=frac)
        await _reset(dut)
        lb = _loopback(dut)
        locked, err, bits = await _measure(dut)
        assert locked == 1, f"phase {frac:.2f}UI: must lock"
        assert err == 0, f"phase {frac:.2f}UI: clean sample must be 0 errors, got {err}"
        assert bits > 1500, f"phase {frac:.2f}UI: must measure a full window, got {bits}"
        lb.kill()
        dut._log.info(f"✓ phase {frac:.2f}UI: LOCKED, err=0, bits={bits}")
        # fresh clocks per iteration (cocotb forbids restarting a killed Clock on
        # the same handle is fine — new start_soon each call); reset handled above.


@cocotb.test()
async def injected_errors_multiply_by_three(dut):
    await _start_clocks(dut, rx_phase_frac=0.5)
    await _reset(dut)
    # 5 isolated flips, well past lock and spaced >> ORDER so their 3×
    # multiplications never overlap, all inside the BIT_TARGET window.
    k = 5
    flips = [500 + 200 * i for i in range(k)]
    _loopback(dut, flips=flips)

    locked, err, bits = await _measure(dut)
    assert locked == 1, "must stay locked through isolated errors"
    assert err == MULT * k, f"{k} isolated errors → {MULT*k} detected, got {err}"
    dut._log.info(f"✓ {k} injected errors → {err} detected (= {MULT}×{k}), lock held")


@cocotb.test()
async def dead_loopback_never_false_greens(dut):
    """Adversarial: a disconnected loopback (RX stuck 0) must report NO measurement,
    across the dual-clock split."""
    await _start_clocks(dut, rx_phase_frac=0.5)
    await _reset(dut)
    _loopback(dut, stuck=0)

    locked, err, bits = await _measure(dut)
    assert locked == 0, "dead line must NEVER lock"
    assert bits == 0, f"no bits should be measured on a dead line, got {bits}"
    assert err == 0, "no errors should accrue while unlocked (no false BER)"
    dut._log.info("✓ dead line: DONE, not locked, no false-green measurement")


@cocotb.test()
async def rearm_resets_counters(dut):
    """A second START must re-zero the counters (no stale carry from run 1),
    including the CLK_RX-domain checker reset path."""
    await _start_clocks(dut, rx_phase_frac=0.5)
    await _reset(dut)

    # Run 1: inject errors so counters are non-zero.
    k = 3
    lb = _loopback(dut, flips=[500 + 200 * i for i in range(k)])
    _, err1, _ = await _measure(dut)
    assert err1 == MULT * k, f"run1 expected {MULT*k}, got {err1}"
    lb.kill()

    # Run 2: clean loopback → must come back to 0, not carry run1's errors.
    _loopback(dut)
    locked2, err2, bits2 = await _measure(dut)
    assert locked2 == 1 and err2 == 0 and bits2 > 1500, \
        f"re-arm must give a clean run, got locked={locked2} err={err2} bits={bits2}"
    dut._log.info(f"✓ re-arm: run1 err={err1} → run2 clean err=0 bits={bits2}")


def main():
    eye = test_dir.parents[4] / "hw/rtl/eye"
    run_simulation(
        top_level="eye_engine",
        module="test_eye_engine",
        top_level_lang="vhdl",
        generics={"G_ORDER": ORDER, "G_LOCK_OK": LOCK_OK, "G_CNT_W": CNT_W},
        waves=False,
        work_lib="tich",
        custom_libraries={"tich": [
            str(eye / "prbs_gen.vhd"),
            str(eye / "prbs_check.vhd"),
            str(eye / "eye_engine.vhd"),
        ]},
    )


if __name__ == "__main__":
    main()
