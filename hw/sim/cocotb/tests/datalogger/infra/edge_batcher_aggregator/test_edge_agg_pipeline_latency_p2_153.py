# rr unknown-llm (AI-generated)
"""DS-P2.153 — PF-F0 timing closure: the aggregator's DECLARED input pipeline.

The PolarFire MPFS095T -1 fabric misses 100 MHz by 5.5-6.2 ns on ONE path
class (docs/evidence/pf-f0/DS-P2.151-pf-f0-fit-timing.md): the combinational
``f_event_timestamp`` datapath fanning out from the EV_* input to every
batcher's ``TIMESTAMP_64`` and on into ``state_r`` / the 1024-bit
``payload_buf`` variable-slot write. DS-D.45 keeps clk_sys at 100 MHz and
pipelines the aggregator instead.

This suite pins the pipeline's CONTRACT (EBA-REQ-40), red-first:

  * ``test_capacity_flush_latency_is_exactly_the_declared_pipeline`` — the
    DECLARED latency. FR_VALID for a capacity flush rises exactly
    the declared pipeline depth (= 3: input + arb grant + output) cycles
    LATER than the pre-DS-P2.153 zero-stage aggregator. RED on the pre-fix
    RTL (measures 0), GREEN only with all declared register stages present.
  * ``test_timestamp_is_captured_at_the_ev_handshake_cycle`` — the
    behaviour-preservation half: the event timestamp entering a batcher must
    be the value LATCHED AT THE EV HANDSHAKE CYCLE, never a live re-sample
    one cycle later. Kills the cheap mutant that registers EV_WORD/EV_VALID
    but wires TIMESTAMP_64 straight through.
  * ``test_pipeline_sustains_one_event_per_cycle`` — the throughput half:
    EV_READY holds through a full-rate 32-event burst. Kills the cheap
    mutant whose stage only re-arms after a bubble (half throughput).

Same DUT/compile set as test_edge_batcher_aggregator.py (default generics:
identity map, G_USE_EVENT_TIMESTAMP=false, N_MAX=32).
"""

import re
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

try:
    from engine.simulation import run_simulation
except ImportError:
    print("Error: Could not import engine. Make sure the script is running in "
          "the correct environment.")
    sys.exit(1)

from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]

# Match the VHDL default generics.
NUM_CHANNELS = 8
N_MAX = 32
PERIOD_NS = 10

# The DECLARED total pipeline depth is READ FROM THE RTL — the sum of
# C_EBA_INPUT_PIPE_STAGES + C_EBA_ARB_PIPE_STAGES + C_EBA_OUTPUT_PIPE_STAGES
# in edge_batcher_aggregator.vhd: one EV-input register stage (event +
# resolved timestamp latched at the EV handshake), one registered arbitration
# grant, and one FR-output store-and-forward stage (the granted winner
# captured and its batcher released at capture). A capacity flush therefore
# reaches FR_VALID exactly that many cycles later than the pre-DS-P2.153
# zero-stage aggregator, which measured 0 here. Each stage is a measured
# PF-F0 fit cut: input stage (f_event_timestamp fan-out), output stage
# (payload mux -> framer pre_payload_r + the merge priority READY chain),
# arb-grant stage (BATCH_VALID -> arb -> capture enables/selects cone).
#
# Parsing the constants keeps the declaration LIVE: edit a C_EBA_*_PIPE_STAGES
# value without a matching RTL depth change and the measured==declared assert
# below goes red; edit it AND the RTL away from the EBA-REQ-40 total of 3 and
# the pin below goes red. (A hardcoded 3 here left the constants dead —
# changing them could not fail any test; DS-P2.153 revision.)
_EBA_RTL = (DEEPSKOPION_ROOT / "hw" / "rtl" / "datalogger" / "infra"
            / "edge_batcher_aggregator"
            / "edge_batcher_aggregator.vhd").read_text(encoding="utf-8")


