# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.337: `pip install -r requirements.txt` after routertl downgrades nothing.

The clean-machine rehearsal (routertl 5.5.0 from PyPI, then the README's
`pip install -r requirements.txt`) downgraded pytest 9.2.0 -> 9.0.2, pluggy
1.7.0 -> 1.6.0, packaging 26.3 -> 26.0, markdown-it-py 4.3.0 -> 3.0.0 and more:
requirements.txt pinned exact versions of packages routertl's own dependencies
had already installed newer. Reproduced on Python 3.10 with the same nine
downgrades; with this file, no version changes and `pip check` is clean.

Two guards: no exact pin in requirements.txt (static, here), and CI compares
`pip freeze` before and after the install (tools/ci/requirements_no_downgrade.py).
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "ci" / "requirements_no_downgrade.py"
_SPEC = importlib.util.spec_from_file_location("requirements_no_downgrade", TOOL)
nd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(nd)

WORKFLOWS = (".forgejo/workflows/ci.yml", ".github/workflows/ci.yml")


def _requirements() -> list[Requirement]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [Requirement(line.split("#", 1)[0].strip())
            for line in lines if line.split("#", 1)[0].strip()]


def test_requirements_txt_has_no_exact_pin():
    exact = [str(r) for r in _requirements()
             if any(s.operator in ("==", "===") for s in r.specifier)]
    assert exact == [], f"an exact pin downgrades what routertl installed; use a floor: {exact}"


def test_the_packages_that_were_downgraded_are_floors_or_absent():
    """The rehearsal's downgraded set, by name: each is now unlisted or a floor."""
    by_name = {r.name.lower(): r for r in _requirements()}
    for name in ("pytest", "pluggy", "packaging", "markdown-it-py", "numpy", "typing_extensions",
                 "iniconfig", "pygments", "tomli", "find_libpython"):
        req = by_name.get(name)
        if req is not None:
            assert all(s.operator in (">=", "<") for s in req.specifier), str(req)


def test_the_markdown_parser_range_admits_both_tested_majors():
    md = next(r for r in _requirements() if r.name == "markdown-it-py")
    assert md.specifier.contains("3.0.0") and md.specifier.contains("4.3.0")


# ── the CI check ─────────────────────────────────────────────────────────────

def test_a_lower_version_is_a_downgrade():
    before = nd.parse_freeze("pytest==9.2.0\npluggy==1.7.0\nrich==15.0.0\n")
    after = nd.parse_freeze("pytest==9.0.2\npluggy==1.7.0\nrich==15.1.0\nscapy==2.7.0\n")
    assert nd.downgrades(before, after) == [("pytest", "9.2.0", "9.0.2")]


def test_names_are_compared_canonically():
    before = nd.parse_freeze("Markdown_It_Py==4.3.0\n")
    after = nd.parse_freeze("markdown-it-py==3.0.0\n")
    assert nd.downgrades(before, after) == [("markdown-it-py", "4.3.0", "3.0.0")]


def test_editable_and_url_lines_are_not_versions():
    text = "-e git+https://example.invalid/x.git@abc#egg=x\nfoo @ file:///w/foo\nbar==1.0\n"
    assert nd.parse_freeze(text) == {"bar": "1.0"}


def _run(before: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), str(before)], capture_output=True, text=True)


def test_the_cli_fails_when_this_interpreter_holds_a_lower_version(tmp_path):
    """Planted: a 'before' that had a far newer pytest than this interpreter has."""
    before = tmp_path / "before.txt"
    before.write_text("pytest==999.0.0\n", encoding="utf-8")
    r = _run(before)
    assert r.returncode == 1, r.stdout + r.stderr
    assert re.search(r"pytest 999\.0\.0 -> \S+", r.stdout), r.stdout


def test_the_cli_passes_when_nothing_went_down(tmp_path):
    before = tmp_path / "before.txt"
    before.write_text("pytest==0.0.1\n", encoding="utf-8")
    r = _run(before)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("content", [None, "", "-e git+https://example.invalid/x.git#egg=x\n"])
def test_an_unusable_before_file_is_an_error_not_a_pass(tmp_path, content):
    before = tmp_path / "before.txt"
    if content is not None:
        before.write_text(content, encoding="utf-8")
    assert _run(before).returncode == 2


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_ci_checks_every_requirements_install(workflow):
    text = (ROOT / workflow).read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]
    installs = [i for i, line in enumerate(lines) if line == "python -m pip install -r requirements.txt"]
    assert installs, workflow
    for i in installs:
        assert lines[i - 1].startswith("python -m pip freeze > "), (workflow, lines[i - 1])
        assert lines[i + 1].startswith("python tools/ci/requirements_no_downgrade.py "), (workflow, lines[i + 1])
        assert lines[i + 2] == "python -m pip check", (workflow, lines[i + 2])
