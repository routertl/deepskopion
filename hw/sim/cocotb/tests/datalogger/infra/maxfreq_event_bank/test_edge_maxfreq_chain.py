# rr unknown-llm (AI-generated)
"""DS-P3.117 end-to-end — gather → maxfreq_event_bank live datapath.

Drives REAL edge pulse trains onto the datalogger_gather's EDGE pins and proves the
whole in-band max-frequency chain:

  real edges → edge_datalogger min-interval tracker latches a min + fires ED_MAX_NEW
            → gather re-exports per-channel EDGE_MAX_IVALS / EDGE_MAX_NEW
            → maxfreq_event_bank emits an EVT_EDGE_MAXFREQ (msg_type 0x0602) frame
              naming the channel, with the latched interval in the 2-word payload
            → reconstruct the zs_msg the framer would pack and assert it is
              BYTE-EXACT to sw.stream.zs_msg.pack_edge_maxfreq
            → feed those bytes through the daemon decoder (decode_edge) and assert
              kind='edge_maxfreq' with the right freq_hz.

Plus: a host re-ping (CONFIG[2]) clears the latch and the bank emits the all-ones
sentinel frame (freq_hz → None).

Hard-coded expected values (ROUTERTL-002): edges are driven at a DETERMINISTIC
clock-aligned spacing so the latched min interval is known exactly, and the
expected frame bytes / freq are computed from the SW contract packers, never read
back from the DUT and re-asserted against itself.

bug-finding-is-success: the assertions are strict (byte-exact frame, exact
interval, exact channel id, exact freq) and the BFM only drives pins — no DUT or
assertion was weakened to make this pass.
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

from sdk.cocotb_helpers import requires        # noqa: E402,F401


def _find_root(start):
    for p in [start, *start.parents]:
        if (p / "project.yml").exists() and (p / "hw" / "rtl").is_dir():
            return p
    raise RuntimeError("deepskopion root not found")


ROOT = _find_root(Path(__file__).resolve())
sys.path.insert(0, str(ROOT))                                  # sw.stream.*
sys.path.insert(0, str(ROOT / "hw" / "sim" / "cocotb"))        # ds_sim_sources
from ds_sim_sources import scoped_libraries     # noqa: E402

from sw.stream import zs_msg                     # noqa: E402
from sw.stream import decoders                   # noqa: E402
from sw.stream.edge_capture import maxfreq_interval_to_hz   # noqa: E402

# ── Hard-coded contract values (ROUTERTL-002) ───────────────────────────────
SYS_CLK_HZ      = 100_000_000
IVAL_WIDTH      = 24                       # C_EDGE_MAXFREQ_WIDTH (protocols.yml)
ENABLE_CAPTURING = 0b01                     # edge_datalogger CONFIG capture-both-edges
REARM_BIT       = 2                        # CONFIG[2] host re-ping
SENTINEL        = (1 << IVAL_WIDTH) - 1    # 0x00FFFFFF
# SEV_INFO(0x0) | DOMAIN_EDGE(0x6) | EVT_EDGE_MAXFREQ(0x02)
EXPECT_MSG_TYPE = (0x0 << 12) | (0x6 << 8) | 0x02   # 0x0602

# Per-channel: device id + the cocotb edge-line port name. Ids match the DS-P1.104
# default-mix datalogger_config (C_EDGE0..2_ID = 1, 2, 3).
CHANNELS = [
    {"id": 1, "line": "EDGE0_IN"},
    {"id": 2, "line": "EDGE1_IN"},
    {"id": 3, "line": "EDGE2_IN"},
]


async def reset(dut, n=12):
    dut.RST.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.MF_FR_READY.value = 0
    dut.EDGE0_IN.value = 0
    dut.EDGE1_IN.value = 0
    dut.EDGE2_IN.value = 0
    await ClockCycles(dut.CLK, n)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


async def _enable_channel(dut, dev_id):
    """Enable one EDGE datalogger (config_control filters by device id, latches)."""
    dut.CONFIG.value = (dev_id << 11) | ENABLE_CAPTURING
    await ClockCycles(dut.CLK, 16)


async def _toggle_after(dut, line_attr, clocks):
    """Wait `clocks` rising edges, then flip the named edge line. Each flip is one
    glitch-filter-surviving edge; spacing in CLK ticks == the measured interval."""
    await ClockCycles(dut.CLK, clocks)
    h = getattr(dut, line_attr)
    h.value = 1 - int(h.value)


def _reconstruct_frame(dut) -> bytes:
    """Rebuild the on-wire zs_msg the zs_msg_framer would pack from the bank's
    current FR_* outputs — header (20 B) + 8-B EDGE_MAXFREQ payload. Mirrors the
    framer's field mapping exactly so a byte-exact compare against the SW packer
    proves the bank drives the framer correctly."""
    payload64 = int(dut.MF_FR_PAYLOAD.value)          # {word1, word0}
    word0 = payload64 & 0xFFFFFFFF                     # min_interval (LE u32)
    word1 = (payload64 >> 32) & 0xFFFFFFFF
    channel    = word1 & 0xFF
    ival_width = (word1 >> 8) & 0xFF
    flags      = (word1 >> 16) & 0xFFFF
    datalogger_id = int(dut.MF_FR_DATALOGGER_ID.value)
    timestamp  = int(dut.MF_FR_TIMESTAMP.value)
    mf = zs_msg.EdgeMaxFreq(channel=channel, min_interval=word0, ival_width=ival_width)
    return zs_msg.pack_edge_maxfreq(mf, datalogger_id=datalogger_id,
                                    timestamp=timestamp, flags=flags)


async def _wait_valid(dut, limit=4000):
    for _ in range(limit):
        await RisingEdge(dut.CLK)
        if int(dut.MF_FR_VALID.value) == 1:
            return True
    return False


@cocotb.test()
@requires("DS-REQ-MAXFREQ")
async def test_gather_to_bank_emits_byte_exact_maxfreq(dut):
    """Real edges on EDGE0 → bank emits a byte-exact EVT_EDGE_MAXFREQ frame whose
    interval + channel are exactly the driven ones, and the daemon decodes it."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await reset(dut)
    # Hold the framer NOT-ready while edges are driven so the bank's emitted frame
    # stays PENDING (it doesn't retire on a handshake) and we can inspect it.
    dut.MF_FR_READY.value = 0

    ch = CHANNELS[0]
    await _enable_channel(dut, ch["id"])

    # Drive a DETERMINISTIC edge train on EDGE0 at clock-aligned spacings. The
    # FIRST edge only opens the window; subsequent edges close intervals. The
    # SMALLEST validated interval becomes the latched min. We use spacings
    # 80, 80, 50 ticks → min interval must be EXACTLY 50.
    line = ch["line"]
    dut.EDGE0_IN.value = 0
    await _toggle_after(dut, line, 80)    # edge 1 (opens window)
    await _toggle_after(dut, line, 80)    # edge 2 → interval 80 latched (new min)
    await _toggle_after(dut, line, 50)    # edge 3 → interval 50 (smaller new min)
    # Let the 50-tick min propagate through the sync + glitch_filter + tracker and
    # REFRESH the bank's pending snapshot (the emitter keeps the LATEST min while
    # FR_READY is held low — proven in the emitter unit test).
    await ClockCycles(dut.CLK, 30)

    EXPECT_INTERVAL = 50

    assert await _wait_valid(dut), "bank never emitted a max-freq frame for real edges"
    # READY is held low (frame is pending + stable for inspection). Poll until the
    # pending snapshot has refreshed to the final (smallest) min interval.
    for _ in range(40):
        if (int(dut.MF_FR_PAYLOAD.value) & 0xFFFFFFFF) == EXPECT_INTERVAL:
            break
        await RisingEdge(dut.CLK)

    # Header/field-level checks first.
    assert int(dut.MF_FR_MSG_TYPE.value) == EXPECT_MSG_TYPE, \
        f"msg_type {int(dut.MF_FR_MSG_TYPE.value):#06x} != 0x0602"
    assert int(dut.MF_FR_DATALOGGER_ID.value) == ch["id"], \
        f"datalogger_id {int(dut.MF_FR_DATALOGGER_ID.value)} != channel id {ch['id']}"
    assert int(dut.MF_FR_WORDS.value) == 2, "EDGE_MAXFREQ payload is 2 words"

    payload64 = int(dut.MF_FR_PAYLOAD.value)
    got_interval = payload64 & 0xFFFFFFFF
    assert got_interval == EXPECT_INTERVAL, \
        f"latched min interval {got_interval} != driven min {EXPECT_INTERVAL}"
    assert ((payload64 >> 32) & 0xFF) == ch["id"], "payload channel byte != id"
    assert ((payload64 >> 40) & 0xFF) == IVAL_WIDTH, "payload ival_width byte wrong"

    # BYTE-EXACT: the reconstructed wire frame must equal the SW packer's bytes.
    got_bytes = _reconstruct_frame(dut)
    want_bytes = zs_msg.pack_edge_maxfreq(
        zs_msg.EdgeMaxFreq(channel=ch["id"], min_interval=EXPECT_INTERVAL,
                           ival_width=IVAL_WIDTH),
        datalogger_id=ch["id"],
        timestamp=int(dut.MF_FR_TIMESTAMP.value),
        flags=0,
    )
    assert got_bytes == want_bytes, (
        f"frame bytes mismatch:\n got={got_bytes.hex()}\nwant={want_bytes.hex()}")

    # DAEMON DECODE: feed the bytes through the registered DOMAIN_EDGE decoder.
    header = zs_msg.parse_header(got_bytes, 0)
    decoded = decoders.decode_frame(header, got_bytes[zs_msg.HEADER_SIZE:], SYS_CLK_HZ)
    assert decoded.kind == "edge_maxfreq", f"decoder kind={decoded.kind}"
    assert decoded.fields["channel"] == ch["id"]
    assert decoded.fields["min_interval_ticks"] == EXPECT_INTERVAL
    want_freq = maxfreq_interval_to_hz(EXPECT_INTERVAL, SYS_CLK_HZ, IVAL_WIDTH)
    assert decoded.fields["freq_hz"] == want_freq, \
        f"freq {decoded.fields['freq_hz']} != {want_freq}"
    dut._log.info(
        f"e2e maxfreq: ch{ch['id']} min={EXPECT_INTERVAL}tk → "
        f"{decoded.fields['freq_hz']/1e6:.3f} MHz, byte-exact frame PASS")

    # Release the framer → frame retires.
    dut.MF_FR_READY.value = 1
    await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)


