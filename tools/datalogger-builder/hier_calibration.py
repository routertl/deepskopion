#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""Sum a Libero compile-netlist hierarchy CSV under a hierarchy prefix, refusing a prefix that matches nothing.

The configurator's PolarFire constants are calibrated from the per-module CSV Libero writes for a build
(designer/<top>/<top>_compile_netlist_hier_resources.csv). A calibration selects rows by hierarchy prefix, e.g.
the gather block `u_fabric/u_core/u_datalogger_gather/`. When a refactor moves the hierarchy (DS-P2.326 put the
PolarFire core under `u_fabric/`), an old prefix matches no row; summed naively it reads as zero resources, a
plausible-looking calibration. DS-P3.274: a prefix that matches no row is an error naming the prefix and the CSV.

    python3 hier_calibration.py <hier_resources.csv> <prefix> [<prefix> ...]

prints, per prefix, the matched row count and the 4LUT / DFF / LSRAM sums (4LUT and DFF = fabric + interface);
exits 2 naming the prefix and the CSV if any prefix matches no row, 1 if the CSV is not a hierarchy report.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import NamedTuple

HEADER = ["Module Name", "Fabric 4LUT", "Fabric DFF", "Interface 4LUT", "Interface DFF"]


class CalibrationError(ValueError):
    """A calibration input that would silently produce a wrong number."""


class HierRow(NamedTuple):
    module: str
    luts: int
    dffs: int
    lsram: int


def load_hier_csv(path: Path) -> list[HierRow]:
    """Rows of a Libero compile-netlist hierarchy CSV: (module, 4LUT, DFF, LSRAM), 4LUT/DFF = fabric + interface."""
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        first = next(reader, None)
        header = next(reader, None)
        if first != ["Detailed Resource Usage"] or not header or header[:5] != HEADER or len(header) < 9 \
                or header[8] != "LSRAM (20K)":
            raise CalibrationError(f"{path}: not a Libero compile-netlist hierarchy CSV (header {header[:9] if header else None})")
        rows = []
        for row in reader:
            if row and row[0]:
                v = [int(x) for x in row[1:]]
                rows.append(HierRow(row[0], v[0] + v[2], v[1] + v[3], v[7]))
    return rows


def rows_under(rows: list, prefix: str, source: object) -> list:
    """The rows (tuples whose first field is the module path) under `prefix`; a zero match raises CalibrationError."""
    matched = [r for r in rows if r[0].startswith(prefix)]
    if not matched:
        raise CalibrationError(
            f"calibration prefix {prefix!r} matches no row of {source} ({len(rows)} rows); a zero match is not "
            f"zero resources: the hierarchy has moved or the prefix is wrong (DS-P3.274)")
    return matched


def sums(rows: list[HierRow]) -> tuple[int, int, int]:
    return sum(r.luts for r in rows), sum(r.dffs for r in rows), sum(r.lsram for r in rows)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        sys.stderr.write("usage: hier_calibration.py <hier_resources.csv> <prefix> [<prefix> ...]\n")
        return 1
    csv_path = Path(args[0])
    try:
        rows = load_hier_csv(csv_path)
    except (OSError, CalibrationError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    for prefix in args[1:]:
        try:
            matched = rows_under(rows, prefix, csv_path)
        except CalibrationError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2
        lut, dff, lsram = sums(matched)
        print(f"{prefix}\trows={len(matched)}\t4LUT={lut}\tDFF={dff}\tLSRAM={lsram}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
