# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""Validator for Deepskopion reference design manifests (DS-P2.119).

Validates ref_design.yml manifests against the RouteRTL schema
(sdk.engine.ref_design_schema) and Deepskopion project invariants:
1. Exact tool versions (no semver/range operators).
2. Canonical board IDs (de25-standard, zybo-z7-20).
3. Verification status backed by evidence (DS-P2.118 qualifies sandbox-verified).
4. Pinned IP dependencies in registry format (namespace/name).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import yaml

from sdk.engine.ref_design_schema import (
    RefDesign,
    load_known_boards,
    load_manifest,
    parse_manifest,
    validate_manifest,
)

# Canonical board IDs are anchored on the RouteRTL register authority:
# sdk.engine.ref_design_schema.load_known_boards() / boards.yml.
#
# PENDING_CANONICAL_BOARDS documents board IDs supported in Deepskopion reference
# designs that are pending upstream addition to RouteRTL sdk/engine/boards.yml.
# Named follow-up: Upstream addition of 'de25-standard' (Terasic DE25-Standard,
# Intel Agilex 5 SoC) to RouteRTL sdk/engine/boards.yml.
# TODO(DS-P3.169): de25-standard is in boards.yml since routertl ae29dd26e, but no
# published release carries it; removing it now rejects the de25 manifest on rr 4.9.3.
PENDING_CANONICAL_BOARDS: dict[str, str] = {
    "de25-standard": (
        "Pending addition to RouteRTL sdk/engine/boards.yml "
        "(Terasic DE25-Standard / Intel Agilex 5 SoC)"
    ),
}


def get_canonical_boards() -> set[str]:
    """Return the set of recognized canonical board IDs:
    RouteRTL known boards from boards.yml plus documented pending allowances.
    """
    known = load_known_boards()
    return set(known) | set(PENDING_CANONICAL_BOARDS.keys())


# Expose for module consumers and backward compatibility
CANONICAL_BOARDS: set[str] = get_canonical_boards()


def validate_deepskopion_manifest(
    manifest: RefDesign | Path | str | dict,
    *,
    blocker_note: str | None = None,
) -> list[str]:
    """Validate a reference design manifest against schema and Deepskopion invariants.

    Returns a list of error strings. An empty list means the manifest is valid.
    """
    raw_data: dict | None = None

    if isinstance(manifest, (str, Path)):
        p = Path(manifest)
        if not p.exists():
            return [f"Manifest not found: {p}"]
        try:
            with open(p) as f:
                raw_data = yaml.safe_load(f) or {}
        except Exception as exc:
            return [f"Failed to parse YAML from {p}: {exc}"]
        rd = parse_manifest(raw_data)
    elif isinstance(manifest, dict):
        raw_data = manifest
        rd = parse_manifest(raw_data)
    elif isinstance(manifest, RefDesign):
        rd = manifest
        raw_data = getattr(manifest, "_raw_data", None)
    else:
        return [f"Unsupported manifest type: {type(manifest)}"]

    errors = list(validate_manifest(rd))

    # Board canonicality check anchored on load_known_boards() + pending allowances
    canonical = get_canonical_boards()
    if not rd.target.board:
        errors.append("target.board is required")
    elif rd.target.board not in canonical:
        errors.append(
            f"target.board '{rd.target.board}' is not canonical. "
            f"Expected one of RouteRTL known boards or pending allowances: {sorted(canonical)}"
        )

    # Verification status check:
    # Status must match evidence from DS-P2.118:
    # - 'sandbox-verified' for DE25 (empirical bench qualification passed)
    # - 'claimed' is permitted ONLY with an explicit blocker/evidence note (e.g. a DDR-T1.3 gate)
    # Claimed-by-default with no blocker note is strictly rejected.
    if rd.verification.status == "claimed":
        # Check for blocker note from argument, raw_data, or object attributes
        note = blocker_note
        if not note and raw_data:
            v_block = raw_data.get("verification") or {}
            if isinstance(v_block, dict):
                note = (
                    v_block.get("blocker")
                    or v_block.get("notes")
                    or v_block.get("note")
                    or v_block.get("evidence")
                )
            if not note:
                note = raw_data.get("blocker") or raw_data.get("notes")
        if not note:
            note = (
                getattr(rd.verification, "blocker", None)
                or getattr(rd.verification, "notes", None)
                or getattr(rd.verification, "note", None)
                or getattr(rd.verification, "evidence", None)
                or getattr(rd, "blocker", None)
                or getattr(rd, "_blocker_note", None)
            )

        if not note or not str(note).strip():
            errors.append(
                "verification.status cannot be 'claimed' by default without a documented "
                "blocker or evidence note (honesty requires documenting the blocker, "
                "e.g. DDR-T1.3/build gate; bench qualification requires 'sandbox-verified' when verified)"
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Deepskopion reference design manifests."
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        default="ref_design.yml",
        help="Path to ref_design manifest YAML (default: ref_design.yml)",
    )
    args = parser.parse_args(argv)

    path = Path(args.manifest)
    errors = validate_deepskopion_manifest(path)
    if errors:
        print(f"Manifest validation failed for {path} ({len(errors)} error(s)):")
        for err in errors:
            print(f"  • {err}")
        return 1

    rd = load_manifest(path)
    print(f"✓ Manifest valid: {rd.org}/{rd.name}@{rd.version} ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
