#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Print the RouteRTL floor this tree declares, so CI can install exactly it.

The floor is declared in two machine-read places: ``requirements.txt``
(``routertl>=X``) and ``tools/preflight/supported_environment.yml``
(``routertl.min``). This prints X when both agree and exits 1 when they
differ or either is missing.

Public CI's main leg installs the newest wheel, which satisfies any floor. A
floor older than the code the tree needs therefore stays green there. The
floor leg in ``.github/workflows/ci.yml`` installs ``routertl==X`` instead and
runs the same checks.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

_REQ_RE = re.compile(r"^\s*routertl\s*>=\s*([0-9][0-9A-Za-z.]*)\s*(?:#.*)?$")


class FloorError(Exception):
    """The declared floors are missing or disagree."""


def requirements_floor(text: str) -> str:
    found = [m.group(1) for line in text.splitlines() if (m := _REQ_RE.match(line))]
    if len(found) != 1:
        raise FloorError(
            f"requirements.txt: expected exactly one 'routertl>=X' line, found {len(found)}")
    return found[0]


def supported_env_floor(text: str) -> str:
    data = yaml.safe_load(text) or {}
    value = (data.get("routertl") or {}).get("min")
    if not value:
        raise FloorError("supported_environment.yml: routertl.min is not set")
    return str(value)


def declared_floor(root: Path = ROOT) -> str:
    req = requirements_floor((root / "requirements.txt").read_text(encoding="utf-8"))
    env = supported_env_floor(
        (root / "tools" / "preflight" / "supported_environment.yml").read_text(encoding="utf-8"))
    if req != env:
        raise FloorError(
            f"floors disagree: requirements.txt routertl>={req}, "
            f"supported_environment.yml routertl.min {env}")
    return req


def main() -> int:
    try:
        print(declared_floor())
    except FloorError as exc:
        print(f"routertl_floor: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
