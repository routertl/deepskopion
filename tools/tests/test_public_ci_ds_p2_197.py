# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.197: the public CI workflow, and what it leaves out, stay honest."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
EXCLUSIONS = ROOT / "tools" / "ci" / "desk_test_exclusions.txt"
RUNNER = ROOT / "tools" / "ci" / "run_desk_tests.sh"
REASONS = {"simulator", "coverage entitlement"}


def _steps() -> list[dict]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data["jobs"]["desk-checks"]["steps"]


def _run_text() -> str:
    return "\n".join(step.get("run", "") for step in _steps())


def _exclusions() -> list[tuple[str, str]]:
    rows = []
    for line in EXCLUSIONS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        node, _, reason = line.partition("#")
        rows.append((node.strip(), reason.strip()))
    return rows


def test_the_workflow_ships_in_the_public_export():
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    from public_artifact_hygiene import _matches_drop_list
    for path in (".github/workflows/ci.yml", "tools/ci/run_desk_tests.sh",
                 "tools/ci/desk_test_exclusions.txt"):
        assert _matches_drop_list(path) is None, path


def test_the_workflow_cannot_be_green_by_construction():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "secrets." not in text


def test_routertl_comes_from_the_published_wheel():
    """Every routertl install is a PyPI requirement on the declared floor.

    Since DS-P2.209 the step installs ``"$spec"``, set per matrix leg to
    ``routertl==$floor`` or ``routertl>=$floor``; a spec that is a URL, a path
    or an unbounded name fails here."""
    run = _run_text()
    specs = re.findall(r'spec="([^"]*)"', run)
    assert specs, "no routertl install spec in the workflow"
    for spec in specs:
        assert re.fullmatch(r"routertl(==|>=)\$floor", spec), spec
    assert re.search(r'pip install "\$spec"', run)
    assert "git+" not in run
    assert "bitbucket" not in run
    assert "github.com" not in run


def test_the_workflow_runs_sync_lint_and_the_desk_tests():
    run = _run_text()
    for command in ("rr workspace sync", "rr linting", "tools/ci/run_desk_tests.sh"):
        assert command in run


def test_every_exclusion_names_a_known_reason():
    rows = _exclusions()
    assert rows
    assert {reason for _, reason in rows} <= REASONS, rows


def test_every_exclusion_names_a_test_that_exists():
    for node, _ in _exclusions():
        path, _, name = node.partition("::")
        assert (ROOT / path).is_file(), node
        if name:
            assert f"def {name}(" in (ROOT / path).read_text(encoding="utf-8"), node


def test_exclusions_stay_few():
    assert len(_exclusions()) <= 20


def test_a_failing_test_fails_the_runner():
    text = RUNNER.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "|| true" not in text
    assert "--continue-on-collection-errors" not in text


def test_the_private_commit_gate_runs_these_guards():
    """DS-P2.209 turned a check in this file red on main because only public CI
    ran it. The commit gate now runs this file and the floor-leg guard."""
    hook = (ROOT / "hooks" / "pre-commit.local").read_text(encoding="utf-8")
    m = re.search(r"^# ── DS-P2\.197 — the public CI workflow.*?^echo \"✓ Public CI", hook, re.M | re.S)
    assert m, "hooks/pre-commit.local has no DS-P2.197 public CI leg"
    for name in ("tools/tests/test_public_ci_ds_p2_197.py",
                 "tools/tests/test_ci_routertl_floor_leg_ds_p2_209.py"):
        assert name in m.group(0), name
    assert "exit 1" in m.group(0)


def test_the_private_commit_gate_runs_the_cross_target_desk_guards():
    """Commits 2a8e1eb6 (DS-P3.211) and 8ebcce01 (DS-P1.90) broke two fast
    cross-target / core-mux desk guards on main because pre-commit.local did
    not run them. Keep both in the DS-P2.197 pre-commit leg."""
    hook = (ROOT / "hooks" / "pre-commit.local").read_text(encoding="utf-8")
    m = re.search(r"^# ── DS-P2\.197 — the public CI workflow.*?^echo \"✓ Public CI", hook, re.M | re.S)
    assert m, "hooks/pre-commit.local has no DS-P2.197 public CI leg"
    for name in ("tools/tests/test_de25_canfd_target_ds_p2_135.py",
                 "tools/tests/test_pfsoc_mss_fabric_selftest_ds_p2_126.py"):
        assert name in m.group(0), (
            f"hooks/pre-commit.local DS-P2.197 leg omits cross-target desk guard {name}")


