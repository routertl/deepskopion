#!/usr/bin/env python3
"""DS-P2.109 — cocotb dependency-floor consistency guard.

THE DEFECT (measured 2026-09-03). Deepskopion's ``requirements.txt`` line 1
pinned ``cocotb==2.0.1`` while the routertl distribution it depends on declares
a floor of ``cocotb>=2.1`` (routertl ``pyproject.toml`` ``[project].dependencies``,
raised by RTL-P3.1682). The two gates share ONE interpreter's site-packages
(``~/.local/lib/pythonX.Y/site-packages`` on this bench), so they fought over
it in turn: the routertl canonical pre-commit editable reinstall pulled
cocotb up to 2.1.0 at 09:03Z, and the owner-attended deepskopion HDL gate,
installing this repo's ``requirements.txt``, pulled it back down to 2.0.1 at
10:22:42Z. Under 2.0.1 routertl's RTL-T1.37 branch reads differently; under
2.1 deepskopion direct sims can hit the very "No GPI_USERS" failure RTL-T1.37
fixes in rr. A ``==`` pin against a moving floor is not a lock — it is a
tug-of-war, and neither side's green bar means what it claims while it is on.

This module is the regression guard for that whole class: a version constraint
declared by one registered repo that no single installed version can satisfy
alongside another registered repo's constraint. It is deliberately
self-contained (stdlib + ``packaging``) so it runs in a lone clone with only
``routertl`` pip-installed — no bench-local sibling-checkout paths, which the
DS-X1 pollution guard would (rightly) refuse.

Two checks, two CLI subcommands, both fail CLOSED (the no-third-state rule
this repo already lives by — a guard that goes green because it could not look
is the false green ``docs/GOLDEN_PATH.md`` catalogues):

  check-constraints
      Assert the cocotb constraints across the registered repos are mutually
      satisfiable by ONE version. From the deepskopion vantage the repos it
      must agree with are its own ``requirements.txt`` cocotb pin and the
      installed routertl distribution's declared cocotb requirement (read from
      package metadata, i.e. routertl's pyproject ``cocotb>=2.1``). RED when
      they cannot both hold (``==2.0.1`` vs ``>=2.1`` — the shipped defect).

  guard-install
      Refuse BEFORE the deepskopion gate installs ``requirements.txt`` into
      the shared site-packages, judging what the INSTALL WOULD DO — i.e. the
      version set ``requirements.txt`` admits — not the pre-existing interpreter
      state. RED only when ``requirements.txt`` itself admits a version that
      violates the routertl floor (so ``pip install -r`` could place, or leave,
      a forbidden cocotb into the shared interpreter). A floor-violating cocotb
      already installed with a floor-safe ``requirements.txt`` is ALLOWED — that
      install is the corrective upgrade, reported as a note, never a refusal.
      This stops the downgrade half of the ping-pong at its source without
      blocking the very install that fixes it. Prints the remedy, not just the
      refusal.

The cross-repo guard that sweeps registered repos' requirements/pyproject
in one place belongs in top-level tooling (which alone can see all sibling
checkouts); this module is the deepskopion-local
half — it constrains this repo against its declared routertl dependency, which
is exactly the pair that ping-ponged.

CLI:
    python3 tools/cocotb_dependency_floor.py check-constraints [--json]
    python3 tools/cocotb_dependency_floor.py guard-install [--json]
        [--requirements PATH]        # default: <repo>/requirements.txt
        [--routertl-floor SPEC]      # default: installed routertl metadata
        [--no-installed-check]       # skip the informational installed-version note

Exit codes: 0 = consistent; 1 = a real inconsistency (with remedy); 2 = the
guard could not run (e.g. routertl not installed, no cocotb line) — a gate
that cannot look is not a green gate.
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import sys
from pathlib import Path
from typing import Iterable

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = REPO_ROOT / "requirements.txt"

# Grounded real cocotb release points, so the satisfiability probe below is not
# limited to the exact boundary literals appearing in the specifiers (which can
# miss the feasible region of an unusual operator such as ``!=``). These are the
# cocotb versions that have actually existed across the 1.x/2.x/3.x line.
_GROUNDED_COCOTB_VERSIONS = (
    "1.6.0", "1.7.2", "1.8.1", "1.9.2",
    "2.0.0", "2.0.1", "2.1.0", "2.2.0", "3.0.0",
)


class GuardError(RuntimeError):
    """Raised when the guard cannot evaluate its inputs (-> exit 2)."""


def _cocotb_spec_from_lines(lines: Iterable[str]) -> tuple[str, SpecifierSet] | None:
    """Return (raw_line, SpecifierSet) for the cocotb (not cocotb-*) pin, or None."""
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            req = Requirement(line)
        except Exception:
            continue
        # ``cocotb`` proper — never cocotb-bus / cocotbext-* / cocotb-test.
        if req.name.lower() == "cocotb":
            return raw.strip(), req.specifier
    return None


def read_requirements_cocotb(path: Path) -> tuple[str, SpecifierSet]:
    if not path.is_file():
        raise GuardError(f"requirements file not found: {path}")
    got = _cocotb_spec_from_lines(path.read_text().splitlines())
    if got is None:
        raise GuardError(f"no bare `cocotb` requirement found in {path}")
    return got


def routertl_cocotb_floor() -> SpecifierSet:
    """The cocotb constraint declared by the INSTALLED routertl distribution.

    Read from package metadata (Requires-Dist), which is exactly routertl's
    ``pyproject.toml`` ``[project].dependencies`` cocotb entry after install.
    Fails closed if routertl is absent or declares no cocotb requirement.
    """
    try:
        reqs = md.requires("routertl") or []
    except md.PackageNotFoundError as exc:  # pragma: no cover - env dependent
        raise GuardError(
            "routertl is not installed, so its cocotb floor cannot be read; "
            "deepskopion depends on routertl (`pip install routertl`)."
        ) from exc
    for entry in reqs:
        try:
            req = Requirement(entry)
        except Exception:
            continue
        if req.name.lower() == "cocotb":
            return req.specifier
    raise GuardError(
        "installed routertl declares no cocotb requirement; cannot derive its "
        "floor (expected e.g. cocotb>=2.1 from routertl pyproject)."
    )


def routertl_requirements() -> list[Requirement]:
    """Every requirement the INSTALLED routertl declares for a plain install.

    DS-P1.86: a deepskopion pin on any package routertl also requires must
    leave at least one version both accept, or ``pip install -r
    requirements.txt`` beside routertl is ResolutionImpossible (measured
    2026-11-04: ``cocotb-test==0.2.6`` here, ``cocotb-test>=0.3`` in routertl
    5.2.0). Requirements behind an extra are not part of a plain install.
    """
    try:
        entries = md.requires("routertl") or []
    except md.PackageNotFoundError as exc:  # pragma: no cover - env dependent
        raise GuardError(
            "routertl is not installed, so its requirements cannot be read; "
            "deepskopion depends on routertl (`pip install routertl`)."
        ) from exc
    out: list[Requirement] = []
    for entry in entries:
        try:
            req = Requirement(entry)
        except Exception:
            continue
        if req.marker is not None and not req.marker.evaluate({"extra": ""}):
            continue
        out.append(req)
    return out


def shared_pin_conflicts(
    requirement_lines: Iterable[str], routertl_reqs: Iterable[Requirement]
) -> list[tuple[str, str]]:
    """(deepskopion line, routertl requirement) pairs no single version satisfies.

    ``cocotb`` itself is left to the floor checks above; every other package
    deepskopion pins and routertl also requires is judged here.
    """
    by_name: dict[str, list[Requirement]] = {}
    for req in routertl_reqs:
        by_name.setdefault(canonicalize_name(req.name), []).append(req)
    conflicts: list[tuple[str, str]] = []
    for raw in requirement_lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            ds_req = Requirement(line)
        except Exception:
            continue
        name = canonicalize_name(ds_req.name)
        if name == "cocotb":
            continue
        for rt_req in by_name.get(name, []):
            ok, _ = is_satisfiable([ds_req.specifier, rt_req.specifier])
            if not ok:
                conflicts.append((raw.strip(), str(rt_req)))
    return conflicts


def installed_cocotb_version() -> Version | None:
    try:
        return Version(md.version("cocotb"))
    except (md.PackageNotFoundError, InvalidVersion):
        return None


def _candidate_versions(specs: Iterable[SpecifierSet]) -> list[Version]:
    """Version probe points: every literal in every specifier, its dev/post
    neighbours (to straddle strict < / > boundaries), plus grounded real
    releases and whatever cocotb is installed right now."""
    seen: set[str] = set()
    for text in _GROUNDED_COCOTB_VERSIONS:
        seen.add(text)
    inst = installed_cocotb_version()
    if inst is not None:
        seen.add(str(inst))
    for spec in specs:
        for s in spec:
            base = s.version
            seen.add(base)
            # ``.dev0`` sorts just below the release, ``.post1`` just above —
            # this straddles ``<X`` / ``>X`` strict boundaries.
            seen.add(f"{base}.dev0")
            seen.add(f"{base}.post1")
    out: list[Version] = []
    for text in seen:
        try:
            out.append(Version(text))
        except InvalidVersion:
            continue
    return sorted(out)


def is_satisfiable(specs: list[SpecifierSet]) -> tuple[bool, Version | None]:
    """True + a witness version if some cocotb version satisfies EVERY spec."""
    for v in _candidate_versions(specs):
        if all(spec.contains(v, prereleases=True) for spec in specs):
            return True, v
    return False, None


def version_admitted_but_violating(
    admits: SpecifierSet, floor: SpecifierSet
) -> Version | None:
    """A version the ``admits`` spec allows that the ``floor`` spec forbids.

    None means every version deepskopion could install also satisfies the
    routertl floor (i.e. ``admits`` is floor-safe)."""
    for v in _candidate_versions([admits, floor]):
        if admits.contains(v, prereleases=True) and not floor.contains(
            v, prereleases=True
        ):
            return v
    return None


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def cmd_check_constraints(args: argparse.Namespace) -> int:
    req_path = Path(args.requirements) if args.requirements else DEFAULT_REQUIREMENTS
    ds_line, ds_spec = read_requirements_cocotb(req_path)
    floor = (
        SpecifierSet(args.routertl_floor)
        if args.routertl_floor
        else routertl_cocotb_floor()
    )
    constraints = {
        f"deepskopion requirements.txt (`{ds_line}`)": ds_spec,
        f"routertl distribution (`cocotb{floor}`)": floor,
    }
    cocotb_ok, witness = is_satisfiable(list(constraints.values()))
    conflicts = shared_pin_conflicts(
        req_path.read_text().splitlines(), routertl_requirements())
    ok = cocotb_ok and not conflicts
    payload = {
        "check": "check-constraints",
        "ok": ok,
        "witness": str(witness) if witness else None,
        "constraints": {k: str(v) for k, v in constraints.items()},
        "shared_pin_conflicts": [
            {"deepskopion": ds, "routertl": rt} for ds, rt in conflicts],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    if conflicts and not args.json:
        print("❌ requirements.txt pins a package that routertl also requires, "
              "and no version satisfies both (DS-P1.86):")
        for ds, rt in conflicts:
            print(f"    deepskopion `{ds}`  vs  routertl `{rt}`")
        print("\n   `pip install -r requirements.txt` beside this routertl is"
              "\n   ResolutionImpossible. Remedy: track routertl's requirement"
              "\n   (a range inside it), not an older `==` pin.")
    if cocotb_ok and conflicts:
        return 1
    if ok:
        if not args.json:
            print(
                f"✓ cocotb constraints are mutually satisfiable (e.g. {witness}):"
            )
            for name, spec in constraints.items():
                print(f"    {name}: cocotb{spec}")
        return 0
    if not args.json:
        print("❌ cocotb constraints are NOT mutually satisfiable:")
        for name, spec in constraints.items():
            print(f"    {name}: cocotb{spec}")
        print(
            "\n   No single installed cocotb can satisfy every registered repo,"
            "\n   so the two gates downgrade/upgrade the ONE shared site-packages"
            "\n   in turn and break each other (DS-P2.109)."
            "\n   Remedy: track the routertl floor — set deepskopion"
            f"\n   requirements.txt to `cocotb{floor},<3` (or a range inside it)."
        )
    return 1


def cmd_guard_install(args: argparse.Namespace) -> int:
    req_path = Path(args.requirements) if args.requirements else DEFAULT_REQUIREMENTS
    ds_line, ds_spec = read_requirements_cocotb(req_path)
    floor = (
        SpecifierSet(args.routertl_floor)
        if args.routertl_floor
        else routertl_cocotb_floor()
    )
    violations: list[str] = []
    notes: list[str] = []

    # The ONLY gating question is what THIS install would leave in the shared
    # interpreter, not what happens to be there now. `pip install -r
    # requirements.txt` resolves to a version admitted by ``ds_spec``; if every
    # such version satisfies the floor, the install is floor-safe — and if the
    # currently-installed version violates the floor, this install is exactly
    # the corrective upgrade. So gate solely on the requirement set.
    would_install = version_admitted_but_violating(ds_spec, floor)
    if would_install is not None:
        violations.append(
            f"requirements.txt (`{ds_line}`) admits cocotb {would_install}, which"
            f" violates the routertl floor cocotb{floor}"
        )

    # The installed-version leg is INFORMATIONAL, never gating. A floor-violating
    # version already in the shared site-packages is not a reason to refuse a
    # floor-safe install — that install is the fix. (The one case where the
    # install would *leave* a violating version — ds_spec itself admitting it,
    # so pip won't upgrade — is already caught by ``would_install`` above.)
    installed = None
    if not args.no_installed_check:
        installed = installed_cocotb_version()
        if installed is not None and not floor.contains(installed, prereleases=True):
            if ds_spec.contains(installed, prereleases=True):
                # ds_spec admits the installed violating version -> would_install
                # has already flagged it; nothing to add here.
                pass
            else:
                notes.append(
                    f"shared site-packages currently holds cocotb {installed}"
                    f" (violates cocotb{floor}), but requirements.txt (`{ds_line}`)"
                    f" excludes it, so installing UPGRADES to a floor-safe cocotb"
                    f" — that install is the corrective action, not a violation."
                )

    payload = {
        "check": "guard-install",
        "ok": not violations,
        "requirements_spec": str(ds_spec),
        "routertl_floor": str(floor),
        "installed": str(installed) if installed else None,
        "violations": violations,
        "notes": notes,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    if not violations:
        if not args.json:
            print(
                f"✓ deepskopion cocotb pin (`{ds_line}`) stays within the routertl"
                f" floor cocotb{floor}; safe to install into shared site-packages."
            )
            for n in notes:
                print(f"    ℹ {n}")
        return 0
    if not args.json:
        print("❌ REFUSING to install: cocotb would violate the routertl floor.")
        for v in violations:
            print(f"    - {v}")
        print(
            "\n   Installing this into the shared interpreter is exactly the"
            "\n   downgrade half of the DS-P2.109 ping-pong that breaks the"
            "\n   routertl gate. Remedy: bring deepskopion requirements.txt to a"
            f"\n   range wholly inside cocotb{floor} (e.g. `cocotb{floor},<3`) so"
            "\n   every version it admits satisfies the floor, then re-run the gate."
        )
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser(
        "check-constraints",
        help="assert every registered repo's cocotb constraint is satisfiable by one version",
    )
    c.add_argument("--requirements", help="path to deepskopion requirements.txt")
    c.add_argument("--routertl-floor", help="override routertl cocotb floor (testing)")
    c.set_defaults(func=cmd_check_constraints)

    g = sub.add_parser(
        "guard-install",
        help="refuse to install a cocotb that violates the routertl floor",
    )
    g.add_argument("--requirements", help="path to deepskopion requirements.txt")
    g.add_argument("--routertl-floor", help="override routertl cocotb floor (testing)")
    g.add_argument(
        "--no-installed-check",
        action="store_true",
        help="skip the informational note about the currently-installed cocotb",
    )
    g.set_defaults(func=cmd_guard_install)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GuardError as exc:
        msg = f"❌ GATE COULD NOT RUN: {exc}"
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(msg)
            print("   A guard that cannot look is not a green guard (exit 2).")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
