#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""
CLI Configurator for Deepskopion Datalogger System (DS-P2.114).

Selects target board and datalogger counts per protocol, validates against
board pin budgets and datalogger ID constraints, calculates estimated resource
utilization (LUT/FF/BRAM) calibrated against Vivado/Quartus fit reports, and emits
configuration YAML consumed by datalogger_gather_gen.py.

Supports both interactive prompt mode and non-interactive command-line flag mode.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import yaml

import datalogger_gather_gen as dgg  # same dir: owns the logical-pin -> board-pin map (DS-P1.66)

MAX_DATALOGGER_ID = 31
RESERVED_DEVICE_IDS = [0]
DEFAULT_OUTPUT_FILENAME = "datalogger_config.yml"

@dataclass
class ProtocolMeta:
    name: str              # Upper-case canonical name (e.g. "UART")
    yaml_key: str          # Title-case YAML block name (e.g. "Uart")
    signals: List[str]     # Pin signal names
    pins_per_instance: int
    dma_bw_mb: int         # Bandwidth per instance in MB/s
    # Xilinx 7-series (Zynq-7000) costs per instance
    xilinx_luts: int
    xilinx_ffs: int
    xilinx_bram_tiles: float
    # Altera Agilex-5 costs per instance
    altera_alms: int
    altera_ffs: int
    altera_m20k_blocks: float
    description: str
    has_rtl: bool = True   # True if RTL entity exists in hw/rtl/datalogger
    # Microchip PolarFire SoC costs per instance (DS-P3.156). None means no
    # PolarFire measurement exists yet: the configurator refuses the protocol
    # on a polarfire_soc board instead of estimating it.
    microchip_4luts: Optional[int] = None
    microchip_dffs: Optional[int] = None
    microchip_lsram_blocks: Optional[float] = None


# DS-P3.156 / DS-P2.331 PolarFire SoC per-instance costs. Source: Libero SoC 2025.2
# (Version 2025.2.0.14) compile netlist report
# deepskopion_pfsoc_top_compile_netlist_hier_resources.csv of the
# pfsoc_datalogger target at 43b88faa (MPFS095T FCSG325 -1, 2026-11-16, the default
# mix), committed as tests/fixtures/pfsoc_default_mix_hier_resources.csv. JTAG, which
# the default mix does not contain, keeps its cost from the 2026-11-05 build (git
# 5d496202 dirty, tests/fixtures/pfsoc_compile_netlist_hier_resources.csv).
# One instance = every row under GEN_<P>_BLOCK.GEN_<P>_DATALOGGERS.<n>.i_<p>_datalogger;
# 4LUT = Fabric 4LUT + Interface 4LUT, DFF = Fabric DFF + Interface DFF.
# Each value is the per-protocol MAXIMUM over the measured instances.
# uSRAM is not modelled (JTAG uses 2 uSRAM (64x12) per instance).


