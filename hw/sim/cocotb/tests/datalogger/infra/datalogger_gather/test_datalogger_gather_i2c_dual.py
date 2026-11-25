# rr unknown-llm (AI-generated)
"""DS-P2.107 — two independent I2C buses through the full datalogger_gather.

The DE25 stereo-vision capture use-case: the Zybo Z7-20 (VmodCAM) configures
cam A and cam B over two SEPARATE I2C buses, and the DE25 deepskopion datalogger
must decode BOTH concurrently, byte-exact, with NO cross-bleed. Channel map
(DE25_BOARD_FACTS.md): cam B -> I2C0 (device_id 4, pins 12/13 = ja[0]/ja[1]);
cam A -> I2C1 (device_id 5, pins 14/15 = ja[2]/ja[3]).

Two REAL I2C buses (two cocotb I2cMasterDriver + two VHDL i2c_slave BFMs, each
open-drain) run at 100 kHz on their own pins simultaneously. The harness runs
at G_SYS_CLK = 50 MHz (the DE25 clk_sys = CLOCK2_50), so this is ALSO the
behavioural proof of the DS-T1.10 fix (datalogger baud/glitch math at the real
DE25 timebase, not the 100 MHz Zynq default).

Prohibition (SGA-REQ-3, single-source routing, at the two-channel level): a
transaction on one bus must NEVER appear on the other channel's device_id.
Distinct data per bus makes any cross-bleed a byte-exact FAILURE, not a
tolerated set-overlap.

bug-finding-is-success: a green bar means both channels demuxed their own bus
byte-for-byte; any drop / mis-route / swapped byte is a real DUT defect.
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

I2C0_ID = 4          # cam B — C_I2C0_ID, pins 12/13
I2C1_ID = 5          # cam A — C_I2C1_ID, pins 14/15
SLAVE0_ADDR = 0x50   # cam B sensor address
SLAVE1_ADDR = 0x51   # cam A sensor address (distinct → cross-bleed is detectable)
PERIOD_NS = 20       # 50 MHz — the DE25 clk_sys
I2C_PERIOD_NS = 10_000   # 100 kHz — well within the I2C glitch-filter window


async def reset(dut, n=10):
    dut.RST.value = 1
    dut.CONFIG.value = 0
    dut.TIMESTAMP.value = 0
    dut.DOUT_REQ.value = 0
    dut.I2C0_SLAVE_ADDR.value = SLAVE0_ADDR
    dut.I2C1_SLAVE_ADDR.value = SLAVE1_ADDR
    dut.M0_SCL.value = 1
    dut.M0_SDA_OUT.value = 1
    dut.M1_SCL.value = 1
    dut.M1_SDA_OUT.value = 1
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


def _decode(frames, sid, decoder):
    """Return (payload bytes, event codes) for one channel's frames."""
    msgs = [decoder.decode(pw) for s, pw in frames if s == sid]
    return ([m.data for m in msgs if m.is_payload],
            [m.event_code for m in msgs if m.is_event])


