"""DS-P1.105: the EMIO UART0 test wire is found by its base address, not a tty number.

The Zybo PS has two UARTs: UART0 at 0xE0000000 on EMIO (the test wire into
the fabric) and UART1 at 0xE0001000 (the USB console). /dev/ttyPS<N> follows
the device tree's serialN aliases. The XSCT generator made UART0 ttyPS0;
SDTGen + Lopper (rr since 2026-10-20, measured on the 3d925cc6 build) make
UART1 ttyPS0 and UART0 ttyPS1. emio_inject opened a fixed /dev/ttyPS0, which
on an SDT image is the console.

Each case builds a fake /sys/class/tty tree in tmp_path; no device is opened.
"""
from __future__ import annotations

from pathlib import Path
import sys
import types

import pytest

from sw.stream.testbench import emio_inject as ei

UART0 = 0xE0000000
UART1 = 0xE0001000


def _tty(root: Path, name: str, base: int | None = None, platform_name: str | None = None) -> None:
    """One /sys/class/tty/<name> entry whose device node carries ``base`` in
    its of_node/reg, or (``platform_name``) only a device symlink target."""
    tty = root / name
    tty.mkdir(parents=True)
    if base is not None:
        node = root.parent / "nodes" / f"{name}" / "of_node"
        node.mkdir(parents=True)
        (node / "reg").write_bytes(base.to_bytes(4, "big") + (0x1000).to_bytes(4, "big"))
        (tty / "device").symlink_to(node.parent)
    elif platform_name is not None:
        dev = root.parent / "platform" / platform_name
        dev.mkdir(parents=True)
        (tty / "device").symlink_to(dev)


def test_sdt_alias_order_resolves_uart0_to_ttyps1(tmp_path):
    sys_tty = tmp_path / "class" / "tty"
    _tty(sys_tty, "ttyPS0", UART1)   # SDTGen: serial0 = UART1 (the console)
    _tty(sys_tty, "ttyPS1", UART0)
    assert ei.emio_uart_dev(sys_class_tty=sys_tty) == "/dev/ttyPS1"


def test_dtg_alias_order_resolves_uart0_to_ttyps0(tmp_path):
    sys_tty = tmp_path / "class" / "tty"
    _tty(sys_tty, "ttyPS0", UART0)   # XSCT generator: serial0 = UART0
    _tty(sys_tty, "ttyPS1", UART1)
    assert ei.emio_uart_dev(sys_class_tty=sys_tty) == "/dev/ttyPS0"


def test_the_console_is_never_returned(tmp_path):
    sys_tty = tmp_path / "class" / "tty"
    _tty(sys_tty, "ttyPS0", UART1)
    _tty(sys_tty, "ttyPS1", UART0)
    assert ei.emio_uart_dev(sys_class_tty=sys_tty) != "/dev/ttyPS0"
    assert ei.emio_uart_dev(UART1, sys_class_tty=sys_tty) == "/dev/ttyPS0"


def test_platform_device_name_is_the_fallback(tmp_path):
    sys_tty = tmp_path / "class" / "tty"
    _tty(sys_tty, "ttyPS0", platform_name="e0001000.serial")
    _tty(sys_tty, "ttyPS1", platform_name="e0000000.serial")
    assert ei.emio_uart_dev(sys_class_tty=sys_tty) == "/dev/ttyPS1"


def test_no_uart_at_the_base_is_refused(tmp_path):
    sys_tty = tmp_path / "class" / "tty"
    _tty(sys_tty, "ttyPS0", UART1)
    with pytest.raises(FileNotFoundError, match="0xE0000000"):
        ei.emio_uart_dev(sys_class_tty=sys_tty)


def test_two_ttys_at_one_base_are_refused(tmp_path):
    sys_tty = tmp_path / "class" / "tty"
    _tty(sys_tty, "ttyPS0", UART0)
    _tty(sys_tty, "ttyPS1", UART0)
    with pytest.raises(RuntimeError, match="ttyPS0, ttyPS1"):
        ei.emio_uart_dev(sys_class_tty=sys_tty)


def test_no_fixed_tty_number_is_the_default():
    assert ei.DEFAULT_UART_DEV is None
    assert ei.EMIO_UART0_BASE == UART0


def test_uart_with_no_dev_opens_the_resolved_tty(monkeypatch):
    opened: list[str] = []

    class FakeSerial:
        def __init__(self, port, **_kw):
            opened.append(port)

    fake = types.SimpleNamespace(Serial=FakeSerial, EIGHTBITS=8, PARITY_NONE="N",
                                 STOPBITS_ONE=1, SerialException=Exception)
    monkeypatch.setitem(sys.modules, "serial", fake)
    monkeypatch.setattr(ei, "emio_uart_dev", lambda: "/dev/ttyPS1")
    uart = ei.UART()
    assert opened == ["/dev/ttyPS1"]
    assert uart.dev == "/dev/ttyPS1"
    ei.UART("/dev/ttyEXPLICIT")
    assert opened[-1] == "/dev/ttyEXPLICIT"
