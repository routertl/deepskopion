# rr unknown-llm (AI-generated)
"""DS-P3.71 integration repro — does piece-3's in-band loss frame survive the
emitter → 3:1 mux → zs_msg_framer path under the SILICON condition?

Silicon symptom (Zybo Z7-20, GIT 0x77aab29b): the edge FIFO overflows
(ERR_CNT_EDGE=2.2M) but NO SEV_ERROR frame appears in 1 MB+ of post-overflow
DDR. The emitter alone is proven correct (test_error_event_emitter step 4), so
this reproduces the *integration*: downstream backpressured (OUT_READY=0 ≡ ring
Full) with a competing gather/edge frame at the mux while ERR_CNT climbs, then
OUT_READY released. Asserts the loss frame emerges with the right delta+msg_type.

bug-finding-is-success: if it does NOT emerge, this is the reproducer for the
silicon defect (file T-tier). Hard-coded driven values + independent parse
oracle (ROUTERTL-002).
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402


def _find_root(start):
    for p in [start, *start.parents]:
        if (p / "project.yml").exists() and (p / "hw" / "rtl").is_dir():
            return p
    raise RuntimeError("deepskopion root not found")


ROOT = _find_root(Path(__file__).resolve())
RTL = ROOT / "hw" / "rtl"
sys.path.insert(0, str(ROOT))
from sw.stream import zs_msg as zsmsg   # noqa: E402  (sanctioned parse oracle)

FLUSH = 20
EXPECT_MSG_TYPE = (0x2 << 12) | (0x6 << 8) | 0xF0   # SEV_ERROR|DOMAIN_EDGE|0xF0 = 0x26F0
GATHER_MSG_TYPE = (0x0 << 12) | (0x6 << 8) | 0x01   # sev=info edge DATA frame (the contender)


async def _capture_out(dut, words):
    """Append OUT_DATA on every accepted output beat (OUT_VALID & OUT_READY)."""
    while True:
        await RisingEdge(dut.CLK)
        if int(dut.OUT_VALID.value) == 1 and int(dut.OUT_READY.value) == 1:
            words.append(int(dut.OUT_DATA.value) & 0xFFFFFFFF)


def _frames(words):
    """Parse the captured word stream into (msg_type, payload0) frames via the
    PS-side oracle — independent of the values driven in."""
    buf = b"".join(int(w).to_bytes(4, "little") for w in words)
    out, off = [], 0
    while off + zsmsg.HEADER_SIZE <= len(buf):
        try:
            hdr = zsmsg.parse_header(buf, off)
        except ValueError:
            off += 4
            continue
        fb = hdr.total_size
        if fb <= 0 or off + fb > len(buf):
            break
        pl0 = int.from_bytes(buf[off + zsmsg.HEADER_SIZE: off + zsmsg.HEADER_SIZE + 4], "little") \
            if hdr.payload_len >= 4 else None
        out.append((hdr.msg_type, hdr.severity, hdr.event_id, pl0))
        off += fb
    return out


@cocotb.test()
async def test_loss_frame_survives_backpressure_contention(dut):
    cocotb.start_soon(Clock(dut.CLK, 10, units="ns").start())
    dut.RST.value = 1
    dut.ERR_CNT.value = 0
    dut.TIMESTAMP.value = 0
    dut.GATHER_VALID.value = 0
    dut.GATHER_MSG_TYPE.value = GATHER_MSG_TYPE
    dut.GATHER_PAYLOAD.value = 0xEDCEDCED
    dut.OUT_READY.value = 0                      # downstream blocked = ring Full
    await ClockCycles(dut.CLK, 5)
    dut.RST.value = 0

    words = []
    cocotb.start_soon(_capture_out(dut, words))

    # ── Ring Full + edge data contending while the edge FIFO overflows ─────────
    dut.GATHER_VALID.value = 1                   # gather keeps a frame pressing at the mux
    dut.ERR_CNT.value = 7                        # 7 lost edges (hard-coded oracle value)
    # Hold backpressure across several flush windows so the emitter latches and
    # the report sits pending behind Full — exactly the silicon state.
    for _ in range(6):
        await ClockCycles(dut.CLK, FLUSH)
    assert int(dut.ERR_FR_VALID.value) == 1, \
        "emitter never raised a pending report while ERR_CNT=7 under backpressure"

    # ── Free the ring: downstream accepts again ───────────────────────────────
    dut.OUT_READY.value = 1
    await ClockCycles(dut.CLK, 12 * FLUSH)      # let the framer drain frames out

    frames = _frames(words)
    loss = [f for f in frames if f[1] == zsmsg.SEV_ERROR or f[2] >= 0xF0]
    dut._log.info(f"captured {len(words)} OUT words → {len(frames)} frames; "
                  f"loss frames: {len(loss)}; first few msg_types: "
                  f"{[hex(f[0]) for f in frames[:6]]}")

    assert loss, ("REPRODUCED SILICON DEFECT: no SEV_ERROR loss frame emerged from the "
                  "emitter→mux→framer path under backpressure+contention "
                  f"({len(frames)} frames out, msg_types={[hex(f[0]) for f in frames[:8]]})")
    mt, sev, evt, pl0 = loss[0]
    assert mt == EXPECT_MSG_TYPE, f"loss msg_type {mt:#06x} != {EXPECT_MSG_TYPE:#06x}"
    assert pl0 == 7, f"loss payload(delta) {pl0} != 7 (hard-coded ERR_CNT)"
    dut._log.info("PASS — loss frame survives backpressure+contention: integration OK")


def main():
    run_simulation(
        top_level="tb_emit_framer_int",
        module="test_emit_framer_integration",
        custom_libraries={"tich": [
            f"{RTL}/pkg/zs_msg_pkg.vhd",
            f"{RTL}/datalogger/infra/error_event_emitter/error_event_emitter.vhd",
            f"{RTL}/datalogger/infra/zs_msg_framer/zs_msg_framer.vhd",
            f"{ROOT}/hw/sim/cocotb/tb/tb_emit_framer_int.vhd",
        ]},
        generics={"G_FLUSH_CYCLES": FLUSH},
        waves=True,
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
