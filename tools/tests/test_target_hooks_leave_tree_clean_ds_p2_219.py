# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.219: a target's pre_sim / pre_build hooks never modify a tracked file.

`rr artifact publish` refuses a build from a dirty tree, and the only dirt a clean commit's build left
was the hooks' own output: the CTU CAN FD bench targets (canfd_demo, de25_canfd) ran the datalogger
generator with their own mix straight into the tracked default-mix hw/rtl/pkg/datalogger_config_pkg.vhd
(one line: logical pin 28, JB5 against CAN_BUS). The stamp (rom_info_pkg.vhd, DS-P2.231) and the
per-target ip.lock (DS-P1.103) were already clean.

The check copies the tracked tree into a throw-away git repository (one commit, so `git status` is the
verdict a publish sees), runs every pre_sim then every pre_build hook of one target from that root, and
requires `git status --porcelain` to be empty after each hook. The per-target generated package is a
gitignored output, so the same copy also proves a first sim of a fresh clone finds it: after the
pre_sim hooks alone, every output declared in tools/generated_sources.yml `per_target_families` for the
target exists. The preflight (environment) and seed-libs (simulator work libraries) hooks declare no
tracked output and are skipped; every other hook must exit 0.

The mutant is one text change: the CTU target's `--pkg-dir` points back at the tracked directory, and
the same checker must then report dirt.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from target_files import all_target_files  # noqa: E402

SKIPPED_HOOKS = {"golden-path-preflight", "pre-sim-seed-libs"}
STAGES = ("pre_sim", "pre_build")
TRACKED_DEFAULT = "hw/rtl/pkg/datalogger_config_pkg.vhd"
POINTS_AT_TRACKED = "    - --pkg-dir\n    - hw/rtl/pkg\n"


