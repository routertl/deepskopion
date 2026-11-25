#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""Refuse a `pip install -r requirements.txt` that downgraded a package.

The README installs routertl first and requirements.txt second. An exact pin
here on a package routertl's dependencies had already installed newer
DOWNGRADED it (DS-P2.337: pytest 9.2.0 -> 9.0.2, pluggy, packaging,
markdown-it-py 4.3.0 -> 3.0.0, ... on a clean machine with routertl 5.5.0).

CI records `pip freeze` before the install and runs this after it:

    python -m pip freeze > before.txt
    python -m pip install -r requirements.txt
    python tools/ci/requirements_no_downgrade.py before.txt

Exit 0: no installed package went to a lower version (upgrades and new
packages are fine). Exit 1: each downgrade is printed. Exit 2: the input could
not be read.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


def parse_freeze(text: str) -> dict[str, str]:
    """``name -> version`` for every ``name==version`` line of ``pip freeze``."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue   # editable installs and direct URLs carry no comparable version
        name, _, version = line.partition("==")
        out[canonicalize_name(name)] = version.strip()
    return out


def downgrades(before: dict[str, str], after: dict[str, str]) -> list[tuple[str, str, str]]:
    """``(name, before, after)`` for each package whose version went down."""
    found = []
    for name in sorted(set(before) & set(after)):
        try:
            if Version(after[name]) < Version(before[name]):
                found.append((name, before[name], after[name]))
        except InvalidVersion:
            if after[name] != before[name]:
                found.append((name, before[name], after[name]))
    return found


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: requirements_no_downgrade.py <pip-freeze-before.txt>", file=sys.stderr)
        return 2
    try:
        before = parse_freeze(Path(args[0]).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"cannot read {args[0]}: {exc}", file=sys.stderr)
        return 2
    if not before:
        print(f"{args[0]} lists no packages; it is not a pip freeze", file=sys.stderr)
        return 2
    now = subprocess.run([sys.executable, "-m", "pip", "freeze"],
                         capture_output=True, text=True, check=False)
    if now.returncode != 0:
        print(f"pip freeze failed: {now.stderr.strip()}", file=sys.stderr)
        return 2
    found = downgrades(before, parse_freeze(now.stdout))
    if found:
        print("pip install -r requirements.txt downgraded installed packages:")
        for name, old, new in found:
            print(f"  {name} {old} -> {new}")
        print("Use a floor (>=) in requirements.txt, not an exact pin.")
        return 1
    print(f"no package downgraded ({len(before)} were installed before)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
