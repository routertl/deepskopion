# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P1.103: the public export carries no bench subject, and the bench tests it drops really are bench tests.

The export drops bench/ and the CTU CAN FD pieces. A test whose subject is a bench target therefore moves to a
``*_bench.py`` sibling of its public file (or its whole file is dropped), and both are DROP rows. Two rules hold
that split honest:

1. every test in a dropped DS-P1.103 test file references a bench subject (a bench target, a bench/ path, a
   dropped CTU file, or a module name bound to one of those), so no product test hides in a dropped file;
2. no public test file names a bench path or a bench target in a string, so nothing in the export depends on
   bench/.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import public_artifact_hygiene as H  # noqa: E402
from target_files import BENCH_DIR, bench_target_files  # noqa: E402

#: Whole test files DS-P1.103 drops (their every test has a bench subject). The *_bench.py siblings are found
#: by name.
WHOLE_FILES = (
    "tools/tests/test_canfd_demo_can_datalogger_ds_p2_218.py",
    "tools/tests/test_de25_canfd_target_ds_p2_135.py",
    "tools/tests/test_can_opt_in_targets_carry_can_datalogger_ds_p3_211.py",
    "tools/tests/test_eye_loopback_buildability_ds_p3_172.py",
    "tools/tests/test_pfsoc_mss_wrapper_isolation_ds_p3_174.py",
    "sw/linux/tests/test_ctucanfd_ntxbufs_patch_ds_p2_67.py",
    "sw/can/tests/test_ctu_canfd.py",
    "hw/sim/cocotb/tests/datalogger/can/test_ctu_two_node_wire_ds_t2_97.py",
    "hw/sim/cocotb/tests/datalogger/can/test_ctu_two_txtb_ds_p2_67.py",
    "hw/sim/cocotb/tests/datalogger/can/test_can_datalogger_ctu_wire_ds_t2_97.py",
    "hw/sim/cocotb/tests/datalogger/can/test_ctu_stuff_oracle.py",
)
#: CTU CAN FD files the export drops; a test naming one (by its stem) has a bench subject.
CTU_DROPPED = (
    "sw/linux/kernel_ctucanfd.config-fragment",
    "sw/linux/kernel_patches/ctucanfd_ntxbufs.sh",
    "bench/sim/tb_ctu_two_node.vhd",
    "bench/sim/tb_ctu_two_txtb.vhd",
    "bench/sim/tb_lwh2f_ctucanfd_probe.vhd",
    "hw/sim/cocotb/can_ctu_oracle.py",
    "hw/sim/cocotb/tests/datalogger/can/ds_t2_97_ctu_two_node_wire.json",
    "hw/xdc/pblock_ctu.xdc",
    "sw/can/ctu_canfd.py",
)


def _tracked(*patterns: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", *patterns],
                         capture_output=True, text=True, check=True).stdout
    return sorted(p for p in out.split("\0") if p)


def _siblings() -> list[str]:
    return [p for p in _tracked("*test_*_bench.py") if (ROOT / p).with_name(
        Path(p).name.replace("_bench.py", ".py")).is_file()]


def _bench_tokens(root: Path = ROOT) -> set[str]:
    stems = {p.stem for p in bench_target_files(root)}
    return stems | {"bench/", BENCH_DIR.as_posix(), '"bench"'} | {Path(p).stem for p in CTU_DROPPED}


#: Dropped tests whose bench subject is indirect, each with the reason. Nothing else is excused.
INDIRECT_SUBJECT = {
    # Its subject is the tool-image exemption, read from bench/deployment_contract.yml (the only tool image,
    # de25_p2writer, is a bench target); with no bench sidecar the exemption is empty and the test has no subject.
    "sw/stream/tests/test_overlay_in_sync_bench.py::"
    "test_a_normal_target_without_its_stage_command_still_fails_beside_the_exemption":
        "bench tool-image exemption (bench/deployment_contract.yml)",
    # Its subject is the real CTU core's TX-buffer count, which matters only where the CTU core is compiled
    # (the bench CTU targets); the products bind the stub, whose generic is unused.
    "sw/linux/tests/test_ctucanfd_ntxbufs_patch_ds_p2_67.py::test_de25_keeps_the_core_default":
        "CTU core TX-buffer generic (bench CTU targets only)",
}


