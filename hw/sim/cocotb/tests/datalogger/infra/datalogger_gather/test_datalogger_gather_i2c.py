# rr unknown-llm (AI-generated)
"""DS-P3.32 — protocol-level I2C traffic through the full datalogger_gather.

A REAL I2C bus (cocotb I2cMasterDriver + a VHDL i2c_slave BFM that ACKs, wired
open-drain) is driven onto the gather's I2C0 datalogger pins (SCL=12, SDA=13); every
other pin idles high so only the CONFIG-enabled I2C0 instance emits. This lifts
the datalogger-level BFM harness up to the AGGREGATOR — SGA-REQ-3 (single-source
routing) attacked with an ACTUAL protocol transaction, not synthetic edges.

bug-finding-is-success: a green bar here means the gather faithfully routed a
real transaction byte-for-byte; any drop / mis-route / mangled byte is a defect.

History (config_control timing, hard-won): the per-datalogger enable latch used to
need `newConfEvent_r` (from the DIRECT CONFIG input) and `deviceIdMatch_r` (from
a delta-delayed concurrent `deviceIdMatch` signal) high the SAME cycle. A CONFIG
step issued edge-aligned to the clock skewed those two by a sim delta and the
enable silently never latched (the datalogger ignored a perfectly good bus). This
cost ~15 debug iterations and motivated DS-P3.96, which derives both latch terms
from the same registered CONFIG read — so a plain CONFIG step now latches and
this test no longer needs a between-edges workaround. (Hardware was always fine:
timing-closed combinational logic has no delta race.) See hint H.106.
"""
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import RisingEdge, ClockCycles

import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))
from engine.simulation import run_simulation   # noqa: E402

from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv
from rr_cocotb_tb.drivers.i2c_master import I2cMasterDriver, I2cTiming

DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(DEEPSKOPION_ROOT / "hw" / "sim"))             # vip.datalogger.i2c
sys.path.insert(0, str(DEEPSKOPION_ROOT / "hw" / "sim" / "cocotb"))   # ds_sim_sources
from vip.datalogger.i2c import I2cMsgDecoder, I2cEvent, I2cDataloggerCfg, pack_config   # noqa: E402
from ds_sim_sources import scoped_libraries   # noqa: E402

I2C0_ID = 4          # C_I2C0_ID — the gather's I2C0 datalogger device id
SLAVE_ADDR = 0x50
PERIOD_NS = 10
I2C_PERIOD_NS = 10_000   # 100 kHz — well within the I2C glitch-filter window


async def reset(dut, n=10):
    dut.RST.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.DOUT_REQ.value = 0
    dut.I2C_SLAVE_ADDR.value = SLAVE_ADDR
    dut.M_SCL.value = 1
    dut.M_SDA_OUT.value = 1
    await ClockCycles(dut.CLK, n)
    dut.RST.value = 0
    await RisingEdge(dut.CLK)


class DoutCollector:
    """Drains the gather burst output (DOUT_REQ held high) into a frame list."""
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


async def _ramp_timestamp(dut):
    t = 0
    while True:
        await RisingEdge(dut.CLK)
        t = (t + 1) & 0xFFFFFFFF
        dut.TIMESTAMP.value = t


@cocotb.test()
@requires("SGA-REQ-3")
async def test_i2c_single_source_routing(dut):
    """SGA-REQ-3: a real I2C write transaction driven through the whole gather
    emerges on DOUT routed to the I2C device_id (4), with START + the addr/data
    bytes + STOP recovered byte-exact via the sanctioned I2C decoder."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset(dut)
    cocotb.start_soon(_ramp_timestamp(dut))

    master = I2cMasterDriver(env, sda=dut.M_SDA_OUT, sda_in=dut.M_SDA_IN,
                             scl=dut.M_SCL, timing=I2cTiming(period_ns=I2C_PERIOD_NS))
    await master.initialize()
    decoder = I2cMsgDecoder(device_id=I2C0_ID)
    coll = DoutCollector(dut)
    cocotb.start_soon(coll.run())

    # Enable ONLY the I2C0 datalogger (device_id=4). DS-P3.96 hardened
    # config_control's enable latch against the CONFIG-write delta skew, so this
    # no longer needs a between-edges workaround — a plain CONFIG step latches.
    dut.CONFIG.value = pack_config(I2cDataloggerCfg(device_id=I2C0_ID, enable=True))
    await ClockCycles(dut.CLK, 64)

    data = [0xAB, 0xCD, 0xAB, 0x5A]   # 0xAB repeated → exercises multiplicity
    await master.write_transaction(SLAVE_ADDR, data)
    await ClockCycles(dut.CLK, 400)
    coll.stop()

    assert coll.frames, "gather emitted NO DOUT frames for a real I2C transaction"

    # Single-source routing: every frame must carry the I2C0 device id.
    misrouted = sorted({sid for sid, _ in coll.frames if sid != I2C0_ID})
    assert not misrouted, f"frames mis-routed to device_ids {misrouted} (expected {I2C0_ID})"

    msgs = [decoder.decode(pw) for _, pw in coll.frames]
    ev_codes = [m.event_code for m in msgs if m.is_event]
    assert I2cEvent.START in ev_codes, f"START not captured: events={ev_codes}"
    assert I2cEvent.STOP in ev_codes, f"STOP not captured: events={ev_codes}"

    # Byte-EXACT routing (DS-T2.44): the captured payload bytes must EQUAL the
    # addr+W byte followed by the data bytes — exact count, order, multiplicity;
    # no spurious/duplicated/reordered bytes. Set-membership tolerated all three.
    got = [m.data for m in msgs if m.is_payload]
    expect = [(SLAVE_ADDR << 1)] + data        # addr+W byte, then the data bytes
    assert got == expect, (
        f"I2C payload not byte-exact through the gather: got {[hex(x) for x in got]}, "
        f"expected {[hex(x) for x in expect]}")

    dut._log.info(f"SGA-REQ-3 I2C protocol routing PASS — {len(coll.frames)} frames, "
                  f"events={ev_codes}, payload bytes={[hex(x) for x in got]}")


def main():
    run_simulation(
        top_level="tb_datalogger_gather_i2c",
        module=Path(__file__).stem,
        custom_libraries=scoped_libraries("tb_datalogger_gather_i2c"),
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
