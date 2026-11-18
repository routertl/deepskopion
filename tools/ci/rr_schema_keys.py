#!/usr/bin/env python3
"""Check every project.yml and target-yml key against the installed rr's schema.

``rr schema --json`` is the installed RouteRTL release's own field reference:
dotted keys, each with a scope (``root``, ``target`` or ``both``). rr ignores a
key it does not know, or at best warns, so a key that only a newer release
reads looks configured and does nothing. Public CI's floor leg runs this
against exactly the declared floor, where such a key is reported.

A key is accepted when the schema lists it, or when the schema lists keys below
it (a section such as ``linux``), in which case the walk continues into its
children. A key the schema lists with no keys below it is a leaf: its value is
not walked. A key is reported when the schema has neither, and when its scope
excludes the file it is in (a ``root`` key in a target yml, a ``target`` key in
project.yml).

``tools/ci/rr_schema_key_exceptions.txt`` lists keys that rr reads but its
schema omits. Each line is ``<dotted key> <ticket> <reason>``; a line with no
ticket is refused, and an exception that matches no key in the tree is
reported, so the list cannot outlive its keys.

  python tools/ci/rr_schema_keys.py                 # runs rr schema --json
  python tools/ci/rr_schema_keys.py --schema s.json # a saved envelope

Exit 0 when nothing is reported, 1 otherwise, 2 when the schema cannot be read.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)

_TICKET_RE = re.compile(r"^[A-Z]+-(?:[PT][0-4]|D)\.[0-9]+$")


class SchemaError(Exception):
    """The schema envelope cannot be read."""


def load_schema(text: str) -> Dict[str, dict]:
    """The ``data.entries`` map of an ``rr schema --json`` envelope."""
    try:
        env = json.loads(text)
        entries = env["data"]["entries"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SchemaError(f"not an rr schema --json envelope: {exc}") from exc
    if not isinstance(entries, dict) or not entries:
        raise SchemaError("rr schema --json returned no entries")
    return entries


def load_exceptions(text: str) -> Dict[str, str]:
    """``{dotted key: ticket}``; a line without a ticket raises ValueError."""
    out: Dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3 or not _TICKET_RE.match(parts[1]):
            raise ValueError(
                f"rr_schema_key_exceptions.txt:{n}: expected '<key> <ticket> <reason>', "
                f"got {line!r}")
        out[parts[0]] = parts[1]
    return out


def _has_children(schema: Dict[str, dict], path: str) -> bool:
    prefix = path + "."
    return any(k.startswith(prefix) for k in schema)


def walk(node, schema: Dict[str, dict], where: str,
         prefix: str = "") -> Iterable[Tuple[str, str]]:
    """Yield ``(dotted key, problem)`` for the keys of ``node``.

    ``where`` is ``root`` for project.yml and ``target`` for a target yml."""
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        entry = schema.get(path)
        if entry is not None:
            scope = entry.get("scope")
            if where == "root" and scope == "target":
                yield path, "scope target: belongs in a target yml"
            elif where == "target" and scope == "root":
                yield path, "scope root: belongs in project.yml"
            if _has_children(schema, path):
                yield from walk(value, schema, where, path)
        elif _has_children(schema, path):
            yield from walk(value, schema, where, path)
        else:
            yield path, "not in this rr release's schema"


def check(root: Path, schema: Dict[str, dict],
          exceptions: Dict[str, str]) -> List[str]:
    """The report lines for project.yml and targets/*.yml under ``root``."""
    files = [(root / "project.yml", "root")]
    files += [(p, "target") for p in all_target_files(root)]  # DS-P1.103: products and bench
    lines: List[str] = []
    used = set()
    for path, where in files:
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        for key, problem in walk(data, schema, where):
            if key in exceptions and problem.startswith("not in"):
                used.add(key)
                continue
            lines.append(f"{path.relative_to(root)}: {key}: {problem}")
    for key in sorted(set(exceptions) - used):
        lines.append(f"tools/ci/rr_schema_key_exceptions.txt: {key} ({exceptions[key]}): "
                     f"no file uses this key, or the schema now lists it; remove the line")
    return lines


def _rr_schema_json() -> str:
    proc = subprocess.run(["rr", "schema", "--json"], capture_output=True, text=True,
                          errors="replace", check=False)
    if proc.returncode != 0:
        raise SchemaError(f"rr schema --json exited {proc.returncode}: {proc.stderr[-500:]}")
    return proc.stdout


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tools/ci/rr_schema_keys.py")
    p.add_argument("--schema", type=Path, help="a saved rr schema --json envelope")
    p.add_argument("--root", type=Path, default=ROOT)
    args = p.parse_args(argv)
    try:
        text = (args.schema.read_text(encoding="utf-8", errors="replace")
                if args.schema else _rr_schema_json())
        schema = load_schema(text)
    except (SchemaError, OSError) as exc:
        print(f"rr_schema_keys: {exc}", file=sys.stderr)
        return 2
    exc_path = args.root / "tools" / "ci" / "rr_schema_key_exceptions.txt"
    exceptions = load_exceptions(exc_path.read_text(encoding="utf-8")) if exc_path.is_file() else {}
    lines = check(args.root, schema, exceptions)
    for line in lines:
        print(line)
    print(f"rr_schema_keys: {len(schema)} schema keys, {len(lines)} reported")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
