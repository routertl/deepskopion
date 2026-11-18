"""DS-P3.50 — sparse→dense channel remap (EBA-REQ-30).

Drives the aggregator (via tb_edge_agg_sparse) with the real deepskopion edge id
set: global ids 1,2,3,6 mapped onto 4 DENSE batchers. Proves:
  * an event with global device_id=6 routes to the dense batcher that owns it
    and flushes with FR_DATALOGGER_ID = 6 (the GLOBAL id, NOT the dense index 3);
  * every mapped id batches independently with the correct global id;
  * an UNMAPPED id (0/4/5/7) is dropped-and-consumed (EV_READY='1'), never
    routed to a batcher.

Hard-coded driven ids + independent expected global ids (ROUTERTL-002).
"""
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT))
from sw.stream import zs_msg as zsmsg   # noqa: E402

RTL = DEEPSKOPION_ROOT / "hw" / "rtl"
N_MAX = 32
T_FLUSH = 10_000
PERIOD_NS = 10
GLOBAL_IDS = [1, 2, 3, 6]          # dense batchers 0,1,2,3


def make_ev_word(device_id, s=False, f=False, e=False, ts_us=0, ext=0):
    return ((device_id & 0x1F) << 27
            | (1 if s else 0) << 26
            | (1 if f else 0) << 25
            | (1 if e else 0) << 24
            | (ts_us & 0xFFFF) << 8
            | (ext & 0xFF))


async def reset_dut(dut, n=8):
    dut.RST.value = 1
    dut.EV_VALID.value = 0
    dut.EV_WORD.value = 0
    dut.TIMESTAMP_64.value = 0
    dut.FR_READY.value = 0
    await ClockCycles(dut.CLK, n)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


async def push_event(dut, device_id, ts_64, e=False):
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
        raise TimeoutError(f"event for global id {device_id} never handshaked")
    dut.EV_VALID.value = 0


async def consume_frame(dut, deadline=64):
    for _ in range(deadline):
        await RisingEdge(dut.CLK)
        if int(dut.FR_VALID.value) == 1:
            break
    else:
        raise TimeoutError("FR_VALID never rose")
    out = dict(
        datalogger_id=int(dut.FR_DATALOGGER_ID.value),
        payload_words=int(dut.FR_PAYLOAD_WORDS.value),
        timestamp=int(dut.FR_TIMESTAMP.value),
        payload=int(dut.FR_PAYLOAD.value),
    )
    dut.FR_READY.value = 1
    await RisingEdge(dut.CLK)
    dut.FR_READY.value = 0
    return out


@cocotb.test()
@requires("EBA-REQ-30")
async def test_sparse_global_ids_emit_global_not_dense(dut):
    """Each global id (1,2,3,6) fills its dense batcher to capacity and flushes
    with FR_DATALOGGER_ID == the GLOBAL id — proving the remap output stamps the
    global id, not the dense batcher index."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    for gid in GLOBAL_IDS:
        base = 0x1000 * gid
        for i in range(N_MAX):
            await push_event(dut, device_id=gid, ts_64=base + i)
        frame = await consume_frame(dut)
        assert frame["datalogger_id"] == gid, (
            f"global id {gid} flushed FR_DATALOGGER_ID={frame['datalogger_id']} "
            f"(expected the global id, not a dense index)")
        assert frame["payload_words"] == N_MAX, frame
        assert frame["timestamp"] == base, f"id {gid} anchor wrong: {frame['timestamp']:#x}"
        # bytes [23:16] of each entry carry the global channel id too.
        words = [(frame["payload"] >> (s * 32)) & 0xFFFFFFFF for s in range(N_MAX)]
        for w in words:
            assert (w >> 16) & 0xFF == gid, f"entry channel byte != global id {gid}"


@cocotb.test()
@requires("EBA-REQ-2")
@requires("EBA-REQ-30")
async def test_unmapped_ids_drop_and_consume(dut):
    """Ids absent from the sparse map (0, 4, 5, 7) must drop-and-consume
    (EV_READY='1', no batcher, FR_VALID low) — the remap must not deadlock on an
    id that has no dense batcher."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset_dut(dut)

    for gid in (0, 4, 5, 7):
        dut.EV_WORD.value = make_ev_word(device_id=gid, e=True)
        dut.TIMESTAMP_64.value = 0x900 + gid
        dut.EV_VALID.value = 1
        await ReadOnly()
        assert int(dut.EV_READY.value) == 1, (
            f"unmapped id {gid} must drop-and-consume (EV_READY='1'), not stall")
        await RisingEdge(dut.CLK)
        dut.EV_VALID.value = 0
        await ClockCycles(dut.CLK, 2)
        assert int(dut.FR_VALID.value) == 0, f"unmapped id {gid} reached a batcher"

    # A mapped id still flushes cleanly afterwards.
    for i in range(N_MAX):
        await push_event(dut, device_id=6, ts_64=0xBEEF_0000 + i)
    frame = await consume_frame(dut)
    assert frame["datalogger_id"] == 6, frame
    assert frame["payload_words"] == N_MAX


def main():
    run_simulation(
        top_level="tb_edge_agg_sparse",
        module=Path(__file__).stem,
        custom_libraries={"tich": [
            f"{RTL}/datalogger/infra/edge_batcher/edge_batcher.vhd",
            f"{RTL}/datalogger/infra/edge_batcher_aggregator/edge_batcher_aggregator.vhd",
            f"{DEEPSKOPION_ROOT}/hw/sim/cocotb/tb/tb_edge_agg_sparse.vhd",
        ]},
        generics={"G_T_FLUSH_CYCLES": T_FLUSH},
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