def _scratch_env() -> dict:
    """The caller's environment without Git's repository selectors, so a scratch command never addresses the caller's repo."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return dict(env, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, errors="replace",
                          check=True, env=_scratch_env())


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True,
                         errors="replace", check=True).stdout
    return [p for p in out.split("\0") if p]


def _make_repo(dest: Path, mutate: tuple[str, str, str] | list[tuple[str, str, str]] | None = None) -> Path:
    """Copy the working tree's tracked files into *dest* and commit them; gitignored `libs/` content
    (synced packages the hooks read) is linked in, never committed. *mutate* = (file, old, new)."""
    dest.mkdir(parents=True)
    for rel in _tracked_files():
        src = ROOT / rel
        if not src.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    if mutate is not None:
        for rel, old, new in ([mutate] if isinstance(mutate, tuple) else mutate):
            text = (dest / rel).read_text(encoding="utf-8")
            assert old in text, f"mutant anchor not found in {rel}"
            (dest / rel).write_text(text.replace(old, new), encoding="utf-8")
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--no-verify", "-m", "clean")
    libs = ROOT / "libs"
    if libs.is_dir():
        # Scratch-owned package files (a hook that wrote through a link would modify the real checkout's
        # packages while the scratch `git status` stayed clean).
        with (dest / ".git" / "info" / "exclude").open("a", encoding="utf-8") as fh:
            fh.write("/libs/**\n")   # the packages are scaffolding, not part of the clean commit
        shutil.copytree(libs, dest / "libs", symlinks=True, dirs_exist_ok=True)
    return dest


def _hooks(root: Path, target_yml: str) -> list[tuple[str, dict]]:
    doc = yaml.safe_load((root / target_yml).read_text(encoding="utf-8")) or {}
    hooks = doc.get("hooks") or {}
    return [(stage, h) for stage in STAGES for h in (hooks.get(stage) or []) if h["name"] not in SKIPPED_HOOKS]


def _run_hook(root: Path, hook: dict) -> subprocess.CompletedProcess:
    script = hook["script"]
    cmd = ([sys.executable, script] if script.endswith(".py") else [script]) + [str(a) for a in hook.get("args") or []]
    return subprocess.run(cmd, cwd=root, capture_output=True, text=True, errors="replace", check=False, env=_scratch_env())


def _porcelain(root: Path) -> str:
    return _git(root, "status", "--porcelain").stdout.strip()


def dirt_after_hooks(root: Path, target_yml: str, stages: tuple[str, ...] = STAGES) -> list[str]:
    """One line per hook of *stages* that failed or left `git status` non-empty (empty list = clean)."""
    problems: list[str] = []
    for stage, hook in _hooks(root, target_yml):
        if stage not in stages:
            continue
        proc = _run_hook(root, hook)
        if proc.returncode != 0:
            problems.append(f"{stage}/{hook['name']}: exit {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}")
            continue
        dirt = _porcelain(root)
        if dirt:
            problems.append(f"{stage}/{hook['name']}: left {dirt.replace(chr(10), ' | ')}")
    return problems


def _declared_targets() -> list[str]:
    """Targets the inventory names that exist in this tree (the public export carries no bench/)."""
    manifest = yaml.safe_load((ROOT / "tools" / "generated_sources.yml").read_text(encoding="utf-8")) or {}
    names = [t for fam in (manifest.get("per_target_families") or {}).values() for t in (fam.get("targets") or {})]
    return [t for t in names if (ROOT / t).is_file()]


def _declared_per_target_outputs(target_yml: str) -> list[str]:
    manifest = yaml.safe_load((ROOT / "tools" / "generated_sources.yml").read_text(encoding="utf-8")) or {}
    out: list[str] = []
    for family in (manifest.get("per_target_families") or {}).values():
        entry = (family.get("targets") or {}).get(target_yml)
        if entry:
            out.extend(entry["outputs"])
    return out


TARGETS = [p.relative_to(ROOT).as_posix() for p in all_target_files(ROOT)]


@pytest.mark.parametrize("target_yml", TARGETS)
def test_target_hooks_leave_a_clean_tree(target_yml, tmp_path):
    """Each stage from its own clean clone, then both in sequence, so one stage cannot supply what the
    other stage's hook was supposed to produce."""
    problems: list[str] = []
    for label, stages in (("pre_sim only", ("pre_sim",)), ("pre_build only", ("pre_build",)), ("both", STAGES)):
        root = _make_repo(tmp_path / label.replace(" ", "_"))
        if "pre_build" in stages and _declared_per_target_outputs(target_yml):
            # the target's build hooks read its pinned packages (the REA hook reads libs/.targets): stage
            # them in the scratch tree instead of relying on what the caller's checkout holds
            _install_target_packages(root, target_yml, tmp_path)
        problems += [f"[{label}] {p}" for p in dirt_after_hooks(root, target_yml, stages)]
        for output in _declared_per_target_outputs(target_yml):
            if not (root / output).is_file():
                problems.append(f"[{label}] {output} was not written")
            elif (root / output).read_text(encoding="utf-8") == (root / TRACKED_DEFAULT).read_text(encoding="utf-8"):
                # the whole point of the per-target copy: this target's mix (logical pin 28 on CAN_BUS) differs
                problems.append(f"[{label}] {output} equals the tracked default-mix package: the target's own mix was not used")
    assert not problems, f"{target_yml}: hooks dirtied the tree or failed:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("target_yml", TARGETS)
def test_first_sim_of_a_fresh_clone_finds_the_per_target_outputs(target_yml, tmp_path):
    declared = _declared_per_target_outputs(target_yml)
    if not declared:
        pytest.skip(f"{target_yml} declares no per-target generated output")
    root = _make_repo(tmp_path / "clone")
    for output in declared:
        assert not (root / output).exists(), f"{output} exists in a fresh clone: not a generated output"
    for stage, hook in _hooks(root, target_yml):
        if stage != "pre_sim":
            continue
        proc = _run_hook(root, hook)
        assert proc.returncode == 0, f"pre_sim/{hook['name']} exit {proc.returncode}: {proc.stderr[-300:]}"
    missing = [o for o in declared if not (root / o).is_file()]
    assert not missing, f"after the pre_sim hooks of {target_yml} a fresh clone still lacks {missing}"