@cocotb.test()
@requires("SGA-REQ-3")
async def test_two_i2c_buses_decode_without_crossbleed(dut):
    """Two concurrent I2C write transactions on separate buses each emerge on
    their own device_id (4 and 5), byte-exact, with neither channel carrying
    the other bus's bytes — proven at the DE25's 50 MHz timebase."""
    env = TbEnv(dut, clk="CLK", rst="RST", period_ns=PERIOD_NS)
    await env.start_clock()
    await reset(dut)
    cocotb.start_soon(_ramp_timestamp(dut))

    m0 = I2cMasterDriver(env, sda=dut.M0_SDA_OUT, sda_in=dut.M0_SDA_IN,
                         scl=dut.M0_SCL, timing=I2cTiming(period_ns=I2C_PERIOD_NS))
    m1 = I2cMasterDriver(env, sda=dut.M1_SDA_OUT, sda_in=dut.M1_SDA_IN,
                         scl=dut.M1_SCL, timing=I2cTiming(period_ns=I2C_PERIOD_NS))
    await m0.initialize()
    await m1.initialize()

    coll = DoutCollector(dut)
    cocotb.start_soon(coll.run())

    # Enable BOTH I2C dataloggers — two CONFIG steps, each latches its own device.
    dut.CONFIG.value = pack_config(I2cDataloggerCfg(device_id=I2C0_ID, enable=True))
    await ClockCycles(dut.CLK, 64)
    dut.CONFIG.value = pack_config(I2cDataloggerCfg(device_id=I2C1_ID, enable=True))
    await ClockCycles(dut.CLK, 64)

    # Distinct payloads so a swapped byte between channels fails byte-exact.
    data0 = [0xAB, 0xCD, 0x01, 0x02]   # cam B
    data1 = [0x3C, 0x5A, 0x77, 0x99]   # cam A
    assert data0 != data1

    # Drive both buses CONCURRENTLY — the datalogger must demux by pin, not by time.
    t0 = cocotb.start_soon(m0.write_transaction(SLAVE0_ADDR, data0))
    t1 = cocotb.start_soon(m1.write_transaction(SLAVE1_ADDR, data1))
    await t0
    await t1
    await ClockCycles(dut.CLK, 800)
    coll.stop()

    assert coll.frames, "gather emitted NO DOUT frames for two concurrent I2C buses"

    # Every frame must belong to one of the two enabled I2C channels — nothing else.
    stray = sorted({sid for sid, _ in coll.frames if sid not in (I2C0_ID, I2C1_ID)})
    assert not stray, f"frames on unexpected device_ids {stray} (expected {{4,5}})"
    seen = {sid for sid, _ in coll.frames}
    assert I2C0_ID in seen and I2C1_ID in seen, (
        f"both channels must emit — got only {sorted(seen)}")

    decoder = I2cMsgDecoder(device_id=I2C0_ID)  # payload decode is id-agnostic
    got0, ev0 = _decode(coll.frames, I2C0_ID, decoder)
    got1, ev1 = _decode(coll.frames, I2C1_ID, decoder)

    exp0 = [(SLAVE0_ADDR << 1)] + data0
    exp1 = [(SLAVE1_ADDR << 1)] + data1

    # Byte-EXACT per channel: exact count, order, multiplicity. Because exp0 and
    # exp1 differ, this exact match is itself the cross-bleed guard — any byte
    # from the other bus leaking in makes got != exp.
    assert got0 == exp0, (
        f"cam B / id4 not byte-exact: got {[hex(x) for x in got0]}, "
        f"expected {[hex(x) for x in exp0]}")
    assert got1 == exp1, (
        f"cam A / id5 not byte-exact: got {[hex(x) for x in got1]}, "
        f"expected {[hex(x) for x in exp1]}")

    # Explicit cross-bleed prohibition: neither channel reproduced the other's bus.
    assert got0 != exp1 and got1 != exp0, (
        "cross-bleed: a channel carried the OTHER bus's byte sequence")

    for sid, ev in ((I2C0_ID, ev0), (I2C1_ID, ev1)):
        assert I2cEvent.START in ev, f"id{sid} START not captured: {ev}"
        assert I2cEvent.STOP in ev, f"id{sid} STOP not captured: {ev}"

    dut._log.info(
        f"DS-P2.107 two-bus I2C PASS @50MHz — id4={[hex(x) for x in got0]}, "
        f"id5={[hex(x) for x in got1]}, frames={len(coll.frames)}")


def main():
    run_simulation(
        top_level="tb_datalogger_gather_i2c_dual",
        module=Path(__file__).stem,
        custom_libraries=scoped_libraries("tb_datalogger_gather_i2c_dual"),
        work_lib="tich",
    )


if __name__ == "__main__":
    main()