PROTOCOLS: Dict[str, ProtocolMeta] = {
    "can": ProtocolMeta(
        name="CAN",
        yaml_key="Can",
        signals=["RX"],
        pins_per_instance=1,
        dma_bw_mb=50,
        xilinx_luts=525,
        xilinx_ffs=400,
        xilinx_bram_tiles=0.0,
        altera_alms=530,
        altera_ffs=410,
        altera_m20k_blocks=0.0,
        description="CAN / CAN-FD datalogger (passive RX tap)",
        has_rtl=True,
        microchip_4luts=924,  # PolarFire hier CSV (43b88faa default mix), 1 instance: 4LUT 924, DFF 524
        microchip_dffs=524,
        microchip_lsram_blocks=0.0,
    ),
    "edge": ProtocolMeta(
        name="EDGE",
        yaml_key="Edge",
        signals=["LINE"],
        pins_per_instance=1,
        dma_bw_mb=100,
        xilinx_luts=128,  # Calibrated from hw/rtl/datalogger/nonstd/edge_datalogger/EDGE_DATALOGGER_V2_FDD.md:869
        xilinx_ffs=138,
        xilinx_bram_tiles=0.5,  # 1 RAMB18
        altera_alms=130,
        altera_ffs=150,
        altera_m20k_blocks=1.0,
        description="Raw edge / logic-analyzer capture",
        has_rtl=True,
        microchip_4luts=428,  # PolarFire hier CSV (43b88faa default mix), 3 instances: 4LUT 402..428, DFF 226, LSRAM 1
        microchip_dffs=226,
        microchip_lsram_blocks=1.0,
    ),
    "i2c": ProtocolMeta(
        name="I2C",
        yaml_key="I2c",
        signals=["SCL", "SDA"],
        pins_per_instance=2,
        dma_bw_mb=10,
        xilinx_luts=70,
        xilinx_ffs=120,
        xilinx_bram_tiles=0.0,
        altera_alms=70,
        altera_ffs=120,
        altera_m20k_blocks=0.0,
        description="I2C bus datalogger (SCL/SDA)",
        has_rtl=True,
        microchip_4luts=149,  # PolarFire hier CSV (43b88faa default mix), 2 instances: 4LUT 144..149, DFF 119
        microchip_dffs=119,
        microchip_lsram_blocks=0.0,
    ),
    "jtag": ProtocolMeta(
        name="JTAG",
        yaml_key="Jtag",
        signals=["TCK", "TMS", "TDI", "TDO"],
        pins_per_instance=4,
        dma_bw_mb=20,
        xilinx_luts=185,
        xilinx_ffs=150,
        xilinx_bram_tiles=0.0,
        altera_alms=185,
        altera_ffs=150,
        altera_m20k_blocks=0.0,
        description="JTAG passive TAP datalogger (4-wire)",
        has_rtl=True,
        # JTAG is not in the default mix: this cost is kept from the 2026-11-05 pfsoc_datalogger
        # build (2 UART, 2 I2C, 1 SPI, 1 JTAG, 4 EDGE), 1 instance: 4LUT 87, DFF 62 (+2 uSRAM).
        microchip_4luts=87,
        microchip_dffs=62,
        microchip_lsram_blocks=0.0,
    ),
    "spi": ProtocolMeta(
        name="SPI",
        yaml_key="Spi",
        signals=["SCLK", "CS", "MOSI", "MISO"],
        pins_per_instance=4,
        dma_bw_mb=50,
        xilinx_luts=75,
        xilinx_ffs=205,
        xilinx_bram_tiles=0.0,
        altera_alms=75,
        altera_ffs=205,
        altera_m20k_blocks=0.0,
        description="SPI bus datalogger (4-wire mode)",
        has_rtl=True,
        microchip_4luts=137,  # PolarFire hier CSV (43b88faa default mix), 1 instance: 4LUT 137, DFF 134
        microchip_dffs=134,
        microchip_lsram_blocks=0.0,
    ),
    "uart": ProtocolMeta(
        name="UART",
        yaml_key="Uart",
        signals=["TX"],
        pins_per_instance=1,
        dma_bw_mb=5,
        xilinx_luts=983,  # In-tree silicon measurement from hw/rtl/datalogger/uart/UART_DATALOGGER_FEATURES.md:668
        xilinx_ffs=885,   # Vivado 2024.1 synthesis: 983 LUT, 885 FF, 4 RAMB36 tiles
        xilinx_bram_tiles=4.0,
        altera_alms=940,
        altera_ffs=885,
        altera_m20k_blocks=4.0,
        description="UART datalogger with auto-baud detection",
        has_rtl=True,
        microchip_4luts=1889,  # PolarFire hier CSV (43b88faa default mix), 2 instances: 4LUT 1887..1889, DFF 1135..1151, LSRAM 6
        microchip_dffs=1151,
        microchip_lsram_blocks=6.0,
    ),
    "qspi": ProtocolMeta(
        name="QSPI",
        yaml_key="Qspi",
        signals=["SCLK", "CS", "IO0", "IO1", "IO2", "IO3"],
        pins_per_instance=6,
        dma_bw_mb=200,
        xilinx_luts=1200,
        xilinx_ffs=600,
        xilinx_bram_tiles=0.0,
        altera_alms=1200,
        altera_ffs=600,
        altera_m20k_blocks=0.0,
        description="Quad-SPI datalogger (6-pin)",
        has_rtl=True,
    ),
    "mdio": ProtocolMeta(
        name="MDIO",
        yaml_key="Mdio",
        signals=["MDC", "MDIO"],
        pins_per_instance=2,
        dma_bw_mb=5,
        xilinx_luts=0,
        xilinx_ffs=0,
        xilinx_bram_tiles=0.0,
        altera_alms=0,
        altera_ffs=0,
        altera_m20k_blocks=0.0,
        description="MDIO / MII management datalogger (no RTL implementation in hw/)",
        has_rtl=False,
    ),
    "i2s": ProtocolMeta(
        name="I2S",
        yaml_key="I2s",
        signals=["SCK", "WS", "SD"],
        pins_per_instance=3,
        dma_bw_mb=25,
        xilinx_luts=0,
        xilinx_ffs=0,
        xilinx_bram_tiles=0.0,
        altera_alms=0,
        altera_ffs=0,
        altera_m20k_blocks=0.0,
        description="I2S digital audio datalogger (no RTL implementation in hw/)",
        has_rtl=False,
    ),
    "onewire": ProtocolMeta(
        name="ONEWIRE",
        yaml_key="Onewire",
        signals=["DQ"],
        pins_per_instance=1,
        dma_bw_mb=2,
        xilinx_luts=0,
        xilinx_ffs=0,
        xilinx_bram_tiles=0.0,
        altera_alms=0,
        altera_ffs=0,
        altera_m20k_blocks=0.0,
        description="1-Wire bus datalogger (no RTL implementation in hw/)",
        has_rtl=False,
    ),
}


@dataclass
class BoardProfile:
    board_id: str
    name: str
    device: str
    family: str               # "zynq7000", "agilex5" or "polarfire_soc"
    logic_unit_name: str      # "LUT", "ALM" or "4LUT"
    logic_capacity: int
    ff_capacity: int
    bram_unit_name: str       # "Block RAM Tiles (36Kb)", "M20K Blocks" or "LSRAM (20 Kb)"
    bram_capacity: float
    pin_budget: int
    max_dma_bw_mb: Optional[int]  # None: no sourced interface figure for the board
    base_logic: int
    base_ff: int
    base_bram: float
    calibration_provenance: str
    # deepskopion_core capture sources (board_pins.yml names) that this board's
    # top ties to a constant, so the configurator never wires a signal to them.
    unwired_core_pins: Tuple[str, ...] = ()
    # Where this board's top ties deepskopion_core's REA tap inputs to
    # constants (file:line), or None when the tap is live. A JTAG datalogger
    # reads that tap (board_pins.yml REA_*), so on such a board it records
    # no live JTAG traffic.
    rea_tap_tied: Optional[str] = None


