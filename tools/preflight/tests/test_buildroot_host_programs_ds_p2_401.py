# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.401: the preflight names every missing Buildroot host program at once.

A clean ubuntu:24.04 with the host setup packages stopped in the PolarFire guide's Buildroot step on the first missing
program ("You must install '/usr/bin/file' on your build machine"); Buildroot names one per failed run.

The two entry-point tests below need nothing from the declaration to run, so on a tree without this change they fail
on behaviour (an unknown --stage, a script that starts Buildroot), not at collection.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "preflight"))
import ds_preflight as P  # noqa: E402

CHECK = "linux.buildroot_programs"
PREFLIGHT = ROOT / "tools" / "preflight" / "ds_preflight.py"
OVERLAY_SCRIPT = ROOT / "sw" / "linux" / "pfsoc" / "build_python_overlay.sh"
#: The names a stub PATH provides. Fixed here, not read from the declaration, so the entry-point tests run on any tree.
STUB_NAMES = ("which", "sed", "make", "gcc", "cc", "g++", "c++", "bash", "perl", "tar", "wget", "cpio", "unzip",
              "rsync", "bc", "cmp", "find", "xargs", "patch", "grep", "gzip", "bzcat", "git", "dirname", "env")


def _programs() -> list[str]:
    return list((P.load_declaration().get("linux_host") or {}).get("buildroot_programs") or [])


def _probe(**found: bool) -> P.Probe:
    programs = {name: True for name in _programs()}
    programs.update(found)
    return P.Probe(host_programs=programs)


def _result(probe: P.Probe) -> P.Result:
    return P._check_buildroot_host_programs(probe, P.load_declaration(), ROOT)


def _stub_path(base: Path, drop: set[str], log: Path) -> str:
    """A PATH directory with a stub for each name in STUB_NAMES except *drop*, and the test's own python3.

    Every stub appends its name and arguments to *log*, so a test can see whether Buildroot was cloned or built."""
    base.mkdir()
    for name in STUB_NAMES:
        if name in drop:
            continue
        real = Path("/usr/bin") / name
        if name in ("bash", "dirname", "env", "sed", "grep") and real.exists():
            (base / name).symlink_to(real)  # the scripts and python run these; a recording stub would break them
            continue
        stub = base / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n', encoding="utf-8")
        stub.chmod(0o755)
    (base / "python3").symlink_to(sys.executable)
    return str(base)


def _run(argv: list[str], path: str) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=ROOT, env=dict(os.environ, PATH=path), capture_output=True, text=True,
                          check=False)


def _found_line(stderr: str) -> str:
    return next((ln for ln in stderr.splitlines() if "found    :" in ln), "")


def test_the_declared_set_holds_the_programs_buildroot_stops_on():
    # The F21 program, the compilers (either name), and git, which the scripts clone Buildroot with.
    programs = _programs()
    for name in ("/usr/bin/file", "gcc|cc", "g++|c++", "make", "patch", "grep", "gzip", "bzcat", "git"):
        assert name in programs, name


def test_two_missing_programs_are_both_named_with_the_fix():
    r = _result(_probe(bc=False, cpio=False))
    assert not r.ok and r.blocking and r.stage == "linux"
    assert r.found == "missing cpio, bc", r.found
    assert "rr linux tools install" in r.fix
    assert _result(_probe()).ok
    assert not _result(P.Probe(host_programs=None)).ok


@pytest.mark.parametrize("program", ["grep", "gzip", "bzcat"])
def test_a_missing_extractor_or_grep_is_reported(program):
    r = _result(_probe(**{program: False}))
    assert not r.ok and r.found == f"missing {program}", r.found


