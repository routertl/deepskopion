# rr unknown-llm (AI-generated)
"""
test_i2c_injector — cocotb regression for hw/rtl/responder/i2c_injector.vhd
(DS-P3.43; closes part of DS-P2.51 Phase 3).

Wraps i2c_injector in tb_i2c_injector (master-side open-drain bus model)
and exercises the regbank-fed FIFO → slave-driven MISO contract from
the master side. Each test anchors on a single RESP-REQ-N from
hw/rtl/responder/requirements.yml.

Run with:
    rr sim run test_i2c_injector
"""
import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

# Routertl SDK cocotb path (mirrors test_i2c_datalogger.py).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

# Deepskopion VIP path.
_ds_root = Path(__file__).resolve().parents[5]
_vip_parent = _ds_root / "hw" / "sim"
if str(_vip_parent) not in sys.path:
    sys.path.insert(0, str(_vip_parent))

from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv
from rr_cocotb_tb.drivers.i2c_master import I2cMasterDriver, I2cTiming


GENERICS = {
    "G_FIFO_DEPTH": 4,
    # Scale the inactivity-watchdog timeout down for sim: at 100 kHz it is
    # (100000/1000)*25 = 2500 CLK cycles (real silicon = 100 MHz → 25 ms).
    # 2500 >> the ~125-cycle SCL half-period, so healthy reads never trip it,
    # but it's reachable in the abort test.
    "G_CLK_FREQ_HZ": 100_000,
}
# Inactivity-watchdog timeout in CLK cycles for the configured G_CLK_FREQ_HZ.
WATCHDOG_CYCLES = (GENERICS["G_CLK_FREQ_HZ"] // 1000) * 25

SLAVE_ADDR = 0x50          # 7-bit slave address pinned by CONTROL[7:1].
CTRL_ENABLE = 0x1          # CONTROL[0] = enable.
CTRL_FIFO_RESET = 1 << 9   # CONTROL[9] = self-clearing FIFO reset.


def make_control(enable: bool = True, addr: int = SLAVE_ADDR,
                 fifo_reset: bool = False) -> int:
    """Pack the i2c_injector CONTROL word per its header table."""
    val = 0
    if enable:
        val |= CTRL_ENABLE
    val |= (addr & 0x7F) << 1
    if fifo_reset:
        val |= CTRL_FIFO_RESET
    return val


class I2cInjectorTb:
    """Testbench environment for tb_i2c_injector."""

    def __init__(self, dut):
        self.dut = dut
        self.env = TbEnv(
            dut,
            clk="CLK",
            rst="RST",
            period_ns=10,  # 100 MHz clk_sys (matches deepskopion_top)
        )
        # Master driver — feeds M_SDA_OUT / M_SCL, observes M_SDA_IN.
        self.i2c = I2cMasterDriver(
            self.env,
            sda=dut.M_SDA_OUT,
            sda_in=dut.M_SDA_IN,
            scl=dut.M_SCL,
            timing=I2cTiming(period_ns=10_000),   # 100 kHz I2C
        )

    async def setup(self):
        await self.env.start_clock()
        # Idle bus + CONTROL gate before reset goes low (avoid X propagation
        # into the slave on the first cycle).
        self.dut.ENABLE.value     = 0
        self.dut.CONTROL.value    = 0
        self.dut.DATAIN.value     = 0
        self.dut.DATAIN_VLD.value = 0
        self.dut.M_SDA_OUT.value  = 1
        self.dut.M_SCL.value      = 1
        await self.env.reset(cycles=5)
        await self.i2c.initialize()

    async def enable_slave(self, addr: int = SLAVE_ADDR):
        """Pin slave address + enable."""
        self.dut.CONTROL.value = make_control(enable=True, addr=addr)
        self.dut.ENABLE.value  = 1
        await ClockCycles(self.env.clk, 4)

    async def push_byte(self, byte: int):
        """Push one byte into the TX FIFO via DATAIN strobe."""
        self.dut.DATAIN.value = byte & 0xFF
        self.dut.DATAIN_VLD.value = 1
        await ClockCycles(self.env.clk, 1)
        self.dut.DATAIN_VLD.value = 0
        await ClockCycles(self.env.clk, 1)

    async def push_bytes(self, payload):
        for b in payload:
            await self.push_byte(b)

    async def reset_fifo(self):
        """Pulse CONTROL[9] (self-clearing edge) and wait for drain to complete.

        DS-T3.2: CONTROL[9] now enters a drain-mode FSM rather than asserting
        the XPM FIFO RST. Drain runs at one byte per cycle, so the wait
        must scale with how many bytes are queued. We poll STATUS[15]
        (drain_busy) with a generous bound so the test is robust to
        whatever the host had loaded — without padding any specific
        cycle count.
        """
        prev = self.dut.CONTROL.value.integer
        self.dut.CONTROL.value = prev | CTRL_FIFO_RESET
        await ClockCycles(self.env.clk, 2)
        self.dut.CONTROL.value = prev
        # Poll until drain settles (drain_busy=0 AND fifo_empty=1). Single
        # drain_busy=0 isn't enough — drain mode takes ~3 cycles of entry
        # latency from the CONTROL[9] write (cocotb signal-commit + the
        # 2-cycle p_fifo_reset → p_drain_mode pipeline), so a naive poll
        # could break on the pre-entry "drain_busy=0" state. Bound at
        # 4 * 2**G_FIFO_DEPTH cycles (entry latency + 1 cy / byte × max
        # depth × safety factor).
        max_drain_cycles = 4 * (2 ** GENERICS["G_FIFO_DEPTH"])
        for _ in range(max_drain_cycles):
            s = self.dut.STATUS.value.integer
            if ((s >> 15) & 0x1) == 0 and (s & 0x1) == 1:
                break
            await ClockCycles(self.env.clk, 1)
        else:
            raise AssertionError(
                f"drain did not settle (drain_busy + fifo_empty) within "
                f"{max_drain_cycles} cycles: "
                f"STATUS=0x{self.dut.STATUS.value.integer:08X}"
            )

    def status_fifo_empty(self) -> int:
        return self.dut.STATUS.value.integer & 0x1

    def status_fifo_count(self) -> int:
        # STATUS bits [G_FIFO_DEPTH+3 downto 3].
        depth = GENERICS["G_FIFO_DEPTH"]
        mask = (1 << (depth + 1)) - 1
        return (self.dut.STATUS.value.integer >> 3) & mask

    def status_drain_busy(self) -> int:
        """STATUS[15] (DS-T3.2) — CONTROL[9] drain in progress."""
        return (self.dut.STATUS.value.integer >> 15) & 0x1


# ─────────────────────────────────────────────────────────────────────────
#  Functional tests
# ─────────────────────────────────────────────────────────────────────────

@cocotb.test()
@requires("I2C-INJ-REQ-4")
async def test_datain_push_increments_count(dut):
    """DATAIN_VLD pulses push bytes; STATUS count advances; empty clears."""
    tb = I2cInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    # Sanity — FIFO starts empty.
    assert tb.status_fifo_empty() == 1, \
        f"FIFO not empty post-reset: STATUS=0x{dut.STATUS.value.integer:08X}"
    assert tb.status_fifo_count() == 0

    await tb.push_byte(0xA5)
    await ClockCycles(tb.env.clk, 4)

    assert tb.status_fifo_empty() == 0, \
        f"FIFO still empty after push: STATUS=0x{dut.STATUS.value.integer:08X}"
    assert tb.status_fifo_count() == 1, \
        f"FIFO count after 1 push = {tb.status_fifo_count()}, want 1"
    tb.env.log.info("✓ DATAIN_VLD push increments count + clears empty")


@cocotb.test()
@requires("I2C-INJ-REQ-5")
async def test_fifo_reset_drains(dut):
    """CONTROL[9] pulse drains the FIFO."""
    tb = I2cInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    await tb.push_bytes([0x11, 0x22, 0x33])
    await ClockCycles(tb.env.clk, 4)
    assert tb.status_fifo_count() == 3

    await tb.reset_fifo()
    assert tb.status_fifo_empty() == 1, \
        f"FIFO not empty after reset: STATUS=0x{dut.STATUS.value.integer:08X}"
    assert tb.status_fifo_count() == 0
    tb.env.log.info("✓ CONTROL[9] drains the FIFO")


@cocotb.test()
@requires("I2C-INJ-REQ-1")
async def test_master_read_returns_first_byte(dut):
    """Master read at the configured address returns the first FIFO byte."""
    tb = I2cInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    expected = 0xDE
    await tb.push_byte(expected)
    await ClockCycles(tb.env.clk, 4)

    await tb.i2c.start()
    ack = await tb.i2c.write_byte((SLAVE_ADDR << 1) | 1)   # R/W = 1 (read)
    assert ack, f"slave did not ACK address 0x{SLAVE_ADDR:02X} read"

    got = await tb.i2c.read_byte(send_ack=False)           # NAK last byte
    await tb.i2c.stop()

    assert got == expected, \
        f"master read 0x{got:02X}, expected 0x{expected:02X}"
    tb.env.log.info(f"✓ master read returned 0x{got:02X}")


@cocotb.test()
@requires("I2C-INJ-REQ-2")
async def test_master_read_multi_byte_in_fifo_order(dut):
    """Multi-byte read returns FIFO bytes in push order."""
    tb = I2cInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    payload = [0xDE, 0xAD, 0xBE, 0xEF]
    await tb.push_bytes(payload)
    await ClockCycles(tb.env.clk, 4)

    await tb.i2c.start()
    ack = await tb.i2c.write_byte((SLAVE_ADDR << 1) | 1)
    assert ack, "slave did not ACK address byte"

    received = []
    for idx, _ in enumerate(payload):
        last = (idx == len(payload) - 1)
        received.append(await tb.i2c.read_byte(send_ack=not last))
    await tb.i2c.stop()

    assert received == payload, \
        f"FIFO-order mismatch: got {[hex(b) for b in received]}, want {[hex(b) for b in payload]}"
    tb.env.log.info(f"✓ multi-byte read returned {[hex(b) for b in received]} in order")


@cocotb.test()
@requires("I2C-INJ-REQ-3")
async def test_wrong_address_does_not_consume_fifo(dut):
    """Master read at an unmatched address does not consume any FIFO entry."""
    tb = I2cInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    await tb.push_bytes([0xCA, 0xFE])
    await ClockCycles(tb.env.clk, 4)
    count_before = tb.status_fifo_count()
    assert count_before == 2

    wrong_addr = (SLAVE_ADDR ^ 0x0F) & 0x7F
    await tb.i2c.start()
    ack = await tb.i2c.write_byte((wrong_addr << 1) | 1)
    await tb.i2c.stop()
    await ClockCycles(tb.env.clk, 4)

    # The slave SHOULD NAK the wrong address. If the implementation does
    # ACK it, this REQ catches the deviation.
    assert not ack, \
        f"slave ACK'd wrong address 0x{wrong_addr:02X} (expected NAK)"
    assert tb.status_fifo_count() == count_before, (
        f"FIFO count drifted from {count_before} to {tb.status_fifo_count()} "
        "during wrong-address read"
    )
    tb.env.log.info("✓ wrong address → NAK, FIFO untouched")


# ─────────────────────────────────────────────────────────────────────────
#  DS-T1.3 adversarial coverage — the bench-shaped scenarios that surfaced
#  the "byte-late" pattern on PS-EMIO I2C reads (REQ-6).
#
#  Bench observation (pre-DS-T1.2 + pre-DS-T2.14, with 5× FIFO amplification):
#      preload [0xFF] → FIFO has 5 copies → master 4-byte read got
#      [0x00, 0x00, 0x00, 0xFF] instead of [0xFF, 0xFF, 0xFF, 0xFF].
#
#  These tests pin the slave's behaviour in the regimes that combine to
#  produce that pattern, so any future regression in the FSM (cntSamples
#  reset, FIFO-empty handling mid-burst, back-to-back transactions) is
#  caught here before the bench cycle.
# ─────────────────────────────────────────────────────────────────────────


@cocotb.test()
@requires("I2C-INJ-REQ-2")
async def test_master_read_4byte_identical_payload(dut):
    """4-byte read of [0xFF]*5 (bench-shaped) returns [0xFF]*4 exactly.

    Mirrors the DS-T1.3 reproducer: identical-byte payload defeats the
    eyeball-it heuristic ("master sees 0x00 instead of 0xFF") that the
    bench surfaced.
    """
    tb = I2cInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    payload = [0xFF] * 5
    await tb.push_bytes(payload)
    await ClockCycles(tb.env.clk, 4)

    await tb.i2c.start()
    ack = await tb.i2c.write_byte((SLAVE_ADDR << 1) | 1)
    assert ack, "slave did not ACK address byte"

    n_read = 4
    received = []
    for idx in range(n_read):
        last = (idx == n_read - 1)
        received.append(await tb.i2c.read_byte(send_ack=not last))
    await tb.i2c.stop()

    expected = [0xFF] * n_read
    assert received == expected, (
        f"DS-T1.3 reproducer: got {[hex(b) for b in received]}, "
        f"want {[hex(b) for b in expected]} — "
        f"slave drove wrong bits on a constant-payload read"
    )
    tb.env.log.info(f"✓ identical-payload 4-byte read returned {[hex(b) for b in received]}")


@cocotb.test()
@requires("I2C-INJ-REQ-1")
async def test_master_single_byte_reads_back_to_back(dut):
    """Three back-to-back 1-byte read transactions return push order.

    Each transaction is START+addr+1 byte+NACK+STOP — separate transactions,
    not a 3-byte read. Mirrors what Linux i2c-cdns may issue if userspace
    calls read(1) three times. Pins the slave's WAIT_START → CAPTURE_ADDRESS
    → ADDRESS_ACK → WAIT_INTERNAL_DATA → SEND_DATA → WAIT_ACK (NACK) →
    WAIT_START cycle across transaction boundaries.
    """
    tb = I2cInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    payload = [0xA5, 0x5A, 0xC3]
    await tb.push_bytes(payload)
    await ClockCycles(tb.env.clk, 4)

    received = []
    for _ in payload:
        await tb.i2c.start()
        ack = await tb.i2c.write_byte((SLAVE_ADDR << 1) | 1)
        assert ack, "slave did not ACK address on a single-byte transaction"
        received.append(await tb.i2c.read_byte(send_ack=False))
        await tb.i2c.stop()
        await ClockCycles(tb.env.clk, 8)

    assert received == payload, (
        f"back-to-back 1-byte reads: got {[hex(b) for b in received]}, "
        f"want {[hex(b) for b in payload]}"
    )
    tb.env.log.info(f"✓ three back-to-back 1-byte reads returned {[hex(b) for b in received]}")


@cocotb.test()
@requires("I2C-INJ-REQ-2")
async def test_master_read_more_than_fifo_holds(dut):
    """4-byte read against a 2-byte FIFO returns the 2 real bytes then the
    'fifo empty' release level (0xFF, pullup) for the rest — NOT 0x00.

    Pins WAIT_INTERNAL_DATA's released-SDA behaviour when I2C_TX_VLD goes
    low mid-burst. The slave must NOT continue driving the prior ACK level
    (0x00) into the next byte's data window — the bench-observed 0x00 pattern
    is precisely what this test forbids.
    """
    tb = I2cInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()

    real_payload = [0xC3, 0x3C]
    await tb.push_bytes(real_payload)
    await ClockCycles(tb.env.clk, 4)

    await tb.i2c.start()
    ack = await tb.i2c.write_byte((SLAVE_ADDR << 1) | 1)
    assert ack, "slave did not ACK address byte"

    n_read = 4
    received = []
    for idx in range(n_read):
        last = (idx == n_read - 1)
        received.append(await tb.i2c.read_byte(send_ack=not last))
    await tb.i2c.stop()

    # First two bytes are the real FIFO payload. Remaining bytes are
    # "released SDA" = master pullup = 0xFF.
    expected = real_payload + [0xFF] * (n_read - len(real_payload))
    assert received == expected, (
        f"drain-mid-read: got {[hex(b) for b in received]}, "
        f"want {[hex(b) for b in expected]} — slave drove wrong bits "
        "for bytes past FIFO exhaustion"
    )
    tb.env.log.info(f"✓ over-read returned {[hex(b) for b in received]} (real + released)")


@cocotb.test()
@requires("I2C-INJ-REQ-7")
async def test_aborted_read_releases_sda_after_timeout(dut):
    """DS-T2.21 / Haz.61 regression: a master that abandons a read mid-byte
    (no clean STOP) must NOT leave the slave holding SDA low forever.

    Real-HW wedge: the master aborts, SCL idles HIGH (so the 'SCL stuck low'
    path can't help), and the slave sits in a driving state pulling SDA low →
    the EMIO-I2C bus wedges into permanent EAGAIN, unrecoverable in software.
    The free-running timeout kicks the FSM to I2C_WAIT_START, where the slave
    MUST release SDA. Before the fix, I2C_WAIT_START held the prior sdaOut_r=0,
    so the bus never freed.
    """
    tb = I2cInjectorTb(dut)
    await tb.setup()
    await tb.enable_slave()
    await tb.push_byte(0x00)        # first data bit = 0 → slave drives SDA low
    await ClockCycles(tb.env.clk, 4)

    # Matched-address read, then clock one data bit so the slave is in SEND_DATA
    # driving bit7=0 (SDA low). Then ABANDON: freeze SCL HIGH, master releases.
    await tb.i2c.start()
    ack = await tb.i2c.write_byte((SLAVE_ADDR << 1) | 1)
    assert ack, "slave did not ACK matched address"
    await tb.i2c.read_bit()
    dut.M_SCL.value = 1
    dut.M_SDA_OUT.value = 1
    await ClockCycles(tb.env.clk, 8)

    # Precondition — the repro must actually hold SDA low, else the test proves
    # nothing (gate against a silent false pass).
    assert dut.M_SDA_IN.value == 0, (
        "repro precondition failed: expected the slave holding SDA low after an "
        f"aborted read, got M_SDA_IN={dut.M_SDA_IN.value}"
    )

    # Fire the inactivity watchdog (DS-T2.34/DS-T2.36 fix): with the bus frozen
    # (SCL HIGH, master released → NO SCL edges), the watchdog counts CLK cycles
    # and after WATCHDOG_CYCLES with no activity fires sclLowTimeout → FSM to
    # I2C_WAIT_START → SDA released. Wait comfortably past the timeout. (The old
    # design free-ran off a TIMESTAMP bit and fired regardless of activity —
    # which is what spuriously aborted ~35% of healthy reads, DS-T2.34.)
    await ClockCycles(tb.env.clk, WATCHDOG_CYCLES + 50)

    assert dut.M_SDA_IN.value == 1, (
        "EMIO-I2C WEDGE: slave still driving SDA low after the abort timeout "
        f"(M_SDA_IN={dut.M_SDA_IN.value}) — I2C_WAIT_START is not releasing SDA "
        "(DS-T2.21 / Haz.61 regression)"
    )
    tb.env.log.info("✓ aborted read: slave released SDA after timeout — no wedge")


# ─────────────────────────────────────────────────────────────────────────
#  Standalone Execution
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from engine.simulation import run_simulation
    from engine.paths import SRC_PATH, SDK_SRC_PATH

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[5]
    DEEPSKOPION_RTL  = DEEPSKOPION_ROOT / "hw" / "rtl"
    from hw.sim.portable_fifo_sources import olo_fifo_sources

    run_simulation(
        top_level="tb_i2c_injector",
        module="test_i2c_injector",
        top_level_lang="vhdl",
        generics=GENERICS,
        waves=True,
        verbose=True,
        work_lib="tich",
        custom_libraries={
            "olo": olo_fifo_sources(DEEPSKOPION_ROOT),
            "tich": [
                # SDK shared units
                f"{SDK_SRC_PATH}/units/edge_detector.vhd",
                # Deepskopion packages
                f"{DEEPSKOPION_RTL}/pkg/datalogger_config_pkg.vhd",
                f"{DEEPSKOPION_RTL}/pkg/datalogger_pkg.vhd",
                # Responder modules (DUT chain)
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                f"{DEEPSKOPION_RTL}/datalogger/i2c/i2c_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/i2c_injector.vhd",
                # Sim wrapper
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_i2c_injector.vhd",
            ],
        },
    )