BOARDS: Dict[str, BoardProfile] = {
    "zybo-z7-10": BoardProfile(
        board_id="zybo-z7-10",
        name="Zybo Z7-10",
        device="xc7z010clg400-1",
        family="zynq7000",
        logic_unit_name="LUT",
        logic_capacity=17600,
        ff_capacity=35200,
        bram_unit_name="Block RAM Tiles (36Kb)",
        bram_capacity=60.0,
        pin_budget=32,
        max_dma_bw_mb=800,
        base_logic=22504,
        base_ff=33691,
        base_bram=33.0,
        calibration_provenance="Calibrated from in-tree silicon scoping (hw/rtl/datalogger/can/STEP7_SILICON_SCOPING.md: base design 22,504 LUT / 33,691 FF / 33 BRAM)",
    ),
    "zybo-z7-20": BoardProfile(
        board_id="zybo-z7-20",
        name="Zybo Z7-20",
        device="xc7z020clg400-1",
        family="zynq7000",
        logic_unit_name="LUT",
        logic_capacity=53200,
        ff_capacity=106400,
        bram_unit_name="Block RAM Tiles (36Kb)",
        bram_capacity=140.0,
        pin_budget=40,
        max_dma_bw_mb=1200,
        base_logic=23971,
        base_ff=32817,
        base_bram=10.5,
        calibration_provenance="Calibrated from Vivado 2024.1 postRoute report (Zybo CAN FD bench build 2026-08-03, git 5b3aad42, xc7z020clg400-1)",
    ),
    "de25-standard": BoardProfile(
        board_id="de25-standard",
        name="DE25-Standard",
        device="A5ED013BB32AE4SCS",
        family="agilex5",
        logic_unit_name="ALM",
        logic_capacity=46800,
        ff_capacity=93600,
        bram_unit_name="M20K Blocks",
        bram_capacity=358.0,
        pin_budget=36,
        max_dma_bw_mb=2000,
        base_logic=26010,
        base_ff=43516,
        base_bram=54.0,
        rea_tap_tied="hw/rtl/de25/deepskopion_de25_top.vhd:822",
        calibration_provenance="Calibrated from Quartus Prime Pro 25.3.1 Fitter report (de25_datalogger build 2026-09-03, A5ED013BB32AE4SCS)",
    ),
    # DS-P3.156 / DS-P2.331. Capacities: "Resource Usage" Total column of the Libero
    # SoC 2025.2 compile netlist report (4LUT 93516, DFF 93516, LSRAM 308) of the
    # 43b88faa build, committed as tests/fixtures/pfsoc_default_mix_resources.rpt.
    # Base = report total (4LUT 50446, DFF 25876, LSRAM 45) minus the
    # u_fabric/u_core/u_datalogger_gather subtree of the hier CSV (4LUT 8858, DFF 5519,
    # LSRAM 32), i.e. everything outside the generated datalogger fabric.
    # Pin budget: the 14 rpi_gpio capture inputs of
    # hw/rtl/pfsoc/deepskopion_pfsoc_fabric.vhd; core jb(6)/jb(7) are tied '0' there.
    # max_dma_bw_mb: no PolarFire DMA/FIC bandwidth figure exists in the repo
    # docs, and the existing boards' figures cite no source, so none is set.
    "polarfire-soc-discovery": BoardProfile(
        board_id="polarfire-soc-discovery",
        name="PolarFire SoC Discovery Kit",
        device="MPFS095T-1FCSG325E",
        family="polarfire_soc",
        logic_unit_name="4LUT",
        logic_capacity=93516,
        ff_capacity=93516,
        bram_unit_name="LSRAM (20 Kb)",
        bram_capacity=308.0,
        pin_budget=14,
        max_dma_bw_mb=None,
        base_logic=41588,
        base_ff=20357,
        base_bram=13.0,
        calibration_provenance=(
            "Calibrated from Libero SoC 2025.2 compile netlist report (pfsoc_datalogger build "
            "at 43b88faa, 2026-11-16, MPFS095T FCSG325 -1, the default mix: 2 UART, 2 I2C, "
            "1 SPI, 1 CAN FD, 3 EDGE); both regbank ROMs synthesized with their data. The JTAG "
            "cost is from the 2026-11-05 build (JTAG is not in the default mix)"
        ),
        unwired_core_pins=("JB6", "JB7"),
        rea_tap_tied="hw/rtl/pfsoc/deepskopion_pfsoc_fabric.vhd:793",
    ),
}

POLARFIRE_FAMILY = "polarfire_soc"


@dataclass
class ProtocolUtilization:
    protocol_key: str
    protocol_name: str
    count: int
    pins_per_inst: int
    pins_subtotal: int
    logic_per_inst: int
    logic_subtotal: int
    ff_per_inst: int
    ff_subtotal: int
    bram_per_inst: float
    bram_subtotal: float
    dma_bw_subtotal: int


@dataclass
class ResourceEstimate:
    board: BoardProfile
    protocol_breakdown: List[ProtocolUtilization]
    total_loggers: int
    total_pins: int
    gather_logic: int
    gather_ff: int
    gather_bram: float
    base_logic: int
    base_ff: int
    base_bram: float
    total_logic: int
    total_ff: int
    total_bram: float
    total_dma_bw: int
    logic_util_pct: float
    ff_util_pct: float
    bram_util_pct: float
    dma_util_pct: float
    calibration_provenance: str
    # Header pins the mix takes (header_pin_demand); None when it cannot be allocated.
    header_pins: Optional[int] = None


def normalize_board_id(name: str) -> Optional[str]:
    """Normalize input board string to a registered board_id."""
    key = name.strip().lower().replace("_", "-")
    if key in BOARDS:
        return key
    if key in ("zybo10", "z710", "z7-10", "zybo-10"):
        return "zybo-z7-10"
    if key in ("zybo20", "z720", "z7-20", "zybo-20", "zybo"):
        return "zybo-z7-20"
    if key in ("de25", "de25standard"):
        return "de25-standard"
    if key in ("pfsoc", "polarfire-soc", "mpfs-disco-kit"):
        return "polarfire-soc-discovery"
    return None


def get_supported_boards() -> Dict[str, BoardProfile]:
    return BOARDS


def get_board(board_id: str) -> BoardProfile:
    canonical = normalize_board_id(board_id)
    if not canonical:
        raise ValueError(f"Unknown board '{board_id}'. Supported boards: {', '.join(BOARDS.keys())}")
    return BOARDS[canonical]


def _is_internal_can_tap(bp: dict) -> bool:
    """board_pins.yml CAN_BUS: the core's wired-AND of its own CAN controllers' TX (a self-test source)."""
    return bp.get("general") is False and str(bp.get("prefer", "")).startswith("CAN.")


def board_capture_pins(board: BoardProfile) -> List[dict]:
    """deepskopion_core capture sources the board's top actually wires, for a user's mix.

    DS-P2.332: the internal CAN bus tap is left out. It carries the core's own CAN
    controllers, not the user's bus, so a configured CAN RX takes an external header
    pin like every other input (the DS-P1.104 default wires it to JB5).
    """
    return [bp for bp in dgg.load_board_pins()
            if bp["name"] not in board.unwired_core_pins and not _is_internal_can_tap(bp)]


