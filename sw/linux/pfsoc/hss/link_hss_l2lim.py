#!/usr/bin/env python3
"""Relink the built HSS objects for L2-LIM (0x08000000) instead of the L2 scratchpad.

WHY THIS EXISTS (DS-P2.145 / PF-L0e)
------------------------------------
The upstream Hart Software Services build for ``mpfs-disco-kit`` produces exactly
one transient RAM ELF, ``hss-l2scratch.elf``, whose ENTRY and single LOAD segment
are both ``0x0A000000`` — the L2 *scratchpad* zero-device. That region is
**UNBACKED at reset**: the MSS has not carved scratchpad ways out of the L2 cache
(``WayEnable = 0``), so ``rr program pfsoc hss-load`` correctly refuses to resume
the E51 into it (measured PF-L0c/PF-L0d/RTL-P2.1253, 2026-09-14 — the >128 KB
canary readback DIFFs). Composing the scratchpad is a *firmware* operation HSS
performs at boot (``config_l2_cache()``); it cannot run from the very region it is
supposed to compose.

The fix the RTL-P2.1253 witness explicitly asked deepskopion for: build an HSS
transient variant **linked for the backed L2-LIM region ``0x08000000``**. HSS then
runs from LIM, executes ``config_l2_cache()`` (WayEnable -> 0xB), trains DDR, and
composes ``0x0A000000`` so a subsequent readback there MATCHES. The upstream
linker-script comment for this exact board documents the LIM run mode
("set the reset vector in the linker script to 0x0800_0000 ... run from LIM
instead of envm").

This script does that WITHOUT patching the upstream tree (the HSS clone under
``src/`` is gitignored and regenerated per build) and without a bench-wide
install. It:

  1. reads the upstream, already-preprocessed ``hss-l2scratch.ld``,
  2. rewrites the two placement anchors (``. = __l2_start`` -> ``. = __l2lim_start``
     and every ``>l2zerodevice`` output region -> ``>l2lim``) — i.e. relocates the
     loadable image base from the scratchpad to LIM while leaving the switch-code
     and sdata regions untouched,
  3. re-runs the *identical* upstream link command (same objects, same CFLAGS,
     captured from ``make -n``) with the LIM linker script, emitting
     ``hss-lim.elf`` (ENTRY + LOAD at ``0x08000000``).

Deterministic and re-runnable: given the same built objects it always emits the
same LIM ELF. Verified against the built ``hss-l2scratch.elf`` (same board XML).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# The upstream ``main-build-target`` link flags, verbatim from
# ``make BOARD=mpfs-disco-kit -n hss-l2scratch.elf`` (the only part that differs
# for the LIM variant is ``-T <linker script>`` and the ``-o``/``-Map`` names).
_LINK_FLAGS = [
    "-DBOARD=mpfs-disco-kit",
    "-mcmodel=medany",
    "-mstrict-align",
    "-mabi=lp64",
    "-march=rv64imac_zicsr_zifencei",
    "-g3",
    "-DDEBUG",
    "-pipe",
    "-grecord-gcc-switches",
    '-DVENDOR_STRING=unknown',
    "-Wall",
    "-fno-builtin",
    "-fno-builtin-printf",
    "-fomit-frame-pointer",
    "-fno-strict-aliasing",
    "-fno-common",
    "-Wno-array-bounds",
    "-Wl,--no-warn-rwx-segments",
    "-Os",
    "-Wl,--no-relax",
    "-flto=auto",
    "-ffat-lto-objects",
    "-fno-stack-protector",
    "-fwhole-program",
    "-Wno-lto-type-mismatch",
    "-ffunction-sections",
    "-fdata-sections",
    "-static",
    "-nostdlib",
    "-nostartfiles",
    "-nodefaultlibs",
    "-Wl,--build-id",
    "-Wl,--gc-sections",
]


def transform_linker_script(src_ld: Path, dst_ld: Path) -> None:
    """Rewrite the scratchpad-placement linker script to place the image in L2-LIM."""
    text = src_ld.read_text(encoding="utf-8")
    if "__l2lim_start" not in text or ">l2zerodevice" not in text:
        raise SystemExit(
            f"{src_ld} is not the expected HSS l2scratch linker script "
            "(missing __l2lim_start / >l2zerodevice anchors)."
        )
    # Move the section cursor from the scratchpad base to the LIM base.
    text, n_start = re.subn(r"\.\s*=\s*__l2_start\s*;", ". = __l2lim_start;", text)
    # Place every output section that targeted the scratchpad into LIM instead.
    text, n_region = re.subn(r">\s*l2zerodevice\b", ">l2lim", text)
    if n_start != 1:
        raise SystemExit(f"Expected exactly one '. = __l2_start;' anchor, found {n_start}.")
    if n_region < 1:
        raise SystemExit("Expected at least one '>l2zerodevice' output region to retarget.")
    dst_ld.write_text(text, encoding="utf-8")


# The upstream link target and its ``-o`` name, factored out so the object-list
# recovery below and its test agree on exactly one string.
_SCRATCH_ELF_REL = "build/hss-l2scratch.elf"


def collect_link_objects(make_dry_stdout: str, cross: str) -> list[str]:
    """Recover the exact, ORDERED object list from the upstream ``make -n`` link line.

    The authoritative object ordering is whatever ``make`` passes to the final
    ``gcc`` link (LTO whole-program is order-sensitive, so we must not resort or
    dedupe). We find the single link line that produces ``build/hss-l2scratch.elf``
    with the cross ``gcc`` and return everything after the ``-o <elf>`` token —
    that tail is the ordered object list. Raises ``SystemExit`` if the link line
    is absent (a build/toolchain drift we must not paper over).

    Split out of :func:`relink` so it is unit-testable WITHOUT a built HSS tree:
    a caller passes the captured ``make -n`` stdout directly.
    """
    link_line = None
    for line in make_dry_stdout.splitlines():
        if f"-o {_SCRATCH_ELF_REL}" in line and f"{cross}gcc" in line:
            link_line = line
            break
    if link_line is None:
        raise SystemExit(
            "Could not recover the upstream HSS link command from make -n "
            f"(no line producing '-o {_SCRATCH_ELF_REL}' with '{cross}gcc')."
        )
    # Everything after '-o build/hss-l2scratch.elf' is the ordered object list.
    objs = link_line.split(f"-o {_SCRATCH_ELF_REL}", 1)[1].split()
    if not objs:
        raise SystemExit("Recovered an EMPTY object list from the HSS link line.")
    return objs


def relink(hss_dir: Path, cross: str, out_elf: Path) -> None:
    hss_dir = hss_dir.resolve()
    board_dir = hss_dir / "boards" / "mpfs-disco-kit"
    src_ld = board_dir / "hss-l2scratch.ld"
    if not src_ld.is_file():
        raise SystemExit(f"Preprocessed linker script not found: {src_ld} (build HSS first).")
    lim_ld = board_dir / "hss-lim.ld"
    transform_linker_script(src_ld, lim_ld)

    # Recover the exact, ordered object list + the -T target from the upstream
    # link rule via a dry run — this keeps us byte-for-byte consistent with the
    # toolchain's own object ordering (LTO whole-program is order-sensitive).
    dry = subprocess.run(
        ["make", "BOARD=mpfs-disco-kit", "-n", "hss-l2scratch.elf"],
        cwd=hss_dir,
        capture_output=True,
        text=True,
        env=_link_env(cross),
    )
    try:
        objs = collect_link_objects(dry.stdout, cross)
    except SystemExit:
        sys.stderr.write(dry.stdout + "\n" + dry.stderr + "\n")
        raise

    map_file = hss_dir / "build" / "output-lim.map"
    argv = (
        [f"{cross}gcc", "-T", str(lim_ld)]
        + _LINK_FLAGS
        + [f"-Wl,-Map={map_file}", "-o", str(out_elf)]
        + objs
    )
    proc = subprocess.run(argv, cwd=hss_dir, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + "\n" + proc.stderr + "\n")
        raise SystemExit(f"LIM relink failed (exit {proc.returncode}).")
    if not out_elf.is_file():
        raise SystemExit(f"LIM relink reported success but {out_elf} is missing.")


# The transient-RAM ELF filenames the SDK's ``rr program pfsoc hss-load`` search
# (``sdk/cli/commands/linux_steps/_pfsoc.py`` ``transient_candidates``) picks up,
# in first-match order. Staging the LIM ELF OVER the first of these is what puts
# the backed-region image on the caller path — otherwise ``hss-lim.elf`` is a
# sideband file the next ``hss-load`` never sees (the DS-P2.145 landing finding).
SDK_TRANSIENT_CANDIDATE_RELPATHS = (
    "build/hss-l2scratch.elf",
    "Default/hss-l2scratch.elf",
    "build/hss-transient.elf",
    "hss-l2scratch.elf",
    "hss-transient.elf",
)


def stage_lim_as_transient(hss_dir: Path, lim_elf: Path) -> Path:
    """Overwrite the SDK-searched transient ELF path with the LIM-linked image.

    ``rr program pfsoc hss-load`` stages the FIRST existing path in
    ``SDK_TRANSIENT_CANDIDATE_RELPATHS``; by default that is the upstream
    scratchpad-linked ``build/hss-l2scratch.elf`` (entry ``0x0A000000``). This
    copies the backed LIM ELF over the first candidate that exists (or the first
    listed path if none exist yet), so the next ``hss-load`` loads the backed
    ``0x08000000`` image instead of the unbacked scratchpad one. Returns the
    staged path. This is the wiring that makes the transform a CALLER-PATH fix.
    """
    import shutil

    if not lim_elf.is_file():
        raise SystemExit(f"LIM ELF not present to stage: {lim_elf}")
    hss_dir = hss_dir.resolve()
    target = None
    for rel in SDK_TRANSIENT_CANDIDATE_RELPATHS:
        cand = hss_dir / rel
        if cand.is_file():
            target = cand
            break
    if target is None:
        target = hss_dir / SDK_TRANSIENT_CANDIDATE_RELPATHS[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(lim_elf, target)
    return target


def _link_env(cross: str) -> dict:
    import os

    env = dict(os.environ)
    env.setdefault("CROSS_COMPILE", cross)
    env.setdefault("ARCH", "riscv")
    env.setdefault("PLATFORM_CFLAGS", "-Wno-array-bounds")
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hss-dir", required=True, type=Path, help="Path to the built hart-software-services clone.")
    ap.add_argument("--cross", default="riscv64-unknown-elf-", help="Bare-metal RISC-V cross prefix.")
    ap.add_argument("--out", required=True, type=Path, help="Output LIM-linked ELF path.")
    ap.add_argument(
        "--stage-transient",
        action="store_true",
        help="After linking, copy the LIM ELF over the SDK-searched transient ELF path "
        "(build/hss-l2scratch.elf) so the next `rr program pfsoc hss-load` loads the "
        "backed 0x08000000 image instead of the unbacked scratchpad one.",
    )
    args = ap.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    relink(args.hss_dir, args.cross, args.out.resolve())
    print(f"OK: wrote {args.out}")
    if args.stage_transient:
        staged = stage_lim_as_transient(args.hss_dir, args.out.resolve())
        print(f"OK: staged LIM ELF onto SDK transient path {staged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
