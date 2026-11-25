# rr unknown-llm (AI-generated)
"""DS-P3.97 — protocol-level UART traffic through the full datalogger_gather.

A REAL serial UART line (cocotb UartSource) is driven onto the gather's UART0
datalogger pin (TX = C_UART0_TX = 36); every other pin idles high so only the
CONFIG-enabled UART0 instance emits. This lifts the datalogger-level harness up to
the AGGREGATOR — SGA-REQ-3 (single-source routing) attacked with an ACTUAL serial
byte stream, not synthetic edges. UART is one-way (passive monitor): no slave.

The uart_datalogger auto-calibrates the baud (buffers edges, locks, then replays),
so the whole burst decodes; verification = the sent burst appears as a contiguous
subsequence of the captured PAYLOAD bytes (the proven uart_datalogger pattern).

bug-finding-is-success: a green bar means the gather routed a real serial burst
byte-for-byte to the UART device id; any drop / mis-route / mangled byte is a bug.
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
from rr_cocotb_tb.drivers.uart_master import UartSource

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT / "hw" / "sim" / "cocotb"))   # ds_sim_sources
from ds_sim_sources import scoped_libraries   # noqa: E402

UART0_ID = 12          # C_UART0_ID — the gather's UART0 datalogger device id
PERIOD_NS = 10
BAUD = 115_200
DATA_BITS = 8
STOP_BITS = 1
PARITY = "none"
MSG_TYPE_PAYLOAD = 0b010
# Hard-coded burst (ROUTERTL-002). 0x55/0xAA up front give the baud detector
# clean adjacent-bit edges to lock onto.
BURST = [0x55, 0xAA, 0x12, 0x34, 0x12, 0xAD, 0xBE, 0xEF]   # 0x12 repeated → multiplicity


async def reset(dut, n=10):
    dut.RST.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.DOUT_REQ.value = 0
    dut.UART_IN.value = 1            # UART idle high
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
async def test_uart_single_source_routing(dut):
    """SGA-REQ-3: a real UART byte burst driven through the whole gather emerges
    on DOUT routed to the UART device_id (12), with the sent bytes recovered as a
    contiguous PAYLOAD subsequence (the auto-baud datalogger buffers + replays)."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset(dut)
    cocotb.start_soon(_ramp_timestamp(dut))

    coll = DoutCollector(dut)
    cocotb.start_soon(coll.run())

    # Enable the UART0 datalogger (device_id=12): prime the id, then Enable+AutoSync.
    # config_control is hardened (DS-P3.96) so the timing is robust.
    dut.CONFIG.value = (UART0_ID & 0x1F) << 11
    await ClockCycles(dut.CLK, 3)
    dut.CONFIG.value = ((UART0_ID & 0x1F) << 11) | 0x03      # Enable=1, AutoSync=1
    await ClockCycles(dut.CLK, 100)

    uart = UartSource(dut.UART_IN, baud=BAUD, bits=DATA_BITS,
                      stop_bits=STOP_BITS, parity=PARITY)
    await uart.write_bytes(BURST)

    # Settle: flush + baud calibration + edge replay + decode.
    await Timer(2_500_000, units="ns")
    coll.stop()

    assert coll.frames, "gather emitted NO DOUT frames for a real UART burst"

    # Single-source routing: every frame must carry the UART0 device id.
    misrouted = sorted({sid for sid, _ in coll.frames if sid != UART0_ID})
    assert not misrouted, f"frames mis-routed to device_ids {misrouted} (expected {UART0_ID})"

    captured = [pw & 0xFF for sid, pw in coll.frames
                if sid == UART0_ID and ((pw >> 24) & 0x7) == MSG_TYPE_PAYLOAD]

    # Byte-EXACT routing (DS-T2.44): the captured payload bytes must EQUAL the
    # sent burst — exact count, order, multiplicity, no extra/stale bytes outside
    # the window. The auto-baud datalogger buffers+replays the WHOLE burst, so this
    # is a clean equality (a subsequence match tolerated padding/duplication).
    assert captured == BURST, (
        f"UART burst not byte-exact through the gather: got {[hex(x) for x in captured]}, "
        f"expected {[hex(x) for x in BURST]}")

    dut._log.info(f"SGA-REQ-3 UART protocol routing PASS — {len(captured)} payload bytes, "
                  f"byte-exact, all device_id={UART0_ID}")


def main():
    run_simulation(
        top_level="tb_datalogger_gather_uart",
        module=Path(__file__).stem,
        custom_libraries=scoped_libraries("tb_datalogger_gather_uart"),
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