def _declared_stages(name: str) -> int:
    m = re.search(
        rf"constant\s+{name}\s*:\s*natural\s*:=\s*(\d+)\s*;", _EBA_RTL)
    assert m, (f"{name} not found in edge_batcher_aggregator.vhd — the "
               "declared-pipeline contract (EBA-REQ-40) lost its declaration")
    return int(m.group(1))


DECLARED_PIPE_STAGES = (_declared_stages("C_EBA_INPUT_PIPE_STAGES")
                        + _declared_stages("C_EBA_ARB_PIPE_STAGES")
                        + _declared_stages("C_EBA_OUTPUT_PIPE_STAGES"))
# EBA-REQ-40 declares EXACTLY three stages (input + arb grant + output).
assert DECLARED_PIPE_STAGES == 3, (
    f"EBA-REQ-40 declares exactly 3 pipeline stages; the RTL constants sum to "
    f"{DECLARED_PIPE_STAGES} — update requirements.yml AND this contract "
    "together, not the constants alone")


def make_ev_word(device_id, s=False, f=False, e=False, ts_us=0, ext=0):
    """32-bit edge_event word — bits[31:27]=device_id, [26]=S, [25]=F, [24]=E."""
    return ((device_id & 0x1F) << 27
            | (1 if s else 0) << 26
            | (1 if f else 0) << 25
            | (1 if e else 0) << 24
            | (ts_us & 0xFFFF) << 8
            | (ext & 0xFF))


async def reset_dut(dut, n_cycles=8):
    dut.RST.value = 1
    dut.EV_VALID.value = 0
    dut.EV_WORD.value = 0
    dut.TIMESTAMP_64.value = 0
    dut.FR_READY.value = 0
    await ClockCycles(dut.CLK, n_cycles)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


async def push_event(dut, device_id, ts_64, e=False):
    """Drive one (EV_VALID && EV_READY) handshake (pre-edge sampling)."""
    dut.EV_WORD.value = make_ev_word(device_id, e=e)
    dut.TIMESTAMP_64.value = ts_64
    dut.EV_VALID.value = 1
    for _ in range(64):
        await ReadOnly()
        ready = int(dut.EV_READY.value)
        await RisingEdge(dut.CLK)
        if ready == 1:
            break
    else:
        raise TimeoutError(f"event for ch{device_id} never handshaked")
    dut.EV_VALID.value = 0


