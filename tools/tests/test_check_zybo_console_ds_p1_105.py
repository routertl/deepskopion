# rr unknown-llm (AI-generated)
"""DS-P1.105: the Zybo console must resolve to the USB UART in every boot stage.

Measured 2026-11-14 on the zybo-z7-20: the BOOT.bin built at 3d925cc6 reached
FSBL SUCCESSFUL_HANDOFF and then printed nothing. Its generated device trees
order the aliases ``serial0 = /axi/serial@e0001000`` (UART1, USB console) and
``serial1 = /axi/serial@e0000000`` (UART0, EMIO into the fabric), the reverse
of the old generator. The target bootargs said ``console=ttyPS1``, and rr
reconciles U-Boot's control-dt ``stdout-path`` to that same ``serial1``
(RTL-P3.590), so both resolved to the EMIO UART0.

The fixtures rebuild that alias order in synthetic FDTs (no board MAC or
address). The declared-target tests are red on the 3d925cc6 targets
(console=ttyPS1) and green once the targets say console=ttyPS0.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_zybo_console", ROOT / "tools" / "check_zybo_console.py")
cz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cz)

UART0 = 0xE0000000  # EMIO, into the fabric
UART1 = 0xE0001000  # MIO, the USB console

#: /aliases of the 3d925cc6 SDT (sw/linux/src/device_tree_workspace/sdt/linux.dts:1027-1029).
ALIASES_3D925CC6 = {"serial0": "/axi/serial@e0001000", "serial1": "/axi/serial@e0000000"}
#: The order the XSCT generator wrote before rr d2bb99714 (the booting BOOT.bin 5a6a42a8).
ALIASES_DTG = {"serial0": "/axi/serial@e0000000", "serial1": "/axi/serial@e0001000"}

#: The bootargs the 3d925cc6 targets carried.
BOOTARGS_3D925CC6 = ("earlycon=cdns,mmio32,0xe0001000 console=ttyPS1,115200 "
                     "uio_pdrv_genirq.of_id=generic-uio ds_source=ddr ds_ring_word_bytes=16")

sys.path.insert(0, str(ROOT / "tools"))
from target_files import target_file, with_bench  # noqa: E402

#: zybo_datalogger always; the bench target canfd_demo only where the checkout has bench/ (the export has none).
TARGETS = {name: target_file(name) for name in sorted(with_bench({"zybo_datalogger"}, {"canfd_demo"}))}

#: The last u-boot-xlnx tag seen booting this board's chain (DS-P1.105).
UBOOT_PIN = "xilinx-v2026.1"


def _fdt(nodes: dict[str, dict[str, bytes]]) -> bytes:
    """A well-formed FDT holding ``nodes`` ({"/a/b": {prop: value}}), children
    nested by path. Parents are created as empty nodes."""
    strings = bytearray()
    offsets: dict[str, int] = {}

    def stroff(name: str) -> int:
        if name not in offsets:
            offsets[name] = len(strings)
            strings.extend(name.encode("ascii") + b"\x00")
        return offsets[name]

    def pad(blob: bytes) -> bytes:
        return blob + b"\x00" * (-len(blob) % 4)

    tree: dict = {}
    for path, props in nodes.items():
        cur = tree
        for part in [p for p in path.split("/") if p]:
            cur = cur.setdefault(part, {"__props__": {}})
        cur["__props__"] = props

    def emit(name: str, node: dict) -> bytes:
        out = bytearray(struct.pack(">I", 1) + pad(name.encode("ascii") + b"\x00"))
        for prop, value in node.get("__props__", {}).items():
            out += struct.pack(">III", 3, len(value), stroff(prop)) + pad(value)
        for child, sub in node.items():
            if child != "__props__":
                out += emit(child, sub)
        out += struct.pack(">I", 2)
        return bytes(out)

    body = emit("", tree) + struct.pack(">I", 9)
    off_struct = 40 + 16
    off_strings = off_struct + len(body)
    header = struct.pack(">10I", 0xD00DFEED, off_strings + len(strings), off_struct,
                         off_strings, 40, 17, 16, 0, len(strings), len(body))
    return header + b"\x00" * 16 + body + bytes(strings)


def _s(text: str) -> bytes:
    return text.encode("utf-8") + b"\x00"


def _tree(aliases: dict[str, str], stdout: str, bootargs: str) -> bytes:
    return _fdt({
        "/aliases": {k: _s(v) for k, v in aliases.items()},
        "/axi/serial@e0000000": {"reg": struct.pack(">II", UART0, 0x1000)},
        "/axi/serial@e0001000": {"reg": struct.pack(">II", UART1, 0x1000)},
        "/chosen": {"stdout-path": _s(stdout), "bootargs": _s(bootargs)},
    })


def _boot_chain(tmp_path: Path, aliases: dict[str, str], bootargs: str) -> tuple[Path, Path]:
    """The two device trees rr builds for one target, as rr builds them: the
    kernel dt keeps the generator's ``serial0`` stdout-path; U-Boot's control
    dt has its stdout-path reconciled to console=ttyPS<N> (RTL-P3.590)."""
    tty = cz.re.search(r"console=ttyPS(\d+)", bootargs).group(1)
    system = tmp_path / "system.dtb"
    uboot = tmp_path / "uboot_control.dtb"
    system.write_bytes(_tree(aliases, "serial0:115200n8", bootargs))
    uboot.write_bytes(_tree(aliases, f"serial{tty}:115200n8", bootargs))
    return system, uboot


def _declared_bootargs(target: str) -> str:
    doc = yaml.safe_load(TARGETS[target].read_text(encoding="utf-8"))
    return doc["linux"]["bootargs"]


# --- the declared targets (red on 3d925cc6, green after the fix) ------------

@pytest.mark.parametrize("target", sorted(TARGETS))
def test_declared_console_reaches_the_usb_uart_on_the_3d925cc6_alias_order(target, tmp_path):
    system, uboot = _boot_chain(tmp_path, ALIASES_3D925CC6, _declared_bootargs(target))
    assert cz.check(system, uboot) == []


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_declared_earlycon_is_the_console_uart(target):
    bootargs = _declared_bootargs(target)
    assert "earlycon=cdns,mmio32,0xe0001000" in bootargs
    assert "console=ttyPS0," in bootargs


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_uboot_source_is_pinned_to_the_last_booting_tag(target):
    doc = yaml.safe_load(TARGETS[target].read_text(encoding="utf-8"))
    assert doc["linux"].get("uboot_branch") == UBOOT_PIN


# --- the guard itself --------------------------------------------------------

def test_the_3d925cc6_boot_chain_is_refused_naming_the_emio_uart(tmp_path):
    system, uboot = _boot_chain(tmp_path, ALIASES_3D925CC6, BOOTARGS_3D925CC6)
    problems = cz.check(system, uboot)
    joined = "\n".join(problems)
    assert "0xE0000000" in joined
    assert any(p.startswith("system.dtb: console=ttyPS1") for p in problems)
    assert any(p.startswith("uboot_control.dtb: stdout-path") for p in problems)
    assert cz.main(["--system-dtb", str(system), "--uboot-dtb", str(uboot)]) == 1


def test_positive_control_a_correct_chain_is_accepted(tmp_path):
    bootargs = BOOTARGS_3D925CC6.replace("ttyPS1", "ttyPS0")
    system, uboot = _boot_chain(tmp_path, ALIASES_3D925CC6, bootargs)
    assert cz.check(system, uboot) == []
    assert cz.main(["--system-dtb", str(system), "--uboot-dtb", str(uboot)]) == 0


def test_the_booting_5a6a42a8_chain_is_accepted(tmp_path):
    """Positive control from the image that boots: old DTG order, kernel dt
    stdout-path serial0 (UART0, unused because console= is set), console=ttyPS1,
    U-Boot control dt stdout-path serial1 (measured from its partitions). The
    check judges the resolved UART per stage, not the tty number."""
    system = tmp_path / "system.dtb"
    uboot = tmp_path / "uboot_control.dtb"
    system.write_bytes(_tree(ALIASES_DTG, "serial0:115200n8", BOOTARGS_3D925CC6))
    uboot.write_bytes(_tree(ALIASES_DTG, "serial1:115200n8", BOOTARGS_3D925CC6))
    assert cz.check(system, uboot) == []


def test_an_earlycon_on_another_uart_is_refused(tmp_path):
    bootargs = BOOTARGS_3D925CC6.replace("ttyPS1", "ttyPS0").replace("0xe0001000", "0xe0000000")
    system, uboot = _boot_chain(tmp_path, ALIASES_3D925CC6, bootargs)
    problems = cz.check(system, uboot)
    assert any("earlycon base 0xe0000000" in p for p in problems)


def test_a_missing_alias_or_uboot_stdout_path_is_refused(tmp_path):
    good = BOOTARGS_3D925CC6.replace("ttyPS1", "ttyPS0")
    system = tmp_path / "system.dtb"
    uboot = tmp_path / "uboot_control.dtb"
    system.write_bytes(_fdt({
        "/aliases": {"serial1": _s("/axi/serial@e0000000")},
        "/axi/serial@e0000000": {"reg": struct.pack(">II", UART0, 0x1000)},
        "/chosen": {"bootargs": _s(good)},
    }))
    uboot.write_bytes(_fdt({
        "/aliases": {k: _s(v) for k, v in ALIASES_3D925CC6.items()},
        "/chosen": {"bootargs": _s(good)},
    }))
    problems = cz.check(system, uboot)
    assert any("console=ttyPS0" in p and "serial0 (no such alias)" in p for p in problems)
    assert "uboot_control.dtb: no /chosen/stdout-path" in problems


def test_without_console_the_kernel_stdout_path_is_judged(tmp_path):
    """No console= on the cmdline: the kernel uses stdout-path, so a
    stdout-path on UART0 is refused even when earlycon is right."""
    bootargs = "earlycon=cdns,mmio32,0xe0001000 ds_source=ddr"
    system = tmp_path / "system.dtb"
    uboot = tmp_path / "uboot_control.dtb"
    system.write_bytes(_tree(ALIASES_DTG, "serial0:115200n8", bootargs))
    uboot.write_bytes(_tree(ALIASES_3D925CC6, "serial0:115200n8", bootargs))
    problems = cz.check(system, uboot)
    assert "system.dtb: bootargs name no console=ttyPS<N>" in problems
    assert any(p.startswith("system.dtb: stdout-path 'serial0:115200n8' resolves to 0xE0000000")
               for p in problems)


def test_a_missing_or_malformed_dtb_exits_2(tmp_path):
    bootargs = BOOTARGS_3D925CC6.replace("ttyPS1", "ttyPS0")
    system, uboot = _boot_chain(tmp_path, ALIASES_3D925CC6, bootargs)
    missing = tmp_path / "absent.dtb"
    assert cz.main(["--system-dtb", str(missing), "--uboot-dtb", str(uboot)]) == 2
    junk = tmp_path / "junk.dtb"
    junk.write_bytes(b"\x00" * 64)
    assert cz.main(["--system-dtb", str(system), "--uboot-dtb", str(junk)]) == 2


def test_the_default_paths_are_the_rr_linux_out_dir(tmp_path):
    root = tmp_path / "proj"
    out = root / "sw" / "linux" / "out" / "zybo_datalogger"
    out.mkdir(parents=True)
    bootargs = BOOTARGS_3D925CC6.replace("ttyPS1", "ttyPS0")
    system, uboot = _boot_chain(out, ALIASES_3D925CC6, bootargs)
    assert cz.main(["--root", str(root)]) == 0
