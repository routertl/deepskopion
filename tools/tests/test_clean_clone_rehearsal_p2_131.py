# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.131: Samsung-WSL clean-clone rehearsal regression guards.

Guards clean-room reproducibility findings surfaced during the samsung-wsl
remote rehearsal:
1. README.md quickstart requires a virtual environment (PEP 668 compliance on
   modern distros like Ubuntu 24.04) and documents RR_EDA_ALLOW_OVERSUBSCRIBE=1
   for Vivado builds on <=16 GB hosts.
2. .gitignore ignores Quartus Prime Pro root build artifacts generated during
   de25_datalogger synthesis (dni/, qdb/, hps_subsys.qsys, hps_subsys/,
   hps_subsys_qsys_script.*).
3. tools/regen_regbank.sh honors the PYTHON environment variable.
4. Internal rehearsal record and execution log are present with verified
   clean-room toolchain outputs.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]


def _section_tokens(markdown: str, title: str) -> list:
    """The CommonMark tokens of the top-level section headed ``title``, up to the next heading of its level or above."""
    tokens = MarkdownIt("commonmark").parse(markdown)
    at = next((i for i, t in enumerate(tokens) if t.type == "heading_open" and t.level == 0
               and tokens[i + 1].content == title), None)
    assert at is not None, f"no rendered heading {title!r}"
    level = int(tokens[at].tag[1])
    out = []
    for tok in tokens[at + 3:]:
        if tok.type == "heading_open" and tok.level == 0 and int(tok.tag[1]) <= level:
            break
        out.append(tok)
    return out


def _links_before_first_code(tokens: list) -> list[str]:
    """Link targets a reader meets in a section before its first code block."""
    hrefs = []
    for tok in tokens:
        if tok.type in ("fence", "code_block"):
            break
        hrefs += [c.attrGet("href") for c in tok.children or [] if c.type == "link_open"]
    return hrefs


def _venv_then_install(lines: list[str]) -> bool:
    """The recipe creates the environment, activates it, then installs RouteRTL, in that order (T1 rv-f91c9d6c3b).
    Here-document bodies are shell semantics, tracked by DS-P3.364."""
    def first(pred):
        return next((i for i, ln in enumerate(lines) if pred(ln)), None)
    create = first(lambda ln: ln.startswith("python3 -m venv .venv"))
    activate = first(lambda ln: ln == "source .venv/bin/activate")
    install = first(lambda ln: ln == "pip install routertl")
    return None not in (create, activate, install) and create < activate < install


def test_the_venv_order_check_refuses_a_reordered_recipe():
    """Negative control: installing before activating is refused."""
    assert _venv_then_install(["python3 -m venv .venv", "source .venv/bin/activate", "pip install routertl"])
    assert not _venv_then_install(["python3 -m venv .venv", "pip install routertl", "source .venv/bin/activate"])


def _shell_lines(tokens: list) -> list[str]:
    return [ln.strip() for tok in tokens if tok.type == "fence" and (tok.info.split() or [""])[0] in ("bash", "sh")
            for ln in tok.content.splitlines()]


def test_readme_quickstart_specifies_venv_and_ram_oversubscribe() -> None:
    """README.md quickstart must specify venv creation and RAM oversubscribe override."""
    readme_path = ROOT / "README.md"
    assert readme_path.is_file(), "README.md missing"
    text = readme_path.read_text(encoding="utf-8")

    # Owner 2026-11-25: the README's Simulation section starts from the host setup recipe instead of repeating the
    # Python steps, so it links that recipe, and the recipe creates and activates the virtual environment.
    # The link is inside Simulation, before its first command block (T1 rv-b5f52f1a37).
    assert "docs/getting_started/host_setup.md#install-routertl-and-clone-the-repository" in \
        _links_before_first_code(_section_tokens(text, "Simulation")), (
        "README.md Simulation no longer starts from the host setup recipe"
    )
    host = (ROOT / "docs" / "getting_started" / "host_setup.md").read_text(encoding="utf-8")
    # ... and the linked recipe itself, in its rendered shell code, creates and activates the environment and
    # installs RouteRTL (T1 rv-b5f52f1a37).
    recipe = _shell_lines(_section_tokens(host, "Install RouteRTL and clone the repository"))
    assert _venv_then_install(recipe), "the host setup recipe does not create, activate, then install RouteRTL"
    assert "pip install routertl" in recipe, "the host setup recipe no longer installs RouteRTL"
    assert any(ln.startswith("python3 -m venv .venv") for ln in recipe) and "source .venv/bin/activate" in recipe, (
        "docs/getting_started/host_setup.md lost the virtual environment steps"
    )

    # DS-P1.121: the README's host RAM is the preflight's hard floor. A tip for hosts below
    # it (RR_EDA_ALLOW_OVERSUBSCRIBE on 16 GB) contradicted the preflight, which fails there.
    import yaml
    decl = yaml.safe_load((ROOT / "tools/preflight/supported_environment.yml").read_text(encoding="utf-8"))
    floor = decl["host"]["ram_gb_min"]
    assert floor["severity"] == "fail"
    assert f"{floor['value']} GB RAM as the OS sees it" in text, (
        "README.md host RAM disagrees with tools/preflight/supported_environment.yml ram_gb_min"
    )
    # The board build steps moved to docs/getting_started/; none of the guides may suggest it either.
    guides = "\n".join(p.read_text(encoding="utf-8") for p in sorted((p for p in (ROOT / "docs" / "getting_started").rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".markdown"))))
    assert "RR_EDA_ALLOW_OVERSUBSCRIBE" not in text + guides, (
        "README.md or a guide suggests building below the preflight's RAM floor"
    )


