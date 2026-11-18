"""DS-P2.120: vendored cores under libs/ carry their UPSTREAM licences.

libs/ was dropped from the CERN-OHL-W "hw/**" annotation because it stamped
third-party cores as the owner's copyright. This guard pins the replacement:

  (a) every git-TRACKED path under libs/ is matched by some [[annotations]]
      table in REUSE.toml (no vendored file is left unlicensed);
  (b) no table whose path reaches into libs/ names Daniel J. Mazure as the
      copyright holder unless the path is libs/routertl/** or libs/local/**
      (the two RouteRTL-owned subtrees);
  (c) every SPDX identifier used by any table has a LICENSES/<id>.txt.

Set REUSE_TOML=<path> to judge a different REUSE.toml against the same tracked
file list (used to prove the old file fails: `git show main:REUSE.toml`).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REUSE_TOML = Path(os.environ.get("REUSE_TOML", ROOT / "REUSE.toml"))
LICENSES_DIR = ROOT / "LICENSES"
OWNER = "Daniel J. Mazure"
OWNER_ALLOWED_PREFIXES = ("libs/routertl/", "libs/local/")

_EXPR_KEYWORDS = {"AND", "OR", "WITH"}


def _load_toml(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:
        tomllib = None
    if tomllib is not None:
        return tomllib.loads(text)
    try:
        import tomli
        return tomli.loads(text)
    except ModuleNotFoundError:
        return _minimal_toml(text)


def _minimal_toml(text: str) -> dict:
    """Just enough TOML for REUSE.toml: [[annotations]] tables holding string
    or string-array values (arrays may span lines)."""
    annotations: list[dict] = []
    current: dict | None = None
    buf = ""
    key = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if buf:
            buf += line
            if "]" in line:
                current[key] = re.findall(r'"([^"]*)"', buf)
                buf = ""
            continue
        if line.strip() == "[[annotations]]":
            current = {}
            annotations.append(current)
            continue
        m = re.match(r'\s*([\w-]+)\s*=\s*(.*)$', line)
        if not m or current is None:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val.startswith("["):
            if "]" in val:
                current[key] = re.findall(r'"([^"]*)"', val)
            else:
                buf = val
        elif val.startswith('"'):
            current[key] = val.strip('"')
    return {"annotations": annotations}


def _glob_to_regex(pattern: str) -> re.Pattern:
    """REUSE.toml globs: '*' never crosses '/', '**' does; anchored at root."""
    out = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    return re.compile("^" + out + "$")


def _tables() -> list[dict]:
    tables = _load_toml(REUSE_TOML).get("annotations", [])
    for t in tables:
        paths = t.get("path", [])
        t["_paths"] = [paths] if isinstance(paths, str) else list(paths)
        t["_regex"] = [_glob_to_regex(p) for p in t["_paths"]]
        holders = t.get("SPDX-FileCopyrightText", [])
        t["_holders"] = [holders] if isinstance(holders, str) else list(holders)
    return tables


def _tracked_libs() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", "libs"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    return [ln for ln in out.splitlines() if ln]


def _ids_in(expr: str) -> set[str]:
    toks = re.sub(r"[()]", " ", expr).split()
    return {t for t in toks if t.upper() not in _EXPR_KEYWORDS}


def test_reuse_toml_is_present():
    assert REUSE_TOML.is_file(), REUSE_TOML


def test_every_tracked_libs_path_is_annotated():
    tables = _tables()
    tracked = _tracked_libs()
    assert tracked, "git ls-files libs returned nothing — wrong cwd?"
    orphans = [
        p for p in tracked
        if not any(rx.match(p) for t in tables for rx in t["_regex"])
    ]
    assert not orphans, (
        "tracked libs/ files matched by NO [[annotations]] table in "
        f"{REUSE_TOML}:\n  " + "\n  ".join(orphans)
    )


def test_libs_annotations_do_not_claim_owner_copyright():
    offenders = []
    for t in _tables():
        for pat in t["_paths"]:
            if not pat.startswith("libs/"):
                continue
            if pat.startswith(OWNER_ALLOWED_PREFIXES):
                continue
            if any(OWNER in h for h in t["_holders"]):
                offenders.append((pat, t["_holders"]))
    assert not offenders, (
        f"libs/ tables naming {OWNER!r} outside {OWNER_ALLOWED_PREFIXES}: "
        f"{offenders}"
    )


def test_tracked_libs_paths_resolve_to_a_non_owner_holder_unless_routertl_owned():
    """The path-level check above cannot see a broad table like 'hw/**' or a
    catch-all such as '*.yml' claiming a vendored file; resolve each tracked
    file to its WINNING table (last match wins, per the REUSE spec) and judge
    that."""
    tables = _tables()
    bad = []
    for p in _tracked_libs():
        winner = None
        for t in tables:
            if any(rx.match(p) for rx in t["_regex"]):
                winner = t
        if winner is None:
            continue  # reported by the orphan test
        if p.startswith(OWNER_ALLOWED_PREFIXES):
            continue
        if any(OWNER in h for h in winner["_holders"]):
            bad.append((p, winner["_paths"]))
    assert not bad, (
        f"vendored files whose winning table names {OWNER!r}:\n  "
        + "\n  ".join(f"{p}  <- {pats}" for p, pats in bad)
    )


def test_every_spdx_id_has_a_license_text():
    used: set[str] = set()
    for t in _tables():
        expr = t.get("SPDX-License-Identifier", "")
        used |= _ids_in(expr)
    assert used
    missing = sorted(i for i in used if not (LICENSES_DIR / f"{i}.txt").is_file())
    assert not missing, f"no LICENSES/<id>.txt for: {missing}"


@pytest.mark.parametrize("path", sorted((LICENSES_DIR).glob("*.txt")))
def test_license_texts_have_no_trailing_whitespace(path: Path):
    bad = [n for n, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
           if ln != ln.rstrip()]
    assert not bad, f"{path.name}: trailing whitespace on lines {bad[:5]}"
