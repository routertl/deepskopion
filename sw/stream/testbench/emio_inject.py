# rr unknown-llm (AI-generated)
"""PS-side library for driving the deepskopion EMIO injector loop.

Composes four units:

    RegbankExt    — DS-P2.51 helpers (EMIO_MUX_SEL, I2C/SPI INJ_*)
                    sitting on top of sw.stream.regbank.MmioRegbank.
    UART          — pyserial wrapper opening the EMIO UART0 tty (the
                    test wire, found by its base 0xE0000000) at the right
                    baud + 8N1.
    I2CMaster     — smbus2.SMBus(0) wrapper with a small helper for
                    address-targeted reads + writes.
    SPIMaster     — spidev.SpiDev() at mode 0 by default; xfer2() is
                    the full-duplex transfer used by every test.
    SelfTestBench — facade opening all four ready to use.

Why libraries instead of raw ioctls: each of these handles the
edge cases (CS auto-toggle, repeated-START, baud clamping) that
the stdlib path would have to re-implement. Per feedback memory
`feedback_solid_beats_devtime`, the buildroot rebuild cost to ship
them is rounding error against the maintenance saving.

The stdlib equivalents are walked through in
`docs/software/linux_i2c_spi_userspace.md` for reference + the
educational angle — read that doc next to this file and every
class maps onto a section.
"""

from __future__ import annotations

import errno
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Dict, Sequence

# Protocol-driver imports are deferred to the class constructors so the
# module is importable on dev boxes that don't ship pyserial/smbus2/spidev
# (the buildroot rootfs has all three from DS-P3.44). Anything that just
# reads the regbank can import + use this module without the deps.

from sw.stream.regbank import MmioRegbank, Regs

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
#  Constants — mirror hw/rtl/responder/{i2c,spi}_injector.vhd CONTROL bits
# ─────────────────────────────────────────────────────────────────────────

# EMIO_MUX_SEL bit positions (project_regbank.yml @ 0x0024).
EMIO_MUX_BIT_I2C  = 0
EMIO_MUX_BIT_UART = 1
EMIO_MUX_BIT_SPI  = 2

# Per-device datalogger IDs — match hw/rtl/pkg/datalogger_config_pkg.vhd.
# DATALOGGER_CONFIG is a 16-bit broadcast word: bits[15:11] = device_id (5b),
# bits[10:0] = per-device config; bit[0] of the per-device slice = enable.
# Each config_control instance latches its slice when its device_id matches,
# so multiple devices are enabled by sequential DATALOGGER_CONFIG writes.
DATALOGGER_DEV_ID_BITS = 5
DATALOGGER_ID_I2C_4    = 4    # C_I2C0_ID
DATALOGGER_ID_SPI_8    = 8    # C_SPI0_ID
DATALOGGER_ID_UART_12  = 12   # C_UART0_ID
DATALOGGER_ENABLE_BIT  = 0x1  # bit[0] of the per-device config slice


def encode_datalogger_config(device_id: int, enable: bool = True,
                          extra: int = 0) -> int:
    """Pack a DATALOGGER_CONFIG broadcast word for one device.

    Layout (16 bits): [device_id (5b) | config (11b)].
    config[0] = enable; config[10:1] = device-specific extras (default 0).
    """
    if not 0 <= device_id < (1 << DATALOGGER_DEV_ID_BITS):
        raise ValueError(f"device_id {device_id} out of 5-bit range")
    cfg = (extra & 0x3FF) << 1
    if enable:
        cfg |= DATALOGGER_ENABLE_BIT
    return ((device_id & 0x1F) << 11) | (cfg & 0x07FF)

# Per-injector CONTROL bits.
_CTRL_ENABLE     = 0x1
_CTRL_FIFO_RESET = 1 << 9

# I2C injector status field layout.
_I2C_STATUS_FIFO_EMPTY     = 0x1
_I2C_STATUS_FIFO_PROG_FULL = 0x2
_I2C_STATUS_SLAVE_ERROR    = 0x4
_I2C_STATUS_COUNT_SHIFT    = 3
_I2C_STATUS_COUNT_MASK     = 0x7F  # fifo depth + 1 = 7 bits at G_FIFO_DEPTH=6

