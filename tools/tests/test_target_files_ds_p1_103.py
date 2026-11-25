# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.103: targets/ holds exactly the three shipped products; every other target lives in bench/targets/.

tools/target_files.py is the one place that says where target files live. A scan that walks only targets/*.yml
(as RouteRTL's own scans do, RTL-P3.2521) misses every bench target, so tools and tests go through the helper.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest  # noqa: F401  (the _bench.py sibling uses it through this module)

ROOT = Path(__file__).resolve().parents[2]

import sys  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import target_files as T  # noqa: E402


def _tracked(pattern: str) -> set[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", pattern],
                         capture_output=True, text=True, check=True).stdout
    return {p for p in out.split("\0") if p}


def test_targets_holds_exactly_the_three_products():
    assert {Path(p).stem for p in _tracked("targets/*.yml")} == set(T.PRODUCTS)
    assert [p.stem for p in T.product_target_files()] == sorted(T.PRODUCTS)


def test_no_product_name_is_also_a_bench_target():
    assert not {p.stem for p in T.bench_target_files()} & set(T.PRODUCTS)


def test_is_product_tells_a_product_from_a_bench_target():
    assert T.is_product(ROOT / "targets" / "de25_datalogger.yml")
    assert not T.is_product(ROOT / T.BENCH_DIR / "de25_canfd.yml")


def test_the_deployment_contract_checker_scans_the_same_two_directories():
    spec = importlib.util.spec_from_file_location("_ds_contract", ROOT / "sw" / "linux" / "deployment_contract.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclasses resolve their module through sys.modules
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    assert tuple(mod.TARGET_DIRS) == (T.PRODUCTS_DIR, T.BENCH_DIR)