DEFAULT_MIX = "tools/datalogger-builder/config/datalogger_config.yml"


def _non_default_mix_targets() -> list[str]:
    """Targets whose build hook generates a mix other than the tracked default, read from the target files
    themselves (not from the inventory), so a deleted or misspelled inventory entry cannot hide one."""
    found = []
    for path in all_target_files(ROOT):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for hook in (doc.get("hooks") or {}).get("pre_build") or []:
            args = hook.get("args") or []
            if hook.get("name") == "datalogger-builder" and args and args[0] != DEFAULT_MIX:
                found.append(path.relative_to(ROOT).as_posix())
    return found


def test_every_target_with_its_own_mix_declares_its_per_target_output():
    """A target that generates a non-default mix must have its package in per_target_families, or the
    other tests here would skip it silently."""
    missing = [t for t in _non_default_mix_targets() if not _declared_per_target_outputs(t)]
    assert not missing, f"targets generating their own mix with no per_target_families output: {missing}"


def test_the_declared_per_target_outputs_are_gitignored_and_not_runtime_families():
    """ds_sim_sources adds every runtime_families output to EVERY sim; a per-target mix there would
    shadow the tracked default package everywhere, so the two inventories stay apart."""
    manifest = yaml.safe_load((ROOT / "tools" / "generated_sources.yml").read_text(encoding="utf-8")) or {}
    runtime = {o for f in (manifest.get("runtime_families") or {}).values() for o in f.get("outputs") or []}
    declared = [o for t in TARGETS for o in _declared_per_target_outputs(t)]
    if not declared:
        # The public export carries no bench/ targets, so no per-target family resolves to a file
        # there. In the private tree the families name two targets: an empty list is then a defect.
        names = [t for fam in (manifest.get("per_target_families") or {}).values() for t in (fam.get("targets") or {})]
        assert not any((ROOT / t).is_file() for t in names), "per_target_families targets exist but declare no output"
        pytest.skip("this tree carries no per-target bench target (public export)")
    for output in declared:
        assert output not in runtime, f"{output} must not be a runtime_families output"
        ignored = subprocess.run(["git", "check-ignore", "-q", output], cwd=ROOT, check=False).returncode == 0
        assert ignored, f"{output} is not gitignored: a build would leave the tree dirty"
        # An ip.yml owns every .vhd under its directory: a leftover per-target package under such a
        # directory (generated/ has one) is scanned by EVERY target's resolver and conflicts with the
        # tracked default (measured: de25_datalogger, de25_p2writer, pfsoc_* locks went stale).
        for parent in Path(output).parents:
            if str(parent) == ".":
                continue                      # the project-root ip.yml is not a package manifest
            assert not (ROOT / parent / "ip.yml").exists(), \
                f"{output} sits under {parent}/ip.yml: other targets would resolve it too"


def _install_target_packages(root: Path, target_yml: str, tmp_path: Path) -> None:
    """Materialise the target's pinned packages in the scratch tree, so the result never depends on what the
    caller's libs/.targets happened to hold."""
    rr = shutil.which("rr")
    assert rr is not None, "rr is not on PATH; rr is required to run this test (DS-P2.219)"
    env = dict(os.environ, RR_TARGET=target_yml, TMPDIR=str(tmp_path), RR_NO_PYTHON_DEPRECATION_WARNING="1")
    proc = subprocess.run([rr, "pkg", "install"], cwd=root, capture_output=True, text=True, errors="replace",
                          env=env, stdin=subprocess.DEVNULL, timeout=300, check=False)
    assert proc.returncode == 0, f"rr pkg install failed for {target_yml}: {(proc.stdout + proc.stderr)[-1200:]}"