def _header_pins_or_none(board: BoardProfile, protocol_counts: Dict[str, int]) -> Optional[int]:
    try:
        return header_pin_demand(board, protocol_counts)[0]
    except (ValueError, KeyError):
        return None


def header_pin_demand(board: BoardProfile, protocol_counts: Dict[str, int]):
    """(header pins the mix takes, unwired signals), from the generator's own allocation.

    A signal the allocator places on an internal tap (board_pins.yml
    `general: false`: the REA tap a JTAG datalogger reads, the fabric CAN
    bus) uses no header pin; a signal on a general pin does, and so does a
    signal left unwired. pins_per_instance counts signals, not header pins.
    """
    config = yaml.safe_load(generate_config_yaml(board, protocol_counts))
    board_pins = board_capture_pins(board)
    general = {bp["name"] for bp in board_pins if bp.get("general")}
    mapping, unwired = dgg.allocate_board_pins(config, board_pins)
    return sum(1 for name in mapping.values() if name in general) + len(unwired), unwired


def uncalibrated_protocol_error(board: BoardProfile, proto_key: str) -> Optional[str]:
    """DS-P3.156: refusal text when the board has no measured cost for the protocol."""
    if board.family != POLARFIRE_FAMILY:
        return None
    proto = PROTOCOLS[proto_key]
    if proto.microchip_4luts is not None and proto.microchip_dffs is not None \
            and proto.microchip_lsram_blocks is not None:
        return None
    return (
        f"Protocol '{proto_key}' has no PolarFire SoC calibration yet: the Libero SoC 2025.2 "
        f"pfsoc_datalogger compile report has no {proto.name} datalogger instance. "
        f"Refusing to estimate it for board '{board.name}'."
    )


def validate_config(
    board: BoardProfile,
    protocol_counts: Dict[str, int],
) -> Tuple[bool, List[str], List[str]]:
    """Validate protocol selection against board pin budget, ID limits, and RTL availability.

    Returns:
        (is_valid, error_messages, warning_messages)
    """
    errors: List[str] = []
    warnings: List[str] = []

    total_pins = 0
    total_loggers = 0

    for proto_key, count in protocol_counts.items():
        if count < 0:
            errors.append(f"Protocol '{proto_key}' count cannot be negative ({count}).")
            continue
        if count == 0:
            continue
        proto = PROTOCOLS.get(proto_key.lower())
        if not proto:
            errors.append(f"Unknown protocol '{proto_key}'.")
            continue
        if not proto.has_rtl:
            errors.append(
                f"Protocol '{proto_key}' has no RTL implementation under hw/ "
                f"(entity tich.{proto_key}_datalogger does not exist). Cannot generate build config."
            )
            continue
        uncalibrated = uncalibrated_protocol_error(board, proto_key.lower())
        if uncalibrated:
            errors.append(uncalibrated)
            continue
        total_loggers += count
        total_pins += count * proto.pins_per_instance

    if total_loggers == 0 and len(errors) == 0:
        errors.append("At least one datalogger instance must be configured.")

    if total_loggers > MAX_DATALOGGER_ID:
        errors.append(
            f"Max datalogger count exceeded: requested {total_loggers} loggers, "
            f"but maximum allowed is {MAX_DATALOGGER_ID} (device IDs 1..{MAX_DATALOGGER_ID}, ID 0 is reserved)."
        )

    # DS-P1.66: the pin budget counts the board's pins; the core can only
    # capture from the sources in board_pins.yml. Ask the generator, which
    # owns that map, so an accepted mix is one it will wire end to end.
    # DS-P3.203: the pin budget counts header pins, so it is checked on the
    # same allocation, where the REA tap and the CAN bus take none.
    if not errors:
        header, unwired = header_pin_demand(board, protocol_counts)
        if header > board.pin_budget:
            errors.append(
                f"Pin budget exceeded for board '{board.name}': requested {header} pins, "
                f"but board only has {board.pin_budget} pins available."
            )
        if unwired:
            errors.append(
                f"Core capture sources exceeded: {total_pins} signals requested, "
                f"{len(unwired)} left with no deepskopion_core capture source "
                f"({len(board_capture_pins(board))} board pins in board_pins.yml)."
            )

    # DS-P3.204: a JTAG datalogger reads the REA tap; say so where it is tied.
    if protocol_counts.get("jtag", 0) > 0 and board.rea_tap_tied:
        warnings.append(
            f"On {board.name} the JTAG datalogger reads the REA tap, which "
            f"{board.rea_tap_tied} ties to constants: it records no live JTAG traffic."
        )

    if board.base_logic > board.logic_capacity:
        warnings.append(
            f"Board '{board.name}' capacity ({board.logic_capacity:,} {board.logic_unit_name}s) is overflowed by base platform design alone "
            f"({board.base_logic:,} {board.logic_unit_name}s, {board.base_logic / board.logic_capacity:.2f}x capacity per in-tree silicon scoping)."
        )

    return (len(errors) == 0, errors, warnings)