@cocotb.test()
@requires("EBA-REQ-40")
async def test_capacity_flush_latency_is_exactly_the_declared_pipeline(dut):
    """EBA-REQ-40 (red-first): fill channel 3 to capacity back-to-back and
    measure, in whole clock cycles, how long after the FINAL EV handshake
    FR_VALID rises. The zero-stage pre-DS-P2.153 aggregator flushes on the
    handshake edge itself (0 extra cycles); the pipelined aggregator declares
    exactly DECLARED_PIPE_STAGES (= 3: input + arb grant + output) extra
    cycles — no more (a deeper, undeclared pipe is also a contract break)."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    base_ts = 0x2000_0000
    for i in range(N_MAX):
        await push_event(dut, device_id=3, ts_64=base_ts + i)

    measured = None
    for k in range(DECLARED_PIPE_STAGES + 4):
        await ReadOnly()
        if int(dut.FR_VALID.value) == 1:
            measured = k
            break
        await RisingEdge(dut.CLK)
    assert measured is not None, (
        "EBA-PIPE-LAT: FR_VALID never rose after a full-rate capacity fill")
    assert measured == DECLARED_PIPE_STAGES, (
        f"EBA-PIPE-LAT: capacity-flush FR_VALID rose {measured} cycle(s) after "
        f"the final EV handshake; the DS-P2.153 input pipeline declares exactly "
        f"{DECLARED_PIPE_STAGES} (EBA-REQ-40)")


@cocotb.test()
@requires("EBA-REQ-40")
async def test_timestamp_is_captured_at_the_ev_handshake_cycle(dut):
    """EBA-REQ-40 preservation guard: the timestamp a batcher stamps into the
    batch must be the TIMESTAMP_64 value present AT the EV handshake cycle.
    Corrupt TIMESTAMP_64 on the very next cycle after the final handshake — a
    pipeline that registers the word/valid but re-samples the LIVE timestamp
    port one stage late stamps the corrupted value (the delta overflows and
    the capacity flush degenerates to 31+1 words)."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    base_ts = 0x4000_0000
    for i in range(N_MAX):
        await push_event(dut, device_id=2, ts_64=base_ts + i)
    # One cycle after the final handshake: poison the live timestamp input.
    dut.TIMESTAMP_64.value = base_ts + 0x00DE_AD00_0000

    frame_words = None
    frame_anchor = None
    payload = None
    for _ in range(16):
        await ReadOnly()
        if int(dut.FR_VALID.value) == 1:
            frame_words = int(dut.FR_PAYLOAD_WORDS.value)
            frame_anchor = int(dut.FR_TIMESTAMP.value)
            payload = int(dut.FR_PAYLOAD.value)
            break
        await RisingEdge(dut.CLK)
    assert frame_words is not None, "EBA-PIPE-TS: capacity flush never appeared"
    assert frame_words == N_MAX and frame_anchor == base_ts, (
        f"EBA-PIPE-TS: expected a clean {N_MAX}-word batch anchored at "
        f"{base_ts:#x}, got {frame_words} words anchored at {frame_anchor:#x} "
        "— an event's timestamp was NOT captured at its EV handshake cycle "
        "(EBA-REQ-40)")
    for i in range(N_MAX):
        delta = (payload >> (i * 32)) & 0xFFFF
        assert delta == i, (
            f"EBA-PIPE-TS: entry[{i}] ts_delta={delta} != {i} — timestamp "
            "re-sampled after the handshake cycle (EBA-REQ-40)")


@cocotb.test()
@requires("EBA-REQ-40")
async def test_pipeline_sustains_one_event_per_cycle(dut):
    """EBA-REQ-40 throughput guard: with the downstream batcher accepting,
    EV_READY must hold '1' through a full-rate one-event-per-cycle 32-event
    burst (the pipeline stage must re-arm in the same cycle it drains). A
    stage that only re-arms after a bubble halves the edge-capture rate."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    base_ts = 0x1000
    dut.EV_VALID.value = 1
    for i in range(N_MAX):
        dut.EV_WORD.value = make_ev_word(device_id=5, e=(i % 2 == 0))
        dut.TIMESTAMP_64.value = base_ts + i
        await ReadOnly()
        assert int(dut.EV_READY.value) == 1, (
            f"EBA-PIPE-RATE: EV_READY dropped at burst beat {i} — the input "
            "pipeline does not sustain one event per cycle (EBA-REQ-40)")
        await RisingEdge(dut.CLK)
    dut.EV_VALID.value = 0

    # The burst still lands as one clean capacity flush.
    for _ in range(16):
        await ReadOnly()
        if int(dut.FR_VALID.value) == 1:
            break
        await RisingEdge(dut.CLK)
    else:
        raise AssertionError(
            "EBA-PIPE-RATE: capacity flush never appeared after the burst")
    assert int(dut.FR_PAYLOAD_WORDS.value) == N_MAX
    assert int(dut.FR_TIMESTAMP.value) == base_ts
    assert int(dut.FR_DATALOGGER_ID.value) == 5


# ── Sanctioned simulation entry point ────────────────────────────────────────

RTL = DEEPSKOPION_ROOT / "hw" / "rtl"


def main():
    # Same self-contained compile set as test_edge_batcher_aggregator.py.
    run_simulation(
        top_level="edge_batcher_aggregator",
        module=Path(__file__).stem,
        custom_libraries={"tich": [
            f"{RTL}/datalogger/infra/edge_batcher/edge_batcher.vhd",
            f"{RTL}/datalogger/infra/edge_batcher_aggregator/edge_batcher_aggregator.vhd",
        ]},
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
