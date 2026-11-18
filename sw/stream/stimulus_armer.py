"""DS-P1.59 / DS-P2.136 — the production armer for DS-P2.100's fabric stimulus
generator (``hw/rtl/stimulus/ds_stimulus_gen.vhd``).

Arms one protocol using regbank **WRITES ONLY**. It never opens or reads the DDR
ring, so the ``S99deepskopion-stream`` daemon keeps streaming while it runs
(Haz.59 — a second ring reader races the daemon's READ pointer).

Two callers share this one sequence so they cannot drift: the daemon, on a
CONTROL ``op="stim"`` (``data_source.apply_stimulus``), and the console armer
``sw.stream.testbench.de_c0_production_armer``. It lives outside testbench
because the DE25 rootfs strips that package (``tools/de25_linux_image.py``
``STREAM_EXCLUDE``).

The per-protocol sequences are the DS-P1.58 production register contract
(``stimulus_contract``):

  * ``i2c`` arms the on-chip responder BEFORE the master (``I2C_REGISTER_SEQUENCE``)
    so the single transaction is ACK-bound (``start,(0xA0,ACK),(0xAB,ACK),
    (0xCD,ACK),(0x3E,ACK),stop``) — the fix DS-P1.58 landed for the
    DS-P2.101 "unarmed responder NACKs every byte / one transaction per DATA
    write" gap;
  * ``edge`` drives the finite canonical vector (``EDGE_FINITE_CONTROL``) whose
    transitions land exactly on the 20 ns grid at either supported sample clock.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List

from .stimulus_contract import (
    EDGE_FINITE_CONTROL, EMIO_MUX_SEL, I2C_REGISTER_SEQUENCE, DATALOGGER_CONFIG,
    DATALOGGER_ID_EDGE1, DATALOGGER_ID_I2C, DATALOGGER_ID_SPI, DATALOGGER_ID_UART,
    SPI_CLK_DIV, SPI_PAYLOAD,
    STIM_EDGE_CONTROL, STIM_SPI_CONTROL, STIM_SPI_CLK_DIV, STIM_SPI_DATA,
    STIM_UART_BAUD_DIV, STIM_UART_CONTROL, STIM_UART_CTRL_BREAK, STIM_UART_DATA, UART_BAUD_DIV,
    UART_PAYLOAD, emio_mux_word, mss_fabric_requested, transmit_mmuart1,
)

# Anything with ``write_u32(offset, value)`` — a real ``MmioRegbank`` on the
# board, or a recording fake in tests.  DELIBERATELY not typed to MmioRegbank so
# a test can capture the exact write trace without a mapped /dev/mem.
Regbank = object

_INTER_BYTE_S = 0.003
_DATALOGGER_RESET_S = 0.005


#: DATALOGGER_CONFIG extras for the UART datalogger: autoSync=0, dataBits=3,
#: i.e. explicit 8N1 (the same word arm_datalogger_defaults writes at boot).
UART_EXTRA_8N1 = 6


def _enable_datalogger(rb, device_id: int, extra: int = 0) -> None:
    """DS-T2.31 arm sequence: reset (enable=0) then enable (enable=1).

    The disable→enable pair is load-bearing: a config write alone does not reset
    the datalogger calibration FSM, so a stale ``configEnable`` swallows the new
    framing (deepskopion-bringup skill, rung 3). *extra* fills config[10:1]."""
    rb.write_u32(DATALOGGER_CONFIG, (device_id & 0x1F) << 11)         # enable=0
    time.sleep(_DATALOGGER_RESET_S)
    rb.write_u32(DATALOGGER_CONFIG,
                 ((device_id & 0x1F) << 11) | ((extra & 0x3FF) << 1) | 0x1)  # enable=1


def arm_uart(rb) -> None:
    # DS-P2.243: the UART datalogger (id 12) is enabled here at 8N1, like every
    # other protocol's datalogger. It used to rely on the boot-time arming, and
    # on the DE25 (DE-G0 run 12) that step had failed: the card's init script
    # named a module the deployed tree had renamed, so the datalogger stayed
    # off and the stimulus produced 0 packets. On a pfsoc image bit 9 points
    # the logger at MMUART1, so the bytes have to leave /dev/ttyS1. STIM_UART_*
    # still drives ds_stimulus_gen for Zybo and DE25 (DS_MSS_FABRIC unset).
    # The mux first, as arm_edge: the logger auto-calibrates the baud on the first edges it sees, so enabled on the
    # header pin it calibrated on the header-to-stimulus step and reported every byte as a stop error
    # (test_arm_order_uart_i2c_spi_ds_p2_315).
    rb.write_u32(EMIO_MUX_SEL, emio_mux_word("uart"))
    _enable_datalogger(rb, DATALOGGER_ID_UART, extra=UART_EXTRA_8N1)
    if mss_fabric_requested():
        transmit_mmuart1(UART_PAYLOAD)
        return
    rb.write_u32(STIM_UART_BAUD_DIV, UART_BAUD_DIV)
    rb.write_u32(STIM_UART_CONTROL, 0x1)
    for b in UART_PAYLOAD:
        rb.write_u32(STIM_UART_DATA, b)
        time.sleep(_INTER_BYTE_S)


#: DATALOGGER_CONFIG extras for the UART datalogger with autoSync on (config[1]).
UART_EXTRA_AUTOSYNC = 1


def arm_uart_breaks(rb, low_bits: int = 13, count: int = 80,
                    baud_div: int = UART_BAUD_DIV) -> None:
    """Force a UART autoSync calibration failure on silicon (DS-P2.257).

    Arms UART datalogger 12 with autoSync, then has ds_stimulus_gen send *count*
    breaks: the line low for *low_bits* bit-times, then high for one. With 13 or
    more low bit-times no UART format fits, so the calibration fails and the
    datalogger reports CALIBRATION_TIMEOUT (STATUS 0x01). 80 breaks exceed the
    70-byte calibration buffer. Zybo / DE25 stimulus generator only (STIM_UART_*);
    not for the MMUART1 path. Raises ValueError before any write on a bad value."""
    if not 1 <= low_bits <= 0xFF:
        raise ValueError(f"low_bits {low_bits} out of range [1,255]")
    if not 1 <= count <= 0xFFFF:
        raise ValueError(f"count {count} out of range [1,65535]")
    if not 2 <= baud_div <= 0xFFFFFFFF:
        raise ValueError(f"baud_div {baud_div} out of range [2,2^32-1]")
    # The mux first, as arm_uart (DS-P2.315): enabled on a header pin held low, the logger's calibration failed on
    # the header-to-stimulus step after 3 toggles, not on the breaks (test_arm_uart_breaks_order_ds_p2_318).
    rb.write_u32(EMIO_MUX_SEL, emio_mux_word("uart"))
    _enable_datalogger(rb, DATALOGGER_ID_UART, extra=UART_EXTRA_AUTOSYNC)
    rb.write_u32(STIM_UART_BAUD_DIV, baud_div)
    rb.write_u32(STIM_UART_CONTROL, 0x1 | STIM_UART_CTRL_BREAK | (low_bits << 8))
    rb.write_u32(STIM_UART_DATA, count)


def arm_i2c(rb) -> None:
    # The mux first: enabled on a header pin held low, the logger recorded a stop_error at the switch to the
    # stimulus bus (DE25 silicon 2026-11-15; test_arm_order_uart_i2c_spi_ds_p2_315).
    rb.write_u32(EMIO_MUX_SEL, emio_mux_word("i2c"))
    _enable_datalogger(rb, DATALOGGER_ID_I2C)
    # Responder-first: without I2C_INJ_CONTROL the ACK slots are necessarily
    # NACKs (DS-P2.101 gap; DS-P1.58 fix).
    for offset, value in I2C_REGISTER_SEQUENCE:
        rb.write_u32(offset, value)


def arm_spi(rb) -> None:
    # The mux first, as the other armers (no artifact measured for SPI in test_arm_order_uart_i2c_spi_ds_p2_315).
    rb.write_u32(EMIO_MUX_SEL, emio_mux_word("spi"))
    _enable_datalogger(rb, DATALOGGER_ID_SPI)
    rb.write_u32(STIM_SPI_CLK_DIV, SPI_CLK_DIV)
    rb.write_u32(STIM_SPI_CONTROL, 0x1)
    for b in SPI_PAYLOAD:
        rb.write_u32(STIM_SPI_DATA, b)
        time.sleep(_INTER_BYTE_S)


def arm_edge(rb) -> None:
    # The mux first: the logger is enabled only once its input is the stimulus line. Enabled first, it read the
    # header pin, which the header pull-up holds high, and recorded a rise at enable and a fall at the mux switch
    # before the stimulus (DE25 silicon 2026-11-15; test_arm_edge_order_ds_p2_313).
    rb.write_u32(EMIO_MUX_SEL, emio_mux_word("edge"))
    _enable_datalogger(rb, DATALOGGER_ID_EDGE1)
    # 0 first: the finite vector restarts only on a bit-0 0->1 transition (ds_stimulus_gen p_edge). Written straight to
    # 0x3 over an earlier play it does nothing (Zybo silicon 2026-11-15; test_arm_edge_rearm_ds_p1_108).
    rb.write_u32(STIM_EDGE_CONTROL, 0)
    rb.write_u32(STIM_EDGE_CONTROL, EDGE_FINITE_CONTROL)


ARMERS: Dict[str, Callable[[object], None]] = {
    "uart": arm_uart,
    "i2c": arm_i2c,
    "spi": arm_spi,
    "edge": arm_edge,
}

PROTOCOLS: List[str] = list(ARMERS)


def arm(rb, protocol: str) -> None:
    """Arm ``protocol`` on ``rb`` (regbank writes only)."""
    try:
        armer = ARMERS[protocol]
    except KeyError:
        raise SystemExit(f"unknown protocol {protocol!r}; "
                         f"expected one of {PROTOCOLS}")
    armer(rb)
