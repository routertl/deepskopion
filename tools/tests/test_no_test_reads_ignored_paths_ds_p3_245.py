# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""DS-P3.245: no test reads a checkout path that git ignores, unless it first checks the file exists.

Measured 2026-11-12 on a fresh clone of 9faf542f: test_rea_stamp_volatile_dirt_ds_p1_96.py gave 6
errors, FileNotFoundError hw/rtl/pkg/rom_info_pkg.vhd, a gitignored build output since DS-P2.231.
It passed only in a checkout where an earlier build had left the file.

The guard reads every test module's AST. A read site is `<path>.read_text()`, `.read_bytes()` or
`.open()`, or `<path>` as the first argument of open, shutil.copy*/copyfile/copytree, json/yaml load.
`<path>` is a chain from a repo-root name (`ROOT / "hw" / "x"`), a repo-relative string literal, or a
module/function-level name bound to one. `git check-ignore` decides which are ignored. A read is
allowed when the same function (or the module, for a module-level read) also calls `.exists()`,
`.is_file()` or `.is_dir()` on that path: the test then skips or branches when a fresh clone lacks the
file. Anything else must be listed in ALLOWED with its reason.

Not covered: paths built at run time (f-strings, joins of variables) and reads inside helpers the test
calls. The nightly fresh-clone run (TIC-P3.512) is the runtime complement.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST_DIRS = ("tools/tests", "sw/stream/tests", "contracts")
_ROOT_NAMES = {"ROOT", "REPO", "REPO_ROOT", "PROJECT_ROOT", "DS_ROOT"}
_REL = re.compile(r"^(?:hw|sw|tools|targets|build|sim|docs|contracts|libs|runtimes|bitstream|generated)/[\w./-]+$")
_READ_METH = {"read_text", "read_bytes", "open"}
_READ_FUNC = {"open", "copyfile", "copy", "copy2", "copytree", "safe_load", "load"}
_GATE_METH = {"exists", "is_file", "is_dir"}

#: (test file, ignored path) -> why the test may read it without an existence check.
ALLOWED: dict[tuple[str, str], str] = {
    ("tools/tests/test_pfsoc_fit_e2e_ds_p2_151.py",
     "build/pfsoc_datalogger/project/designer/deepskopion_pfsoc_top/"
     "deepskopion_pfsoc_top_place_and_route_constraint_coverage.xml"):
        "written by this test's own rr synth/impl run (vendor_tools, DS_P2_151_E2E_IMPL=1); its "
        "absence after that run is the failure the test reports",
}


def _chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if not (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            return None
        parts.append(node.right.value.strip("/"))
        node = node.left
    if isinstance(node, ast.Name) and node.id in _ROOT_NAMES and parts:
        return "/".join(reversed(parts))
    return None


def _path(node: ast.AST, names: dict[str, str]) -> str | None:
    p = _chain(node)
    if p:
        return p
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and _REL.match(node.value):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Name) and node.left.id in _ROOT_NAMES
            and isinstance(node.right, ast.Name)):
        return names.get(node.right.id)
    return None


def _bindings(body: list[ast.stmt], outer: dict[str, str]) -> dict[str, str]:
    names = dict(outer)
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                p = _path(n.value, names)
                if p:
                    names[n.targets[0].id] = p
    return names


def _calls(scope: ast.AST, *, skip_defs: bool):
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        if skip_defs and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(n, ast.Call):
            yield n
        stack.extend(ast.iter_child_nodes(n))


def _reads_and_gates(scope: ast.AST, names: dict[str, str], *, skip_defs: bool):
    reads, gates = set(), set()
    for call in _calls(scope, skip_defs=skip_defs):
        f = call.func
        if isinstance(f, ast.Attribute):
            p = _path(f.value, names)
            if p and f.attr in _READ_METH:
                reads.add(p)
            if p and f.attr in _GATE_METH:
                gates.add(p)
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
        if name in _READ_FUNC and call.args:
            p = _path(call.args[0], names)
            if p:
                reads.add(p)
    return reads, gates


def ungated_reads(source: str) -> set[str]:
    """Checkout paths a module reads with no existence check in the same function."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    module_names = _bindings(tree.body, {})
    reads, gates = _reads_and_gates(tree, module_names, skip_defs=True)
    out = reads - gates
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = _bindings(fn.body, module_names)
            r, g = _reads_and_gates(fn, names, skip_defs=False)
            out |= r - g
    return out


def ignored(paths: set[str]) -> set[str]:
    if not paths:
        return set()
    r = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"], cwd=ROOT,
                       input="\n".join(sorted(paths)), capture_output=True, text=True,
                       errors="replace")
    return set(r.stdout.split())


def _test_files() -> list[str]:
    tracked = subprocess.run(["git", "ls-files", *TEST_DIRS], cwd=ROOT, capture_output=True,
                             text=True, errors="replace", check=True).stdout.split()
    return [f for f in tracked if re.search(r"(^|/)test_[^/]*\.py$", f)]


def scan() -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for rel in _test_files():
        src = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        for p in sorted(ignored(ungated_reads(src))):
            if (rel, p) not in ALLOWED:
                hits.setdefault(rel, []).append(p)
    return hits


def test_the_scanner_tells_gated_from_ungated_reads():
    src = '''
import shutil
ROOT = None
ROM = "hw/rtl/pkg/rom_info_pkg.vhd"
LOCK = ROOT / "hw" / "sim" / "work.lock"
def test_copy(tmp_path):
    shutil.copyfile(ROOT / ROM, tmp_path / "x")
def test_read():
    return (ROOT / "build" / "x" / "y.json").read_text()
def test_gated():
    p = ROOT / "build" / "z.bin"
    if not p.is_file():
        return None
    return p.read_bytes()
def test_lock():
    return LOCK.read_bytes() if LOCK.exists() else None
'''
    assert ungated_reads(src) == {"hw/rtl/pkg/rom_info_pkg.vhd", "build/x/y.json"}
    assert ignored({"hw/rtl/pkg/rom_info_pkg.vhd", "targets/canfd_demo.yml"}) == {
        "hw/rtl/pkg/rom_info_pkg.vhd"}


def test_no_test_reads_an_ignored_path_without_checking_it_exists():
    hits = scan()
    assert not hits, f"tests reading gitignored checkout paths (build outputs a fresh clone lacks): {hits}"