def estimate_resources(
    board: BoardProfile,
    protocol_counts: Dict[str, int],
) -> ResourceEstimate:
    """Calculate estimated resource utilization (LUT/ALM, FF, BRAM) and DMA bandwidth."""
    breakdown: List[ProtocolUtilization] = []
    total_loggers = 0
    total_pins = 0
    proto_logic = 0
    proto_ff = 0
    proto_bram = 0.0
    total_dma = 0

    active_burst_types = 0
    has_edge = False

    for proto_key in sorted(PROTOCOLS.keys()):
        count = protocol_counts.get(proto_key, 0)
        if count <= 0:
            continue
        proto = PROTOCOLS[proto_key]
        pins_sub = count * proto.pins_per_instance
        total_pins += pins_sub
        total_loggers += count

        if board.family == "zynq7000":
            l_inst = proto.xilinx_luts
            f_inst = proto.xilinx_ffs
            b_inst = proto.xilinx_bram_tiles
        elif board.family == POLARFIRE_FAMILY:
            uncalibrated = uncalibrated_protocol_error(board, proto_key)
            if uncalibrated:
                raise ValueError(uncalibrated)
            l_inst = proto.microchip_4luts
            f_inst = proto.microchip_dffs
            b_inst = proto.microchip_lsram_blocks
        else:
            l_inst = proto.altera_alms
            f_inst = proto.altera_ffs
            b_inst = proto.altera_m20k_blocks

        l_sub = count * l_inst
        f_sub = count * f_inst
        b_sub = count * b_inst
        dma_sub = count * proto.dma_bw_mb

        proto_logic += l_sub
        proto_ff += f_sub
        proto_bram += b_sub
        total_dma += dma_sub

        active_burst_types += 1
        if proto_key == "edge":
            has_edge = True

        breakdown.append(
            ProtocolUtilization(
                protocol_key=proto_key,
                protocol_name=proto.name,
                count=count,
                pins_per_inst=proto.pins_per_instance,
                pins_subtotal=pins_sub,
                logic_per_inst=l_inst,
                logic_subtotal=l_sub,
                ff_per_inst=f_inst,
                ff_subtotal=f_sub,
                bram_per_inst=b_inst,
                bram_subtotal=b_sub,
                dma_bw_subtotal=dma_sub,
            )
        )

    # Calculate Arbiter and Gather Infrastructure overhead
    if total_loggers > 0:
        if board.family == "zynq7000":
            # Calibrated from hierarchical utilization report:
            # 2nd layer arbiter: ~100 + 65*types LUTs, ~50 + 47*types FFs, 1 + 1*types BRAM
            arb_luts = 100 + 65 * active_burst_types
            arb_ffs = 50 + 47 * active_burst_types
            arb_bram = 1.0 + 1.0 * active_burst_types

            edge_arb_luts = 267 if has_edge else 0
            edge_arb_ffs = 199 if has_edge else 0
            edge_arb_bram = 5.0 if has_edge else 0.0

            glue_luts = 536
            glue_ffs = 917
            glue_bram = 6.0

            gather_logic = arb_luts + edge_arb_luts + glue_luts
            gather_ff = arb_ffs + edge_arb_ffs + glue_ffs
            gather_bram = arb_bram + edge_arb_bram + glue_bram
        elif board.family == POLARFIRE_FAMILY:
            # DS-P2.331: rows under u_fabric/u_core/u_datalogger_gather of the Libero
            # SoC 2025.2 hier CSV of the 43b88faa build (4LUT = fabric + interface,
            # DFF = fabric + interface), summed with hier_calibration.rows_under.
            # Only one mix was measured (the default: 2 UART, 2 I2C, 1 SPI, 1 CAN FD,
            # 3 EDGE), so the base + per-type form cannot be fitted; these constants
            # are that build's measured totals.
            # GEN_ARB.i_2nd_layer_burst_arbiter (5 burst types): 844 / 501 / 7 LSRAM
            arb_luts = 844
            arb_ffs = 501
            arb_bram = 7.0
            # GEN_EDGE_BLOCK.GEN_EDGE_ARB.i_edge_arbiter (3 EDGE): 467 / 275 / 4 LSRAM
            edge_arb_luts = 467 if has_edge else 0
            edge_arb_ffs = 275 if has_edge else 0
            edge_arb_bram = 4.0 if has_edge else 0.0
            # Remaining gather rows: u_datalogger_gather/Primitives 390 / 384 / 0,
            # GEN_I2C_ARB.i_i2c_arbiter 363 / 218 / 3, GEN_UART_ARB.i_uart_arbiter
            # 370 / 218 / 3, GEN_*_PIN_MAP i_cond 49 / 63 / 0.
            glue_luts = 1172
            glue_ffs = 883
            glue_bram = 6.0

            gather_logic = arb_luts + edge_arb_luts + glue_luts
            gather_ff = arb_ffs + edge_arb_ffs + glue_ffs
            gather_bram = arb_bram + edge_arb_bram + glue_bram
        else:
            # Altera Agilex-5 / Quartus Pro calibration
            arb_alms = 100 + 65 * active_burst_types
            edge_arb_alms = 267 if has_edge else 0
            glue_alms = 536
            gather_logic = arb_alms + edge_arb_alms + glue_alms

            arb_ffs = 50 + 47 * active_burst_types
            edge_arb_ffs = 200 if has_edge else 0
            glue_ffs = 917
            gather_ff = arb_ffs + edge_arb_ffs + glue_ffs

            arb_m20k = 1.0 + 1.0 * active_burst_types
            edge_arb_m20k = 5.0 if has_edge else 0.0
            glue_m20k = 6.0
            gather_bram = arb_m20k + edge_arb_m20k + glue_m20k
    else:
        gather_logic = 0
        gather_ff = 0
        gather_bram = 0.0
        gather_ff = 0
        gather_bram = 0.0

    total_logic = board.base_logic + proto_logic + gather_logic
    total_ff = board.base_ff + proto_ff + gather_ff
    total_bram = board.base_bram + proto_bram + gather_bram

    return ResourceEstimate(
        board=board,
        protocol_breakdown=breakdown,
        total_loggers=total_loggers,
        total_pins=total_pins,
        header_pins=_header_pins_or_none(board, protocol_counts),
        gather_logic=gather_logic,
        gather_ff=gather_ff,
        gather_bram=gather_bram,
        base_logic=board.base_logic,
        base_ff=board.base_ff,
        base_bram=board.base_bram,
        total_logic=total_logic,
        total_ff=total_ff,
        total_bram=total_bram,
        total_dma_bw=total_dma,
        logic_util_pct=(total_logic / board.logic_capacity) * 100.0 if board.logic_capacity else 0.0,
        ff_util_pct=(total_ff / board.ff_capacity) * 100.0 if board.ff_capacity else 0.0,
        bram_util_pct=(total_bram / board.bram_capacity) * 100.0 if board.bram_capacity else 0.0,
        dma_util_pct=(total_dma / board.max_dma_bw_mb) * 100.0 if board.max_dma_bw_mb else 0.0,
        calibration_provenance=board.calibration_provenance,
    )