def test_the_workflow_installs_the_simulator_the_lock_records():
    """The desk tests that run rr sim need the project's simulator, which
    routertl.lock records (replay 2026-11-05: 8 test_edge_rle_sim cases
    failed with nvc not on PATH)."""
    # DS-P2.287: one lock per target (targets/<t>.routertl.lock), every one checked.
    target_locks = sorted(ROOT.glob("targets/*.routertl.lock"))
    assert target_locks, "no targets/*.routertl.lock"
    runs = [s.get("run", "") for s in _steps()]
    for lock_path in target_locks:
        lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
        sim = lock["simulation"]["simulator"]
        assert f"rr eda install {sim}" in runs, (lock_path.name, sim, runs)


def test_rr_installed_tools_are_on_path_before_they_are_used():
    """rr eda install links into ~/.local/bin; lint found no GHDL without it."""
    runs = [s.get("run", "") for s in _steps()]
    path_step = next(i for i, r in enumerate(runs) if "$HOME/.local/bin" in r and "GITHUB_PATH" in r)
    first_install = next(i for i, r in enumerate(runs) if r.startswith("rr eda install"))
    assert path_step < first_install


def test_fresh_tree_config_control_sim_runs_unskipped_and_fails_without_tools(
    monkeypatch, tmp_path: Path
) -> None:
    """DS-T3.34: test_isolated_unit_sim_fresh_tree_p2_138 runs in desk-checks
    with no skipif markers and fails loudly if rr or nvc is absent from PATH."""
    import importlib.util
    import subprocess

    fresh_rel = "tools/tests/test_isolated_unit_sim_fresh_tree_p2_138.py"
    excluded = [node for node, _ in _exclusions() if node.startswith(fresh_rel)]
    assert excluded == [], f"{fresh_rel} must not be excluded from desk-checks: {excluded}"

    spec = importlib.util.spec_from_file_location("fresh_tree_p2_138", ROOT / fresh_rel)
    assert spec is not None and spec.loader is not None
    fresh_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh_mod)

    marks = getattr(
        fresh_mod.test_config_control_sims_on_fresh_tree_without_rea_artifacts,
        "pytestmark",
        [],
    )
    assert marks == [], (
        "test_config_control_sims_on_fresh_tree_without_rea_artifacts carries "
        f"pytestmark={marks!r}; fresh-tree config_control sim must fail, not skip"
    )

    monkeypatch.setattr(
        fresh_mod.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout=b"", stderr=b""),
    )
    for missing_tool, label in (("rr", "rr CLI"), ("nvc", "nvc simulator")):
        monkeypatch.setattr(
            fresh_mod.shutil,
            "which",
            lambda name, missing=missing_tool: None if name == missing else f"/usr/bin/{name}",
        )
        caught: AssertionError | None = None
        try:
            fresh_mod._copy_tracked_tree(tmp_path / missing_tool)
        except AssertionError as exc:
            caught = exc
        assert caught is not None and (
            f"{label} not on PATH — fresh-tree config_control sim must fail, not skip"
            in str(caught)
        ), (
            f"missing {missing_tool} did not fail with AssertionError: "
            "fresh-tree config_control sim must fail, not skip"
        )


def test_run_desk_tests_requires_rr_and_nvc_on_path(tmp_path: Path) -> None:
    """DS-T3.34: tools/ci/run_desk_tests.sh refuses to run when rr or nvc is
    absent from PATH."""
    import shutil
    import subprocess
    import sys

    for missing_tool, present_tool in (("rr", "nvc"), ("nvc", "rr")):
        bin_dir = tmp_path / f"bin-no-{missing_tool}"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for host_cmd in ("bash", "dirname"):
            real = shutil.which(host_cmd)
            assert real is not None, host_cmd
            (bin_dir / host_cmd).symlink_to(real)
        (bin_dir / "python").symlink_to(sys.executable)
        stub = bin_dir / present_tool
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)

        proc = subprocess.run(
            [str(bin_dir / "bash"), str(RUNNER), "--version"],
            cwd=ROOT,
            env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert (
            proc.returncode != 0
            and f"ERROR: {missing_tool} not on PATH; desk-checks requires rr and nvc"
            in proc.stderr
        ), (
            f"run_desk_tests.sh did not fail when {missing_tool} was absent "
            f"(rc={proc.returncode}, stderr={proc.stderr!r}); "
            "desk-checks requires rr and nvc"
        )