# Defaults — match the EMIO devices we exposed in Phase 1.
#
# The EMIO test wire is PS UART0 at 0xE0000000; the board console is PS UART1
# at 0xE0001000. Which /dev/ttyPS<N> each one becomes follows the device
# tree's serialN aliases, and that order belongs to the device-tree generator:
# the XSCT generator made UART0 ttyPS0, SDTGen + Lopper make UART1 ttyPS0
# (DS-P1.105). A fixed /dev/ttyPS0 therefore opened the console on an SDT
# image. The tty is resolved at open time from the base address instead:
# emio_uart_dev() below.
EMIO_UART0_BASE    = 0xE0000000
DEFAULT_UART_DEV: str | None = None   # None: resolve the EMIO UART0 tty at open
DEFAULT_UART_BAUD  = 9600
DEFAULT_I2C_BUS    = 0
# SPI bus number — DS-P3.45 / DS-P3.49 part 2:
# /dev/spidev<bus>.<cs> bus numbering follows kernel probe order, NOT the
# `spiN` DT alias index. On our Zynq-7 with both QSPI + EMIO SPI0 enabled:
#   * spi@e000d000 (QSPI flash controller, DT alias spi0) → kernel bus 0
#   * spi@e0006000 (PS SPI0 routed to EMIO,   DT alias spi1) → kernel bus 1
# The harness wants the EMIO SPI0 — bus 1, chip-select 0 → /dev/spidev1.0.
# Verified via /sys/class/spi_master/ on QEMU + on hardware.
DEFAULT_SPI_BUS    = 1
DEFAULT_SPI_CS     = 0
DEFAULT_SPI_SPEED  = 1_000_000   # 1 MHz; spi_slave async sync needs SCLK >> clk_sys/3


def encode_i2c_control(enable: bool = True,
                       slave_addr: int = 0x50,
                       fifo_reset: bool = False) -> int:
    """Pack the I2C_INJ_CONTROL word per hw/rtl/responder/i2c_injector.vhd."""
    if not 0 <= slave_addr <= 0x7F:
        raise ValueError(f"7-bit I2C address out of range: {slave_addr:#x}")
    v = 0
    if enable:
        v |= _CTRL_ENABLE
    v |= (slave_addr & 0x7F) << 1
    if fifo_reset:
        v |= _CTRL_FIFO_RESET
    return v


def encode_spi_control(enable: bool = True, fifo_reset: bool = False) -> int:
    v = 0
    if enable:
        v |= _CTRL_ENABLE
    if fifo_reset:
        v |= _CTRL_FIFO_RESET
    return v


# ─────────────────────────────────────────────────────────────────────────
#  RegbankExt — DS-P2.51 helpers on top of the existing MmioRegbank
# ─────────────────────────────────────────────────────────────────────────