def _tests(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield node.name, node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                    yield f"{node.name}::{sub.name}", sub


def _segment(src: str, node: ast.AST) -> str:
    lines = src.splitlines()
    start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
    return "\n".join(lines[start - 1:node.end_lineno])


def _tainted_names(src: str, tokens: set[str], extra: set[str] = frozenset()) -> set[str]:
    """Module-level names whose definition (assignment or function) mentions a bench token or an already
    tainted name, iterated to a fixed point."""
    tree = ast.parse(src)
    defs = {}
    for node in tree.body:
        names = []
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("test"):
            names = [node.name]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [(a.asname or a.name).split(".")[0] for a in node.names]
        for n in names:
            defs[n] = _segment(src, node)
    tainted = set(extra)
    changed = True
    while changed:
        changed = False
        for n, seg in defs.items():
            if n not in tainted and (any(t in seg for t in tokens) or any(_word(t, seg) for t in tainted)):
                tainted.add(n)
                changed = True
    return tainted


def _word(name: str, text: str) -> bool:
    import re
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


def _bench_subjectless(path: str, tokens: set[str]) -> list[str]:
    src = (ROOT / path).read_text(encoding="utf-8")
    extra: set[str] = set()
    if path.endswith("_bench.py"):
        pub = (ROOT / path).with_name(Path(path).name.replace("_bench.py", ".py"))
        extra = _tainted_names(pub.read_text(encoding="utf-8"), tokens)
    tainted = _tainted_names(src, tokens, extra)
    bad = []
    for name, node in _tests(ast.parse(src)):
        seg = _segment(src, node)
        if not (any(t in seg for t in tokens) or any(_word(n, seg) for n in tainted)):
            bad.append(f"{path}::{name}")
    return bad


def test_every_ds_p1_103_dropped_test_file_is_a_drop_row():
    if not (ROOT / BENCH_DIR).is_dir():
        return  # the public export: the dropped files are absent by construction
    sibs = _siblings()
    assert sibs, "no *_bench.py sibling found: the split this guard covers is gone"
    files = sibs + [p for p in WHOLE_FILES if (ROOT / p).is_file()]
    not_dropped = [p for p in files if H._matches_drop_list(p) is None]
    assert not not_dropped, f"DS-P1.103 bench test files the export would ship: {not_dropped}"
    rows = set(H.DROP_PATTERNS)
    assert [p for p in sibs if p not in rows] == [], "each *_bench.py sibling needs its own DROP row"


def test_every_test_in_a_dropped_ds_p1_103_file_has_a_bench_subject():
    if not (ROOT / BENCH_DIR).is_dir():
        return  # the public export: the dropped files are absent, so there is nothing to judge
    tokens = _bench_tokens()
    files = _siblings() + [p for p in WHOLE_FILES if (ROOT / p).is_file()]
    bad = [b for p in files for b in _bench_subjectless(p, tokens) if b not in INDIRECT_SUBJECT]
    assert bad == [], f"tests in a dropped file with no bench subject (a product test hidden from the export): {bad}"


def _public_test_files() -> list[str]:
    return [p for p in _tracked("*test_*.py", "*_test.py") if H._matches_drop_list(p) is None]


def _bench_paths(path: str) -> list[str]:
    """String constants that build a bench/ path: "bench/..." (or containing "/bench/"), or "bench" as an
    operand of a ``/`` path join. Docstrings are skipped."""
    src = (ROOT / path).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    join_operands = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            for side in (node.left, node.right):
                if isinstance(side, ast.Constant):
                    join_operands.add(id(side))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if v.startswith("bench/") or "/bench/" in v or (v == "bench" and id(node) in join_operands):
                hits.append(f"{path}:{node.lineno}: {v!r}")
    return hits


#: Public test files allowed to build a bench/ path, each with its ruling. Nothing else is.
ALLOWED_BENCH_PATHS = {
    # DS-P1.103 F(b) (MC ruling): the deployment-contract sandbox mirrors the checkout, bench/ only when the
    # checkout has it; in the export the branch is never taken and the test runs on the products alone.
    "sw/linux/tests/test_deployment_contract.py",
    # This guard names the patterns it looks for.
    "tools/tests/test_bench_test_split_ds_p1_103.py",
    # DS-P2.287 x DS-P1.103 (MC ruling 21:3x, option A): the lock leg's bench case builds bench/targets/gamma.yml in
    # a scratch repository; it tests the leg's own path handling and never reads this checkout's bench/.
    "tools/tests/test_lock_check_leg_per_target_ds_p2_287.py",
}


def test_no_public_test_builds_a_bench_path():
    hits = [h for p in _public_test_files() if p not in ALLOWED_BENCH_PATHS for h in _bench_paths(p)]
    assert hits == [], f"public tests (shipped in the export) that build a bench/ path: {hits}"


_ENTITY = re.compile(r"^\s*entity\s+(\w+)\s+is\b", re.IGNORECASE | re.MULTILINE)


def _entities(paths: list[str]) -> set[str]:
    names: set[str] = set()
    for p in paths:
        names |= {m.lower() for m in _ENTITY.findall((ROOT / p).read_text(encoding="utf-8", errors="replace"))}
    return names


def _dropped_only_entities() -> set[str]:
    """Entities declared by a VHDL file the export drops and by no file it ships. An entity name that a shipped file
    also declares (a common word such as "top" would be one) is never a finding: only a name unique to dropped files
    proves a public test depends on something the export does not carry."""
    vhd = _tracked("*.vhd", "*.vhdl")
    dropped = [p for p in vhd if H._matches_drop_list(p) is not None]
    shipped = [p for p in vhd if H._matches_drop_list(p) is None]
    return _entities(dropped) - _entities(shipped)


def _entity_refs(path: str, names: set[str]) -> list[str]:
    """String constants that name a dropped-only entity: the bare name, or a path whose stem is it."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8", errors="replace"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value.strip().lower()
            if v in names or (("/" in v or v.endswith((".vhd", ".vhdl"))) and Path(v).stem in names):
                hits.append(f"{path}:{node.lineno}: {node.value!r}")
    return hits


def test_no_public_test_names_a_dropped_entity():
    # DS-P2.300: a public test that elaborates a testbench the export drops cannot run on the export, even when it
    # names the testbench by entity rather than by path.
    names = _dropped_only_entities()
    assert names, "no dropped-only VHDL entity found: the guard has nothing to look for"
    hits = [h for p in _public_test_files() if p not in ALLOWED_BENCH_PATHS for h in _entity_refs(p, names)]
    assert hits == [], f"public tests (shipped in the export) that name a dropped VHDL entity: {hits}"