def _syn_listing(root: Path, target_yml: str, tmp_path: Path) -> list[str]:
    rr = shutil.which("rr")
    assert rr is not None, "rr is not on PATH; rr is required to run this test (DS-P2.219)"
    env = dict(os.environ, RR_TARGET=target_yml, TMPDIR=str(tmp_path), RR_NO_PYTHON_DEPRECATION_WARNING="1")
    proc = subprocess.run([rr, "sources", "--syn"], cwd=root, capture_output=True, text=True, errors="replace",
                          env=env, stdin=subprocess.DEVNULL, timeout=300, check=False)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]
    return [line.strip() for line in proc.stdout.splitlines() if line.strip().endswith("datalogger_config_pkg.vhd")]


def _binding_problems(root: Path, target_yml: str, tmp_path: Path) -> list[str]:
    """What `rr sources --syn` binds for the datalogger package after the build hook ran."""
    problems = dirt_after_hooks(root, target_yml, ("pre_build",))
    generated = _declared_per_target_outputs(target_yml)[0]
    _install_target_packages(root, target_yml, tmp_path)
    listing = _syn_listing(root, target_yml, tmp_path)
    if listing != [generated]:
        problems.append(f"the compile set must hold exactly {generated} and no other datalogger_config_pkg.vhd: {listing}")
    if "hw/rtl/pkg/datalogger_config_pkg.vhd" in listing:
        problems.append(f"the tracked default copy is still compiled: {listing}")
    return problems


@pytest.mark.parametrize("target_yml", _declared_targets() or [pytest.param("", marks=pytest.mark.skip(reason="no per-target target in this tree"))])
def test_the_resolver_binds_exactly_the_generated_copy(target_yml, tmp_path):
    """Through the standard resolver (rr sources --syn): after the build hook ran, the generated copy is in
    the compile set exactly once and the tracked default copy is not. A target that dropped the exclude, the
    paths.sources entry or the hook fails here, which running the hooks alone cannot show."""
    root = _make_repo(tmp_path / "clone")
    assert not _binding_problems(root, target_yml, tmp_path)


@pytest.mark.parametrize("target_yml", _declared_targets() or [pytest.param("", marks=pytest.mark.skip(reason="no per-target target in this tree"))])
def test_mutant_without_the_source_dir_loses_the_generated_copy(target_yml, tmp_path):
    """The binding check bites: with the generated dir gone from paths.sources and the library pin, the
    compile set holds neither copy of the package (the tracked one is excluded), and the check reports it."""
    output = _declared_per_target_outputs(target_yml)[0]
    out_dir = output.rsplit("/", 1)[0]
    root = _make_repo(tmp_path / "mutant", mutate=[
        (target_yml, f"  - hw/rtl\n  - {out_dir}\n", "  - hw/rtl\n"),          # paths.sources entry
        (target_yml, f"    tich:\n    - {output}\n", "    tich: []\n")])        # library pin
    problems = _binding_problems(root, target_yml, tmp_path)
    assert any("must hold exactly" in p for p in problems), f"the mutant was not reported: {problems}"


@pytest.mark.parametrize("target_yml", _declared_targets() or [pytest.param("", marks=pytest.mark.skip(reason="no per-target target in this tree"))])
def test_mutant_pointing_the_hook_at_the_tracked_package_is_reported(target_yml, tmp_path):
    """The checker bites: with `--pkg-dir` back at hw/rtl/pkg the target's hook dirties a tracked file."""
    out_dir = _declared_per_target_outputs(target_yml)[0].rsplit("/", 1)[0]
    points_at_generated = f"    - --pkg-dir\n    - {out_dir}\n"
    root = _make_repo(tmp_path / "mutant", mutate=(target_yml, points_at_generated, POINTS_AT_TRACKED))
    problems = dirt_after_hooks(root, target_yml, ("pre_build",))
    assert any("datalogger-builder" in p and "hw/rtl/pkg/datalogger_config_pkg.vhd" in p for p in problems), \
        f"the mutant was not reported: {problems}"