class RegbankExt:
    """DS-P2.51 helpers wrapping an MmioRegbank.

    Holds an MmioRegbank and exposes the EMIO_MUX_SEL + injector
    control/data/status slots as plain methods. Doesn't subclass —
    composition keeps the existing MmioRegbank surface untouched, and
    the underlying ``raw`` regbank stays available for code that just
    wants ``read_u32`` / ``write_u32``.
    """

    def __init__(self, regbank: MmioRegbank):
        self._rb = regbank

    @property
    def raw(self) -> MmioRegbank:
        return self._rb

    def close(self) -> None:
        self._rb.close()

    # ── Identity / streaming ROM / ring locator ──────────────────────────

    def identity(self) -> Dict[str, int]:
        """Snapshot of the four identity slots (RO, set at synth-time)."""
        return {
            "git_hash":        self._rb.read_u32(Regs.GIT_HASH),
            "system_version": self._rb.read_u32(Regs.SYSTEM_VERSION),
            "build_date":     self._rb.read_u32(Regs.BUILD_DATE),
            "regbank_version": self._rb.read_u32(Regs.REGBANK_VERSION),
        }

    def contract_abi_ver(self) -> int:
        """RTL↔PS-software ABI contract version baked into the bitstream
        (RTL-P2.654). Reads 0 on bitstreams that predate the slot — the
        harness treats 0 as 'unversioned' rather than a mismatch."""
        return self._rb.read_u32(Regs.CONTRACT_ABI_VER)

    def ring_locator(self) -> Dict[str, int]:
        return {
            "base_low":  self._rb.read_u32(Regs.RING_BASE_LOW),
            "base_high": self._rb.read_u32(Regs.RING_BASE_HIGH),
            "bytes":     self._rb.read_u32(Regs.RING_BYTES),
            "write_ptr": self._rb.read_u32(Regs.RING_WRITE_PTR),
        }

    def drain_regbank_rom(self) -> bytes:
        """Read REGBANK_NUM_WORDS then drain that many words from REGBANK_DATA.

        Per project_regbank.yml: reading REGBANK_NUM_WORDS resets the
        streaming-read pointer; each subsequent read of REGBANK_DATA
        returns the next 32-bit word and advances.
        """
        n_words = self._rb.read_u32(Regs.REGBANK_NUM_WORDS)
        words = bytearray()
        for _ in range(n_words):
            w = self._rb.read_u32(Regs.REGBANK_DATA)
            words += w.to_bytes(4, "little")
        return bytes(words)

    # ── EMIO_MUX_SEL ─────────────────────────────────────────────────────

    def set_emio_mux(self,
                     *,
                     i2c: bool = False,
                     uart: bool = False,
                     spi: bool = False,
                     edge_from_spi: bool = False,
                     edge_from_i2c: bool = False) -> None:
        """Engage the EMIO source on each protocol (else PMOD external).

        edge_from_spi (DS-P3.71, bit[3]): route the FULL injected SPI signal set
        into the 4 EDGE dataloggers (EDGE#1←SCLK, #2←MOSI, #3←MISO, #4←CS) — a
        self-test source that overflows EDGE#1 (fast SCLK) to exercise the
        DS-P3.70 in-band loss path AND gives a decodable SPI capture on the
        logic-analyzer channels.

        edge_from_i2c (DS-P3.69, bit[4]): route the injected I2C bus into
        EDGE0(id=1)←SCL and EDGE1(id=2)←SDA so RouteWave host-side sigrok i2c
        decode runs on real silicon edges ('datalogger decode i2c scl=1 sda=2')."""
        v = ((1 << EMIO_MUX_BIT_I2C)  if i2c  else 0) \
          | ((1 << EMIO_MUX_BIT_UART) if uart else 0) \
          | ((1 << EMIO_MUX_BIT_SPI)  if spi  else 0) \
          | ((1 << 3)                 if edge_from_spi      else 0) \
          | ((1 << 4)                 if edge_from_i2c      else 0)
        # DS-P2.126: a pfsoc image may point the UART logger at MMUART1.
        # Only that bit is ORed, and only together with a /dev/ttyS1 write.
        # I2C/SPI stay on the legacy bits (this image cannot open those MSS
        # devices). CAN bit 11 is never set: pfsoc_datalogger is CAN-free and
        # emio_can0_tx_i stays the recessive '1' tie.
        from sw.stream.testbench.de_c0_stimulus_contract import (
            EMIO_MUX_MSS_UART, UART_PAYLOAD, mss_fabric_requested,
            transmit_mmuart1,
        )
        drive_mmuart = bool(uart) and mss_fabric_requested()
        if drive_mmuart:
            v |= EMIO_MUX_MSS_UART
        self._rb.write_u32(Regs.EMIO_MUX_SEL, v)
        if drive_mmuart:
            transmit_mmuart1(UART_PAYLOAD)
        logger.debug("EMIO_MUX_SEL <= 0x%08x (i2c=%s uart=%s spi=%s edge_spi=%s edge_i2c=%s)",
                     v, i2c, uart, spi, edge_from_spi, edge_from_i2c)

    def get_emio_mux(self) -> int:
        return self._rb.read_u32(Regs.EMIO_MUX_SEL)

    # ── DATALOGGER_CONFIG broadcast (DS-T6) ─────────────────────────────────

    def enable_datalogger(self, device_id: int, *, enable: bool = True,
                       extra: int = 0) -> None:
        """Write a DATALOGGER_CONFIG broadcast word that latches into the
        config_control of the matching device_id.

        Each config_control instance latches independently, so multiple
        devices stay enabled across sequential writes to this single
        regbank slot. Without at least one such write the datalogger slot
        stays disabled at boot (C_INIT_REGBANK seeds DATALOGGER_CONFIG=0),
        so REQ-5/6/7 ring-frame checks can never PASS until callers
        enable the datalogger for the slot under test.
        """
        word = encode_datalogger_config(device_id, enable=enable, extra=extra)
        self._rb.write_u32(Regs.DATALOGGER_CONFIG, word)
        logger.debug("DATALOGGER_CONFIG <= 0x%04x (dev=%d, en=%s, extra=%d)",
                     word, device_id, enable, extra)

    def reset_datalogger(self, device_id: int, *, settle_s: float = 0.001) -> None:
        """Per-datalogger reset (DS-T2.31): latch ENABLE=0 for this device so
        config_control asserts CAPTURE_RST and the datalogger flushes ALL state
        (calibration FSM, baud accumulator, edge FIFO/BRAM).

        ALWAYS call this immediately before enable_datalogger(...) + capture, so
        auto-calibration starts from a clean state with no traces of a prior
        config (D.20 / CC-REQ-51 / UART-REQ-15). Pairs with the rule that the
        datalogger must be enabled BEFORE the bus traffic it should calibrate on
        (a datalogger 'late to the party' calibrates on a partial first frame).
        """
        self.enable_datalogger(device_id, enable=False, extra=0)
        time.sleep(settle_s)        # let CAPTURE_RST flush the FIFO (RST_BUSY)

    def reset_and_enable_datalogger(self, device_id: int, *, extra: int = 0) -> None:
        """Sanctioned arm sequence: reset (ENABLE=0) then enable (ENABLE=1)."""
        self.reset_datalogger(device_id)
        self.enable_datalogger(device_id, enable=True, extra=extra)

    def release_stimulus_generator(self) -> None:
        """DS-P1.90: hand the EMIO legs back to the PS.

        On the Zybo (G_STIM_DRIVES_EMIO true) the fabric stimulus generator
        drives a protocol's EMIO legs while that protocol's STIM_*_CONTROL
        bit[0] is set, and the stimulus armers leave it set. A PS-driven
        transfer made while it is set never reaches the dataloggers, so the
        PS-side benches clear the three muxed enables before driving. On DE25
        and PolarFire the enables only gate the generator itself.
        """
        for offset in (Regs.STIM_UART_CONTROL, Regs.STIM_I2C_CONTROL,
                       Regs.STIM_SPI_CONTROL):
            self._rb.write_u32(offset, 0)

    def enable_all_emio_dataloggers(self) -> None:
        """Enable the three EMIO-mux'd datalogger slots (I2C #4, SPI #8,
        UART #12). Idempotent; safe to call multiple times.

        Per DS-T6 root cause: the bitstream boots with DATALOGGER_CONFIG=0
        and no upstream actor enables the datalogger slots, so even with
        EMIO_MUX_SEL routing the EMIO bus into the datalogger pin matrix,
        the per-slot datalogger FSMs stay in their disabled state and emit
        no zs_msg frames.
        """
        for dev_id in (DATALOGGER_ID_I2C_4, DATALOGGER_ID_SPI_8, DATALOGGER_ID_UART_12):
            self.enable_datalogger(dev_id, enable=True)

    # ── I2C injector ─────────────────────────────────────────────────────

    def load_i2c_response(self, slave_addr: int, payload: Sequence[int]) -> None:
        """Reset + enable the I2C injector + push response bytes into its FIFO.

        After this call the slave responds at ``slave_addr`` with the
        supplied bytes (FIFO order) on master read transactions. The
        FIFO is reset first so repeated calls don't stack.

        DS-T1.2: The reset MUST set ENABLE=1 AND FIFO_RESET=1 in the same
        write — the injector's DS-T3.2 edge-detect produces fifo_reset_r
        on the rising edge of ``(CONTROL(9) AND ENABLE)``. Splitting
        across two writes (enable=0+reset=1 then enable=1+reset=0)
        leaves that AND term at 0 in BOTH writes → no edge, no reset,
        bytes accumulate across runs. Verified on bench: the split form
        let the FIFO accumulate 16 stale bytes across 4 self-test runs.
        """
        # Atomic ENABLE+RESET → fires the injector's rising-edge detector.
        self._rb.write_u32(
            Regs.I2C_INJ_CONTROL,
            encode_i2c_control(enable=True, slave_addr=slave_addr, fifo_reset=True),
        )
        # Release reset bit; keep ENABLE so the slave stays addressable.
        self._rb.write_u32(
            Regs.I2C_INJ_CONTROL,
            encode_i2c_control(enable=True, slave_addr=slave_addr, fifo_reset=False),
        )
        # Push bytes (DATAIN_VLD = the regbank's per-slot valid pulse).
        for b in payload:
            self._rb.write_u32(Regs.I2C_INJ_DATAIN, b & 0xFF)

    def i2c_injector_status(self) -> Dict[str, int]:
        v = self._rb.read_u32(Regs.I2C_INJ_STATUS)
        return {
            "fifo_empty":     bool(v & _I2C_STATUS_FIFO_EMPTY),
            "fifo_prog_full": bool(v & _I2C_STATUS_FIFO_PROG_FULL),
            "slave_error":    bool(v & _I2C_STATUS_SLAVE_ERROR),
            "fifo_count":     (v >> _I2C_STATUS_COUNT_SHIFT) & _I2C_STATUS_COUNT_MASK,
        }

    # ── SPI injector ─────────────────────────────────────────────────────

    def load_spi_response(self, payload: Sequence[int]) -> None:
        """Reset + enable the SPI injector + push response bytes into its FIFO.

        DS-T1.2 (mirror): same atomic ENABLE+RESET write as the I2C path;
        spi_injector mirrors i2c_injector's DS-T3.2 edge-detect on
        ``(CONTROL(9) AND ENABLE)``, so the split form silently drops
        the reset edge.
        """
        self._rb.write_u32(
            Regs.SPI_INJ_CONTROL,
            encode_spi_control(enable=True, fifo_reset=True),
        )
        self._rb.write_u32(
            Regs.SPI_INJ_CONTROL,
            encode_spi_control(enable=True, fifo_reset=False),
        )
        for b in payload:
            self._rb.write_u32(Regs.SPI_INJ_DATAIN, b & 0xFF)

    def spi_injector_status(self) -> Dict[str, int]:
        v = self._rb.read_u32(Regs.SPI_INJ_STATUS)
        return {
            "fifo_empty":     bool(v & _I2C_STATUS_FIFO_EMPTY),
            "fifo_prog_full": bool(v & _I2C_STATUS_FIFO_PROG_FULL),
            "fifo_count":     (v >> _I2C_STATUS_COUNT_SHIFT) & _I2C_STATUS_COUNT_MASK,
        }


