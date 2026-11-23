"""Shared pytest configuration for tools/tests.

DS-P2.223: the tests that drive a simulator through ``rr`` share this
checkout's sim state (staged sources and ROM hex files, ``sim/``). Run in
parallel under pytest-xdist they failed, while each passed alone (measured
2026-11-06: 10 of them, e.g. "failed to open hw/rtl/regbank/regbank_schema.hex"
in a sweep that overlapped a plain ``rr sim run``). So every such module joins
ONE xdist group, and a parallel run schedules with ``--dist loadgroup``: the
group runs on one worker, one test at a time, and every other test still
spreads across the workers.

The group is computed, not listed by hand, so a new sim-driving test cannot
silently fall outside it. A module drives a sim when it

* spawns ``rr sim run`` / ``rr sim sweep`` (the argv tokens "sim", "run"|"sweep");
* runs a cocotb test file under hw/sim with the interpreter (the file's
  ``__main__`` calls ``run_simulation``, the same rr machinery); or
* imports a module of this directory that drives a sim (helpers such as
  ``datalogger_mix_bench`` and ``ip_standalone_closure``, or another test's
  runner), transitively.

DS-P2.276: a second rule beside it, ``vendor_tool_modules``, names the modules that start a vendor
tool through a real ``rr``. A module does so when an argv list starts with an rr executable
followed by "synth"|"impl"|"implementation"|"bitstream" and "run" (``[rr, "synth", "run", ...]``), or
imports such a module of this directory, transitively. Assertions over a fake rr's recorded calls
(``["synth", "run"] in calls``) do not match: the list must begin with the rr executable. Those
modules carry the ``vendor_tools`` marker, so a desk run deselects them with ``-m "not
vendor_tools"``, and a job meant to start vendor tools reads the same function to select them.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SIM_GROUP = "rr_sim_one_checkout"
HERE = Path(__file__).resolve().parent

_SPAWN = re.compile(r"""["']sim["']\s*,\s*["'](?:run|sweep)["']""")
_COCOTB_FILE = re.compile(r"""hw/sim/cocotb|["']hw["']\s*/\s*["']sim["']""")
_VENDOR_SPAWN = re.compile(
    r"""\[\s*(?:\w*rr\w*|shutil\.which\(\s*["']rr["']\s*\)|["'][^"']*\brr["'])\s*,\s*["'](?:synth|impl|implementation|bitstream)["']\s*,"""
    r"""\s*["']run["']""")
VENDOR_MARK = "vendor_tools"


def _imported_names(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[-1])
    return names


def _sources(directory: Path) -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8", errors="replace")
            for p in directory.glob("*.py") if p.name != "conftest.py"}


def vendor_tool_modules(directory: Path = HERE) -> set[str]:
    """Stems of the modules in ``directory`` that start a vendor tool through a real rr (DS-P2.276)."""
    sources = _sources(directory)
    driving = {stem for stem, src in sources.items() if _VENDOR_SPAWN.search(src)}
    imports = {stem: _imported_names(src) for stem, src in sources.items()}
    changed = True
    while changed:
        changed = False
        for stem, names in imports.items():
            if stem not in driving and names & driving:
                driving.add(stem)
                changed = True
    return driving


def sim_driving_modules(directory: Path = HERE) -> set[str]:
    """Stems of the modules in ``directory`` that drive a simulator."""
    sources = _sources(directory)
    driving = {stem for stem, src in sources.items()
               if _SPAWN.search(src)
               or ("sys.executable" in src and _COCOTB_FILE.search(src))}
    imports = {stem: _imported_names(src) for stem, src in sources.items()}
    changed = True
    while changed:
        changed = False
        for stem, names in imports.items():
            if stem not in driving and names & driving:
                driving.add(stem)
                changed = True
    return driving


_WORKER_KEY = "ds_p2_223_loadgroup"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", f"{VENDOR_MARK}: starts a vendor tool through a real rr (DS-P2.276); "
                   "desk runs deselect it")
    # Controller: pytest-xdist turns -n N into --dist load before configure;
    # switch it to loadgroup so the group below is honoured. An explicit
    # other mode (each, worksteal, ...) is left as the caller chose it.
    if getattr(config.option, "dist", "no") == "load":
        config.option.dist = "loadgroup"
    # Worker: xdist re-parses the original command line there and decides
    # loadgroup before this hook runs, so it never sees the switch above.
    # The controller hands it over in workerinput (pytest_configure_node).
    if getattr(config, "workerinput", {}).get(_WORKER_KEY):
        config.option.loadgroup = True


@pytest.fixture(scope="session", autouse=True)
def _fetched_packages():
    """DS-P2.360: registry IP is fetched at the ip.lock pin, not tracked. Before any test of this directory runs,
    every fetched package is present and equal to its ip.lock hashes (one `rr workspace sync` when absent), or the
    session FAILS; no test depends on another having fetched first, and none skips for want of the IP.
    A copy of this conftest outside the repository (test_sim_xdist_group_ds_p2_223 runs one in a scratch dir) has no
    ip.lock above it and nothing pinned to fetch."""
    root = HERE.parents[1]
    if not (root / "ip.lock").is_file():
        return
    import sys
    sys.path.insert(0, str(HERE))
    from fetched_packages import ensure_all_fetched
    ensure_all_fetched(root)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:
    node.workerinput[_WORKER_KEY] = node.config.option.dist == "loadgroup"


# tryfirst: the xdist worker appends "@<group>" to node ids in its own
# collection hook, which must see the marker this one adds.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items) -> None:
    driving = sim_driving_modules()
    vendor = vendor_tool_modules()
    for item in items:
        path = Path(str(item.fspath))
        if path.parent == HERE and path.stem in driving:
            item.add_marker(pytest.mark.xdist_group(SIM_GROUP))
        if path.parent == HERE and path.stem in vendor:
            item.add_marker(getattr(pytest.mark, VENDOR_MARK))
