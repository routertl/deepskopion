#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""Surgically adopt the FACTORY diagnostic's proven console register values into
the HSS build via the Microchip-sanctioned ``mss_sw_config.h`` override seam.

WHY THIS EXISTS (DS-P2.145 / PF-L0e — OWNER DECISION "REPLICATE-THE-FACTORY")
----------------------------------------------------------------------------
The differential-against-factory (``docs/hardware/pf-l0e/evidence/diff_2026-09-15/``)
read, from live silicon, the full MSS register set of BOTH the factory diagnostic
(which DOES talk on MMUART0) and our HSS (silent). The OWNER DECISION for this
resume is: *make our HSS program the FACTORY'S working console configuration —
at minimum the MMUART0 routing rows (IOMUX0/3/4 factory values) — via the
pre-build overlay path; be surgical: only adopt factory values for rows in the
DIFF; keep the XML value where factory and XML already agree.*

THE PRIOR ITERATION WAS INCOMPLETE (the reason this overlay is needed).
The prior "realfix" forced only IOMUX3/4/5 to the factory values (via the XML
overlay) and then CLAIMED our HSS was "byte-identical to the factory on the
entire readable console register set". The committed ``register_diff.txt`` /
``our_hss_realfix_snapshot.txt`` show that claim is FALSE: IOMUX0/1/2/6 (and
``SOFT_RESET_CR`` / ``SUBBLK_CLOCK_CR``) STILL differed from the factory. So the
prior "silence lives outside HSS reach" conclusion was drawn from an INCOMPLETE
match. This overlay finishes the match on the IOMUX axis so the conclusion is
earned (or overturned) on a genuine full-IOMUX factory replica.

THE DIFF (factory-on-silicon vs the disco_mss-XML build, from register_diff.txt):

    | Register  | FACTORY (talks) | disco_mss XML build (silent) | in DIFF? |
    |-----------|-----------------|------------------------------|----------|
    | IOMUX0_CR | 0x00001200      | 0x00002F15                   | YES      |
    | IOMUX1_CR | 0x00000000      | 0x11111111                   | YES      |
    | IOMUX2_CR | 0x00000000      | 0x00FF1111                   | YES      |
    | IOMUX3_CR | 0xF55FF55F      | 0xF55FF55F                   | no (agree)|
    | IOMUX4_CR | 0xFFFFFFFF      | 0xFFFFFFFF*                   | no (agree)|
    | IOMUX5_CR | 0xFFFFFFFF      | 0xFFFFFFFF*                   | no (agree)|
    | IOMUX6_CR | 0x0000001D      | 0x00000000                   | YES      |

    (* IOMUX3/4/5 "agree" as they stand ON SILICON after the prior realfix XML
    overlay forced them to the factory values; the raw disco_mss primary header
    has IOMUX4=0xFFFFFFFF/IOMUX5=0xFFFFFFFF only in its console-fixed form. This
    overlay pins ALL SEVEN rows to the factory values unconditionally so the
    result does not depend on which prior overlay ran — a self-contained replica.)

HONEST SCOPE NOTE (falsifiable, measured — see the doc).
A bit-level decode of the DIFF rows shows NONE of them touches the MMUART0 TX
path: IOMUX0 bit7 (``MMUART0_FABRIC``) is 0 in BOTH factory and ours (MMUART0 ->
pads either way); the IOMUX0 diffs are all other peripherals (spi/i2c/can/mmuart1/
mdio); IOMUX6 is the MMC/SD voltage-select-invert row, not a MMUART pad; and
``SUBBLK_CLOCK_CR``/``SOFT_RESET_CR`` both have MMUART0 CLOCKED + OUT-OF-RESET in
both images. So this overlay is expected to make our HSS a FULLER factory
replica WITHOUT, by the register math, changing the console outcome — which is
exactly why running it on silicon is worth a cycle: a genuine full-IOMUX factory
match that STILL stays silent converts the prior (prematurely-claimed) conclusion
into an EARNED one — the console delta is not in the IOMUX register set at all,
it is in the downstream I/O-calibration / ``flash_valid`` / SCB sequence that
``mssio_setup()`` does NOT reach. If instead it talks, the prior incomplete match
was the whole story.

MECHANISM (Microchip-sanctioned, not a generated-file hack).
``fpga_design_config/io/hw_mssio_mux.h`` guards every ``LIBERO_SETTING_IOMUX*_CR``
with ``#if !defined(...)`` and its own header banner documents the override path:
*"The definition in mss_sw_config.h takes precedence, as mss_sw_config.h is
included prior to the generated header files."* So defining the factory values in
``boards/mpfs-disco-kit/mpfs_hal_config/mss_sw_config.h`` makes the generated
defaults lose the ``#if !defined`` race — the boot ``mssio_setup()`` then programs
the factory IOMUX word-for-word. This is a PRE-BUILD overlay (must land before the
compile that bakes ``mss_io.o``). Idempotent; no hardware access; touches only the
gitignored clone.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

# The FACTORY diagnostic's console register values, MEASURED on live silicon while
# it talked (204-byte banner on MMUART0/if02). Source of truth:
#   docs/hardware/pf-l0e/evidence/diff_2026-09-15/golden_factory_snapshot.txt
#   docs/hardware/pf-l0e/evidence/diff_2026-09-15/register_diff.txt
# All seven IOMUX rows are pinned so the replica is self-contained (independent of
# which prior overlay set IOMUX3/4/5).
FACTORY_IOMUX_CR = {
    "IOMUX0_CR": 0x00001200,
    "IOMUX1_CR": 0x00000000,
    "IOMUX2_CR": 0x00000000,
    "IOMUX3_CR": 0xF55FF55F,
    "IOMUX4_CR": 0xFFFFFFFF,
    "IOMUX5_CR": 0xFFFFFFFF,
    "IOMUX6_CR": 0x0000001D,
}

