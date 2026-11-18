"""Production DE-C0 register and wire stimulus constants (DS-P1.58).

Production code: the daemon arms ds_stimulus_gen from these (DS-P2.136), and the
DE25 rootfs strips sw/stream/testbench, so this module must not live there.
``sw.stream.testbench.de_c0_stimulus_contract`` re-exports it for the witness
tooling."""

from __future__ import annotations

import os
from pathlib import Path

I2C_ADDRESS_7B = 0x50
I2C_ADDRESS_BYTE = I2C_ADDRESS_7B << 1
I2C_DATA = [0xAB, 0xCD, 0x3E]
I2C_PACKED_DATA = 0x003ECDAB
I2C_INJ_CONTROL = 0x30
STIM_I2C_CONTROL = 0x22C
STIM_I2C_CLK_DIV = 0x230
STIM_I2C_DATA = 0x234

# Exact production arming sequence.  The responder write is deliberately
# first; changing expected ACK booleans cannot substitute for this effect.
I2C_REGISTER_SEQUENCE = (
    (I2C_INJ_CONTROL, (I2C_ADDRESS_7B << 1) | 0x1),
    (STIM_I2C_CLK_DIV, 125),
    (STIM_I2C_CONTROL, ((len(I2C_DATA) - 1) << 8) | (I2C_ADDRESS_7B << 1) | 0x1),
    (STIM_I2C_DATA, I2C_PACKED_DATA),
)

EDGE_CHANNEL = 1
EDGE_TRANSITIONS = [(10_000, 1), (10_200, 0), (10_520, 1), (10_800, 0)]
STIM_EDGE_CONTROL = 0x244
EDGE_FINITE_CONTROL = 0x3
# Restart contract (ds_stimulus_gen.vhd p_edge): the finite vector plays once per STIM_EDGE_CONTROL bit[0] 0->1
# transition, then holds low. Writing EDGE_FINITE_CONTROL while the register already holds it replays nothing:
# re-arming needs a 0 write first (stimulus_armer.arm_edge does; a hand-driven devmem sequence must too).

# ``ds_stimulus_gen.vhd`` p_edge step 0 counts 500 * C_EDGE_QUANTUM_CYCLES
# (500 * 20 ns) with the line held LOW before it raises the first canonical
# transition, so the vector is preceded by a guaranteed QUIET LOW gap of exactly
# this length measured from the ``STIM_EDGE_CONTROL`` enable write.  It equals
# ``EDGE_TRANSITIONS[0][0]`` by construction — the canonical vector's absolute
# times ARE arm-relative times.  This is the only part of the arm anchor that is
# observable in a capture (the enable write itself is not timestamped in the
# datalogger's timebase — see ``compare_edge_arm_anchored``).
EDGE_ARM_DELAY_NS = EDGE_TRANSITIONS[0][0]

# ── Regbank byte offsets shared by every DE-C0 armer (DS-P1.59) ────────────────
# The single source of truth so the stop-daemon driver
# (``de_c0_live_stimulus_driver.py``) and the daemon-live production armer
# (``de_c0_production_armer.py``) cannot drift apart.  Offsets mirror
# ``hw/rtl/pkg/system_regbank_pkg.vhd``'s ``C_INDEX_STIM_*`` constants
# (byte offset = word index * 4) and ``ds_stimulus_gen.vhd``'s port comments.
EMIO_MUX_SEL = 0x24
DATALOGGER_CONFIG = 0x20
STIM_UART_CONTROL, STIM_UART_BAUD_DIV, STIM_UART_DATA = 0x220, 0x224, 0x228
STIM_UART_CTRL_BREAK = 0x2     # STIM_UART_CONTROL[1]: break pattern; [15:8] = low bit-times
STIM_SPI_CONTROL, STIM_SPI_CLK_DIV, STIM_SPI_DATA = 0x238, 0x23C, 0x240

# Datalogger device_ids — tools/datalogger-builder/config/datalogger_config.yml.
DATALOGGER_ID_I2C, DATALOGGER_ID_SPI, DATALOGGER_ID_UART, DATALOGGER_ID_EDGE1 = 4, 8, 12, 1

# DS-P2.136: the data logger that observes each generator — the datalogger_id a
# CONTROL op="stim" frame carries.
STIM_DATALOGGER_ID = {
    "uart": DATALOGGER_ID_UART,
    "i2c": DATALOGGER_ID_I2C,
    "spi": DATALOGGER_ID_SPI,
    "edge": DATALOGGER_ID_EDGE1,
}

