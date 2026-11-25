# rr unknown-llm (AI-generated)
"""Deepskopion channel topology — descriptors that go into the HELO frame.

The PL exposes several datalogger types (see hw/rtl/datalogger/):
  - 8 nonstd level pins (level_datalogger): raw 1-bit inputs for cross-protocol
    correlation and ad-hoc probing.
  - UART datalogger: emits both the raw RX/TX line state (SAMPLES) and decoded
    byte frames (ANNOTATIONS) at sub-µs precision.
  - I2C datalogger: SDA/SCL line states + decoded START/STOP/byte frames.
  - QSPI datalogger: clock + 4 data lines + decoded transactions.

Phase-1 fixture: a representative subset that fits in 64 packed bits, sized
so RouteWave's slice-2 path accepts the SAMPLES (slice-2b hex-string handling
lands later in LT-P2.X). The first SAMPLES path covers the 8 level pins +
UART RX/TX + I2C SDA/SCL + QSPI CLK/CS — 14 raw bits in total. Annotations
ride alongside the samples on the dedicated channels (uart_byte, i2c_frame:4,
i2c_frame:5, qspi_xact).

Bit layout (LSB-first concatenation in table order — matches mock_wave_stream
convention so the consumer's slice logic is uniform):

  bits  0..7  : level_pin[0..7]    (8 channels x 1 bit)
  bit   8     : uart_rx
  bit   9     : uart_tx
  bit  10     : i2c_sda
  bit  11     : i2c_scl
  bit  12     : qspi_clk
  bit  13     : qspi_cs

Decoded-frame channels (ANNOTATIONS only — width=0 means no SAMPLES bits
contributed; the consumer renders these in the annotation lane):
  - uart_byte
  - i2c_frame:4 (cam B #4)
  - i2c_frame:5 (cam A #5)
  - qspi_xact

Sample channel ids start at SAMPLE_CHANNEL_BASE (32); ids 0..31 belong to the
edge lanes, declared at their EDGE_EVENTS device_id (DS-P3.124, see
edge_channels below).

Deepskopion is pre-ship — this layout is a baseline that future PL
configurations override at HELO time. A real daemon driving live PL data
would build CHANNELS dynamically from the regbank's datalogger-config readout.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# DS-P3.124: HELO ids below EDGE_DEVICE_ID_SPACE are the EDGE_EVENTS device_id
# namespace. EDGE_EVENT word bits [31:27] carry a 5-bit device_id, and RouteWave
# names an Edge Scope lane after the HELO channel whose id EQUALS that device_id
# (logic-trace edge_naming.hpp, LT-P3.257). The edge lanes are therefore
# declared at their own device_ids, and every other channel must stay out of
# 0..31. The sample lanes used to sit at 0..13: level_pin[1]/[2] occupied the
# ids of EDGE0 (1) and EDGE1 (2), so an edge lane could not be declared there
# (HELO channel ids MUST be unique, wave_stream_v1) and the lanes rendered as
# edge_1 / edge_2. The sample lanes now start at SAMPLE_CHANNEL_BASE. Slicing
# is by table ORDER, not by id, so the packed SAMPLES layout is unchanged.
EDGE_DEVICE_ID_SPACE: int = 32
SAMPLE_CHANNEL_BASE: int = EDGE_DEVICE_ID_SPACE

# Sample-bearing channels (contribute bits to the packed SAMPLES value).
_SAMPLE_CHANNELS: List[Dict[str, Any]] = [
    {"id": SAMPLE_CHANNEL_BASE + 0,  "name": "level_pin[0]", "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 1,  "name": "level_pin[1]", "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 2,  "name": "level_pin[2]", "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 3,  "name": "level_pin[3]", "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 4,  "name": "level_pin[4]", "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 5,  "name": "level_pin[5]", "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 6,  "name": "level_pin[6]", "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 7,  "name": "level_pin[7]", "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 8,  "name": "uart_rx",      "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 9,  "name": "uart_tx",      "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 10, "name": "i2c_sda",      "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 11, "name": "i2c_scl",      "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 12, "name": "qspi_clk",     "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
    {"id": SAMPLE_CHANNEL_BASE + 13, "name": "qspi_cs",      "width": 1, "type": "Signal", "role": "Wire", "radix": "Binary"},
]

# Annotation-only channels (width=0 — no bits in SAMPLES, only ANNOTATIONS).
# IDs are namespaced above the sample-channel range so a future widening of
# the sample-bearing set doesn't collide.
#
# `type` is "Signal" and not "Annotation", and `radix` is "DecimalUnsigned" and
# not "Decimal", because those are the registered members of the wire contract's
# ChannelType/Radix sets (contracts/wave_stream_v1). An unregistered member is
# not rejected by the consumer — it falls back to Signal/Hexadecimal — so the
# eye and stats lanes asked for decimal and were rendered in hex, with nothing
# on either side reporting it. width=0 already carries "annotation-only".
# Caught by WS-C0 (DS-P1.29).
_ANNOTATION_CHANNELS: List[Dict[str, Any]] = [
    {"id": 100, "name": "uart_byte",   "width": 0, "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},
    {"id": 101, "name": "i2c_frame:4", "width": 0, "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},  # DS-P3.155 cam B (#4)
    {"id": 102, "name": "qspi_xact",   "width": 0, "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},
    {"id": 103, "name": "jtag_tap",    "width": 0, "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},
    {"id": 104, "name": "jtag_reg",    "width": 0, "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},
    {"id": 105, "name": "can_msg",     "width": 0, "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},  # DS-P3.104 per-message
    {"id": 106, "name": "can_frame",   "width": 0, "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},  # DS-P3.104 reassembled frame
    {"id": 107, "name": "eye_point",   "width": 0, "type": "Signal", "role": "Wire", "radix": "DecimalUnsigned"},      # DS-P2.66 (freq,phase,BER) bathtub cell
    {"id": 108, "name": "eye_summary", "width": 0, "type": "Signal", "role": "Wire", "radix": "DecimalUnsigned"},      # DS-P3.119 max-rate / link-margin verdict
    {"id": 109, "name": "i2c_frame:5", "width": 0, "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},  # DS-P3.155 cam A (#5)
]

# System annotation channels — PL liveness + protocol-stat summaries emitted
# by RegbankDataSource (DS-P2.31 Phase A). 200-range to stay above the
# protocol-decoder annotations and leave room for per-protocol additions.
# 203 = zs_frame: per-zs_msg-header annotation emitted by DdrRingDataSource
# (DS-P3.27 Phase B) when no domain-specific decoder owns the frame.
_SYSTEM_CHANNELS: List[Dict[str, Any]] = [
    {"id": 200, "name": "pl_heartbeat",    "width": 0, "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},
    {"id": 201, "name": "fifo_overflow",   "width": 0, "type": "Signal", "role": "Wire", "radix": "DecimalUnsigned"},
    {"id": 202, "name": "protocol_stats",  "width": 0, "type": "Signal", "role": "Wire", "radix": "DecimalUnsigned"},
    {"id": 203, "name": "zs_frame",        "width": 0, "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},
    {"id": 204, "name": "zs_resync",       "width": 0, "type": "Signal", "role": "Wire", "radix": "Hexadecimal"},
]

CHANNELS: List[Dict[str, Any]] = _SAMPLE_CHANNELS + _ANNOTATION_CHANNELS + _SYSTEM_CHANNELS

# ── DS-P3.124: edge lanes, named from the live EMIO_MUX_SEL routing ──────────
#
# The four Edge Detector instances and their device_ids (C_EDGE0..3_ID,
# hw/rtl/pkg/datalogger_config_pkg.vhd). EDGE_EVENT words carry these ids.
EDGE_DEVICE_IDS: tuple = (1, 2, 3, 6)

# EMIO_MUX_SEL bits that steer a signal onto the edge inputs
# (hw/rtl/deepskopion_core.vhd, the emio_edge_sel..stim_edge_sel decode).
EMIO_EDGE_SPI_BIT = 1 << 3    # emio_edge_sel: EDGE0..3 <- SPI SCLK/MOSI/MISO/CS
EMIO_EDGE_I2C_BIT = 1 << 4    # i2c_edge_sel:  EDGE0/1  <- I2C SCL/SDA (DS-P3.69)
EMIO_EDGE_UART_BIT = 1 << 5   # uart_edge_sel: EDGE0    <- UART0 TX (DS-T3.5)
EMIO_EDGE_CAN_BIT = 1 << 6    # can_edge_sel:  EDGE0/1  <- CAN RX / CTU-A TX (DS-P3.107)
EMIO_EDGE_STIM_BIT = 1 << 7   # stim_edge_sel: EDGE0    <- stimulus edge/pulse (DS-P2.100)

# Per edge device_id: (mux bit, name) in the RTL's priority order, highest
# first, then the PMOD pin the input falls back to when no bit selects it.
# This mirrors the board_pin(C_BOARD_PIN_JB2..JB5) assignments in
# deepskopion_core.vhd; a change there must be made here too.
_EDGE_ROUTING: Dict[int, Tuple[Tuple[Tuple[int, str], ...], str]] = {
    1: (((EMIO_EDGE_CAN_BIT, "can_rx"),
         (EMIO_EDGE_UART_BIT, "uart_tx"),
         (EMIO_EDGE_I2C_BIT, "i2c_scl"),
         (EMIO_EDGE_SPI_BIT, "spi_sclk"),
         (EMIO_EDGE_STIM_BIT, "stim_edge")), "pmod_jb2"),
    2: (((EMIO_EDGE_CAN_BIT, "can_tx"),
         (EMIO_EDGE_I2C_BIT, "i2c_sda"),
         (EMIO_EDGE_SPI_BIT, "spi_mosi")), "pmod_jb3"),
    3: (((EMIO_EDGE_SPI_BIT, "spi_miso"),), "pmod_jb4"),
    6: (((EMIO_EDGE_SPI_BIT, "spi_cs"),), "pmod_jb5"),
}


def edge_lane_name(device_id: int, emio_mux_sel: int) -> str:
    """The signal EDGE device ``device_id`` is wired to under ``emio_mux_sel``.

    Resolves the same priority chain the RTL mux applies, so the name is the
    signal the edges actually come from, not a guess. KeyError for an id that
    is not an Edge Detector instance."""
    routes, fallback = _EDGE_ROUTING[device_id]
    for bit, name in routes:
        if emio_mux_sel & bit:
            return name
    return fallback


def edge_channels(emio_mux_sel: int) -> List[Dict[str, Any]]:
    """HELO descriptors for the edge lanes, one per Edge Detector instance.

    Each sits at id == its EDGE_EVENTS device_id, so RouteWave labels the lane
    by this name instead of ``edge_<id>``. width=0: an edge lane contributes no
    SAMPLES bits, its data arrives as EDGE_EVENTS."""
    return [{"id": dev, "name": edge_lane_name(dev, emio_mux_sel), "width": 0,
             "type": "Signal", "role": "Wire", "radix": "Binary"}
            for dev in EDGE_DEVICE_IDS]

CHANNEL_TOTAL_BITS: int = sum(c["width"] for c in CHANNELS)


def slice_offset(channel_id: int) -> int:
    """Return the LSB bit offset of `channel_id` inside the packed sample.

    Mirrors the consumer's slice math. 0 for level_pin[0], 8 for uart_rx, etc.
    Annotation-only channels (width=0) return the offset where they would
    have started (useful for diagnostic prints).
    """
    offset = 0
    for ch in CHANNELS:
        if ch["id"] == channel_id:
            return offset
        offset += ch["width"]
    raise KeyError(f"unknown channel_id={channel_id}")
