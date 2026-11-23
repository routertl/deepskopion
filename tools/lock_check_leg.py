#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""The pre-commit leg that keeps the per-target routertl locks in step with the tree.

Since routertl 5.5.0 (RTL-P2.2112) a project with targets keeps one lock per target,
targets/<t>.routertl.lock, and no root routertl.lock (DS-P2.287). `rr lock --check` judges the working tree, so
the leg checks a target only when the commit stages a path that target's lock covers, and only when those paths
have no unstaged changes. It checks EVERY tracked target when the commit touches ip.lock, project.yml, a path two
or more targets share, or a target file it cannot map, or an added HDL/constraint file no lock lists yet
(DS-P2.306), or the routertl floor declaration (requirements.txt, tools/preflight/supported_environment.yml;
DS-P2.347), or when run with --all.

On every commit, without calling rr, every tracked lock's sdk.version must equal the declared floor
(supported_environment.yml routertl.min, as staged): a lock written by an rr newer than the floor fails
`rr lock --check` for a user on the floor, and a floor raised without re-locking fails it for everyone (DS-P2.347).

Fails closed: no tracked target lock, a tracked root routertl.lock, a lock that does not parse, or an rr that
cannot judge a target each stop the commit. Targets that rr itself refuses to lock are listed in
tools/lock_exemptions.yml (each with rr's refusal and the ticket that removes it).

Exit codes:
  0  every checked lock holds, or the commit touches nothing a lock covers
  1  a lock is stale, missing or in the wrong layout for this commit
  2  the leg could not judge the commit
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT_LOCK = "routertl.lock"
#: DS-P1.103: a target's file is targets/<t>.yml (a product) or bench/targets/<t>.yml (bench-only, not exported);
#: rr keeps the lock of either at targets/<t>.routertl.lock. Same order as tools/target_files.target_file().
TARGET_DIRS = ("targets", "bench/targets")


def target_yml(target: str) -> str:
    """The repo-relative file of *target*: the first of TARGET_DIRS that has it, else targets/<t>.yml."""
    for d in TARGET_DIRS:
        if Path(d, f"{target}.yml").is_file():
            return f"{d}/{target}.yml"
    return f"targets/{target}.yml"


def _target_inputs(target: str) -> set[str]:
    return {f"{d}/{target}.yml" for d in TARGET_DIRS} | {f"targets/{target}{LOCK_SUFFIX}"}
LOCK_SUFFIX = ".routertl.lock"
#: Files that choose what every target's lock resolves.
SHARED_INPUTS = ("project.yml", "ip.lock")
#: DS-P2.347: where the routertl floor is declared (tools/ci/routertl_floor.py keeps the two in agreement). A change
#: to either selects every target, and every tracked lock must record FLOOR_FILE's routertl.min as its sdk.version.
FLOOR_FILE = "tools/preflight/supported_environment.yml"
FLOOR_INPUTS = ("requirements.txt", FLOOR_FILE)
EXEMPTIONS = Path("tools/lock_exemptions.yml")
MISMATCH = "Lockfile mismatch"
UNRESOLVED = ("PackageResolutionError", "Failed to resolve project state")
#: Upper bound on one `rr pkg install` the leg runs for a target whose pinned packages are absent (seconds;
#: DS_LOCK_INSTALL_TIMEOUT_S overrides). An install that has not finished by then is a verdict of 2, not a hang.
INSTALL_TIMEOUT_S = 300


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          errors="replace", check=False)


