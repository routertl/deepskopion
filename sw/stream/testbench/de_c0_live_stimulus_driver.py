"""DS-P2.101 — on-board driver that armed the DE-C0 live-silicon witness.

Runs ON THE BOOTED DE25 BOARD (over the console, root shell — see
the live-witness execution documentation for the
exact transfer/invocation commands and the captured transcript). It arms
DS-P2.100's fabric stimulus generator (``hw/rtl/stimulus/ds_stimulus_gen.vhd``)
from the regbank for one protocol, then drains + decodes the real DDR ring
through the SAME ``sw.stream.data_source.DdrRingDataSource`` code the
``python3 -m sw.stream`` daemon uses — the only difference from
``capture_correctness.py``'s board-free harness is the byte source: here it is
``RealDdrMmap`` reading the actual DDR ring a real PL wrote, not a
``FakeDdrMmap``.

The ``S99deepskopion-stream`` daemon must be stopped first (``/etc/init.d/
S99deepskopion-stream stop``) — it and this script would otherwise race for
the same DDR ring read pointer.

Register offsets mirror ``hw/rtl/pkg/system_regbank_pkg.vhd``'s
``C_INDEX_STIM_*`` constants (byte offset = word index * 4) and
``hw/rtl/stimulus/ds_stimulus_gen.vhd``'s port comments; there is no
``sw.stream.regbank.Regs`` entry for the STIM_* slots yet (DS-P2.100 added
the RTL/regbank side but not a Python symbolic name — a candidate follow-up,
not required for this one-off driver).

Run (as root, on the board, after stopping the daemon):

    python3 de_c0_live_stimulus_driver.py {uart,i2c,spi,edge}
"""
import sys
import time

sys.path.insert(0, "/usr/local/share/deepskopion")
from sw.stream.regbank import MmioRegbank, Regs
from sw.stream.ddr_mmap import RealDdrMmap
from sw.stream.data_source import DdrRingDataSource
# DS-P1.59: the arm sequences live in the shared production armer so this
# stop-daemon driver and the daemon-live witness cannot drift apart.
from sw.stream.testbench.de_c0_production_armer import arm as arm_protocol

rb = MmioRegbank(base_addr=0x20000000, size=0x1000)
ring_base = (rb.read_u32(Regs.RING_BASE_HIGH) << 32) | rb.read_u32(Regs.RING_BASE_LOW)
ring_bytes = rb.read_u32(Regs.RING_BYTES)
print("ring_base=%#x ring_bytes=%#x" % (ring_base, ring_bytes))
ddr = RealDdrMmap(base_addr=ring_base, size=ring_bytes)
# clk_sys = CLOCK2_50 direct, no PLL (DS-T2.73) — 50MHz, NOT 100MHz.
src = DdrRingDataSource(regbank=rb, ddr=ddr, sample_clk_hz=50_000_000,
                         poll_period_s=0.02, clock=time.monotonic)


def drain(seconds):
    end = time.monotonic() + seconds
    anns, edges = [], []
    while time.monotonic() < end:
        b = src.next_batch()
        if b is not None:
            anns.extend(b.annotations or [])
            edges.extend(b.all_edge_words())
        time.sleep(0.02)
    return anns, edges


def report(proto, anns, edges, expect_desc):
    print("RESULT %s: %d annotation(s), %d edge word(s) -- expect %s" %
          (proto, len(anns), len(edges), expect_desc))
    for a in anns:
        print("  ANN", a.kind, a.channel_id, a.fields)
    for e in edges[:20]:
        print("  EDGE %#010x" % e)


src.next_batch()  # seed the read pointer, discard any backlog

proto = sys.argv[1] if len(sys.argv) > 1 else "uart"

_EXPECT = {
    "uart": "[0xA3,0x5C,0x71,0x0E,0xFF,0x42,0x00,0xFF,0x00,0xFF] in order",
    "i2c": "one ACK-bound transaction addr=0x50 data=[0xAB,0xCD,0x3E]",
    "spi": "[0xDE,0xAD,0xBE,0xEF] in order",
    "edge": "finite [(10000,1),(10200,0),(10520,1),(10800,0)] ns vector",
}
_DRAIN_S = {"uart": 2.0, "i2c": 2.0, "spi": 2.0, "edge": 0.1}

# Arm via the SHARED production armer (regbank writes only) — the exact same
# sequence the daemon-live witness uses.  This driver then reads the ring
# ITSELF (stop-daemon model), so the daemon must be stopped first.
arm_protocol(rb, proto)
anns, edges = drain(_DRAIN_S.get(proto, 2.0))
report(proto, anns, edges, _EXPECT.get(proto, "?"))

src.close()
rb.close()
print("DONE " + proto)
