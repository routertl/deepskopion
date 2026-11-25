# rr unknown-llm (AI-generated)
"""
test_emio_loop — EMIO self-validation loop reproduced in simulation against
the REAL fabric RTL (DS-T2.11 / DS-T7).

Reproduces the three Layer-1 self-validation failures observed on the booted
Zybo Z7-20 (run_self_test FAIL on REQ-5 UART / REQ-6 I2C / REQ-7 SPI) by
wiring tich.ds_emio_mux + tich.i2c_injector + tich.spi_injector EXACTLY as
deepskopion_top does (tb_emio_loop.vhd), then driving the PS master legs from
cocotb and observing the resolved bus read-back.

────────────────────────────────────────────────────────────────────────────
 DS-T7: making the I2C master FAITHFUL to real async open-drain hardware
────────────────────────────────────────────────────────────────────────────
The HW defect (DS-T7 REQ-6): an I2C master read of the i2c_injector (addr
0x50, FIFO preloaded DE AD BE EF, EMIO_MUX_SEL engaged) returns
NON-DETERMINISTIC garbage on the Zybo Z7-20 — mostly 0x00000000, sometimes
0x0000ffff. The same RTL PASSES in the old cocotb master (clean push-pull,
edges perfectly aligned to sim time → aligned to clk_sys) returning
0xDEADBEEF. That was a FALSE GREEN.

Leading hypothesis (the thing this test exposes): i2c_slave.vhd PR_GLITCH
(lines ~490-505) ORs sclRisingEdge + sdaRisingEdge into ONE debounce counter
(C_DEBOUNCE_TIME_CYCLES=3 → fires when two rising edges land within ~2
clk_sys cycles). On HW the PS-I2C SCL/SDA edges are asynchronous to the
100 MHz clk_sys at a random phase, so an SCL-rising and an SDA-rising
(e.g. the slave releasing SDA, or an open-drain pull-up RC transition near
an SCL edge) CAN be detected within the debounce window → i2cGlitchError_r
pulses → the FSM resets to I2C_WAIT_START mid-read (lines ~403-407) →
phase-dependent garbage read-back.

To reproduce that in sim WITHOUT touching the routertl SDK (the stock
I2cMasterDriver is push-pull and time-aligned), this file ships a LOCAL,
faithful I2C master, `AsyncOpenDrainI2cMaster`:

  1. TRUE open-drain: releases SDA/SCL by tri-stating (_T=1) and pulls low
     with _T=0,_O=0 — never push-pull. Reads back the resolved I2C0_BUS_SDA.
  2. ASYNCHRONOUS phase: every SCL/SDA transition happens at a random
     sub-clk_sys-cycle phase, with per-edge jitter, so SCL-rising and
     SDA-rising can land within 1-2 clk_sys cycles of each other (as on HW).
  3. The read is run MANY times (N_ITER) with fresh preload + a varied phase
     seed each, and EACH iteration must return 0xDEADBEEF (ROUTERTL-002,
     hard-coded — never derived from the DUT).

INSTRUMENTATION: each iteration samples the DUT-internal i2c_slave signals
(i2cGlitchError_r, fsmI2c_r, sdaShiftReg_r, cntSamples_r, errorIntFifo_r) via
hierarchical access (dut.u_i2c_injector.u_slave.<sig>) and logs whether a
glitch-error pulsed and whether the FSM jumped back to I2C_WAIT_START
mid-transfer — so a failure can be CORRELATED with the PR_GLITCH hypothesis.

ADVERSARIAL verification (§"Definition of done" + ROUTERTL-002):
every expected value is HARD-CODED (0xDEADBEEF). No assertion is softened to
force green; the goal is to MAKE THE TEST FAIL the way HW fails.

Run:  rr sim run test_emio_loop   (from the deepskopion dir)
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, Timer

# Routertl SDK cocotb path (mirrors every deepskopion datalogger test).
import routertl_core as _rrc
sys.path.insert(0, str(Path(_rrc.__file__).resolve().parents[1] / "sim" / "cocotb"))

# Deepskopion VIP path (hw/sim) — parents[5] from this test's location:
#   hw/sim/cocotb/tests/datalogger/infra/emio_loop/test_emio_loop.py
_ds_root = Path(__file__).resolve().parents[7]
_vip_parent = _ds_root / "hw" / "sim"
if str(_vip_parent) not in sys.path:
    sys.path.insert(0, str(_vip_parent))

from sdk.cocotb_helpers import requires
from rr_cocotb_tb.env import TbEnv
from rr_cocotb_tb.drivers.spi_master import SpiBus, SpiConfig, SpiMaster
from rr_cocotb_tb.drivers.uart_master import UartSource, UartSink


GENERICS = {
    "G_FIFO_DEPTH": 4,
}

# i2c_injector CONTROL layout (i2c_injector.vhd header).
SLAVE_ADDR      = 0x50         # CONTROL[7:1]
CTRL_ENABLE     = 0x1          # CONTROL[0]
CTRL_FIFO_RESET = 1 << 9       # CONTROL[9], self-clearing

# EMIO_MUX_SEL bit positions (ds_emio_mux.vhd header).
SEL_I2C  = 0b001   # bit[0]
SEL_UART = 0b010   # bit[1]
SEL_SPI  = 0b100   # bit[2]

# Hard-coded expected responses (ROUTERTL-002 — NOT runtime-derived).
I2C_PAYLOAD = [0xDE, 0xAD, 0xBE, 0xEF]   # mirrors the HW "want deadbeef" case
SPI_PAYLOAD = [0xDE, 0xAD, 0xBE, 0xEF]
UART_BYTE   = 0x5A

# DS-T7: how many faithful-master read iterations to run, and the base RNG
# seed. The HW symptom is non-deterministic, so a single pass is meaningless;
# we sweep the async phase across many runs.
N_ITER     = 20
BASE_SEED  = 0xDEADBEEF

CLK_PERIOD_NS = 10            # 100 MHz clk_sys


def make_i2c_control(enable: bool = True, addr: int = SLAVE_ADDR) -> int:
    """Pack the i2c_injector CONTROL word per its header table."""
    val = 0
    if enable:
        val |= CTRL_ENABLE
    val |= (addr & 0x7F) << 1
    return val


def make_spi_control(enable: bool = True) -> int:
    return CTRL_ENABLE if enable else 0


# ═════════════════════════════════════════════════════════════════════════
#  Faithful async open-drain I2C master (LOCAL — does NOT touch routertl SDK)
# ═════════════════════════════════════════════════════════════════════════
class AsyncOpenDrainI2cMaster:
    """I2C master that drives the PS I2C0 legs the way real async HW does.

    Differences from the stock push-pull SDK I2cMasterDriver that make the
    PR_GLITCH path reachable:

      * TRUE OPEN-DRAIN. To pull a line low: _T=0, _O=0 (active pull). To
        release it: _T=1 (tri-stated → ds_emio_mux pulls it up to '1').
        Never drives a hard '1' — exactly like a real I2C pad.
      * ASYNCHRONOUS PHASE + JITTER. Every transition is preceded by a
        Timer wait whose length is NOT a whole number of clk_sys cycles and
        carries a random per-edge jitter, so SCL-rising and SDA-rising land
        at an arbitrary, varying phase relative to clk_sys — and can fall
        within 1-2 clk_sys cycles of each other (the HW glitch window).

    The transition cadence is deliberately tight (sub-microsecond) so an
    SDA edge and an SCL edge can be detected within C_DEBOUNCE_TIME_CYCLES.
    Real fast-mode I2C edges, sampled by an async edge detector on a 100 MHz
    clock, exhibit exactly this proximity.
    """

    def __init__(self, dut, log, rng: random.Random,
                 sda_o, sda_t, scl_o, scl_t, sda_in, scl_in,
                 phase_ns: int = 90, setup_ns: int = 45, jitter_ns: float = 8.0,
                 async_phase: bool = True, fixed_phase_ns: float = 0.0):
        self.dut = dut
        self.log = log
        self.rng = rng
        self._sda_o = sda_o
        self._sda_t = sda_t
        self._scl_o = scl_o
        self._scl_t = scl_t
        self._sda_in = sda_in
        self._scl_in = scl_in

        # Timing knobs. The defaults are fast-mode-plus-ish: SCL high/low
        # ~90 ns (≈9 clk_sys) with ~45 ns SDA setup (≈4.5 clk). Wide enough
        # that the address + ACK phases complete (the master ACKs, like HW),
        # while the read path is exercised at a fresh sub-clk_sys phase each
        # iteration.
        self._phase_ns = phase_ns    # SCL high/low width
        self._setup_ns = setup_ns    # SDA setup before SCL rise
        self._jitter_ns = jitter_ns  # ± random per-edge jitter

        # async_phase=True  → every edge is offset by a random sub-clk_sys
        #                     fraction (+jitter): faithful asynchronous PS-I2C.
        # async_phase=False → edges land at a FIXED sub-cycle offset
        #                     (fixed_phase_ns), no jitter — used to prove the
        #                     outcome is PHASE-dependent by sweeping the offset.
        self._async_phase = async_phase
        self._fixed_phase_ns = fixed_phase_ns

    # ── low-level open-drain primitives ──────────────────────────────────
    def _release(self, sig_t):
        """Tri-state a line (open-drain release → bus pulls up to '1')."""
        sig_t.value = 1

    def _pull_low(self, sig_o, sig_t):
        """Actively pull a line low (_T=0, _O=0)."""
        sig_o.value = 0
        sig_t.value = 0

    async def _wait(self, base_ns: int):
        """Async, jittered, sub-cycle-phase wait (the faithfulness knob).

        Quantised to whole femtoseconds — the NVC sim precision is 1e-15 s,
        and cocotb refuses a Timer value it cannot represent exactly at that
        step. Femtosecond granularity still gives a continuous, fractional
        (NON clk_sys-aligned) phase: 1 clk_sys cycle = 10_000_000 fs, so a
        random fs offset lands anywhere within a cycle.
        """
        if self._async_phase:
            j = self.rng.uniform(-self._jitter_ns, self._jitter_ns)
            # Add a sub-clk fractional offset so edges never align to clk_sys.
            frac = self.rng.uniform(0.0, float(CLK_PERIOD_NS))
            dt_ns = base_ns + j + frac
        else:
            # Deterministic fixed sub-cycle offset (no jitter) — phase sweep.
            dt_ns = base_ns + self._fixed_phase_ns
        dt_ns = max(1.0, dt_ns)
        dt_fs = int(round(dt_ns * 1_000_000))   # ns → fs, integer
        await Timer(dt_fs, unit="fs")

    def _read_sda(self) -> int:
        return int(self._sda_in.value) & 1

    async def initialize(self):
        """Idle: both lines released (open-drain high)."""
        self._release(self._sda_t)
        self._release(self._scl_t)
        # _O parked low so a later _T=0 is a clean active pull.
        self._sda_o.value = 0
        self._scl_o.value = 0
        await self._wait(self._phase_ns)

    async def start(self):
        """START = SDA falling while SCL released-high."""
        self._release(self._sda_t)
        self._release(self._scl_t)
        await self._wait(self._phase_ns)
        # SDA pulled low while SCL high.
        self._pull_low(self._sda_o, self._sda_t)
        await self._wait(self._setup_ns)
        # SCL pulled low to begin the first bit.
        self._pull_low(self._scl_o, self._scl_t)
        await self._wait(self._phase_ns)

    async def stop(self):
        """STOP = SDA rising while SCL released-high."""
        self._pull_low(self._sda_o, self._sda_t)
        self._pull_low(self._scl_o, self._scl_t)
        await self._wait(self._setup_ns)
        # Release SCL (rises first)…
        self._release(self._scl_t)
        await self._wait(self._setup_ns)
        # …then release SDA → SDA rises while SCL high = STOP.
        self._release(self._sda_t)
        await self._wait(self._phase_ns)

    async def write_bit(self, bit: int):
        """Master drives one bit (SCL low → set SDA → SCL high → SCL low)."""
        # SCL already low. Set SDA at a jittered phase.
        if bit:
            self._release(self._sda_t)          # '1' = release (pull-up)
        else:
            self._pull_low(self._sda_o, self._sda_t)
        await self._wait(self._setup_ns)
        self._release(self._scl_t)              # SCL rises → slave samples
        await self._wait(self._phase_ns)
        self._pull_low(self._scl_o, self._scl_t)  # SCL falls
        await self._wait(self._setup_ns)

    async def read_bit(self) -> int:
        """Master clocks one bit out of the slave (releases SDA, samples)."""
        self._release(self._sda_t)              # let the slave drive SDA
        await self._wait(self._setup_ns)
        self._release(self._scl_t)              # SCL rises
        await self._wait(self._phase_ns)
        bit = self._read_sda()                  # sample near end of SCL-high
        self._pull_low(self._scl_o, self._scl_t)  # SCL falls
        await self._wait(self._setup_ns)
        return bit

    async def write_byte(self, data: int) -> bool:
        for i in range(7, -1, -1):
            await self.write_bit((data >> i) & 1)
        ack = await self.read_bit()
        return ack == 0

    async def read_byte(self, send_ack: bool = True) -> int:
        data = 0
        for i in range(7, -1, -1):
            data |= (await self.read_bit()) << i
        await self.write_bit(0 if send_ack else 1)
        return data


# ═════════════════════════════════════════════════════════════════════════
#  i2c_slave internal-signal instrumentation (hierarchical access)
# ═════════════════════════════════════════════════════════════════════════
class SlaveProbe:
    """Best-effort hierarchical handle onto the i2c_slave internals.

    Path from the tb top:  u_i2c_injector → u_slave → <signal>.
    If the simulator does not expose a given signal, that field stays None
    and is reported as 'n/a' (we never silently pretend it didn't fire).
    """

    SIGNALS = ("i2cGlitchError_r", "fsmI2c_r", "sdaShiftReg_r",
               "cntSamples_r", "errorIntFifo_r")

    def __init__(self, dut, log):
        self.log = log
        self.h = {}
        self._available = False
        try:
            slave = dut.u_i2c_injector.u_slave
            self._available = True
        except Exception as e:  # noqa: BLE001 — diagnostic, re-logged
            self.log.warning(f"SlaveProbe: hierarchy access FAILED: {e!r}")
            slave = None
        if slave is not None:
            for s in self.SIGNALS:
                try:
                    self.h[s] = getattr(slave, s)
                except Exception:  # noqa: BLE001
                    self.h[s] = None
                    self.log.warning(f"SlaveProbe: signal '{s}' not exposed")

    @property
    def available(self) -> bool:
        return self._available

    def _read(self, name):
        sig = self.h.get(name)
        if sig is None:
            return None
        try:
            return sig.value
        except Exception:  # noqa: BLE001
            return None

    def glitch(self) -> int | None:
        v = self._read("i2cGlitchError_r")
        if v is None:
            return None
        try:
            return int(v)
        except Exception:  # noqa: BLE001
            return None

    def fsm(self) -> str | None:
        v = self._read("fsmI2c_r")
        if v is None:
            return None
        # NVC exposes VHDL enums as their string label.
        return str(v)

    def errfifo(self) -> int | None:
        v = self._read("errorIntFifo_r")
        if v is None:
            return None
        try:
            return int(v)
        except Exception:  # noqa: BLE001
            return None


# t_fsmI2c declaration order (i2c_slave.vhd) → ordinal map. NVC renders the
# VHDL enum as its bare ordinal index (observed: '1','2'), so the probe must
# decode both the ordinal and the (label) forms.
_FSM_ORDINAL = {
    0: "I2C_DISABLED", 1: "I2C_WAIT_START", 2: "I2C_CAPTURE_ADDRESS",
    3: "I2C_WAIT_ACK", 4: "I2C_CAPTURE_DATA", 5: "I2C_SEND_DATA",
    6: "I2C_ASSERT_ACK", 7: "I2C_ADDRESS_ACK", 8: "I2C_WAIT_INTERNAL_DATA",
}


def _fsm_label(fsm: str | None) -> str | None:
    """Normalise a rendered fsmI2c_r value to its upper-case label."""
    if fsm is None:
        return None
    f = fsm.strip()
    if f.isdigit():
        return _FSM_ORDINAL.get(int(f))
    return f.upper()


def _is_wait_start(fsm: str | None) -> bool:
    return _fsm_label(fsm) == "I2C_WAIT_START"


def _is_in_transfer(fsm: str | None) -> bool:
    """True once the FSM has left WAIT_START into ANY active-transfer state
    (address capture onward) — used to detect a glitch reset *mid-transfer*."""
    lbl = _fsm_label(fsm)
    return lbl is not None and lbl not in (
        "I2C_DISABLED", "I2C_WAIT_START",
    )


async def watch_glitch_and_fsm(env, probe: SlaveProbe, state: dict):
    """Background coroutine: per clk_sys cycle, latch any glitch pulse and any
    reset of the FSM back to I2C_WAIT_START *after* it had progressed into a
    deep (mid-read) state — the signature of a PR_GLITCH-driven FSM reset."""
    if not probe.available:
        return
    prev_in_transfer = False
    while True:
        await ClockCycles(env.clk, 1)
        g = probe.glitch()
        if g == 1:
            state["glitch_pulses"] += 1
        fsm = probe.fsm()
        if fsm is not None:
            lbl = _fsm_label(fsm)
            state["fsm_states_seen"].add(lbl if lbl is not None else fsm.strip())
            if _is_in_transfer(fsm):
                prev_in_transfer = True
            elif _is_wait_start(fsm):
                # Fell back to WAIT_START after being in a transfer state =
                # FSM reset mid-transfer (the PR_GLITCH signature).
                if state["started"] and prev_in_transfer:
                    state["wait_start_after_start"] += 1
                prev_in_transfer = False
        ef = probe.errfifo()
        if ef == 1:
            state["errfifo_pulses"] += 1


# ═════════════════════════════════════════════════════════════════════════
#  Shared TB scaffold
# ═════════════════════════════════════════════════════════════════════════
class EmioLoopTb:
    """Drives the carved-out EMIO loop (tb_emio_loop) from the PS-master side."""

    def __init__(self, dut):
        self.dut = dut
        self.env = TbEnv(dut, clk="CLK", rst="RST", period_ns=CLK_PERIOD_NS)  # 100 MHz

        # SPI master — drives the PS SPI0 legs, samples resolved SPI0_BUS_MISO.
        # DS-T2.29 / Haz.62: chip-select is the MANUAL EMIO GPIO, NOT the native PS
        # SS (which never reaches the PL seam on Zynq-7). Frame the transfer on the
        # GPIO leg, exactly as the SW workaround does on silicon; SPI0_SS_O stays idle.
        self.spi_bus = SpiBus(
            sclk=dut.SPI0_SCLK_O,
            mosi=dut.SPI0_MOSI_O,
            miso=dut.SPI0_BUS_MISO,
            cs=dut.SPI0_CS_GPIO,
        )
        self.spi_cfg = SpiConfig(
            word_width=8,
            sclk_freq=1_000_000,     # 1 MHz (100× margin over clk_sys)
            cpol=False, cpha=False,  # mode 0 (matches spi_slave)
            msb_first=True,
            cs_active_low=True,
        )
        # DS-P3.221: the strict SPI protocol monitor (on by default since
        # routertl 32332abc1) samples on the TB clock.
        self.spi = SpiMaster(self.spi_bus, self.spi_cfg, sample_clk=dut.CLK)

    async def setup(self):
        await self.env.start_clock()
        # Idle every leg before reset (avoid X-prop into the slaves).
        self.dut.EMIO_MUX_SEL.value   = 0
        self.dut.TIMESTAMP.value      = 0
        # I2C master legs: open-drain RELEASED at idle (_T=1), _O parked low.
        self.dut.I2C0_SCL_T.value     = 1
        self.dut.I2C0_SDA_T.value     = 1
        self.dut.I2C0_SCL_O.value     = 0
        self.dut.I2C0_SDA_O.value     = 0
        # SPI master legs idle (CS released, SCLK low for mode 0).
        self.dut.SPI0_SCLK_O.value    = 0
        self.dut.SPI0_MOSI_O.value    = 0
        self.dut.SPI0_SS_O.value      = 1   # native PS SS — dead at the seam (Haz.62), parked idle
        self.dut.SPI0_CS_GPIO.value   = 1   # DS-T2.29: manual GPIO CS released (active-low)
        # UART legs idle high.
        self.dut.UART0_TX.value       = 1
        self.dut.PMOD_UART_TX.value   = 1
        # Injector control planes off.
        self.dut.I2C_CONTROL.value    = 0
        self.dut.I2C_DATAIN.value     = 0
        self.dut.I2C_DATAIN_VLD.value = 0
        self.dut.SPI_CONTROL.value    = 0
        self.dut.SPI_DATAIN.value     = 0
        self.dut.SPI_DATAIN_VLD.value = 0
        await self.env.reset(cycles=5)

    def make_i2c_master(self, rng: random.Random, **kw) -> AsyncOpenDrainI2cMaster:
        return AsyncOpenDrainI2cMaster(
            self.dut, self.env.log, rng,
            sda_o=self.dut.I2C0_SDA_O, sda_t=self.dut.I2C0_SDA_T,
            scl_o=self.dut.I2C0_SCL_O, scl_t=self.dut.I2C0_SCL_T,
            sda_in=self.dut.I2C0_BUS_SDA, scl_in=self.dut.I2C0_BUS_SCL,
            **kw,
        )

    # ── injector control-plane helpers (mirror the regbank delivery) ──────
    async def push_i2c_byte(self, byte: int):
        self.dut.I2C_DATAIN.value = byte & 0xFF
        self.dut.I2C_DATAIN_VLD.value = 1
        await ClockCycles(self.env.clk, 1)
        self.dut.I2C_DATAIN_VLD.value = 0
        await ClockCycles(self.env.clk, 1)

    async def push_spi_byte(self, byte: int):
        self.dut.SPI_DATAIN.value = byte & 0xFF
        self.dut.SPI_DATAIN_VLD.value = 1
        await ClockCycles(self.env.clk, 1)
        self.dut.SPI_DATAIN_VLD.value = 0
        await ClockCycles(self.env.clk, 1)

    async def enable_i2c(self, addr: int = SLAVE_ADDR):
        self.dut.I2C_CONTROL.value = make_i2c_control(enable=True, addr=addr)
        await ClockCycles(self.env.clk, 4)

    async def disable_i2c(self):
        self.dut.I2C_CONTROL.value = 0
        await ClockCycles(self.env.clk, 4)

    async def fresh_fifo_via_global_reset(self):
        """Return the i2c_injector to a known empty-FIFO state between
        iterations WITHOUT using the CONTROL[9] fifo-reset.

        Why not CONTROL[9]? The injector's TX FIFO is an XPM sync FIFO whose
        wr_rst_busy is tied to ``open`` (i2c_injector.vhd RST_BUSY => open),
        so the host has no way to know when a CONTROL[9] pulse has finished;
        a preload issued too soon after the pulse is silently dropped while
        wr_rst_busy is still high → FIFO underrun → garbage read-back. (That
        is a real injector gap, filed separately as DS-T7-CTRL9; it is NOT
        the EMIO-loop scenario, where each self-test boots a fresh FIFO.) We
        therefore reset via the global RST leg, held long enough for the XPM
        write-domain reset to complete, before re-idling the I2C legs."""
        self.dut.I2C_CONTROL.value = 0
        self.dut.I2C0_SCL_T.value = 1
        self.dut.I2C0_SDA_T.value = 1
        self.dut.I2C0_SCL_O.value = 0
        self.dut.I2C0_SDA_O.value = 0
        await self.env.reset(cycles=10)

    async def enable_spi(self):
        self.dut.SPI_CONTROL.value = make_spi_control(enable=True)
        await ClockCycles(self.env.clk, 4)

    def i2c_status(self) -> int:
        return self.dut.I2C_STATUS.value.to_unsigned()

    def spi_status(self) -> int:
        return self.dut.SPI_STATUS.value.to_unsigned()


# ─────────────────────────────────────────────────────────────────────────
#  REQ-6 — I2C, FAITHFUL async open-drain master, N-iteration phase sweep
# ─────────────────────────────────────────────────────────────────────────

@cocotb.test()
@requires("EMIO-LOOP-REQ-6")
async def test_i2c_loop_reads_deadbeef(dut):
    """
    DS-T7 reproduction. Preload i2c_injector with DE AD BE EF, engage
    EMIO_MUX_SEL bit0, then drive a READ through the REAL ds_emio_mux
    open-drain resolution with a FAITHFUL async master — N_ITER times, each
    with a fresh FIFO preload and a varied async phase. EACH iteration must
    return EXACTLY 0xDEADBEEF (ROUTERTL-002 hard-coded).

    Internal i2c_slave signals are sampled throughout so any failure is
    correlated with i2cGlitchError_r pulsing + fsmI2c_r resetting to
    I2C_WAIT_START mid-read (the PR_GLITCH hypothesis).
    """
    tb = EmioLoopTb(dut)
    await tb.setup()

    dut.EMIO_MUX_SEL.value = SEL_I2C

    probe = SlaveProbe(dut, tb.env.log)
    if probe.available:
        tb.env.log.info("SlaveProbe: hierarchy access OK (u_i2c_injector.u_slave)")
    else:
        tb.env.log.warning(
            "SlaveProbe: i2c_slave internals NOT reachable — glitch/FSM "
            "correlation will report 'n/a' (read-back assert still runs)."
        )

    failures = []
    results = []

    for it in range(N_ITER):
        rng = random.Random(BASE_SEED + it * 0x9E3779B1)

        # Fresh empty FIFO each iteration via the global RST leg (NOT the
        # CONTROL[9] path — see fresh_fifo_via_global_reset), then enable +
        # preload the four response bytes.
        await tb.fresh_fifo_via_global_reset()
        dut.EMIO_MUX_SEL.value = SEL_I2C
        await tb.enable_i2c()
        for b in I2C_PAYLOAD:
            await tb.push_i2c_byte(b)
        await ClockCycles(tb.env.clk, 4)

        assert (tb.i2c_status() & 0x1) == 0, (
            f"[iter {it}] i2c FIFO empty after preload: "
            f"STATUS=0x{tb.i2c_status():08X}"
        )

        # Per-iteration instrumentation state + background watcher.
        state = {"started": False, "glitch_pulses": 0,
                 "wait_start_after_start": 0, "errfifo_pulses": 0,
                 "fsm_states_seen": set()}
        watcher = cocotb.start_soon(watch_glitch_and_fsm(tb.env, probe, state))

        # FAITHFUL: fully asynchronous phase + per-edge jitter, fresh seed
        # each iteration. The transition cadence is tight enough that an
        # SDA-rising (slave releasing the bus after a '0' bit / between bytes)
        # and an SCL-rising can land within C_DEBOUNCE_TIME_CYCLES at SOME
        # phases — the PR_GLITCH window — while the address + ACK normally
        # complete. Whether a glitch trips on a given run is phase-dependent,
        # mirroring the Zybo's non-determinism.
        master = tb.make_i2c_master(
            rng, async_phase=True,
            phase_ns=80, setup_ns=22, jitter_ns=10.0,
        )
        await master.initialize()
        await master.start()
        state["started"] = True

        ack = await master.write_byte((SLAVE_ADDR << 1) | 1)   # R/W=1 (read)

        received = []
        for idx in range(len(I2C_PAYLOAD)):
            last = (idx == len(I2C_PAYLOAD) - 1)
            received.append(await master.read_byte(send_ack=not last))
        await master.stop()
        await ClockCycles(tb.env.clk, 4)
        watcher.kill()

        word = ((received[0] << 24) | (received[1] << 16)
                | (received[2] << 8) | received[3])
        ok = (ack and word == 0xDEADBEEF)

        tb.env.log.info(
            f"[iter {it:2d}] seed=0x{(BASE_SEED + it * 0x9E3779B1) & 0xFFFFFFFF:08X} "
            f"ack={ack} bytes={[hex(b) for b in received]} "
            f"word=0x{word:08X} (want 0xDEADBEEF) | "
            f"glitch_pulses={state['glitch_pulses']} "
            f"fsm_reset_mid_xfer={state['wait_start_after_start']} "
            f"errfifo_pulses={state['errfifo_pulses']} "
            f"{'PASS' if ok else 'FAIL'}"
        )
        if it == 0:
            tb.env.log.info(
                f"[iter  0] fsmI2c_r states observed by probe: "
                f"{sorted(state['fsm_states_seen'])}"
            )
        snap = {k: (sorted(v) if isinstance(v, set) else v)
                for k, v in state.items()}
        results.append((it, ok, word, snap))
        if not ok:
            failures.append((it, word, snap))

    # ── Correlation summary (DS-T7 deliverable) ───────────────────────────
    n_fail = len(failures)
    n_fail_with_glitch = sum(1 for _, _, s in failures if s["glitch_pulses"] > 0)
    n_fail_with_reset = sum(
        1 for _, _, s in failures if s["wait_start_after_start"] > 0
    )
    n_pass_with_glitch = sum(
        1 for it, ok, w, s in results if ok and s["glitch_pulses"] > 0
    )
    tb.env.log.info(
        f"DS-T7 SUMMARY: {n_fail}/{N_ITER} iterations FAILED. "
        f"of the failures, {n_fail_with_glitch} had a glitch-error pulse and "
        f"{n_fail_with_reset} had an FSM reset to I2C_WAIT_START mid-transfer. "
        f"({n_pass_with_glitch} PASSING iterations also saw a glitch pulse.)"
    )
    if not probe.available:
        tb.env.log.warning(
            "DS-T7 SUMMARY: glitch/FSM counters are 'n/a' (probe unavailable) "
            "— correlation is INCONCLUSIVE on the internal-signal side."
        )

    # Hard-coded expected (ROUTERTL-002). The HW reads garbage here; a
    # faithful async-phase sim should too if the PR_GLITCH hypothesis holds.
    first5 = [
        (it, f"0x{w:08X}",
         f"glitch={s['glitch_pulses']}",
         f"fsm_reset={s['wait_start_after_start']}")
        for it, w, s in failures[:5]
    ]
    assert n_fail == 0, (
        f"REQ-6 FAIL: {n_fail}/{N_ITER} faithful async-phase I2C reads did "
        f"NOT return 0xDEADBEEF. First failures: {first5}"
    )
    tb.env.log.info(
        f"✓ REQ-6 PASS — all {N_ITER} faithful async-phase I2C reads returned "
        "0xDEADBEEF through the real fabric"
    )


# ─────────────────────────────────────────────────────────────────────────
#  DS-T7 CONTROL — stock push-pull SDK master through the SAME harness.
#  Isolates "is the 0xBEEFFFFF / errorIntFifo failure caused by my faithful
#  BFM's protocol, or by the preload / FWFT path itself?" If THIS passes
#  (0xDEADBEEF) the difference is the BFM; if it ALSO fails the defect is in
#  the injector's FWFT read path regardless of the master.
# ─────────────────────────────────────────────────────────────────────────

@cocotb.test()
@requires("EMIO-LOOP-REQ-6")
async def test_i2c_loop_control_stock_pushpull(dut):
    """CONTROL experiment (not the DS-T7 faithful path). Drives the read with
    the stock push-pull I2cMasterDriver at the old slow 100 kHz timing through
    the identical fifo-reset + preload + EMIO_MUX_SEL harness used by the
    faithful test, and reports the read-back + errorIntFifo count. Diagnostic
    only: asserts hard-coded 0xDEADBEEF (ROUTERTL-002)."""
    from rr_cocotb_tb.drivers.i2c_master import I2cMasterDriver, I2cTiming

    tb = EmioLoopTb(dut)
    await tb.setup()
    dut.EMIO_MUX_SEL.value = SEL_I2C

    probe = SlaveProbe(dut, tb.env.log)

    # Stock push-pull master: _T held low, value via _O (NOT open-drain).
    dut.I2C0_SCL_T.value = 0
    dut.I2C0_SDA_T.value = 0
    i2c = I2cMasterDriver(
        tb.env, sda=dut.I2C0_SDA_O, sda_in=dut.I2C0_BUS_SDA,
        scl=dut.I2C0_SCL_O, timing=I2cTiming(period_ns=10_000),  # 100 kHz
    )

    await tb.fresh_fifo_via_global_reset()
    dut.EMIO_MUX_SEL.value = SEL_I2C
    # stock master legs: push-pull again after the reset re-idled them.
    dut.I2C0_SCL_T.value = 0
    dut.I2C0_SDA_T.value = 0
    await tb.enable_i2c()
    for b in I2C_PAYLOAD:
        await tb.push_i2c_byte(b)
    await ClockCycles(tb.env.clk, 4)
    assert (tb.i2c_status() & 0x1) == 0, "control: FIFO empty after preload"

    state = {"started": False, "glitch_pulses": 0,
             "wait_start_after_start": 0, "errfifo_pulses": 0,
             "fsm_states_seen": set()}
    watcher = cocotb.start_soon(watch_glitch_and_fsm(tb.env, probe, state))

    await i2c.initialize()
    await i2c.start()
    state["started"] = True
    ack = await i2c.write_byte((SLAVE_ADDR << 1) | 1)
    received = []
    for idx in range(len(I2C_PAYLOAD)):
        last = (idx == len(I2C_PAYLOAD) - 1)
        received.append(await i2c.read_byte(send_ack=not last))
    await i2c.stop()
    await ClockCycles(tb.env.clk, 4)
    watcher.kill()

    word = ((received[0] << 24) | (received[1] << 16)
            | (received[2] << 8) | received[3])
    tb.env.log.info(
        f"CONTROL (stock push-pull) ack={ack} bytes={[hex(b) for b in received]} "
        f"word=0x{word:08X} (want 0xDEADBEEF) | glitch={state['glitch_pulses']} "
        f"errfifo={state['errfifo_pulses']} fsm_reset={state['wait_start_after_start']}"
    )
    assert word == 0xDEADBEEF, (
        f"CONTROL FAIL: stock push-pull master read 0x{word:08X} "
        f"(errfifo={state['errfifo_pulses']}), expected 0xDEADBEEF"
    )
    tb.env.log.info("✓ CONTROL PASS — stock push-pull returned 0xDEADBEEF")


# ─────────────────────────────────────────────────────────────────────────
#  REQ-7 — SPI (HW symptom: PS controller ETIMEDOUT)
# ─────────────────────────────────────────────────────────────────────────

@cocotb.test()
@requires("EMIO-LOOP-REQ-7")
async def test_spi_loop_reads_preloaded_bytes(dut):
    """
    Preload spi_injector with a known byte sequence, set EMIO_MUX_SEL bit2,
    drive a mode-0 SPI transfer through the REAL ds_emio_mux push-pull MISO
    resolution, and assert the master reads back the preloaded bytes on MISO
    in FIFO order.
    """
    tb = EmioLoopTb(dut)
    await tb.setup()

    dut.EMIO_MUX_SEL.value = SEL_SPI

    await tb.enable_spi()
    for b in SPI_PAYLOAD:
        await tb.push_spi_byte(b)
    await ClockCycles(tb.env.clk, 4)

    assert (tb.spi_status() & 0x1) == 0, \
        f"spi FIFO empty after preload: STATUS=0x{tb.spi_status():08X}"

    # Full-duplex: master writes dummy 0x00s, only MISO matters.
    rx = await tb.spi.write([0x00] * len(SPI_PAYLOAD))
    tb.env.log.info(
        f"REQ-7 SPI MISO read-back={[hex(b) for b in rx]} (want {[hex(b) for b in SPI_PAYLOAD]})"
    )

    # Hard-coded expected (ROUTERTL-002).
    assert rx == SPI_PAYLOAD, (
        f"REQ-7 FAIL: MISO read {[hex(b) for b in rx]}, "
        f"expected {[hex(b) for b in SPI_PAYLOAD]}"
    )
    tb.env.log.info("✓ REQ-7 PASS — SPI loop returned the preloaded bytes through the real fabric")


# ─────────────────────────────────────────────────────────────────────────
#  REQ-5 — UART (HW symptom: 0 frames captured / word_delta=0)
# ─────────────────────────────────────────────────────────────────────────

@cocotb.test()
@requires("EMIO-LOOP-REQ-5")
async def test_uart_mux_passes_tx_through(dut):
    """
    With EMIO_MUX_SEL bit1='1' (UART source = EMIO bus), a byte driven on the
    PS UART0_TX leg must be reproduced bit-for-bit on the muxed datalogger source
    SRC_UART_TX (a UartSink decodes it back to the same byte). With bit1='0',
    SRC_UART_TX must instead follow PMOD_UART_TX (proven by a static level
    check while UART0_TX is held low and PMOD is high). Light first cut per
    the ticket — confirms the mux half of the UART capture path.
    """
    tb = EmioLoopTb(dut)
    await tb.setup()

    # ── Branch A: bit1='1' → SRC_UART_TX follows UART0_TX. ────────────────
    dut.EMIO_MUX_SEL.value = SEL_UART
    dut.UART0_TX.value = 1        # idle high before the source starts
    dut.PMOD_UART_TX.value = 0    # PMOD held LOW — must be ignored when bit1=1
    await ClockCycles(tb.env.clk, 4)

    # Static check: with UART0_TX idle high and PMOD low, the mux must
    # present the EMIO source (high), proving PMOD is NOT selected.
    assert int(dut.SRC_UART_TX.value) == 1, (
        f"REQ-5 FAIL (bit1=1 idle): SRC_UART_TX={int(dut.SRC_UART_TX.value)}, "
        "expected 1 (following UART0_TX, not PMOD low)"
    )

    # Drive a byte on UART0_TX and decode SRC_UART_TX with a UartSink.
    sink = UartSink(dut.SRC_UART_TX, baud=115200)
    cocotb.start_soon(sink.start())
    src = UartSource(dut.UART0_TX, baud=115200)
    await src.write(UART_BYTE)
    # Allow the sink to finish framing the byte.
    await ClockCycles(tb.env.clk, 200)

    got = sink.read_nowait()
    tb.env.log.info(
        f"REQ-5 UART (bit1=1): SRC_UART_TX decoded byte={None if got is None else hex(got)} "
        f"(want {hex(UART_BYTE)})"
    )
    assert got == UART_BYTE, (
        f"REQ-5 FAIL (bit1=1): SRC_UART_TX reproduced "
        f"{None if got is None else hex(got)}, expected {hex(UART_BYTE)}"
    )

    # ── Branch B: bit1='0' → SRC_UART_TX follows PMOD_UART_TX. ────────────
    dut.EMIO_MUX_SEL.value = 0    # all selects → PMOD
    dut.UART0_TX.value = 1        # EMIO leg held HIGH — must be ignored now
    dut.PMOD_UART_TX.value = 0    # PMOD LOW — must show through
    await ClockCycles(tb.env.clk, 4)
    assert int(dut.SRC_UART_TX.value) == 0, (
        f"REQ-5 FAIL (bit1=0): SRC_UART_TX={int(dut.SRC_UART_TX.value)}, "
        "expected 0 (following PMOD_UART_TX low, not EMIO high)"
    )
    dut.PMOD_UART_TX.value = 1
    await ClockCycles(tb.env.clk, 4)
    assert int(dut.SRC_UART_TX.value) == 1, (
        f"REQ-5 FAIL (bit1=0): SRC_UART_TX={int(dut.SRC_UART_TX.value)}, "
        "expected 1 (following PMOD_UART_TX high)"
    )

    tb.env.log.info("✓ REQ-5 PASS — UART mux selects EMIO/PMOD per bit1 and passes TX through")


# ─────────────────────────────────────────────────────────────────────────
#  Standalone Execution (ROUTERTL-001 — engine.simulation.run_simulation only)
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from engine.simulation import run_simulation
    from engine.paths import SRC_PATH, SDK_SRC_PATH

    DEEPSKOPION_ROOT = Path(__file__).resolve().parents[7]
    DEEPSKOPION_RTL  = DEEPSKOPION_ROOT / "hw" / "rtl"
    from hw.sim.portable_fifo_sources import olo_fifo_sources

    run_simulation(
        top_level="tb_emio_loop",
        module="test_emio_loop",
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
                # FIFO primitive (injector dependency)
                f"{DEEPSKOPION_RTL}/fifo_sync.vhd",
                # I2C responder chain
                f"{DEEPSKOPION_RTL}/datalogger/i2c/i2c_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/i2c_injector.vhd",
                # SPI responder chain
                f"{DEEPSKOPION_RTL}/responder/spi_slave.vhd",
                f"{DEEPSKOPION_RTL}/responder/spi_injector.vhd",
                # Real fabric mux (DUT under reproduction)
                f"{DEEPSKOPION_RTL}/emio/ds_emio_mux.vhd",
                # Sim wrapper
                f"{DEEPSKOPION_ROOT}/hw/sim/tb_emio_loop.vhd",
            ],
        },
    )
