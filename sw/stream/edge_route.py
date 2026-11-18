"""DS-P3.123: route a protocol's lines onto the Edge Detector inputs, from the daemon.

EMIO_MUX_SEL (regbank 0x24) has three groups of bits (hw/rtl/deepskopion_core.vhd):

  bits [2:0]   per-protocol source: the PS EMIO master or the PMOD pins
               (ds_emio_mux; the stimulus armers write these)
  bits [7:3]   which signal set feeds the four Edge Detectors
               (bit 3 SPI, 4 I2C, 5 UART TX, 6 CAN, 7 stimulus edge/pulse);
               none set: the PMOD jb[5:2] pins
  bits [11:8]  PolarFire MSS fabric peripheral instead of the stimulus loopback

Until this module the edge bits were reachable only by a host-side devmem, so
a consumer could enable the edge data loggers (CONTROL set_config) but not
give them a signal. ``apply_edge_route`` writes bits [7:3] to one route and
leaves bits [2:0] and [11:8] as they read, so it composes with a stimulus arm:
``stim i2c`` (bits [2:0] = 0x1) then ``edge_route i2c`` gives 0x11, the value
of the 2026-06-22 live I2C edge demo. It then enables the edge data loggers
the route feeds, with the same reset-then-enable pair the stimulus armers use.

Regbank reads and writes only; the DDR ring reader is not touched.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import channels
from .stimulus_armer import _enable_datalogger
from .stimulus_contract import EMIO_MUX_SEL

#: EMIO_MUX_SEL bits [7:3], the edge-input routing group.
EDGE_ROUTE_MASK = (channels.EMIO_EDGE_SPI_BIT | channels.EMIO_EDGE_I2C_BIT
                   | channels.EMIO_EDGE_UART_BIT | channels.EMIO_EDGE_CAN_BIT
                   | channels.EMIO_EDGE_STIM_BIT)

#: route -> (EMIO_MUX_SEL edge bit, Edge Detector device_ids the route drives).
#: The device_ids follow the RTL board_pin(JB2..JB5) mux: EDGE0=1, EDGE1=2,
#: EDGE2=3, EDGE3=6 (C_EDGE0..3_ID).
ROUTES: Dict[str, Tuple[int, Tuple[int, ...]]] = {
    "i2c": (channels.EMIO_EDGE_I2C_BIT, (1, 2)),
    "spi": (channels.EMIO_EDGE_SPI_BIT, (1, 2, 3, 6)),
    "uart": (channels.EMIO_EDGE_UART_BIT, (1,)),
    "can": (channels.EMIO_EDGE_CAN_BIT, (1, 2)),
    "stim": (channels.EMIO_EDGE_STIM_BIT, (1,)),
    "pmod": (0, channels.EDGE_DEVICE_IDS),
}


def route_word(current: int, route: str) -> int:
    """EMIO_MUX_SEL after routing ``route``: bits [7:3] replaced, the rest kept."""
    bit, _ = ROUTES[route]
    return (current & ~EDGE_ROUTE_MASK & 0xFFFFFFFF) | bit


def apply_edge_route(regbank, route: str) -> Tuple[Optional[str], Optional[int]]:
    """Route ``route`` onto the edge inputs and enable its edge data loggers.

    Returns ``(reject reason or None, EMIO_MUX_SEL as read back)``."""
    if route not in ROUTES:
        return (f"unknown edge route {route!r}; expected one of {sorted(ROUTES)}", None)
    word = route_word(regbank.read_u32(EMIO_MUX_SEL), route)
    regbank.write_u32(EMIO_MUX_SEL, word)
    for device_id in ROUTES[route][1]:
        _enable_datalogger(regbank, device_id)
    applied = regbank.read_u32(EMIO_MUX_SEL)
    if applied != word:
        return (f"EMIO_MUX_SEL reads 0x{applied:08x} after writing 0x{word:08x}", applied)
    return (None, applied)


def edge_lanes(emio_mux_sel: int) -> List[dict]:
    """The HELO edge-lane descriptors for ``emio_mux_sel`` (DS-P3.124 names)."""
    return channels.edge_channels(emio_mux_sel)
