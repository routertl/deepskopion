# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""Where this repository's rr target files live (DS-P1.103).

targets/ holds the three shipped products and nothing else. Every other target (fixtures, CTU CAN FD bench
targets, bring-up images) lives in bench/targets/, which the public export drops. RouteRTL's own scans read
targets/*.yml only (RTL-P3.2521), so any tool or test here that walks "all targets" goes through this module, and
anything judging what SHIPS uses product_target_files().
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTS_DIR = Path("targets")
BENCH_DIR = Path("bench") / "targets"
#: The shipped products, one per vendor. targets/*.yml must be exactly these.
PRODUCTS = ("de25_datalogger", "pfsoc_datalogger", "zybo_datalogger")


def product_target_files(root: Path = ROOT) -> list[Path]:
    """targets/*.yml: what ships."""
    return sorted((Path(root) / PRODUCTS_DIR).glob("*.yml"))


def bench_target_files(root: Path = ROOT) -> list[Path]:
    """bench/targets/*.yml: bench-only targets, not exported."""
    return sorted((Path(root) / BENCH_DIR).glob("*.yml"))


def all_target_files(root: Path = ROOT) -> list[Path]:
    """Every rr target file in the repository, products first."""
    return product_target_files(root) + bench_target_files(root)


def target_file(name: str, root: Path = ROOT) -> Path:
    """The file of target *name*: targets/<name>.yml for a product, else bench/targets/<name>.yml.

    Raises FileNotFoundError naming both places when neither exists.
    """
    root = Path(root)
    for base in (PRODUCTS_DIR, BENCH_DIR):
        path = root / base / f"{name}.yml"
        if path.is_file():
            return path
    raise FileNotFoundError(f"no target {name!r}: neither {PRODUCTS_DIR / (name + '.yml')} nor "
                            f"{BENCH_DIR / (name + '.yml')} exists under {root}")


def is_product(path: Path, root: Path = ROOT) -> bool:
    """True when *path* is a shipped product's target file."""
    return Path(path).resolve().parent == (Path(root) / PRODUCTS_DIR).resolve()


def has_bench(root: Path = ROOT) -> bool:
    """True where bench/targets/ exists (the private tree). The public export carries no bench/."""
    return (Path(root) / BENCH_DIR).is_dir()


def with_bench(products, bench=(), root: Path = ROOT) -> set[str]:
    """The target stems a scan must see: *products* always, *bench* only where bench/ exists.

    Every name in *products* must be a shipped product, so a bench name cannot ride into the
    public expectation.
    """
    products, bench = set(products), set(bench)
    if not products <= set(PRODUCTS):
        raise ValueError(f"not shipped products: {sorted(products - set(PRODUCTS))}")
    if bench & set(PRODUCTS):
        raise ValueError(f"products named as bench targets: {sorted(bench & set(PRODUCTS))}")
    return products | (bench if has_bench(root) else set())


def per_tree(public: int, private: int, root: Path = ROOT) -> int:
    """A scan's expected count: *private* where bench/ exists, *public* in the export."""
    return private if has_bench(root) else public
