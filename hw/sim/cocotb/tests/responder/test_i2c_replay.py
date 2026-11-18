"""DS-P2.63 — replay a REAL silicon SDA/SCL edge capture as the sim stimulus.

"Dogfood a captured real signal" (edge_capture.py / DS-P2.60): instead of a
synthetic clean/jittered master, this drives the i2c_injector DUT with the EXACT
SCL timing recorded off the live board by the (now end-to-end) edge datalogger
(DS-T2.35). The captures live in ``captures/`` and were produced by the working
``zs_msg.parse_edge_batch`` decode — each reconstructs byte-exact against the
silicon master readback, so they are faithful ground truth.

Two captures, two purposes:
  * i2c_pass_capture.jsonl  — a PASSING read (master read deadbeef). Replaying it
    MUST reconstruct deadbeef in sim: this validates that the replay harness
    drives the DUT correctly with the real, irregular SCL cadence. (Harness
    self-check — a hard assert.)
  * i2c_fail_capture.jsonl  — the SCL cadence of a read that FAILED on the OLD
    silicon (master read deadbfff). DS-T2.34's root cause turned out to be a
    free-running mis-scaled sclLowTimeout (fixed deepskopion c696225), NOT a
    bus-pattern fault — so this cadence only ACCOMPANIED a failure, it didn't
    cause it (a clock-domain timeout is not reproducible via SCL-edge replay).
    Post-fix this is a regression: the fixed DUT must read clean (deadbeef) under
    that exact real-silicon timing.

The replay reuses the proven I2cMasterDriver protocol logic and overrides ONLY
its SCL timing — feeding the captured inter-edge intervals (the real cdns-i2c
cadence under PS/AXI load) the same way test_i2c_overconsume fed jitter. The DUT
uses the REAL xpm_fifo_sync, so the FWFT empty/DOUT latency is present.

Run:  rr sim run test_i2c_replay
"""
import sys
from pathlib import Path

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
_ds_root = Path(__file__).resolve().parents[5]
for _p in (_ds_root / "hw" / "sim", _ds_root):          # VIP + sw.stream import roots
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cocotb

from sdk.cocotb_helpers import requires
from sw.stream.edge_capture import EdgeCapture

# Reuse the existing injector harness + the silicon-faithful load.
from test_i2c_injector import I2cInjectorTb, SLAVE_ADDR, GENERICS
from test_i2c_overconsume import load_like_silicon

PAYLOAD = [0xDE, 0xAD, 0xBE, 0xEF]
CAPTURES = Path(__file__).resolve().parent / "captures"


class ReplayTiming:
    """I2cTiming whose SCL half-period plays back a captured edge sequence.

    The driver reads ``half_period_ns`` once per SCL half-period, so handing it
    the captured inter-edge intervals in order reproduces the recorded SCL
    cadence — including the real per-edge irregularity of the cdns-i2c master
    under PS/AXI load that a clean BFM never models. Past the captured tail it
    holds the last interval (the transaction is over by then)."""

    def __init__(self, half_periods, setup_ns=200, hold_ns=200):
        self._hp = [max(50, int(h)) for h in half_periods]
        self._i = 0
        self.setup_ns = setup_ns
        self.hold_ns = hold_ns

    @property
    def half_period_ns(self) -> int:
        if self._i < len(self._hp):
            v = self._hp[self._i]
            self._i += 1
            return v
        return self._hp[-1] if self._hp else 1250


def load_capture(name):
    """Return (scl_half_periods, expected_readback_hex) from a v1 capture."""
    cap = EdgeCapture.read(str(CAPTURES / name))
    scl_inst = int(cap.meta.get("scl_instance", 1))
    scl = cap.transitions(instance=scl_inst)           # [(ts_ns, level), ...]
    half = [scl[i + 1][0] - scl[i][0] for i in range(len(scl) - 1)]
    return half, cap.meta.get("master_readback", "")


async def _replay_read(dut, capture_name):
    tb = I2cInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()
    half, expected = load_capture(capture_name)
    tb.i2c.timing = ReplayTiming(half)

    await load_like_silicon(tb, PAYLOAD)               # silicon load: no drain wait
    await tb.i2c.start()
    ack = await tb.i2c.write_byte((SLAVE_ADDR << 1) | 1)
    if not ack:
        await tb.i2c.stop()
        return "addr-NACK", expected
    got = [await tb.i2c.read_byte(send_ack=(i < 3)) for i in range(4)]
    await tb.i2c.stop()
    return "".join(f"{b:02x}" for b in got), expected


@cocotb.test()
@requires("RESP-REQ-3")
async def replay_passing_capture_reconstructs_deadbeef(dut):
    """HARNESS SELF-CHECK: replaying a PASSING silicon capture (real SCL cadence)
    must reconstruct deadbeef — proving the replay faithfully drives the DUT."""
    got, expected = await _replay_read(dut, "i2c_pass_capture.jsonl")
    dut._log.info(f"[replay PASS-capture] sim got {got}, silicon read {expected}")
    assert expected == "deadbeef", f"capture metadata sanity: {expected}"
    assert got == "deadbeef", (
        f"replay harness failed to reconstruct the PASSING transaction: got {got}. "
        "The real-SCL-cadence replay must drive the DUT cleanly on a good read.")


@cocotb.test()
@requires("RESP-REQ-3")
async def replay_failing_capture_reads_clean_post_fix(dut):
    """Regression: replay the SCL cadence of a read that FAILED on the OLD silicon
    (master read deadbfff) and confirm the FIXED DUT now reconstructs deadbeef.

    DS-T2.34's root cause (proven 2026-06-09, fixed deepskopion c696225) was a
    free-running, mis-scaled sclLowTimeout firing a spurious STRETCH_ERROR abort —
    NOT a FIFO or bus-pattern fault. So this captured SCL cadence never *caused*
    the corruption (the timeout fired asynchronously to the bus); it merely
    accompanied it, and an SCL-edge replay structurally cannot reproduce a
    clock-domain timeout. Post-fix, the point is to confirm the inactivity-watchdog
    fix leaves a healthy read healthy under the exact real-silicon timing that was
    on the wire when a failure occurred.

    (Was @undetermined against RTL-P3.746 / a faithful XPM FWFT model — both
    retracted: the bug was the timeout, not FWFT latency. See the memory
    feedback_sim_hw_mismatch_scale_timeouts.)"""
    got, silicon = await _replay_read(dut, "i2c_fail_capture.jsonl")
    dut._log.info(f"[replay FAIL-capture, post-fix] sim got {got} "
                  f"(silicon-at-capture read {silicon})")
    assert got == "deadbeef", (
        f"the fixed DUT did NOT read clean on the historically-failing capture's "
        f"real SCL timing: got {got}. The DS-T2.34 inactivity-watchdog fix should "
        "leave a healthy read healthy regardless of the recorded cadence.")


def main():
    from engine.simulation import run_simulation
    from engine.paths import SDK_SRC_PATH

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[5]
    DEEPSKOPION_RTL = DEEPSKOPION_ROOT / "hw" / "rtl"
    from hw.sim.portable_fifo_sources import olo_fifo_sources

    run_simulation(
        top_level="tb_i2c_injector",
        module="test_i2c_replay",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/i2c/i2c_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/i2c_injector.vhd",
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_i2c_injector.vhd",
            ],
        },
    )


if __name__ == "__main__":
    main()