def covered_paths(lock: dict) -> set[str]:
    """Every repository path the lock records."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "path" and isinstance(value, str):
                    found.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, str):
                    if "/" in item and not item.startswith("$"):
                        found.add(item)
                else:
                    walk(item)

    walk(lock)
    excluded = {
        item["path"]
        for key in ("lock_exclude", "sources")
        for item in (lock.get(key) or ())
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and (key == "lock_exclude" or item.get("excluded"))
    }
    return {p for p in found if not p.startswith("$")} - excluded


def tracked_target_locks() -> dict[str, str]:
    """{target name: lock path} for every targets/<t>.routertl.lock in the index."""
    out = _git("ls-files", "-z", "--", f"targets/*{LOCK_SUFFIX}")
    return {Path(p).name[: -len(LOCK_SUFFIX)]: p for p in out.stdout.split("\0") if p}


def exempt_targets() -> set[str]:
    if not EXEMPTIONS.is_file():
        return set()
    doc = yaml.safe_load(EXEMPTIONS.read_text(encoding="utf-8")) or {}
    return {e["target"] for e in doc.get("exemptions") or () if isinstance(e, dict) and e.get("target")}


def _staged() -> list[str] | None:
    r = _git("diff", "--cached", "--name-only", "-z")
    return None if r.returncode != 0 else [p for p in r.stdout.split("\0") if p]


#: DS-P2.306: suffixes rr autodiscovers into a target's sources or constraints. A commit that ADDS such a file that no
#: lock lists yet cannot be mapped to the targets whose closure it enters (ccc83cb0 and 906b6a14 each added a hw/sim
#: bench, left all 9 locks stale and passed), so it selects every target.
NEW_FILE_SUFFIXES = (".vhd", ".vhdl", ".v", ".sv", ".svh", ".vh", ".xdc", ".sdc", ".pdc")


def _staged_added() -> list[str] | None:
    r = _git("diff", "--cached", "--name-only", "--diff-filter=AR", "-z")
    return None if r.returncode != 0 else [p for p in r.stdout.split("\0") if p]


def select_targets(staged: list[str], covered: dict[str, set[str]], check_all: bool,
                   added: list[str] = ()) -> tuple[list[str], str]:
    """The targets to check for *staged*, and why. Narrowing is to the targets whose own inputs changed; anything
    shared or unmappable selects all of them, and so does an *added* HDL or constraint file no lock lists yet."""
    names = sorted(covered)
    if check_all:
        return names, "--all"
    listed = set().union(*covered.values()) if covered else set()
    for p in added:
        if p.endswith(NEW_FILE_SUFFIXES) and p not in listed:
            return names, f"{p} is a new file no lock lists; it may enter any target's closure"
    owners: dict[str, set[str]] = {}
    for t, paths in covered.items():
        for p in paths | _target_inputs(t):
            owners.setdefault(p, set()).add(t)
    chosen: set[str] = set()
    for p in staged:
        if p in SHARED_INPUTS:
            return names, f"{p} is shared by every target"
        if p in FLOOR_INPUTS:
            return names, f"{p} declares the routertl floor every lock records"
        hit = owners.get(p)
        if hit and len(hit) > 1:
            return names, f"{p} is shared by {len(hit)} targets"
        if hit:
            chosen |= hit
        elif p.startswith(tuple(f"{d}/" for d in TARGET_DIRS)) and (p.endswith(".yml") or p.endswith(LOCK_SUFFIX)):
            return names, f"{p} maps to no tracked target lock"
    return sorted(chosen), "the targets whose inputs this commit stages"


def missing_packages(lock: dict) -> list[str]:
    """The packages *lock* pins whose files are not in this tree (a fresh clone or land tree has no libs/; the
    CAN FD targets' CTU package is opt-in). Judged from the lock's own pins: each dependency's local_path and
    files."""
    missing = []
    for dep in lock.get("dependencies") or ():
        if not isinstance(dep, dict):
            continue
        local = dep.get("local_path")
        files = [f for f in dep.get("files") or () if isinstance(f, str)]
        if (local and not Path(local).is_dir()) or any(not Path(f).is_file() for f in files):
            missing.append(str(dep.get("name") or local))
    return missing


def _install(rr: str, target: str, lock: dict) -> tuple[int, str]:
    """Install the packages *target* pins before judging it: `rr lock --check` on a tree without them reports a
    target unresolvable, or (a shared package absent) a mismatch that is not one (RTL-P2.2362). Fails closed: an install that
    fails, leaves a pinned package missing, or changes a tracked file is a verdict of 2."""
    missing = missing_packages(lock)
    if not missing:
        return 0, ""
    print(f"  {target}: {len(missing)} pinned package(s) not installed here ({', '.join(missing[:4])}"
          f"{', ...' if len(missing) > 4 else ''}); installing them: RR_TARGET={target_yml(target)} rr pkg install")
    env = dict(os.environ, RR_TARGET=target_yml(target))
    before = _git("diff", "--name-only", "-z").stdout
    limit = float(os.environ.get("DS_LOCK_INSTALL_TIMEOUT_S", INSTALL_TIMEOUT_S))
    try:
        proc = subprocess.run([rr, "pkg", "install"], capture_output=True, text=True, errors="replace",
                              check=False, stdin=subprocess.DEVNULL, env=env, timeout=limit)
    except subprocess.TimeoutExpired:
        return 2, (f"rr pkg install for {target} did not finish in {limit:g} s (no network or package cache?): "
                   "its lock cannot be judged here")
    except OSError as exc:
        return 2, f"rr could not start: {exc}"
    out = proc.stdout + proc.stderr
    if proc.returncode != 0:
        return 2, out + f"\nrr pkg install exited {proc.returncode} for {target}: its lock cannot be judged here"
    changed = _git("diff", "--name-only", "-z")
    if changed.returncode != 0 or changed.stdout != before:
        return 2, out + f"\nrr pkg install for {target} changed tracked files; the commit's tree is not the one judged"
    still = missing_packages(lock)
    if still:
        return 2, out + f"\nafter rr pkg install, {target} still lacks pinned package(s): {', '.join(still)}"
    return 0, ""


def _check(rr: str, target: str) -> tuple[int, str]:
    env = dict(os.environ, RR_TARGET=target_yml(target))
    try:
        proc = subprocess.run([rr, "lock", "--check"], capture_output=True, text=True, errors="replace",
                              check=False, stdin=subprocess.DEVNULL, env=env)
    except OSError as exc:
        return 2, f"rr could not start: {exc}"
    out = proc.stdout + proc.stderr
    if proc.returncode == 0:
        return 0, out
    if MISMATCH in out or "No lockfile for this target" in out:
        return 1, out
    if any(u in out for u in UNRESOLVED):
        return 2, out + (f"\nrr could not resolve {target}'s packages: run `rr pkg install --target {target}`, "
                         "then commit again.")
    return 2, out + f"\nrr lock --check exited {proc.returncode} for {target} without naming a mismatch"


class FloorError(Exception):
    """The floor declaration is tracked but names no usable routertl.min."""


def staged_floor() -> str | None:
    """routertl.min from the staged FLOOR_FILE; None when that file is not tracked (nothing declares a floor)."""
    shown = _git("show", f":{FLOOR_FILE}")
    if shown.returncode != 0:
        return None
    try:
        doc = yaml.safe_load(shown.stdout) or {}
    except yaml.YAMLError as exc:
        raise FloorError(f"the staged {FLOOR_FILE} does not parse: {exc}") from exc
    value = (doc.get("routertl") or {}).get("min") if isinstance(doc, dict) else None
    if value is None or not str(value).strip():
        raise FloorError(f"the staged {FLOOR_FILE} sets no routertl.min")
    return str(value).strip()


def floor_mismatches(floor: str, parsed: dict[str, dict]) -> list[tuple[str, str]]:
    """(target, recorded sdk.version) for every staged lock whose sdk.version is not *floor*. Parse only."""
    off = []
    for target in sorted(parsed):
        sdk = parsed[target].get("sdk") if isinstance(parsed[target], dict) else None
        recorded = str(sdk.get("version") or "").strip() if isinstance(sdk, dict) else ""
        if recorded != floor:
            off.append((target, recorded or "(none)"))
    return off


def main(argv: list[str] | None = None) -> int:
    check_all = "--all" in (argv if argv is not None else sys.argv[1:])
    root_tracked = _git("ls-files", "--error-unmatch", "--", ROOT_LOCK).returncode == 0
    locks = tracked_target_locks()
    if root_tracked:
        print(f"A root {ROOT_LOCK} is tracked. Since routertl 5.5.0 a project with targets keeps one lock per "
              f"target (targets/<t>{LOCK_SUFFIX}); the root file is the old layout and is not checked by rr. "
              f"Remove it from the index (git rm --cached {ROOT_LOCK}) and track the per-target locks.")
        return 1
    if not locks:
        print(f"No targets/<t>{LOCK_SUFFIX} is tracked, so nothing would check the lock: refusing. Run "
              "`rr lock --all-targets` and track the per-target locks (tools/lock_exemptions.yml lists the "
              "targets rr refuses to lock).")
        return 1
    covered: dict[str, set[str]] = {}
    parsed: dict[str, dict] = {}
    for target, path in locks.items():
        staged_lock = _git("show", f":{path}")
        try:
            parsed[target] = yaml.safe_load(staged_lock.stdout) or {}
            covered[target] = covered_paths(parsed[target])
        except yaml.YAMLError as exc:
            print(f"the staged {path} does not parse: {exc}")
            return 2
    try:
        floor = staged_floor()
    except FloorError as exc:
        print(exc)
        return 2
    if floor is not None:
        off = floor_mismatches(floor, parsed)
        if off:
            print(f"The routertl floor is {floor} ({FLOOR_FILE} routertl.min), but {len(off)} target lock(s) "
                  "record another sdk.version, so `rr lock --check` fails for a user on the floor:")
            for target, recorded in off:
                print(f"  {locks[target]}: sdk.version {recorded}, floor {floor}")
            print(f"Re-lock each with `RR_TARGET=<target yml> rr lock` under routertl {floor} and stage the locks "
                  "in the same commit as the floor.")
            return 1
    staged = _staged()
    added = _staged_added()
    if staged is None or added is None:
        print("git diff --cached failed: cannot map the commit to targets")
        return 2
    targets, why = select_targets(staged, covered, check_all, added)
    if not targets:
        print("✓ routertl locks: this commit stages nothing a target lock covers")
        return 0
    relevant = set().union(*(covered[t] | _target_inputs(t) for t in targets))
    relevant |= set(SHARED_INPUTS)
    unstaged = _git("diff", "--name-only", "-z")
    dirty = sorted(p for p in unstaged.stdout.split("\0") if p and p in relevant)
    if dirty:
        print("These paths the checked locks cover have unstaged changes, so the tree")
        print("rr lock --check would judge is not the one being committed:")
        for path in dirty:
            print(f"  {path}")
        print("Stage them or restore them, then commit again.")
        return 1
    rr = os.environ.get("DS_RR_BIN", "rr")
    print(f"Checking {len(targets)} target lock(s) ({why}): {', '.join(targets)}")
    worst = 0
    for target in targets:
        rc, out = _install(rr, target, parsed[target])
        if rc == 0:
            rc, out = _check(rr, target)
        if rc:
            print(f"--- {target}: {'STALE' if rc == 1 else 'COULD NOT JUDGE'}")
            print(out.rstrip())
            worst = max(worst, rc) if worst != 1 else 1
    if worst == 0:
        print(f"✓ routertl locks hold for {len(targets)} target(s)")
    return 1 if worst == 1 else worst


if __name__ == "__main__":
    sys.exit(main())