# EMIO mux routing bit per protocol (deepskopion_core.vhd stim_*_sel).
EMIO_MUX_UART, EMIO_MUX_I2C, EMIO_MUX_SPI, EMIO_MUX_EDGE = 0x2, 0x1, 0x4, 0x80
# DS-P2.126: OR with the matching protocol bit so the logger observes the
# PolarFire MSS fabric peripheral instead of the ds_stimulus_gen loopback.
# Edge/pulse has no MSS source and stays on EMIO_MUX_EDGE.
EMIO_MUX_MSS_I2C, EMIO_MUX_MSS_UART, EMIO_MUX_MSS_SPI, EMIO_MUX_MSS_CAN = (
    1 << 8, 1 << 9, 1 << 10, 1 << 11,
)


def mss_fabric_requested() -> bool:
    """True when this boot asked the logger to watch MSS fabric peripherals.

    ``DS_MSS_FABRIC=0`` forces the legacy EMIO/stimulus mux (Zybo PS EMIO,
    DE25 ``ds_stimulus_gen``). ``DS_MSS_FABRIC=1`` or a kernel cmdline token
    ``ds_mss_fabric=1`` ORs bits [11:8]. Unset, the cmdline is the decider so
    an interactive armer on the kit follows the image without an extra export.
    """
    flag = os.environ.get("DS_MSS_FABRIC")
    if flag == "1":
        return True
    if flag == "0":
        return False
    try:
        tokens = Path("/proc/cmdline").read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return False
    return "ds_mss_fabric=1" in tokens


# Userspace on this image can open MMUART1 as /dev/ttyS1 (kernel 8250, no
# extra buildroot package). I2C_0 and SPI_0 have no Linux device node here,
# and sw/linux/pfsoc/buildroot_pfsoc_defconfig ships no i2c-tools or spidev,
# so those MSS bits stay clear and ds_stimulus_gen keeps driving them.
# CAN bit 11 stays clear on the CAN-free pfsoc target: the top ties
# emio_can0_tx_i to the recessive '1'.
MMUART1_DEV = "/dev/ttyS1"
_MSS_DRIVABLE = {
    "uart": EMIO_MUX_MSS_UART,
}


def emio_mux_word(protocol: str) -> int:
    """Legacy protocol bit, OR an MSS select only when userspace can drive it.

    Edge/pulse has no MSS generator and stays on ``EMIO_MUX_EDGE``. I2C and
    SPI stay on the legacy bit (``ds_stimulus_gen``) because this image cannot
    open an MSS I2C or SPI device. CAN contributes no bit: selecting bit 11
    would replace the recessive tie with an undriven fabric pin.
    """
    legacy = {
        "uart": EMIO_MUX_UART,
        "i2c": EMIO_MUX_I2C,
        "spi": EMIO_MUX_SPI,
        "edge": EMIO_MUX_EDGE,
        "can": 0,
    }[protocol]
    if not mss_fabric_requested():
        return legacy
    return legacy | _MSS_DRIVABLE.get(protocol, 0)


def _open_mmuart(path: str, mode: str = "wb"):
    """Opener for MMUART1. Tests replace this; production uses ``open``."""
    return open(path, mode)


def _set_115200_8n1(fh) -> None:
    import termios
    attrs = termios.tcgetattr(fh.fileno())
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    termios.tcsetattr(fh, termios.TCSANOW, attrs)


def transmit_mmuart1(payload) -> bytes:
    """Write ``payload`` on the fabric MMUART1 (``/dev/ttyS1``).

    When ``EMIO_MUX_SEL`` bit 9 is set the UART logger samples this pin, not
    ``ds_stimulus_gen``. A caller that sets the bit and skips this write
    leaves the logger with no bytes.
    """
    data = bytes(int(b) & 0xFF for b in payload)
    fh = _open_mmuart(MMUART1_DEV, "wb")
    try:
        try:
            _set_115200_8n1(fh)
        except Exception:
            pass
        written = fh.write(data)
        if written != len(data):
            raise OSError(
                f"MMUART1 never transmits a full payload ({written} of {len(data)})"
            )
        flush = getattr(fh, "flush", None)
        if callable(flush):
            flush()
    finally:
        close = getattr(fh, "close", None)
        if callable(close):
            close()
    return data

# UART/SPI payloads — the DS-X3/DE-R0 Xilinx-oracle MARKER+FLUSH and MOSI runs
# (see capture_correctness.py, which binds these to test_datalogger_to_ddr.py).
UART_PAYLOAD = [0xA3, 0x5C, 0x71, 0x0E, 0xFF, 0x42, 0x00, 0xFF, 0x00, 0xFF]
SPI_PAYLOAD = [0xDE, 0xAD, 0xBE, 0xEF]
UART_BAUD_DIV = 434   # 50e6 / 434 ~= 115200 baud on clk_sys (DS-T2.73, 50 MHz)
SPI_CLK_DIV = 25      # 50e6 / (2*25) = 1 MHz SCLK