def generate_config_yaml(
    board: BoardProfile,
    protocol_counts: Dict[str, int],
) -> str:
    """Generate configuration YAML consumed by datalogger_gather_gen.py."""
    lines: List[str] = [
        f"# Datalogger System Configuration — Generated by Deepskopion CLI Configurator (DS-P2.114)",
        f"# Board : {board.name} ({board.device})",
        f"# Budget: {board.pin_budget} pins, {MAX_DATALOGGER_ID} max dataloggers",
        f"",
        f"Design: {board.name}",
        f"",
        f"Constraints:",
        f"  NumDataloggerPins: 64",
        f"  MaxDataloggerID: {MAX_DATALOGGER_ID}",
        f"",
        f"Dataloggers:",
        f"  Burst:",
    ]

    id_counter = 1
    pin_counter = 0

    # Sort protocols deterministically by yaml_key
    sorted_protos = sorted(
        [p for k, p in PROTOCOLS.items() if protocol_counts.get(k, 0) > 0],
        key=lambda x: x.yaml_key,
    )

    for proto in sorted_protos:
        count = protocol_counts[proto.name.lower()]
        lines.append(f"    {proto.yaml_key}:")
        for _ in range(count):
            current_id = id_counter
            id_counter += 1
            lines.append(f"      {current_id}:")
            for signal in proto.signals:
                lines.append(f"        {signal}: {pin_counter}")
                pin_counter += 1

    # DS-P2.332: pin each CAN RX to the header pin the configurator's allocation gives
    # it. Without a BoardPins entry the build's generator would put it on CAN_BUS.
    if any(protocol_counts.get(k, 0) > 0 for k in PROTOCOLS if PROTOCOLS[k].yaml_key == "Can"):
        config = yaml.safe_load("\n".join(lines) + "\n")
        mapping, _unwired = dgg.allocate_board_pins(config, board_capture_pins(board))
        can_pins = [(pin, mapping[pin]) for proto, _id, _sig, pin in dgg.configured_signals(config)
                    if proto == "CAN" and pin in mapping]
        if can_pins:
            lines.append("")
            lines.append("# CAN RX on an external header pin, not the internal CAN bus tap (DS-P2.332).")
            lines.append("BoardPins:")
            lines.extend(f"  {pin}: {name}" for pin, name in can_pins)

    lines.append("")
    return "\n".join(lines)


def format_wiring_report(board: BoardProfile, protocol_counts: Dict[str, int]) -> str:
    """DS-P1.66: where to wire each datalogger signal, from the generator's map."""
    config = yaml.safe_load(generate_config_yaml(board, protocol_counts))
    mapping, _unwired = dgg.allocate_board_pins(config, board_capture_pins(board))
    lines = [" Capture wiring (logical pin -> deepskopion_core board pin):"]
    for proto, datalogger_id, signal, pin in dgg.configured_signals(config):
        lines.append(f"   {proto:<5} id {datalogger_id:<3} {signal:<5} pin {pin:<3} -> {mapping.get(pin, 'UNWIRED')}")
    return "\n".join(lines)


def format_utilization_report(est: ResourceEstimate) -> str:
    """Format estimated resource utilization report into a clear console table."""
    board = est.board
    lines = [
        "==========================================================================================",
        f" DEEPSKOPION RESOURCE UTILIZATION ESTIMATE — {board.name} ({board.device})",
        "==========================================================================================",
        f" Calibration Provenance: {est.calibration_provenance}",
        "------------------------------------------------------------------------------------------",
        f" {'Component / Protocol':<26} {'Inst':<5} {'Pins':<6} {board.logic_unit_name + 's':<10} {'Registers':<11} {board.bram_unit_name:<16} {'DMA (MB/s)':<10}",
        "------------------------------------------------------------------------------------------",
    ]

    for p in est.protocol_breakdown:
        bram_str = f"{p.bram_subtotal:.1f}" if p.bram_subtotal > 0 else "-"
        lines.append(
            f" {p.protocol_name:<26} {p.count:<5} {p.pins_subtotal:<6} {p.logic_subtotal:<10} {p.ff_subtotal:<11} {bram_str:<16} {p.dma_bw_subtotal:<10}"
        )

    lines.append("------------------------------------------------------------------------------------------")
    bram_gather = f"{est.gather_bram:.1f}" if est.gather_bram > 0 else "-"
    lines.append(
        f" {'Gather & Arbiter Fabric':<26} {'-':<5} {'-':<6} {est.gather_logic:<10} {est.gather_ff:<11} {bram_gather:<16} {'-':<10}"
    )
    bram_base = f"{est.base_bram:.1f}" if est.base_bram > 0 else "-"
    lines.append(
        f" {'Base Platform Overhead':<26} {'-':<5} {'-':<6} {est.base_logic:<10} {est.base_ff:<11} {bram_base:<16} {'-':<10}"
    )
    lines.append("==========================================================================================")

    total_logic_str = f"{est.total_logic:,} / {board.logic_capacity:,} ({est.logic_util_pct:.1f}%)"
    total_ff_str = f"{est.total_ff:,} / {board.ff_capacity:,} ({est.ff_util_pct:.1f}%)"
    total_bram_str = f"{est.total_bram:.1f} / {board.bram_capacity:.0f} ({est.bram_util_pct:.1f}%)"
    pins_used = est.header_pins if est.header_pins is not None else est.total_pins
    total_pins_str = f"{pins_used} / {board.pin_budget} ({pins_used/board.pin_budget*100.0:.1f}%)"
    total_id_str = f"{est.total_loggers} / {MAX_DATALOGGER_ID}"
    if board.max_dma_bw_mb:
        total_dma_str = f"{est.total_dma_bw} / {board.max_dma_bw_mb} MB/s ({est.dma_util_pct:.1f}%)"
    else:
        total_dma_str = f"{est.total_dma_bw} MB/s (no sourced interface capacity for this board)"

    lines.append(f" Total {board.logic_unit_name} Utilization   : {total_logic_str}")
    lines.append(f" Total Register Utilization: {total_ff_str}")
    lines.append(f" Total Memory Utilization  : {total_bram_str}")
    lines.append(f" Pin Budget Usage          : {total_pins_str}")
    lines.append(f" Datalogger ID Allocation  : {total_id_str}")
    lines.append(f" Estimated DMA Throughput  : {total_dma_str}")
    lines.append("==========================================================================================")

    if est.total_logic > board.logic_capacity:
        lines.append(f" ⚠️  WARNING: Estimated logic exceeds board capacity by {est.total_logic - board.logic_capacity:,} {board.logic_unit_name}s!")
    if est.total_bram > board.bram_capacity:
        lines.append(f" ⚠️  WARNING: Estimated memory exceeds board capacity by {est.total_bram - board.bram_capacity:.1f} {board.bram_unit_name}!")
    if board.max_dma_bw_mb and est.total_dma_bw > board.max_dma_bw_mb:
        lines.append(f" ⚠️  WARNING: Aggregate datalogger DMA demand ({est.total_dma_bw} MB/s) exceeds interface capacity ({board.max_dma_bw_mb} MB/s)!")

    return "\n".join(lines)