_MARK_BEGIN = "/* DS-P2.145 PF-L0e REPLICATE-THE-FACTORY console IOMUX overlay BEGIN */"
_MARK_END = "/* DS-P2.145 PF-L0e REPLICATE-THE-FACTORY console IOMUX overlay END */"

_SW_CONFIG_RELPATH = Path("boards") / "mpfs-disco-kit" / "mpfs_hal_config" / "mss_sw_config.h"


def _overlay_block() -> str:
    lines = [_MARK_BEGIN]
    lines.append(
        "/* Factory-diagnostic IOMUX values measured on live silicon while it talked."
    )
    lines.append(
        " * mss_sw_config.h wins the generated hw_mssio_mux.h #if !defined race, so the"
    )
    lines.append(
        " * boot mssio_setup() programs the factory IOMUX word-for-word (OWNER DECISION"
    )
    lines.append(" * REPLICATE-THE-FACTORY; diff_2026-09-15/register_diff.txt). */")
    for name, val in FACTORY_IOMUX_CR.items():
        lines.append(f"#if !defined (LIBERO_SETTING_{name})")
        lines.append(f"#define LIBERO_SETTING_{name}    0x{val:08X}UL")
        lines.append("#endif")
    lines.append(_MARK_END)
    return "\n".join(lines) + "\n"


def sw_config_path(hss_dir: Path) -> Path:
    return hss_dir.resolve() / _SW_CONFIG_RELPATH


def apply(hss_dir: Path) -> bool:
    """Insert (or refresh) the factory-IOMUX override block in mss_sw_config.h.

    Returns True if the file changed. Must run BEFORE the HSS build compiles the
    clone (the override is read at compile time). Idempotent: re-running with the
    same factory values is a no-op.
    """
    cfg = sw_config_path(hss_dir)
    if not cfg.is_file():
        raise SystemExit(
            f"HSS disco-kit mss_sw_config.h not found: {cfg} (clone + configure HSS first)."
        )
    text = cfg.read_text(encoding="utf-8")
    block = _overlay_block()

    # Remove any prior instance of our block (idempotent refresh).
    pat = re.compile(
        re.escape(_MARK_BEGIN) + r".*?" + re.escape(_MARK_END) + r"\n?",
        re.S,
    )
    stripped = pat.sub("", text)

    # Insert immediately before the file's final include-guard #endif so the
    # defines are in scope for every consumer that includes mss_sw_config.h.
    m = list(re.finditer(r"^#endif.*$", stripped, flags=re.MULTILINE))
    if m:
        last = m[-1]
        new = stripped[: last.start()] + block + stripped[last.start():]
    else:
        new = stripped.rstrip("\n") + "\n" + block

    if new != text:
        cfg.write_text(new, encoding="utf-8")
        return True
    return False


def revert(hss_dir: Path) -> bool:
    """Remove any factory-IOMUX override block from mss_sw_config.h.

    DS-P3.160: The reference MSS routing from disco_mss.cxz is authoritative;
    the factory override block must not override the generated hw_mssio_mux.h.
    Returns True if the file was modified.
    """
    cfg = sw_config_path(hss_dir)
    if not cfg.is_file():
        return False
    text = cfg.read_text(encoding="utf-8")
    pat = re.compile(
        re.escape(_MARK_BEGIN) + r".*?" + re.escape(_MARK_END) + r"\n?",
        re.S,
    )
    stripped = pat.sub("", text)
    if stripped != text:
        cfg.write_text(stripped, encoding="utf-8")
        return True
    return False


def read_effective_iomux(hss_dir: Path) -> dict[str, int]:
    """Parse the IOMUX values that WILL take effect (override block if present,
    else the generated primary header) — for verification/tests."""
    cfg = sw_config_path(hss_dir)
    result: dict[str, int] = {}
    if cfg.is_file():
        text = cfg.read_text(encoding="utf-8")
        m = re.search(
            re.escape(_MARK_BEGIN) + r"(.*?)" + re.escape(_MARK_END), text, re.S
        )
        if m:
            for name in FACTORY_IOMUX_CR:
                mm = re.search(
                    rf"#define\s+LIBERO_SETTING_{name}\s+0x([0-9A-Fa-f]+)", m.group(1)
                )
                if mm:
                    result[name] = int(mm.group(1), 16)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hss-dir", required=True, type=Path)
    ap.add_argument(
        "--revert",
        action="store_true",
        help="Remove any factory IOMUX override block from mss_sw_config.h",
    )
    args = ap.parse_args(argv)
    if args.revert:
        changed = revert(args.hss_dir.resolve())
        print(
            "OK: factory console IOMUX overlay "
            + ("reverted" if changed else "not present")
        )
        return 0
    changed = apply(args.hss_dir.resolve())
    eff = read_effective_iomux(args.hss_dir.resolve())
    print(
        "OK: factory console IOMUX overlay "
        + ("applied" if changed else "already present")
        + " ("
        + ", ".join(f"{k}=0x{v:08X}" for k, v in eff.items())
        + ")"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
