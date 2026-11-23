#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""The strict lint count can only go down (DS-P2.352).

DeepSkopion runs `rr linting --profile routertl-vhdl-strict-provisional --src hw/rtl` and compares the findings,
counted per file and rule, with the committed baseline tools/lint/strict_provisional_baseline.json. The baseline is
the lint debt the DS-P3.220 epic pays down: every fix lowers it in the same commit, and nothing may raise it.

  check   measure and compare. A (file, rule) count above the baseline, or a finding in a file/rule the baseline does
          not list, is a rise. A count below the baseline means the baseline is stale: lower it in the same commit
          (`update`), so the committed number is always the measured one.
  update  measure and write the baseline. Refused if any (file, rule) count would go UP, unless --rebaseline is given
          because the profile itself changed (its sha256 differs from the baseline's).

The profile is named explicitly, so plain `rr linting` keeps its default profile until DS-P2.295 declares
linting.profile. The baseline records the sha256 of the profile file of the rr that measured it; a different
profile file cannot be judged against it (exit 2) until a --rebaseline commit records the new one.

Exit codes: 0 holds; 1 a rise or a stale baseline (check), or a refused raise (update); 2 could not judge (rr did
not produce a findings file, the profile file or the baseline cannot be read, or the profile changed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

PROFILE = "routertl-vhdl-strict-provisional"
SRC = "hw/rtl"
BASELINE = Path("tools/lint/strict_provisional_baseline.json")
SCHEMA = "deepskopion.strict-lint-baseline/v1"


class CannotJudge(Exception):
    """The ratchet could not measure or compare (exit 2)."""


def rr_bin() -> str:
    return os.environ.get("DS_RR_BIN", "rr")


def profile_sha256(rr: str) -> str:
    """sha256 of the profile file the given rr ships (routertl_core/profiles/<PROFILE>.yml of rr's interpreter)."""
    exe = shutil.which(rr) or rr
    try:
        first = Path(exe).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError) as exc:
        raise CannotJudge(f"cannot read {exe} to find its interpreter: {exc}") from exc
    if not first.startswith("#!"):
        raise CannotJudge(f"{exe} has no interpreter line, so its profile file cannot be located")
    interp = first[2:].strip().split()
    code = ("import pathlib, routertl_core; "
            f"print(pathlib.Path(routertl_core.__file__).parent / 'profiles' / '{PROFILE}.yml')")
    r = subprocess.run([*interp, "-c", code], capture_output=True, text=True, errors="replace")
    path = Path(r.stdout.strip()) if r.returncode == 0 else None
    if path is None or not path.is_file():
        raise CannotJudge(f"the profile file of {exe} was not found ({(r.stdout + r.stderr).strip()[-300:]})")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tracked(root: Path) -> set[str]:
    """The files under SRC that git tracks: the tree a commit publishes."""
    r = subprocess.run(["git", "ls-files", "-z", "--", SRC], cwd=root, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise CannotJudge(f"`git ls-files {SRC}` exited {r.returncode}: {r.stderr.strip()[-300:]}")
    return {p for p in r.stdout.split("\0") if p}


def measure(rr: str, root: Path) -> Counter:
    """Run the profile over SRC and return Counter{(file, rule): n} from rr's own findings file, for tracked files
    only: an untracked file under SRC (e.g. the gitignored rom_info_pkg.vhd an earlier gate leg materializes for
    lint) is not part of the commit and is reported, not judged."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "findings.json"
        r = subprocess.run([rr, "linting", "--profile", PROFILE, "--src", SRC, "--findings", str(out)],
                           cwd=root, capture_output=True, text=True, errors="replace")
        if not out.is_file():
            raise CannotJudge(f"`{rr} linting --profile {PROFILE}` exited {r.returncode} and wrote no findings "
                              f"file: {(r.stdout + r.stderr).strip()[-600:]}")
        counts = findings_counter(json.loads(out.read_text(encoding="utf-8")))
    keep = tracked(root)
    skipped = Counter()
    for (f, r), n in list(counts.items()):
        if f not in keep:
            skipped[f] += n
            del counts[(f, r)]
    for f, n in sorted(skipped.items()):
        print(f"  not judged (untracked): {f} ({n} finding(s))")
    return counts


def findings_counter(doc: dict) -> Counter:
    if doc.get("profile") not in (PROFILE, None):
        raise CannotJudge(f"the findings file is for profile {doc.get('profile')!r}, not {PROFILE}")
    return Counter((f["file"], f["rule_id"]) for f in doc.get("findings") or [])


def load_baseline(path: Path) -> tuple[str, Counter]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["schema"] == SCHEMA and doc["profile"] == PROFILE
        counts = Counter({(f, r): int(n) for f, rules in doc["counts"].items() for r, n in rules.items()})
        return str(doc["profile_sha256"]), counts
    except (OSError, ValueError, KeyError, AssertionError, TypeError, AttributeError) as exc:
        raise CannotJudge(f"{path} cannot be read as a {SCHEMA} baseline: {exc!r}") from exc


def write_baseline(path: Path, sha: str, counts: Counter) -> None:
    nested: dict = {}
    for (f, r), n in sorted(counts.items()):
        if n:
            nested.setdefault(f, {})[r] = n
    doc = {"schema": SCHEMA, "profile": PROFILE, "profile_sha256": sha, "src": SRC,
           "total": sum(counts.values()), "counts": nested}
    path.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")


def compare(base: Counter, now: Counter) -> tuple[list[str], list[str]]:
    """(rises, stale) as printable lines; keys only in `now` are rises, keys only in `base` count as 0 now."""
    rises, stale = [], []
    for key in sorted(set(base) | set(now)):
        b, n = base.get(key, 0), now.get(key, 0)
        if n > b:
            rises.append(f"  {key[0]}: {key[1]} {b} -> {n}")
        elif n < b:
            stale.append(f"  {key[0]}: {key[1]} {b} -> {n}")
    return rises, stale


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DS-P2.352 strict lint ratchet")
    ap.add_argument("action", choices=("check", "update"))
    ap.add_argument("--rebaseline", action="store_true", help="update only: accept rises because the profile changed")
    ap.add_argument("--findings", type=Path, help="judge this findings file instead of running rr (tests)")
    ap.add_argument("--profile-sha", help="with --findings: the profile sha256 it was measured under (tests)")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    a = ap.parse_args(argv)
    root = Path.cwd()
    try:
        if a.findings:
            now = findings_counter(json.loads(a.findings.read_text(encoding="utf-8")))
            sha = a.profile_sha or ""
        else:
            rr = rr_bin()
            sha = profile_sha256(rr)
            now = measure(rr, root)
        if a.action == "update" and a.rebaseline and not a.baseline.exists():
            write_baseline(a.baseline, sha, now)
            print(f"✓ baseline written: {sum(now.values())} findings under {PROFILE} (profile sha256 {sha[:16]})")
            return 0
        base_sha, base = load_baseline(a.baseline)
        if base_sha != sha and not (a.action == "update" and a.rebaseline):
            raise CannotJudge(f"the profile file differs from the one the baseline was measured under "
                              f"({sha[:16]} now, {base_sha[:16]} in {a.baseline}); re-measure with "
                              f"`{Path(sys.argv[0]).name} update --rebaseline` in its own commit")
    except CannotJudge as exc:
        print(f"strict lint ratchet could not judge: {exc}")
        return 2
    rises, stale = compare(base, now)
    total_b, total_n = sum(base.values()), sum(now.values())
    if a.action == "update":
        if rises and not a.rebaseline:
            print(f"Refused: the baseline can only go down, and {len(rises)} (file, rule) count(s) would rise:")
            print("\n".join(rises))
            return 1
        write_baseline(a.baseline, sha, now)
        print(f"✓ baseline {total_b} -> {total_n} findings ({len(stale)} lowered, {len(rises)} raised by rebaseline)")
        return 0
    if rises:
        print(f"Strict lint ({PROFILE}) rose in {len(rises)} (file, rule) place(s); the count can only go down:")
        print("\n".join(rises))
        return 1
    if stale:
        print(f"Strict lint fell in {len(stale)} (file, rule) place(s) but {a.baseline} still lists the old counts:")
        print("\n".join(stale))
        print(f"Lower the baseline in this commit: {Path(sys.argv[0]).name} update")
        return 1
    print(f"✓ strict lint ratchet holds: {total_n} findings under {PROFILE}, equal to the baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