def parse_vivado_report(report_path: Path) -> Dict[str, float]:
    """Parse Slice LUTs, Slice Registers, and Block RAM from Vivado utilization report."""
    text = report_path.read_text(encoding="utf-8")
    metrics: Dict[str, float] = {}

    lut_match = re.search(r"\|\s*Slice LUTs\s*\|\s*(\d+)\s*\|", text)
    if lut_match:
        metrics["luts"] = int(lut_match.group(1))

    ff_match = re.search(r"\|\s*Slice Registers\s*\|\s*(\d+)\s*\|", text)
    if ff_match:
        metrics["ffs"] = int(ff_match.group(1))

    bram_match = re.search(r"\|\s*Block RAM Tile\s*\|\s*([\d\.]+)\s*\|", text)
    if bram_match:
        metrics["bram_tiles"] = float(bram_match.group(1))

    return metrics


def parse_quartus_report(report_path: Path) -> Dict[str, float]:
    """Parse ALMs, Registers, and M20K RAM Blocks from Quartus fit summary report."""
    text = report_path.read_text(encoding="utf-8")
    metrics: Dict[str, float] = {}

    alm_match = re.search(r";\s*Logic utilization \(in ALMs\)\s*;\s*([\d,]+)\s*/", text)
    if alm_match:
        metrics["alms"] = int(alm_match.group(1).replace(",", ""))

    ff_match = re.search(r";\s*Total dedicated logic registers\s*;\s*([\d,]+)", text)
    if ff_match:
        metrics["ffs"] = int(ff_match.group(1).replace(",", ""))

    ram_match = re.search(r";\s*Total RAM Blocks\s*;\s*([\d,]+)\s*/", text)
    if ram_match:
        metrics["m20k_blocks"] = int(ram_match.group(1).replace(",", ""))

    return metrics


def parse_libero_report(report_path: Path) -> Dict[str, float]:
    """Parse 4LUT, DFF and LSRAM from a Libero SoC compile netlist resources report."""
    text = report_path.read_text(encoding="utf-8", errors="replace")
    metrics: Dict[str, float] = {}

    lut_match = re.search(r"^\|\s*4LUT\s*\|\s*(\d+)\s*\|", text, re.M)
    if lut_match:
        metrics["4luts"] = int(lut_match.group(1))

    ff_match = re.search(r"^\|\s*DFF\s*\|\s*(\d+)\s*\|", text, re.M)
    if ff_match:
        metrics["dffs"] = int(ff_match.group(1))

    lsram_match = re.search(r"^\|\s*LSRAM\s*\|\s*(\d+)\s*\|", text, re.M)
    if lsram_match:
        metrics["lsram_blocks"] = int(lsram_match.group(1))

    return metrics


INTERACTIVE_ORDER = [
    p for p in ["uart", "i2c", "spi", "qspi", "can", "edge", "jtag"]
    if PROTOCOLS[p].has_rtl
]