@cocotb.test()
@requires("DS-REQ-MAXFREQ")
async def test_re_arm_emits_sentinel(dut):
    """A host re-ping (CONFIG[2] rising edge) clears the latch and the bank emits
    the all-ones sentinel frame → daemon freq_hz is None."""
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    await reset(dut)

    ch = CHANNELS[1]
    await _enable_channel(dut, ch["id"])

    # First latch a real min on EDGE1 so the latch is NOT already at the sentinel,
    # then drain it through a READY pulse so the bank is idle before the re-ping.
    line = ch["line"]
    dut.EDGE1_IN.value = 0
    await _toggle_after(dut, line, 60)
    await _toggle_after(dut, line, 60)
    await ClockCycles(dut.CLK, 5)
    assert await _wait_valid(dut), "no frame after the initial real edges"
    dut.MF_FR_READY.value = 1                    # accept the initial frame
    await RisingEdge(dut.CLK)
    await RisingEdge(dut.CLK)
    dut.MF_FR_READY.value = 0                    # hold the next (sentinel) pending

    # Host re-ping: pulse CONFIG[2] for this channel (keep it enabled). The bank
    # edge-detects CONFIG[2] and emits the sentinel; the edge_datalogger clears its
    # latch to all-ones in lock-step.
    dut.CONFIG.value = (ch["id"] << 11) | ENABLE_CAPTURING | (1 << REARM_BIT)
    await ClockCycles(dut.CLK, 4)
    dut.CONFIG.value = (ch["id"] << 11) | ENABLE_CAPTURING   # drop the re-arm bit

    assert await _wait_valid(dut), "no sentinel frame after re-arm"
    # READY held low → sentinel frame pending + stable for inspection.

    payload64 = int(dut.MF_FR_PAYLOAD.value)
    got_interval = payload64 & 0xFFFFFFFF
    assert got_interval == SENTINEL, \
        f"re-arm frame interval {got_interval:#x} != sentinel {SENTINEL:#x}"

    got_bytes = _reconstruct_frame(dut)
    header = zs_msg.parse_header(got_bytes, 0)
    decoded = decoders.decode_frame(header, got_bytes[zs_msg.HEADER_SIZE:], SYS_CLK_HZ)
    assert decoded.kind == "edge_maxfreq"
    assert decoded.fields["freq_hz"] is None, "sentinel must decode to freq_hz=None"
    dut._log.info("e2e re-arm sentinel: freq_hz=None PASS")
    dut.MF_FR_READY.value = 1
    await RisingEdge(dut.CLK)


def main():
    run_simulation(
        top_level="tb_edge_maxfreq_chain",
        module=Path(__file__).stem,
        custom_libraries=scoped_libraries("tb_edge_maxfreq_chain"),
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