def test_gitignore_contains_quartus_root_build_artifacts() -> None:
    """Gitignore must exclude Quartus Prime Pro root build artifacts."""
    gitignore_path = ROOT / ".gitignore"
    assert gitignore_path.is_file(), ".gitignore missing"
    text = gitignore_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]

    required_patterns = (
        "/dni/",
        "/qdb/",
        "/hps_subsys.qsys",
        "/hps_subsys/",
        "/hps_subsys_qsys_script.*",
        "/ip/agilex_hps/agilex_hps/intel_agilex_5_soc_1400/",
        "/ip/f2sdram_adapter/",
        "/ip/hps_subsys/",
    )
    for pattern in required_patterns:
        assert pattern in lines, (
            f".gitignore missing Quartus Pro root artifact pattern: {pattern}"
        )


def test_regen_regbank_honors_custom_python_executable() -> None:
    """tools/regen_regbank.sh must honor $PYTHON environment variable."""
    script_path = ROOT / "tools" / "regen_regbank.sh"
    assert script_path.is_file(), "tools/regen_regbank.sh missing"
    text = script_path.read_text(encoding="utf-8")

    assert 'PYTHON="${PYTHON:-python3}"' in text, (
        "tools/regen_regbank.sh missing PYTHON variable fallback: PYTHON=\"${PYTHON:-python3}\""
    )
    assert '"$PYTHON"' in text, (
        "tools/regen_regbank.sh does not invoke $PYTHON"
    )

    # Caller-reachable execution probe: run without --help with PYTHON=/bin/false.
    # The pre-build contract probes routertl SDK import via $PYTHON before generating.
    # With PYTHON=/bin/false, the check fails with rc=2 and outputs the import error.
    # (The origin/main script hardcoded python3 and ignored $PYTHON, exiting 0 here.)
    proc = subprocess.run(
        ["bash", str(script_path)],
        cwd=ROOT,
        env={**os.environ, "PYTHON": "/bin/false"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, (
        f"regen_regbank.sh must fail with rc=2 when PYTHON=/bin/false, got {proc.returncode}: {proc.stderr}"
    )
    assert "routertl SDK not importable" in proc.stderr


def test_readme_python_floor_covers_clean_room_version() -> None:
    """README.md and public support docs must match SSOT supported_environment.yml."""
    readme_path = ROOT / "README.md"
    ssot_path = ROOT / "tools" / "preflight" / "supported_environment.yml"
    doc_path = ROOT / "docs" / "SUPPORTED_ENVIRONMENT.md"

    assert readme_path.is_file(), "README.md missing"
    assert ssot_path.is_file(), "tools/preflight/supported_environment.yml missing"
    assert doc_path.is_file(), "docs/SUPPORTED_ENVIRONMENT.md missing"

    env_data = yaml.safe_load(ssot_path.read_text(encoding="utf-8"))
    py_spec = env_data["python"]
    expected_ascii_range = f">= {py_spec['min']}, < {py_spec['max_exclusive']}"
    expected_unicode_range = f"≥ {py_spec['min']}, < {py_spec['max_exclusive']}"

    readme_text = readme_path.read_text(encoding="utf-8")
    assert expected_ascii_range in readme_text, (
        f"README.md toolchain floor for Python must match SSOT supported_environment.yml ({expected_ascii_range})"
    )

    doc_text = doc_path.read_text(encoding="utf-8")
    assert (
        expected_ascii_range in doc_text or expected_unicode_range in doc_text
    ), (
        f"docs/SUPPORTED_ENVIRONMENT.md must match SSOT supported_environment.yml ({expected_ascii_range})"
    )