def test_an_absolute_program_is_judged_by_its_path_not_by_a_same_named_path_entry(tmp_path):
    """A PATH entry named ``file`` does not satisfy an absolute ``/…/file`` requirement. Judged through the CLI with
    --declaration, which also proves the probe reads the declaration it is judged against."""
    log = tmp_path / "calls.log"
    absent = "/nonexistent-ds-p2-401/file"
    decl = yaml.safe_load(P.DECLARATION.read_text(encoding="utf-8"))
    programs = decl["linux_host"]["buildroot_programs"]
    programs[programs.index("/usr/bin/file")] = absent
    decl_path = tmp_path / "declaration.yml"
    decl_path.write_text(yaml.safe_dump(decl), encoding="utf-8")
    path = _stub_path(tmp_path / "with-file", set(), log)
    (Path(path) / "file").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (Path(path) / "file").chmod(0o755)
    proc = _run([sys.executable, str(PREFLIGHT), "--stage", "linux", "--declaration", str(decl_path)], path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _found_line(proc.stderr).endswith(f"missing {absent}"), proc.stderr


def test_a_program_added_by_a_declaration_override_is_probed(tmp_path):
    """The probe reads the declaration it is judged against: a program only the --declaration file names, present
    on PATH, is found, not reported missing."""
    log = tmp_path / "calls.log"
    decl = yaml.safe_load(P.DECLARATION.read_text(encoding="utf-8"))
    decl["linux_host"]["buildroot_programs"].append("ds-p2-401-extra")
    decl_path = tmp_path / "declaration.yml"
    decl_path.write_text(yaml.safe_dump(decl), encoding="utf-8")
    path = _stub_path(tmp_path / "with-extra", set(), log)
    (Path(path) / "ds-p2-401-extra").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (Path(path) / "ds-p2-401-extra").chmod(0o755)
    proc = _run([sys.executable, str(PREFLIGHT), "--stage", "linux", "--declaration", str(decl_path)], path)
    assert "ds-p2-401-extra" not in _found_line(proc.stderr), proc.stderr
    if Path("/usr/bin/file").exists():
        assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_linux_stage_runs_no_other_probe_or_check(monkeypatch, capsys):
    """A Buildroot script runs --stage linux; an unrelated probe or check that raises or writes must not stop the
    missing-program report (T1 rv-a664495075)."""
    def boom(*_args, **_kwargs):
        raise AssertionError("an unrelated probe or check ran under --stage linux")

    monkeypatch.setattr(P, "probe_system", boom)
    monkeypatch.setattr(P, "CHECKS", tuple(boom for _ in P.CHECKS))
    monkeypatch.setattr(P, "_host_programs", lambda names: {n: n != "bc" for n in names})
    assert P.main(["--stage", "linux"], root=ROOT) == 1
    err = capsys.readouterr().err
    assert _found_line(err).endswith("missing bc") and "rr linux tools install" in err, err


def test_the_linux_stage_does_not_gate_the_fpga_build():
    ids_by_stage = {s: {r.id for r in P.run_checks(s, probe=_probe(), root=ROOT)} for s in ("build", "bench")}
    assert all(CHECK not in ids for ids in ids_by_stage.values()), ids_by_stage


def test_the_cli_on_a_host_without_two_programs_names_both_and_fails(tmp_path):
    """Done-when 2: a PATH missing two programs; the shipped CLI names both, names the fix, exits 1."""
    log = tmp_path / "calls.log"
    proc = _run([sys.executable, str(PREFLIGHT), "--stage", "linux"],
                _stub_path(tmp_path / "two-missing", {"bc", "rsync"}, log))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    found = _found_line(proc.stderr)
    assert "rsync" in found and "bc" in found, proc.stderr
    assert "rr linux tools install" in proc.stderr
    # Control: the same PATH with nothing removed passes (or names only the absolute path this host lacks).
    full = _run([sys.executable, str(PREFLIGHT), "--stage", "linux"], _stub_path(tmp_path / "all", set(), log))
    if Path("/usr/bin/file").exists():
        assert full.returncode == 0, full.stdout + full.stderr
    else:
        assert _found_line(full.stderr).endswith("missing /usr/bin/file"), full.stderr


def test_a_host_without_any_of_them_gets_the_complete_list(tmp_path):
    """Every missing program is named, not the first few (T1 rv-69e2922ca1): a PATH holding only python3."""
    path = tmp_path / "only-python"
    path.mkdir()
    (path / "python3").symlink_to(sys.executable)
    proc = _run([sys.executable, str(PREFLIGHT), "--stage", "linux"], str(path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    expected = [n for n in _programs() if not (n.startswith("/") and Path(n).exists())]
    assert _found_line(proc.stderr).split("found    : ", 1)[1] == "missing " + ", ".join(expected), proc.stderr


def test_the_polarfire_overlay_script_stops_before_buildroot_naming_git_and_bc(tmp_path):
    """The wrapper itself, on a host without git and bc: exit 1, both named with the fix, nothing cloned or built.

    git is dropped on purpose: a check that needed git to find itself could not report git missing."""
    log = tmp_path / "calls.log"
    proc = _run(["bash", str(OVERLAY_SCRIPT)], _stub_path(tmp_path / "no-git-bc", {"git", "bc"}, log))
    said = f"rc={proc.returncode}\n{proc.stdout}{proc.stderr}"
    assert proc.returncode == 1, said
    found = _found_line(proc.stderr)
    assert re.search(r"\bgit\b", found) and re.search(r"\bbc\b", found), said
    assert "rr linux tools install" in proc.stderr, said
    ran = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert not [c for c in ran if c.split()[0] in ("git", "make")], f"Buildroot was started: {ran}"


def test_the_zybo_script_precheck_list_is_inside_the_declaration():
    """sw/linux/build_buildroot.sh keeps its own list (DS-P2.350); it must not name a program the declaration lacks."""
    text = (ROOT / "sw/linux/build_buildroot.sh").read_text(encoding="utf-8")
    listed = set(re.search(r"for prog in ([a-z+ ]+); do", text).group(1).split())
    declared = {alt.rsplit("/", 1)[-1] for name in _programs() for alt in name.split("|")}
    assert listed - declared == set(), sorted(listed - declared)
