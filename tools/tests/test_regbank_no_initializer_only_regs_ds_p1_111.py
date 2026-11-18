# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.111: no regbank register may get its value only from a VHDL signal initializer.

WHY
---
RouteRTL's generated AXI-Lite regbank gives a register marked ROM (``C_ROM_REGS`` '1', from
``access: ROM`` in project_regbank.yml) its value ONLY through the signal initializer
``reg_bank : t_memArray := C_INIT_REGBANK``: the FPGA-side REGS_IN write and the reset reload both
skip ROM registers, and ROM registers are read-only to AXI. On the PolarFire kit (DS-T1.26) such a
register, REGBANK_NUM_WORDS at 0x10, synthesized as SLE flops with no initial value and read a
different word on every cold boot, while simulation and the Xilinx/Intel boards read 7436.

WHAT THIS GUARDS
----------------
1. The generated regbank still skips ROM registers on both the REGS_IN and the reset paths. If a
   future generator drives or reloads them, this check fails with a message to re-derive the rule,
   so the guard never passes silently on a changed generator.
2. Given that, no register in the generated package is marked ROM.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "hw" / "rtl" / "pkg" / "system_regbank_pkg.vhd"
REGBANK = ROOT / "hw" / "rtl" / "regbank" / "system_regbank.vhd"


def _code(path: Path) -> str:
    return "\n".join(ln.split("--", 1)[0] for ln in path.read_text(encoding="utf-8").splitlines())


def _rom_flags() -> list[tuple[str, str]]:
    """(register name, flag) for every entry of C_ROM_REGS, in order."""
    text = PKG.read_text(encoding="utf-8")
    m = re.search(r"constant\s+C_ROM_REGS\b[^:]*:[^=]*:=\s*\((.*?)\);", text, re.S)
    assert m, f"{PKG.name}: C_ROM_REGS not found"
    rows = re.findall(r"'([01])'\s*,?\s*--\s*(\w+)", m.group(1))
    assert rows, f"{PKG.name}: C_ROM_REGS has no '<0|1>', -- <NAME> entries"
    return [(name, flag) for flag, name in rows]


def test_generated_regbank_still_skips_rom_registers_on_drive_and_reset():
    code = _code(REGBANK)
    drive = re.search(r"if\s+REGS_IN\s*\(\s*i\s*\)\.valid\s*=\s*'1'\s+and\s+C_ROM_REGS\s*\(\s*i\s*\)\s*=\s*'0'", code)
    reset = re.search(r"if\s+C_ROM_REGS\s*\(\s*i\s*\)\s*=\s*'0'\s+then\s+reg_bank\s*\(\s*i\s*\)\s*<=\s*C_INIT_REGBANK", code)
    init = re.search(r"signal\s+reg_bank\s*:\s*t_memArray\s*:=\s*C_INIT_REGBANK\s*;", code)
    assert drive and reset and init, (
        f"{REGBANK.relative_to(ROOT)} no longer has the shape this guard was derived from "
        f"(REGS_IN skips ROM: {bool(drive)}, reset skips ROM: {bool(reset)}, initializer: {bool(init)}). "
        "Re-derive whether a ROM register still depends on the signal initializer alone before changing this test.")


def test_no_register_takes_its_value_only_from_the_signal_initializer():
    rom = [name for name, flag in _rom_flags() if flag == "1"]
    assert rom == [], (
        f"{PKG.relative_to(ROOT)}: {rom} marked ROM (C_ROM_REGS '1'). The generated regbank never drives "
        "or reloads a ROM register, so its value is the signal initializer alone, which a device without "
        "flop power-up init does not honour (DS-T1.26: PolarFire 0x10 read a different word per boot). "
        "Declare it RO in project_regbank.yml and drive it through REGS_IN.")
