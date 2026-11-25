#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""The active build target, resolved the way ``rr`` resolves it.

``rr`` picks the target in this order (routertl RTL-P2.2179, in every release
from 5.3.1):

1. ``$RR_TARGET``;
2. ``.routertl/local/active-target``, the untracked file ``rr target set``
   writes, so a target switch never edits a tracked file;
3. the ``target:`` key of ``project.yml``, the default a fresh clone builds.

Every tool here that needs the active target asks this module, so no tool can
see a different target from the build. A tool that read only 1 and 3 resolved
the project.yml default after ``rr target set``: a Zybo build wrote its
rom_info stage stamp under the DE25 build directory (DS-P2.336).
``tools/tests/test_active_target_ds_p2_336.py`` refuses a new direct reader.

Plain Python and PyYAML only: hooks and the preflight run it on hosts where
the RouteRTL package may be absent or another version.

Usage: ``python3 tools/active_target.py [--stem | --path | --source]``
(prints the stem by default; exit 1 when no target resolves).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_TARGET_FILE = Path(".routertl") / "local" / "active-target"
SOURCE_ENV = "RR_TARGET"
SOURCE_LOCAL = ACTIVE_TARGET_FILE.as_posix()
SOURCE_PROJECT = "project.yml"


def active_target_pointer(root: Path | str = ROOT) -> tuple[str | None, str | None]:
    """``(pointer, source)`` for the active target, or ``(None, None)``.

    *pointer* is the text as written (a path such as ``targets/x.yml`` or a
    bare stem); *source* names where it came from.
    """
    root = Path(root)
    env = os.environ.get(SOURCE_ENV, "").strip()
    if env:
        return env, SOURCE_ENV
    try:
        local = (root / ACTIVE_TARGET_FILE).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        local = ""
    if local:
        return local, SOURCE_LOCAL
    try:
        cfg = yaml.safe_load((root / "project.yml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None, None
    pointer = cfg.get("target") if isinstance(cfg, dict) else None
    if isinstance(pointer, str) and pointer.strip():
        return pointer.strip(), SOURCE_PROJECT
    return None, None


def resolve_pointer(pointer: str, root: Path | str = ROOT) -> Path:
    """The target yml a pointer names: a path (absolute or root-relative), or a
    bare stem under ``targets/``."""
    root = Path(root)
    path = Path(pointer)
    if path.suffix not in (".yml", ".yaml"):
        return root / "targets" / f"{pointer}.yml"
    return path if path.is_absolute() else root / path


def active_target_path(root: Path | str = ROOT) -> Path | None:
    """The active target's yml, or None when nothing resolves to a file."""
    pointer, _ = active_target_pointer(root)
    if not pointer:
        return None
    path = resolve_pointer(pointer, root)
    return path if path.is_file() else None


def active_target_stem(root: Path | str = ROOT) -> str | None:
    """The active target's name (its yml stem), or None."""
    pointer, _ = active_target_pointer(root)
    if not pointer:
        return None
    return resolve_pointer(pointer, root).stem


def load_active_target(root: Path | str = ROOT) -> tuple[Path | None, dict]:
    """``(path, parsed yml)`` of the active target; ``(None, {})`` when unreadable."""
    path = active_target_path(root)
    if path is None:
        return None, {}
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None, {}
    return path, cfg if isinstance(cfg, dict) else {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    what = ap.add_mutually_exclusive_group()
    what.add_argument("--stem", action="store_true", help="print the target name (default)")
    what.add_argument("--path", action="store_true", help="print the target yml path")
    what.add_argument("--source", action="store_true", help="print where the choice came from")
    ap.add_argument("--root", type=Path, default=ROOT, help="project root (default: this repo)")
    args = ap.parse_args(argv)
    pointer, source = active_target_pointer(args.root)
    if not pointer:
        print("no active target: set RR_TARGET, run rr target set, or add target: to project.yml",
              file=sys.stderr)
        return 1
    if args.source:
        print(source)
    elif args.path:
        print(resolve_pointer(pointer, args.root))
    else:
        print(resolve_pointer(pointer, args.root).stem)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