def run_interactive(default_output: Optional[str] = None) -> Tuple[BoardProfile, Dict[str, int], str]:
    """Run prompt-based interactive configuration wizard."""
    print("=================================================================")
    print(" Deepskopion Datalogger System CLI Configurator (DS-P2.114)")
    print("=================================================================")
    print("Select Target Board:")
    board_keys = list(BOARDS.keys())
    for idx, key in enumerate(board_keys, 1):
        b = BOARDS[key]
        dma = f"{b.max_dma_bw_mb} MB/s DMA" if b.max_dma_bw_mb else "DMA capacity not sourced"
        print(f"  {idx}. {b.name} ({b.logic_capacity:,} {b.logic_unit_name}s, {b.pin_budget} pins, {dma})")

    selected_board: Optional[BoardProfile] = None
    while selected_board is None:
        choice = input(f"Choice [1-{len(board_keys)}]: ").strip()
        if not choice:
            continue
        try:
            choice_idx = int(choice) - 1
            if 0 <= choice_idx < len(board_keys):
                selected_board = BOARDS[board_keys[choice_idx]]
            else:
                print(f"Invalid choice. Enter a number between 1 and {len(board_keys)}.")
        except ValueError:
            canonical = normalize_board_id(choice)
            if canonical:
                selected_board = BOARDS[canonical]
            else:
                print(f"Invalid input '{choice}'. Enter a valid board number or name.")

    print(f"\nConfiguring dataloggers for {selected_board.name} (Pin budget: {selected_board.pin_budget} pins):")
    protocol_counts: Dict[str, int] = {}

    while True:
        protocol_counts.clear()
        for proto_key in INTERACTIVE_ORDER:
            proto = PROTOCOLS[proto_key]
            prompt_str = f"  Number of {proto.name:<8} dataloggers ({proto.pins_per_instance} pin/ea) [0]: "
            val_str = input(prompt_str).strip()
            if not val_str:
                count = 0
            else:
                try:
                    count = int(val_str)
                except ValueError:
                    print("  Invalid number. Defaulting to 0.")
                    count = 0
            protocol_counts[proto_key] = max(0, count)

        is_valid, errors, _ = validate_config(selected_board, protocol_counts)
        if is_valid:
            break
        print("\n❌ Configuration Validation Failed:")
        for err in errors:
            print(f"   • {err}")
        print("\nPlease re-enter protocol counts within board constraints.\n")

    if default_output:
        output_path = default_output
    else:
        output_path = input(f"\nOutput configuration file [{DEFAULT_OUTPUT_FILENAME}]: ").strip()
        if not output_path:
            output_path = DEFAULT_OUTPUT_FILENAME

    return selected_board, protocol_counts, output_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deepskopion Datalogger CLI Configurator (DS-P2.114)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--board",
        "-b",
        help="Target board (zybo-z7-10, zybo-z7-20, de25-standard, polarfire-soc-discovery)",
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Run interactive prompt configuration wizard",
    )
    parser.add_argument(
        "--output",
        "-o",
        help=f"Output YAML configuration path (default: {DEFAULT_OUTPUT_FILENAME})",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress console utilization report table",
    )
    parser.add_argument(
        "--check-fit",
        help="Path to real fit report (utilization.rpt or deepskopion.fit.rpt) to verify calibration",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.05,
        help="Acceptable tolerance for fit report check (default: 0.05 = 5%%)",
    )

    # Protocol count arguments
    for proto_key, proto in PROTOCOLS.items():
        parser.add_argument(
            f"--{proto_key}",
            type=int,
            default=0,
            help=f"Number of {proto.name} datalogger instances ({proto.description})",
        )
    # Support --canfd alias for CAN
    parser.add_argument(
        "--canfd",
        type=int,
        default=0,
        help="Alias for --can",
    )

    args = parser.parse_args(argv)

    # Determine interactive mode:
    # Run interactive if explicit flag, or if no board and no protocol flags given and stdin is a tty
    has_flags = (
        args.board is not None
        or any(getattr(args, p) > 0 for p in PROTOCOLS.keys())
        or args.canfd > 0
    )

    if args.interactive or (not has_flags and sys.stdin.isatty()):
        selected_board, protocol_counts, output_path = run_interactive(default_output=args.output)
    else:
        if not args.board:
            sys.stderr.write("Error: Target board must be specified with --board <board_id>.\n")
            sys.stderr.write(f"Supported boards: {', '.join(BOARDS.keys())}\n")
            return 1

        try:
            selected_board = get_board(args.board)
        except ValueError as err:
            sys.stderr.write(f"Error: Unknown board '{args.board}'. {err}\n")
            return 1

        protocol_counts = {p: getattr(args, p) for p in PROTOCOLS.keys()}
        can_count = protocol_counts.get("can", 0)
        if args.canfd > 0:
            if can_count > 0 and can_count != args.canfd:
                sys.stderr.write("Error: Conflicting arguments --can and --canfd. --canfd is an alias for --can.\n")
                return 1
            can_count = args.canfd
        protocol_counts["can"] = can_count

        output_path = args.output if args.output else DEFAULT_OUTPUT_FILENAME

        is_valid, errors, warnings = validate_config(selected_board, protocol_counts)
        if not is_valid:
            for err in errors:
                sys.stderr.write(f"Error: {err}\n")
            return 1

        if warnings:
            for warn in warnings:
                sys.stderr.write(f"Warning: {warn}\n")

    # Calculate resource utilization
    est = estimate_resources(selected_board, protocol_counts)

    if not args.quiet:
        print(format_utilization_report(est))
        print(format_wiring_report(selected_board, protocol_counts))

    # Fit report calibration verification if requested
    if args.check_fit:
        rpt_path = Path(args.check_fit)
        if not rpt_path.exists():
            sys.stderr.write(f"Error: Fit report not found: {rpt_path}\n")
            return 1

        if selected_board.family == "zynq7000":
            real_metrics = parse_vivado_report(rpt_path)
            metric_specs = [
                ("LUT", "luts", est.total_logic),
                ("Register", "ffs", est.total_ff),
                ("Block RAM Tile", "bram_tiles", est.total_bram),
            ]
        elif selected_board.family == POLARFIRE_FAMILY:
            real_metrics = parse_libero_report(rpt_path)
            metric_specs = [
                ("4LUT", "4luts", est.total_logic),
                ("DFF", "dffs", est.total_ff),
                ("LSRAM", "lsram_blocks", est.total_bram),
            ]
        else:
            real_metrics = parse_quartus_report(rpt_path)
            metric_specs = [
                ("ALM", "alms", est.total_logic),
                ("Register", "ffs", est.total_ff),
                ("RAM Block", "m20k_blocks", est.total_bram),
            ]

        has_fit_error = False
        for metric_name, key, est_val in metric_specs:
            real_val = real_metrics.get(key)
            if real_val is None:
                sys.stderr.write(f"Error: Could not extract {metric_name} metrics from {rpt_path}\n")
                return 1

            rel_diff = abs(est_val - real_val) / real_val
            print(f"Fit check: Estimated {est_val} vs Real {real_val} {metric_name}s ({rel_diff*100:.2f}% diff, tolerance {args.tolerance*100:.1f}%)")
            if rel_diff > args.tolerance:
                sys.stderr.write(
                    f"Error: Estimate {est_val} deviates from real report {real_val} for {metric_name} by {rel_diff*100:.2f}%, "
                    f"exceeding tolerance {args.tolerance*100:.1f}%\n"
                )
                has_fit_error = True

        if has_fit_error:
            return 1

    # Emit YAML configuration
    yaml_content = generate_config_yaml(selected_board, protocol_counts)
    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(yaml_content, encoding="utf-8")
    print(f"✅ Configuration successfully written to {out_p}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