# ─────────────────────────────────────────────────────────────────────────
#  Protocol drivers — thin wrappers over pyserial / smbus2 / spidev
# ─────────────────────────────────────────────────────────────────────────


def _tty_base(tty_dir: Path) -> int | None:
    """MMIO base of the UART behind one /sys/class/tty/<name> entry: the first
    cell of its device-tree node's ``reg``, else the unit address in the
    platform device name (``e0000000.serial``)."""
    device = tty_dir / "device"
    reg = device / "of_node" / "reg"
    try:
        cells = reg.read_bytes()
        if len(cells) >= 4:
            return int.from_bytes(cells[:4], "big")
    except OSError:
        pass
    try:
        name = device.resolve(strict=True).name
    except OSError:
        return None
    head = name.split(".", 1)[0]
    try:
        return int(head, 16)
    except ValueError:
        return None


def emio_uart_dev(base: int = EMIO_UART0_BASE,
                  sys_class_tty: Path = Path("/sys/class/tty")) -> str:
    """``/dev/ttyPS<N>`` of the PS UART at ``base`` (default: the EMIO UART0).

    Raises ``FileNotFoundError`` when no ttyPS device sits at ``base`` and
    ``RuntimeError`` when more than one does, so the bench never opens a
    guessed port (the console is one alias swap away)."""
    hits = sorted(t.name for t in sys_class_tty.glob("ttyPS*")
                  if _tty_base(t) == base)
    if not hits:
        raise FileNotFoundError(
            f"no /dev/ttyPS* at 0x{base:08X} under {sys_class_tty}: is the "
            f"PS UART enabled in this bitstream's device tree?")
    if len(hits) > 1:
        raise RuntimeError(
            f"more than one tty at 0x{base:08X}: {', '.join(hits)}")
    return f"/dev/{hits[0]}"


