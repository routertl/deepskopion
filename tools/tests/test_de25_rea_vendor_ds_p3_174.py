# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DS-P3.174 — guards for the parts of the DE25 CDC-signoff clean-up that the
locks and the vendored REA package carry.

`rr cdc check --signoff` on the DE25 top stopped at 7 inferred latches in
rr_rea_regbank.vhd (GHDL→Yosys). REA-P2.24 fixes them in the package. No REA
release tag exists past v1.12.1, so the targets pin the fix by commit. Each
test below guards one independent part (one surgical mutant each in the
test-power receipt):

  1. verilog_counter is gone from libs/, both locks and every target;
  2. the vendored REA package is the REA-P2.24 one: regbank content, the
     ip.lock file hashes and a version >= 1.12.2 (a ^1.12 range accepts 1.12.0);
  3. every target that declares routertl/rea pins exactly what ip.lock holds (a
     range on one target makes the shared ip.lock re-resolve to an old REA).
     DS-P2.193: with a release tag (1.12.3) the exact pin is `==<version>`; a
     commit-resolved lock still needs `commit:<sha>`;
  4. generated/ip.yml places the generated build-id package in library rr_rea.
"""
from __future__ import annotations

import sys
import hashlib
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
REA = "routertl/rea"


def _lock_entry() -> dict:
    return yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))[REA]


def test_verilog_counter_is_gone_everywhere() -> None:
    """routertl/verilog_counter: no libs/ dir, no lock entry, no target package.

    A target that still declares it makes `rr target set` / `rr pkg install`
    restore the .v file into libs/ (the shared ip.lock re-resolves it); the
    locks name the path the package would write. Any such .v sits outside the
    DE25 top closure and fails `rr cdc check --signoff`.
    """
    left: list[str] = []
    if (ROOT / "libs" / "routertl" / "verilog_counter").exists():
        left.append("libs/routertl/verilog_counter")
    # DS-P2.287: ip.lock and every tracked per-target lock (targets/<t>.routertl.lock).
    target_locks = sorted(p.relative_to(ROOT).as_posix() for p in ROOT.glob("targets/*.routertl.lock"))
    assert target_locks, "no targets/*.routertl.lock"
    for lock in ("ip.lock", *target_locks):
        if "verilog_counter" in (ROOT / lock).read_text(encoding="utf-8"):
            left.append(lock)
    for target in all_target_files(ROOT):
        pkgs = (yaml.safe_load(target.read_text(encoding="utf-8")) or {}).get("packages") or {}
        if "routertl/verilog_counter" in pkgs:
            left.append(f"targets/{target.name}")
    assert not left, f"verilog_counter still present: {left}"


def test_vendored_rea_is_the_rea_p2_24_package() -> None:
    """Content first (the pre-fix copy is self-consistent with its old lock)."""
    text = (ROOT / "libs/routertl/rea/rtl/rr_rea_regbank.vhd").read_text(encoding="utf-8")
    clocked = text[text.index("elsif rising_edge(jtag_clk_i) then"):]
    for guard, signals in (
        ("G_TRIG_STAGES", ("seq_value_flat", "seq_mask_flat", "seq_count_flat")),
        ("G_QUAL_CONDS", ("qual_mode_r", "qual_cfg_flat", "qual_val_flat", "qual_sel_r")),
    ):
        block = re.search(rf"if {guard} = 0 then(.*?)end if;", clocked, re.S)
        assert block, f"REA-P2.24 missing: no `if {guard} = 0` clocked block in rr_rea_regbank.vhd"
        for sig in signals:
            assert re.search(rf"\b{sig}\s*<=\s*\(others => '0'\)", block.group(1)), (
                f"REA-P2.24 missing: {sig} is not assigned in the {guard} = 0 clocked block"
            )
    entry = _lock_entry()
    assert entry["file_hashes"], "ip.lock routertl/rea records no file_hashes"
    stale = sorted(
        rel
        for rel, want in entry["file_hashes"].items()
        if hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() != want
    )
    assert not stale, (
        f"vendored REA files differ from the commit {entry['commit']} that ip.lock hashed: {stale}"
    )
    version = yaml.safe_load((ROOT / "libs/routertl/rea/ip.yml").read_text(encoding="utf-8"))["version"]
    assert tuple(int(x) for x in str(version).split(".")) >= (1, 12, 2), version


def test_every_target_pins_the_lock_commit() -> None:
    entry = _lock_entry()
    commit = entry["commit"]
    assert re.fullmatch(r"[0-9a-f]{40}", commit), commit
    resolved = str(entry["resolved_version"])
    exact = (f"commit:{commit}" if resolved.startswith("commit:")
             else f"=={resolved}")
    pins: dict[str, str] = {}
    for target in all_target_files(ROOT):
        spec = ((yaml.safe_load(target.read_text(encoding="utf-8")) or {}).get("packages") or {}).get(REA)
        if spec is not None:
            pins[target.name] = str(spec["version"] if isinstance(spec, dict) else spec)
    assert pins, "no target declares routertl/rea"
    wrong = {t: s for t, s in pins.items() if s != exact}
    assert not wrong, (
        f"targets not pinned to ip.lock {resolved} @ {commit} ({exact!r}): {wrong}"
    )


def test_generated_source_root_declares_the_rea_library() -> None:
    """generated/ip.yml places the generated build-id package in library rr_rea.

    `generated/` is a scanned source root (paths.sources). The REA regbank does
    `use work.rr_rea_build_id_pkg`, and without this manifest the GHDL import in
    `rr cdc check --signoff` fails with `unit "rr_rea_build_id_pkg" not found in
    library "rr_rea"` (measured on this tree with the file removed). The library
    must be the one REA compiles into, which ip.lock records.
    """
    path = ROOT / "generated" / "ip.yml"
    assert path.is_file(), "generated/ip.yml is missing (rr_rea_build_id_pkg gets no library)"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    rea_library = yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))[
        "routertl/rea"
    ]["library"]
    assert manifest["hdl"]["library"] == rea_library, manifest
