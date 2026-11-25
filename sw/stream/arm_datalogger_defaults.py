# rr unknown-llm (AI-generated)
"""Boot-time datalogger config arming (DS-P2.77).

WHY THIS EXISTS. Nothing configures the UART datalogger at power-on: config_control
latches ``(others => '0')`` out of reset, so device 12 comes up DISABLED and
stays that way until software writes DATALOGGER_CONFIG. Every UART line on this
board is 8N1, so the framing has to be written by something — and it must not be
a demo script. Arming it at boot is right on its own terms; the rest of this note
records what is PROVEN and what is not, because a wrong story here already cost a
misfiled ticket once.

MEASURED, and it corrects this file's own first draft (DS-P2.77, 2026-08-10, on
the canfd_demo Z7-20): the unarmed board reads DATALOGGER_CONFIG = 0x00000000, NOT
0x6001. 0x6001 is not a power-on default and never was — it is what
``enable_datalogger(12)`` WRITES when it enables device 12 with extra=0 (see
testbench/uart_autosync_test.py, where the phrase originates). It is a
post-script state. The claim travelled from that verdict string into a ticket,
this docstring and its own test without anyone reading the register; the neighbours
(GIT_HASH, SYSTEM_VERSION, HEARTBEAT_CNT) all read live values, so the zero is a
real reading and not a dead aperture, and the RTL initialiser agrees.

That makes the arming MORE necessary, not less: unarmed, the datalogger is not
mis-framed, it is off.

PROVEN: driving real traffic with extra=6 (8N1) yields a clean decode —
519 bytes, 0 stop errors, matching the curated demo sample. Arming the correct
framing at boot is right on its own terms.

RESOLVED, and it was NOT this: the "43% UART error rate" that originally
motivated this module (DS-P2.75) was never fabrication. It was the daemon's
bounded backfill snapshot ring (DS-P3.63/64) replaying REAL events from an
earlier, genuinely mis-framed era to every new subscriber. Three captures at
5N1, at 8N1, and with the UART deselected from the EMIO mux entirely all began
at the same timestamp and two were byte-identical — hardware state cannot change
replayed history. See DS-P3.136 (closed) and DS-P2.80.

What this module fixes for certain: the deployed system no longer depends on a
DEMO STIMULUS SCRIPT (demo_config1_drive.py) to set a correctness-critical
framing default — a script that was not even present on the deployed rootfs.

Import-light and side-effect-free on import so it is unit-testable without a
board — main() is the only thing that touches hardware.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

CONFIG_NUM_WORDS_OFFSET = 0x0018
CONFIG_DATA_OFFSET = 0x001C
DATALOGGER_CONFIG_OFFSET = 0x0020
EMIO_MUX_SEL_OFFSET = 0x0024

# config_schema.hex ROM format versions (word 0). The generator is
# tools/datalogger-builder/datalogger_gather_gen.py. The version is carried in the
# magic because proto_id means different things in each:
#   v01  0xC0A1DE01  proto_id = index into the ALPHABETICAL list of the protocols
#                    (edge=0, i2c=1, jtag=2, spi=3, uart=4). Every bitstream built
#                    before DS-P2.162 carries this.
#   v02  0xC0A1DE02  proto_id = the protocol's code in system_ids.yml
#                    (uart=1, i2c=2, edge=6, jtag=8, ...).
# A v01 ROM read with the v02 table arms the I2C dataloggers as UART and leaves the
# real UART unarmed, so the two are never interchangeable and any other non-zero magic
# is refused (ConfigSchemaError), never defaulted.
# Magic 0x00000000 indicates an unpopulated/uninitialised BRAM ROM (or an image built
# without the ROM), which falls back to DEFAULT_ARMING (DS-D.47).
CONFIG_SCHEMA_MAGIC_V01 = 0xC0A1DE01
CONFIG_SCHEMA_MAGIC_V02 = 0xC0A1DE02
CONFIG_SCHEMA_MAGIC = CONFIG_SCHEMA_MAGIC_V02   # what the generator writes today

# Device ids as wired in this design (mirrors demo_config1_drive.py).
UART_ID = 12

# extra=6 → autoSync=0, dataBits=3 → 8 data bits, no parity, 1 stop = 8N1.
# NB autoSync=1 (extra=1) is NOT used: on this silicon it calibrates and then
# yields no frames (see uart_autosync_test.py), so explicit 8N1 is the honest
# choice. Revisit if the autoSync path is fixed.
UART_EXTRA_8N1 = 6

# (device_id, extra, human description) — arm order is the write order.
DEFAULT_ARMING: List[Tuple[int, int, str]] = [
    (UART_ID, UART_EXTRA_8N1, "UART 8N1 (lines are 8N1; unarmed the datalogger is OFF, cfg=0x0000)"),
]


try:
    from sw.stream.zs_msg import DOMAIN_NAMES
    PROTOCOL_TABLE: Dict[int, str] = {code: name.lower() for code, name in DOMAIN_NAMES.items()}
except Exception:
    PROTOCOL_TABLE: Dict[int, str] = {
        0x0: "sys",
        0x1: "uart",
        0x2: "i2c",
        0x3: "spi",
        0x4: "i2s",
        0x5: "qspi",
        0x6: "edge",
        0x7: "trig",
        0x8: "jtag",
        0x9: "mdio",
        0xA: "can",
        0xB: "onewire",
        0xC: "lin",
        0xD: "eye",
    }


# v01 proto_id -> name: the alphabetical index the pre-DS-P2.162 generator wrote
# (sorted(protocols)). Frozen: it describes ROMs already baked into bitstreams, and
# is only ever valid for a v01 magic. Index = position in the sorted ACTIVE set, so
# this is exact for the shipped mix (all five protocols present) — the only v01
# ROMs that exist.
PROTOCOL_TABLE_V01: Dict[int, str] = {0: "edge", 1: "i2c", 2: "jtag", 3: "spi", 4: "uart"}


class ConfigSchemaError(ValueError):
    """The config_schema ROM cannot be decoded (unknown magic). Never defaulted."""


def protocol_table_for_magic(magic: int) -> Dict[int, str]:
    """proto_id -> name table for a ROM magic; ConfigSchemaError on an unknown magic."""
    magic &= 0xFFFFFFFF
    if magic == CONFIG_SCHEMA_MAGIC_V01:
        return PROTOCOL_TABLE_V01
    if magic == CONFIG_SCHEMA_MAGIC_V02:
        return PROTOCOL_TABLE
    raise ConfigSchemaError(
        f"config_schema ROM magic 0x{magic:08X} is unknown (known: "
        f"0x{CONFIG_SCHEMA_MAGIC_V01:08X} v01, 0x{CONFIG_SCHEMA_MAGIC_V02:08X} v02); "
        "refusing to guess the datalogger protocol IDs"
    )


def parse_config_schema_words(words: Sequence[int]) -> List[Tuple[int, int, int, int]]:
    """Parse raw 32-bit config_schema words into [(proto_id, datalogger_id, pin_count, flags), ...].

    proto_id is version-specific (see CONFIG_SCHEMA_MAGIC_V01/V02); decode it with
    protocol_table_for_magic(words[0]). Raises ConfigSchemaError on an unknown magic.
    """
    if not words:
        return []
    protocol_table_for_magic(int(words[0]))
    if len(words) < 2:
        return []
    total = int(words[1])
    entries = []
    for w in words[2 : 2 + total]:
        w_int = int(w) & 0xFFFFFFFF
        proto_id = (w_int >> 24) & 0xFF
        datalogger_id = (w_int >> 16) & 0xFF
        pin_count = (w_int >> 8) & 0xFF
        flags = w_int & 0xFF
        entries.append((proto_id, datalogger_id, pin_count, flags))
    return entries


def parse_config_schema(
    source: Union[str, Path, Sequence[int]],
    active_protocols: Optional[Sequence[str]] = None,
) -> Dict[str, List[int]]:
    """Parse a config_schema (.hex file path, hex text, or word list) into {proto_name: [ids...]}.

    proto_id from schema words is mapped through the table matching the ROM's magic
    (v01 alphabetical, v02 canonical PROTOCOL_TABLE) without guessing; an unknown magic raises
    ConfigSchemaError.
    If active_protocols is provided, the output dict is initialized with those protocol names.
    """
    if isinstance(source, (list, tuple)):
        words = [int(w) for w in source]
    else:
        p = Path(str(source))
        if p.is_file():
            text = p.read_text(encoding="utf-8")
        else:
            text = str(source)
        words = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            words.append(int(line, 16))

    entries = parse_config_schema_words(words)
    if not entries:
        return {}
    table = protocol_table_for_magic(int(words[0]))

    out: Dict[str, List[int]] = {}
    if active_protocols:
        for p in active_protocols:
            out[p.lower()] = []

    for proto_id, dev_id, _pins, _flags in entries:
        if proto_id in table:
            proto_name = table[proto_id]
        else:
            proto_name = f"unknown_{proto_id}"
        out.setdefault(proto_name, []).append(dev_id)
    return out


def read_uart_ids_from_config_schema(
    source: Union[str, Path, Sequence[int]],
    active_protocols: Optional[Sequence[str]] = None,
) -> List[int]:
    """Extract UART datalogger IDs from a config_schema (.hex or words)."""
    parsed = parse_config_schema(source, active_protocols=active_protocols)
    return list(parsed.get("uart", []))


def read_uart_ids_from_regbank(
    regbank,
    active_protocols: Optional[Sequence[str]] = None,
) -> Optional[List[int]]:
    """Read UART datalogger IDs from regbank streaming ROM (CONFIG_NUM_WORDS/CONFIG_DATA).

    Returns:
        List[int] of discovered UART datalogger IDs (can be empty [] if the schema
        is valid but contains zero UART dataloggers), or None if the ROM cannot be read
        or is unpopulated (magic 0x00000000, DS-D.47).
    Raises:
        ConfigSchemaError if the ROM is readable but its magic is not a known version.
    """
    try:
        _ = regbank.read_u32(CONFIG_NUM_WORDS_OFFSET)
        magic = int(regbank.read_u32(CONFIG_DATA_OFFSET)) & 0xFFFFFFFF
    except Exception:
        return None
    if magic == 0:
        return None
    protocol_table_for_magic(magic)   # unknown non-zero magic: refuse, do not fall back
    try:
        total = int(regbank.read_u32(CONFIG_DATA_OFFSET))
        words = [magic, total]
        for _ in range(total):
            words.append(int(regbank.read_u32(CONFIG_DATA_OFFSET)))
    except Exception:
        return None
    return read_uart_ids_from_config_schema(words, active_protocols=active_protocols)


def extract_uart_ids_from_helo(helo_or_channels: Union[Dict[str, Any], List[Dict[str, Any]]]) -> List[int]:
    """Extract UART datalogger IDs from a HELO declaration dict or channels list."""
    if isinstance(helo_or_channels, dict):
        channels = helo_or_channels.get("channels", [])
    elif isinstance(helo_or_channels, list):
        channels = helo_or_channels
    else:
        return []

    ids = set()
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        name = str(ch.get("name", "")).lower()
        proto = str(ch.get("protocol", "")).lower()
        if "uart" in name or proto == "uart":
            if "datalogger_id" in ch:
                ids.add(int(ch["datalogger_id"]))
            elif ":" in name:
                part = name.split(":")[-1]
                if part.isdigit():
                    ids.add(int(part))
            elif ch.get("id") is not None and ch.get("width", 0) > 0:
                ids.add(int(ch["id"]))
    return sorted(ids)


def extract_uart_ids_from_registry(registry: Any) -> Optional[List[int]]:
    """Extract UART datalogger IDs from a datalogger_registry module, object, or dict.

    Returns:
        List[int] if the registry defines UART IDs, or None if no UART definition is present.
    """
    if registry is None:
        return None
    if isinstance(registry, dict):
        if "uart" in registry:
            val = registry["uart"]
            return [val] if isinstance(val, int) else list(val)
        if "DATALOGGER_IDS" in registry:
            dids = registry["DATALOGGER_IDS"]
            if isinstance(dids, dict) and "uart" in dids:
                val = dids["uart"]
                return [val] if isinstance(val, int) else list(val)
        if "UART_IDS" in registry:
            return list(registry["UART_IDS"])
    if hasattr(registry, "DATALOGGER_IDS"):
        dids = getattr(registry, "DATALOGGER_IDS")
        if isinstance(dids, dict) and "uart" in dids:
            val = dids["uart"]
            return [val] if isinstance(val, int) else list(val)
    if hasattr(registry, "UART_IDS"):
        return list(getattr(registry, "UART_IDS"))
    return None


def discover_uart_ids(
    regbank: Any = None,
    config_hex: Optional[Union[str, Path, Sequence[int]]] = None,
    registry: Any = None,
    helo: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
    active_protocols: Optional[Sequence[str]] = None,
) -> Optional[List[int]]:
    """Discover UART datalogger IDs from live regbank streaming ROM, config_schema.hex,
    datalogger_registry, or HELO declaration without hardcoding.

    Returns:
        List[int] of discovered IDs (can be empty [] if configuration explicitly has 0 UARTs),
        or None if no discovery source is present/valid.
    Raises:
        ValueError if an explicit registry or HELO declaration conflicts with live schema ROM.
    """
    live_ids: Optional[List[int]] = None

    if config_hex is not None:
        live_ids = read_uart_ids_from_config_schema(config_hex, active_protocols=active_protocols)
    elif regbank is not None:
        live_ids = read_uart_ids_from_regbank(regbank, active_protocols=active_protocols)

    if live_ids is None:
        env_hex = os.environ.get("DS_CONFIG_SCHEMA_HEX")
        if env_hex and Path(env_hex).is_file():
            live_ids = read_uart_ids_from_config_schema(Path(env_hex), active_protocols=active_protocols)

    # Validate registry against live configuration schema if both are provided
    if registry is not None:
        reg_ids = extract_uart_ids_from_registry(registry)
        if reg_ids is not None:
            if live_ids is not None and sorted(reg_ids) != sorted(live_ids):
                raise ValueError(
                    f"Registry UART IDs ({reg_ids}) mismatch live configuration schema UART IDs ({live_ids})"
                )
            if live_ids is None:
                return sorted(reg_ids)

    # Validate HELO declaration against live configuration schema if both are provided
    if helo is not None:
        helo_ids = extract_uart_ids_from_helo(helo)
        if live_ids is not None and sorted(helo_ids) != sorted(live_ids):
            raise ValueError(
                f"HELO declaration UART IDs ({helo_ids}) mismatch live configuration schema UART IDs ({live_ids})"
            )
        if live_ids is None and helo_ids:
            return sorted(helo_ids)

    return live_ids


def build_default_arming(uart_ids: Optional[Sequence[int]] = None) -> List[Tuple[int, int, str]]:
    """Build the arming tuple list [(dev_id, extra, description), ...] for UART IDs."""
    if uart_ids is None:
        return list(DEFAULT_ARMING)
    return [
        (dev_id, UART_EXTRA_8N1, f"UART 8N1 (dev={dev_id}; lines are 8N1; unarmed the datalogger is OFF, cfg=0x0000)")
        for dev_id in sorted(set(uart_ids))
    ]


def encode_datalogger_config(device_id: int, enable: bool = True, extra: int = 0) -> int:
    """Pack a DATALOGGER_CONFIG broadcast word: [device_id (5b) | config (11b)],
    config[0]=enable, config[10:1]=device-specific extras.

    Kept local rather than imported from testbench.emio_inject: this runs at
    boot, on every board, and must not depend on the testbench package being
    deployed — which is precisely the coupling that let DS-P2.77 happen.
    """
    if not 0 <= device_id < 32:
        raise ValueError(f"device_id {device_id} out of 5-bit range")
    cfg = (extra & 0x3FF) << 1
    if enable:
        cfg |= 0x1
    return ((device_id & 0x1F) << 11) | (cfg & 0x07FF)


def describe_config(word: int) -> str:
    """Human-readable decode of a DATALOGGER_CONFIG word, for the boot log."""
    return (
        f"0x{word:04X} dev={(word >> 11) & 0x1F} enable={word & 1} "
        f"autoSync={(word >> 1) & 1} dataBits={((word >> 2) & 7) + 5} "
        f"parityEn={(word >> 5) & 1} stopBits={(word >> 7) & 0xF}"
    )


def _mss_fabric_requested() -> bool:
    """True on the PolarFire fabric path, without importing testbench.

    ``DS_MSS_FABRIC=1`` (the pfsoc init script exports this from
    ``ds_mss_fabric=1``) or that cmdline token when the variable is unset.
    ``DS_MSS_FABRIC=0`` and an unset variable with no token — Zybo and DE25 —
    are false. The decision is local because ``de_c0_stimulus_contract`` lives
    under ``sw/stream/testbench``, which the DE25 rootfs does not ship
    (``tools/de25_linux_image.py`` excludes ``testbench``).
    """
    flag = os.environ.get("DS_MSS_FABRIC")
    if flag == "1":
        return True
    if flag == "0":
        return False
    try:
        tokens = open("/proc/cmdline", encoding="utf-8", errors="replace").read().split()
    except OSError:
        return False
    return "ds_mss_fabric=1" in tokens


def _mss_stim_mux_word() -> Optional[int]:
    """UART mux word for a pfsoc boot, or None on Zybo/DE25.

    Only bit 9 is added. I2C/SPI MSS selects would point those loggers at
    peripherals this image cannot open, and CAN bit 11 would drop the
    recessive tie on a CAN-free target.

    The contract import is reached only after the fabric path is requested.
    A DE25 boot leaves ``DS_MSS_FABRIC`` unset, so it must not touch
    ``sw.stream.testbench`` or the UART datalogger stays disabled.
    """
    if not _mss_fabric_requested():
        return None
    from sw.stream.testbench.de_c0_stimulus_contract import (
        EMIO_MUX_UART, EMIO_MUX_MSS_UART,
    )
    return EMIO_MUX_UART | EMIO_MUX_MSS_UART


def arm(
    regbank,
    arming: Optional[List[Tuple[int, int, str]]] = None,
    sleep=None,
    config_hex: Optional[Union[str, Path, Sequence[int]]] = None,
    registry: Any = None,
    helo: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
    active_protocols: Optional[Sequence[str]] = None,
) -> List[str]:
    """Write the arming words. Returns log lines; never raises on a per-device
    failure — a board that cannot arm one datalogger must still boot and stream.

    The disable→enable pair is REQUIRED, not defensive: a config change alone
    does not reset the datalogger's calibration FSM (configEnable stays high), so
    without it the datalogger keeps its stale calibration and the new framing
    never takes effect.

    When arming is None, UART datalogger IDs are discovered dynamically from:
    1. config_hex (if explicitly supplied)
    2. helo declaration (if explicitly supplied)
    3. registry (if explicitly supplied)
    4. regbank streaming ROM (via CONFIG_NUM_WORDS/CONFIG_DATA if supported)
    5. DS_CONFIG_SCHEMA_HEX environment variable
    Falling back to DEFAULT_ARMING (UART_ID=12) if no discovery source is active
    or the streaming ROM reads 0 (no ROM present, DS-D.47).
    """
    if arming is None:
        discovered = discover_uart_ids(
            regbank=regbank,
            config_hex=config_hex,
            registry=registry,
            helo=helo,
            active_protocols=active_protocols,
        )
        if discovered is not None:
            arming = build_default_arming(discovered)
        else:
            arming = list(DEFAULT_ARMING)
    if sleep is None:
        import time
        sleep = time.sleep

    lines: List[str] = []
    if not arming:
        lines.append("[arm] no UART dataloggers discovered in configuration; 0 dataloggers armed")
    # DS-P2.126: on a pfsoc image the boot arm selects MMUART1 (bit 9) and,
    # after the UART datalogger is enabled, writes the payload to /dev/ttyS1.
    # Zybo and DE25 leave DS_MSS_FABRIC unset and skip both steps.
    mss_word = _mss_stim_mux_word()
    if mss_word is not None:
        try:
            regbank.write_u32(EMIO_MUX_SEL_OFFSET, mss_word)
            lines.append(f"[arm] EMIO_MUX_SEL <= 0x{mss_word:X} (MMUART1 source)")
        except Exception as exc:  # pragma: no cover - hardware failure path
            lines.append(f"[arm] EMIO_MUX_SEL: FAILED ({exc})")
    for device_id, extra, why in arming:
        try:
            before = int(regbank.read_u32(DATALOGGER_CONFIG_OFFSET)) & 0xFFFF
        except Exception:
            before = None
        try:
            regbank.write_u32(DATALOGGER_CONFIG_OFFSET, encode_datalogger_config(device_id, False, 0))
            sleep(0.05)
            word = encode_datalogger_config(device_id, True, extra)
            regbank.write_u32(DATALOGGER_CONFIG_OFFSET, word)
            sleep(0.05)
            lines.append(
                f"[arm] datalogger {device_id}: "
                f"{describe_config(before) if before is not None else '(unread)'} "
                f"-> {describe_config(word)}  — {why}"
            )
        except Exception as exc:  # pragma: no cover - hardware failure path
            lines.append(f"[arm] datalogger {device_id}: FAILED ({exc}) — {why}")
    if mss_word is not None:
        try:
            from sw.stream.testbench.de_c0_stimulus_contract import (
                UART_PAYLOAD, transmit_mmuart1,
            )
            transmit_mmuart1(UART_PAYLOAD)
            lines.append("[arm] MMUART1 /dev/ttyS1 transmitted the UART payload")
        except Exception as exc:  # pragma: no cover - hardware failure path
            lines.append(f"[arm] MMUART1 transmit FAILED ({exc})")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    try:
        from sw.stream.regbank import MmioRegbank
        base = int(os.environ.get("DS_MMIO_BASE", str(MmioRegbank.DEFAULT_BASE_ADDR)), 0)
        regbank = MmioRegbank(base_addr=base)
    except Exception as exc:
        # Never block the boot on this: the daemon is still useful unarmed,
        # and a loud line beats a failed boot.
        print(f"[arm] regbank unavailable ({exc}) — datalogger defaults NOT armed", flush=True)
        return 0
    for line in arm(regbank):
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