class UART:
    """pyserial wrapper for the EMIO UART0 test wire. 8N1, no flow control.

    ``dev=None`` (the default) resolves the tty from UART0's base address."""

    def __init__(self,
                 dev: str | None = DEFAULT_UART_DEV,
                 baud: int = DEFAULT_UART_BAUD,
                 timeout_s: float = 0.5):
        import serial  # pyserial — buildroot package python-pyserial
        if dev is None:
            dev = emio_uart_dev()
        self.dev = dev
        self._ser = serial.Serial(
            port=dev,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout_s,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

    @property
    def serial(self):
        return self._ser

    def write(self, data: bytes) -> int:
        n = self._ser.write(data)
        self._ser.flush()
        return n

    def close(self) -> None:
        with suppress(serial.SerialException):
            self._ser.close()

    def __enter__(self) -> "UART": return self
    def __exit__(self, *exc) -> None: self.close()


class I2CMaster:
    """smbus2 wrapper for /dev/i2c-N (EMIO I2C0)."""

    # The first transaction right after the EMIO mux engages and the PL
    # injector FIFO loads can lose arbitration / miss a clean ACK — the
    # Cadence i2c controller surfaces this as EAGAIN ([Errno 11]). That is
    # the kernel's documented "retry me" signal, NOT a slave/capture defect
    # (the datalogger+injector+ring path is proven correct in isolation: 286
    # frames all on datalogger_id=4). A bounded retry is exactly what a robust
    # real-world i2c-dev master does; the read assertion is untouched (DS-T2.21).
    _EAGAIN_RETRIES = 5
    _EAGAIN_BACKOFF_S = 0.005

    def __init__(self, bus: int = DEFAULT_I2C_BUS, regbank=None):
        import smbus2
        self._smbus2 = smbus2
        self._bus = smbus2.SMBus(bus)
        #: DS-P2.253: when given (an MmioRegbank-like read_u32), every transfer
        #: first checks that the stimulus generator does not hold the legs.
        self._regbank = regbank

    def _refuse_if_generator_holds_the_legs(self) -> None:
        """DS-P2.253: refuse a PS transfer that cannot reach the bus."""
        if self._regbank is None:
            return
        from sw.stream import stim_emio
        why = stim_emio.stim_takes_ps_emio("i2c")
        if why is not None and self._regbank.read_u32(Regs.STIM_I2C_CONTROL) & 0x1:
            raise RuntimeError(f"PS I2C transfer refused: STIM_I2C_CONTROL bit 0 is set; {why}")

    @property
    def smbus(self):
        return self._bus

    def _rdwr(self, *msgs) -> None:
        """i2c_rdwr with a bounded EAGAIN retry (arbitration-lost / not-ready)."""
        self._refuse_if_generator_holds_the_legs()
        last: OSError | None = None
        for attempt in range(self._EAGAIN_RETRIES):
            try:
                self._bus.i2c_rdwr(*msgs)
                if attempt:
                    logger.debug("I2C transaction succeeded after %d EAGAIN retr%s",
                                 attempt, "y" if attempt == 1 else "ies")
                return
            except OSError as exc:
                if exc.errno != errno.EAGAIN:
                    raise
                last = exc
                time.sleep(self._EAGAIN_BACKOFF_S)
        assert last is not None
        raise last

    def write_bytes(self, slave_addr: int, data: bytes) -> None:
        """Raw write transaction (START + addr + payload + STOP)."""
        self._rdwr(self._smbus2.i2c_msg.write(slave_addr, data))

    def read_bytes(self, slave_addr: int, n: int) -> bytes:
        """Raw read transaction (START + addr + read N + STOP)."""
        msg = self._smbus2.i2c_msg.read(slave_addr, n)
        self._rdwr(msg)
        return bytes(msg)

    def write_then_read(self, slave_addr: int, write: bytes, read_n: int) -> bytes:
        """Combined transaction with repeated-START between write + read."""
        wmsg = self._smbus2.i2c_msg.write(slave_addr, write)
        rmsg = self._smbus2.i2c_msg.read(slave_addr, read_n)
        self._rdwr(wmsg, rmsg)
        return bytes(rmsg)

    def close(self) -> None:
        with suppress(OSError):
            self._bus.close()

    def __enter__(self) -> "I2CMaster": return self
    def __exit__(self, *exc) -> None: self.close()


class SPIMaster:
    """spidev wrapper for /dev/spidevB.C (EMIO SPI0). Mode 0 by default."""

    def __init__(self,
                 bus: int = DEFAULT_SPI_BUS,
                 cs: int = DEFAULT_SPI_CS,
                 speed_hz: int = DEFAULT_SPI_SPEED,
                 mode: int = 0,
                 bits_per_word: int = 8):
        import spidev
        self._spi = spidev.SpiDev()
        self._spi.open(bus, cs)
        self._spi.mode = mode
        self._spi.bits_per_word = bits_per_word
        self._spi.max_speed_hz = speed_hz

    @property
    def spi(self):
        return self._spi

    def xfer(self, tx: Sequence[int]) -> bytes:
        """Full-duplex transfer. Returns the MISO bytes received."""
        rx = self._spi.xfer2(list(tx))
        return bytes(rx)

    def close(self) -> None:
        with suppress(OSError):
            self._spi.close()

    def __enter__(self) -> "SPIMaster": return self
    def __exit__(self, *exc) -> None: self.close()


# ─────────────────────────────────────────────────────────────────────────
#  Facade — opens regbank + uart + i2c + spi ready to use
# ─────────────────────────────────────────────────────────────────────────


class SelfTestBench:
    """One-stop entry: regbank + UART + I2C + SPI, all opened ready to use.

    Usage::

        with SelfTestBench() as bench:
            bench.regbank.set_emio_mux(uart=True)
            bench.uart.write(b"U")
            ...
    """

    def __init__(self,
                 mmap_base: int = MmioRegbank.DEFAULT_BASE_ADDR,
                 uart_dev: str | None = DEFAULT_UART_DEV,
                 uart_baud: int = DEFAULT_UART_BAUD,
                 i2c_bus: int = DEFAULT_I2C_BUS,
                 spi_bus: int = DEFAULT_SPI_BUS,
                 spi_cs: int = DEFAULT_SPI_CS,
                 spi_speed_hz: int = DEFAULT_SPI_SPEED,
                 enable_dataloggers: bool = True):
        self.regbank = RegbankExt(MmioRegbank(base_addr=mmap_base))
        # DS-P1.90: this bench drives the PS EMIO legs, so the generator must
        # not hold them (a prior stimulus arm leaves its enables set).
        self.regbank.release_stimulus_generator()
        if enable_dataloggers:
            # DS-T6 root cause: bitstream boots with DATALOGGER_CONFIG=0 and no
            # actor enables the per-slot dataloggers, so even with EMIO_MUX_SEL
            # routing EMIO bus traffic into the datalogger pin matrix, the per-
            # slot datalogger FSMs stay disabled and emit no zs_msg frames. We
            # enable I2C/SPI/UART datalogger slots once at bench open; each
            # config_control latches independently so the three back-to-back
            # writes leave all three enabled.
            self.regbank.enable_all_emio_dataloggers()
        self.uart = UART(uart_dev, uart_baud)
        self.i2c  = I2CMaster(i2c_bus, regbank=self.regbank.raw)
        self.spi  = SPIMaster(spi_bus, spi_cs, speed_hz=spi_speed_hz)

    def close(self) -> None:
        for x in (self.uart, self.i2c, self.spi):
            with suppress(Exception):
                x.close()
        self.regbank.close()

    def __enter__(self) -> "SelfTestBench": return self
    def __exit__(self, *exc) -> None: self.close()
