"""DS-P2.168 — the REA build-id package lands in the library REA compiles into.

routertl/rea's ip.yml places its generated rr_rea_build_id_pkg.vhd in library
`rr_rea`, and `rr pkg` / every build re-derives ip.lock `generated_files` from
that manifest. A consumer library override (`routertl/rea: {library: tich}`)
moves the REA RTL but NOT the generated output, and rr_rea_regbank does
`use work.rr_rea_build_id_pkg`, so Vivado synthesis fails with "package
'rr_rea_build_id_pkg' not found in library 'work'" (queue job 0d5c6cc2e70c,
zybo_datalogger). Hand-editing ip.lock does not help: the next build rewrites
it. A sources.libraries pin does not help either: the lock entry is injected
after it and wins.

So the library follows the package. This guard names the failure before a
synthesis run has to.
"""
from __future__ import annotations

from pathlib import Path

import sys
import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
PIN = "routertl/rea"


def _lock_entry() -> dict:
    return yaml.safe_load((ROOT / "ip.lock").read_text())[PIN]


def test_no_target_overrides_the_rea_library() -> None:
    declaring = {}
    for target in all_target_files(ROOT):
        pkgs = (yaml.safe_load(target.read_text()) or {}).get("packages") or {}
        if PIN in pkgs:
            declaring[target.name] = pkgs[PIN]
    assert declaring, f"no target declares {PIN}"
    overridden = {t: s for t, s in declaring.items() if isinstance(s, dict) and "library" in s}
    assert not overridden, (
        f"{sorted(overridden)} override the {PIN} library; the package's generated "
        "rr_rea_build_id_pkg stays in `rr_rea`, so rr_rea_regbank cannot resolve it"
    )


def test_lock_places_the_generated_build_id_package_in_the_rea_library() -> None:
    entry = _lock_entry()
    rea_library = entry["library"]
    generated = {g["path"]: g["library"] for g in entry["generated_files"]}
    hooked = {
        o["path"]: o["library"]
        for hook in entry["generation_hooks"]["pre_build"]
        for o in hook["outputs"]
    }
    for kind, table in (("generated_files", generated), ("generation_hooks outputs", hooked)):
        assert table, f"ip.lock {PIN} has no {kind}"
        for path, library in table.items():
            assert library == rea_library, (
                f"ip.lock {PIN} {kind}: {path} is placed in library {library!r} but the "
                f"REA RTL compiles into {rea_library!r}"
            )
