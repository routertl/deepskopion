# rr unknown-llm (AI-generated)
"""DS-P3.98 — protocol-level SPI traffic through the full datalogger_gather.

A REAL SPI bus (cocotb SpiMaster BFM) is driven onto the gather's SPI0 datalogger
pins (SCLK=23, CS=20, MOSI=21, MISO=22); every other pin idles high so only the
CONFIG-enabled SPI0 instance emits. This lifts the datalogger-level harness up to
the AGGREGATOR — SGA-REQ-3 (single-source routing) attacked with an ACTUAL SPI
transaction (a BFM, not bit-banged/synthetic edges).

The qspi_datalogger (2-wire SPI mode) captures each transferred byte as a TYPE_PAYLOAD
message whose low byte is the MOSI octet (Q0). Verification = the sent MOSI byte
stream appears as a contiguous subsequence of the captured PAYLOAD low-bytes,
all routed to the SPI device id.

bug-finding-is-success: a green bar means the gather routed a real SPI burst
byte-for-byte to the SPI device id; any drop / mis-route / mangled byte is a bug.
"""
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import RisingEdge, ClockCycles, Timer

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv
from rr_cocotb_tb.drivers.spi_master import SpiBus, SpiConfig, SpiMaster

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT / "hw" / "sim" / "cocotb"))   # ds_sim_sources
from ds_sim_sources import scoped_libraries   # noqa: E402

SPI0_ID = 8            # C_SPI0_ID — the gather's SPI0 datalogger device id
PERIOD_NS = 10
SCLK_FREQ = 1_000_000
TYPE_PAYLOAD = 0b001
# Hard-coded MOSI burst (ROUTERTL-002 — fixed, not derived at runtime).
BURST = [0xA1, 0x3C, 0xA1, 0xF0, 0x0F, 0x99]   # 0xA1 repeated → multiplicity


async def reset(dut, n=10):
    dut.RST.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.DOUT_REQ.value = 0
    dut.SCLK.value = 0          # CPOL=0 idle low
    dut.CS.value = 1            # CS inactive (active-low)
    dut.MOSI.value = 0
    dut.MISO.value = 0          # no slave — held idle
    await ClockCycles(dut.CLK, n)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


async def _ramp_timestamp(dut):
    t = 0
    while True:
        await RisingEdge(dut.CLK)
        t = (t + 1) & 0xFFFFFFFF
        dut.TIMESTAMP.value = t


class DoutCollector:
    def __init__(self, dut):
        self.dut = dut
        self.frames = []   # (datalogger_id, payload)
        self._run = True

    async def run(self):
        self.dut.DOUT_REQ.value = 1
        while self._run:
            await RisingEdge(self.dut.CLK)
            if int(self.dut.DOUT_VALID.value) == 1:
                self.frames.append((int(self.dut.DOUT_DATALOGGER_ID.value),
                                    int(self.dut.DOUT_PAYLOAD.value)))

    def stop(self):
        self._run = False


@cocotb.test()
@requires("SGA-REQ-3")
async def test_spi_single_source_routing(dut):
    """SGA-REQ-3: a real SPI byte burst driven (BFM) through the whole gather
    emerges on DOUT routed to the SPI device_id (8), with the MOSI bytes recovered
    as a contiguous PAYLOAD-low-byte subsequence."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset(dut)
    cocotb.start_soon(_ramp_timestamp(dut))

    coll = DoutCollector(dut)
    cocotb.start_soon(coll.run())

    # Enable the SPI0 datalogger (device_id=8): payload bit0=Enable; CPHA=0,
    # full-duplex, CS-mode (bits 1/2/3 = 0). config_control hardened (DS-P3.96).
    dut.CONFIG.value = ((SPI0_ID & 0x1F) << 11) | 0x001
    await ClockCycles(dut.CLK, 64)

    bus = SpiBus(sclk=dut.SCLK, mosi=dut.MOSI, miso=dut.MISO, cs=dut.CS)
    cfg = SpiConfig(word_width=8, sclk_freq=SCLK_FREQ, cpol=False, cpha=False,
                    msb_first=True, cs_active_low=True)
    # The strict SPI monitor must oversample against a real DUT clock.  Passing
    # no sample clock fails at BFM construction (and disabling strict_protocol
    # would hide malformed traffic), so use the same live-clock contract as
    # the datalogger-to-DDR integration bench.
    master = SpiMaster(bus, cfg, sample_clk=dut.CLK)
    await master.write(BURST)

    # Settle: let the last byte's CS-up + payload propagate to DOUT.
    await Timer(50_000, units="ns")
    coll.stop()

    assert coll.frames, "gather emitted NO DOUT frames for a real SPI burst"

    misrouted = sorted({sid for sid, _ in coll.frames if sid != SPI0_ID})
    assert not misrouted, f"frames mis-routed to device_ids {misrouted} (expected {SPI0_ID})"

    # MOSI octets land in the low byte of each TYPE_PAYLOAD message.
    captured = [pw & 0xFF for sid, pw in coll.frames
                if sid == SPI0_ID and ((pw >> 24) & 0x7) == TYPE_PAYLOAD]

    # Byte-EXACT routing (DS-T2.44): the captured MOSI bytes must EQUAL the sent
    # burst — exact count, order, multiplicity, no extra/stale bytes outside the
    # CS window. A contiguous-subsequence match tolerated padding/duplication.
    assert captured == BURST, (
        f"SPI MOSI burst not byte-exact through the gather: got "
        f"{[hex(x) for x in captured]}, expected {[hex(x) for x in BURST]}")

    dut._log.info(f"SGA-REQ-3 SPI protocol routing PASS — {len(captured)} MOSI bytes, "
                  f"byte-exact, all device_id={SPI0_ID}")


def main():
    run_simulation(
        top_level="tb_datalogger_gather_spi",
        module=Path(__file__).stem,
        custom_libraries=scoped_libraries("tb_datalogger_gather_spi"),
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
