# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
"""DS-P2.365: DeepSkopion declares Python 3.11 or newer, and the README's Ubuntu 22.04 path pins such an interpreter.

RouteRTL drops Python 3.10 in a coming minor release and prints a deprecation line on every rr call under it. On
Ubuntu 22.04, `python3 -m venv .venv` gives 3.10, so the README replaces that line there with a uv-managed Python 3.11
(no sudo, no third-party apt repository). These checks read the committed declaration and README.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[3]
DECL = ROOT / "tools" / "preflight" / "supported_environment.yml"
README = ROOT / "README.md"
# The Ubuntu 22.04 commands moved from the README to the host setup guide it links.
HOST_SETUP = ROOT / "docs" / "getting_started" / "host_setup.md"
FLOOR = (3, 11)
SECTION = "## Python 3.11 on Ubuntu 22.04"
# What `python3` is on each declared Ubuntu release (the distribution's default interpreter).
DEFAULT_PYTHON3 = {"22.04": (3, 10), "24.04": (3, 12)}


def _ver(text: str) -> tuple[int, int]:
    major, minor = text.split(".")[:2]
    return int(major), int(minor)


#: Every rendered bash block of the last _section_block call, at any nesting: each must meet the floor.
_ALL_22_04_BLOCKS: list[str] = []


def _section_block(text: str) -> str:
    """The first rendered bash fence under the rendered SECTION heading, before the next heading of its level or
    higher, as CommonMark parses it: a recipe hidden in an HTML comment is not there (T1 rv-66f326750f)."""
    tokens = MarkdownIt("commonmark").parse(text)
    title, level = SECTION.lstrip("# "), len(SECTION) - len(SECTION.lstrip("#"))
    # The heading must be the rank SECTION names; the cutoff below uses the same rank (T1 rv-bd5b14ed4e).
    at = next((i for i, t in enumerate(tokens) if t.type == "heading_open" and t.level == 0
               and t.tag == f"h{level}" and tokens[i + 1].content == title), None)
    assert at is not None, f"no rendered heading {SECTION!r}"
    found, _ALL_22_04_BLOCKS[:] = [], []
    for tok in tokens[at + 3:]:
        if tok.type == "heading_open" and tok.level == 0 and int(tok.tag[1]) <= level:
            break
        # Raw HTML other than a comment is not read as a recipe, so it is refused in the section (T1 rv-59f510b8fd).
        assert not (tok.type in ("html_block", "html_inline") and not re.fullmatch(
            r"\s*(?:<!--(?:(?!-->)[\s\S])*-->\s*)+", tok.content)), f"raw HTML under {SECTION!r}: {tok.content[:60]!r}"
        if tok.type == "inline":
            for child in tok.children or []:
                assert not (child.type == "html_inline" and not child.content.startswith("<!--")), \
                    f"raw HTML under {SECTION!r}: {child.content[:60]!r}"
        if tok.type in ("code_block", "fence"):
            # Every rendered listing, whatever its fence label (sh, none, ...), indented or quoted
            # (T1 rv-f6b9a3df36, rv-a58ad4bdbb, rv-70ae358262).
            _ALL_22_04_BLOCKS.append(tok.content)
        if tok.type == "fence" and (tok.info.split() or [""])[0] == "bash" and tok.level == 0:
            found.append(tok.content)
    # Exactly one top-level recipe: a quoted or second copy cannot stand in for it (T1 rv-e17d84f5c3).
    if not found:
        raise AssertionError(f"no bash block under {SECTION!r}")
    assert len(found) == 1, f"{len(found)} top-level bash blocks under {SECTION!r}; the recipe must be one block"
    return found[0]


def test_the_declared_minimum_python_is_at_least_3_11():
    decl = yaml.safe_load(DECL.read_text(encoding="utf-8"))
    assert _ver(str(decl["python"]["min"])) >= FLOOR


def test_the_readme_requirements_row_states_the_declared_minimum():
    decl = yaml.safe_load(DECL.read_text(encoding="utf-8"))
    row = f"| Python | `>= {decl['python']['min']}, < {decl['python']['max_exclusive']}` |"
    assert row in README.read_text(encoding="utf-8")


def test_the_22_04_commands_install_and_pin_python_3_11_or_newer_without_sudo():
    _check_22_04(HOST_SETUP.read_text(encoding="utf-8"))


def test_a_conflicting_quoted_recipe_is_refused():
    """Negative control (T1 rv-f6b9a3df36): a block-quoted recipe pinned to 3.10 before the valid 3.11 recipe."""
    text = HOST_SETUP.read_text(encoding="utf-8")
    block = _section_block(text)
    quoted = "".join(f"> {ln}\n" for ln in f"```bash\n{block.replace('3.11', '3.10')}```".splitlines())
    bad = text.replace(f"```bash\n{block}```", quoted + "\n" + f"```bash\n{block}```", 1)
    assert bad != text
    with pytest.raises(AssertionError):
        _check_22_04(bad)


def test_a_conflicting_indented_recipe_is_refused():
    """Negative control (T1 rv-a58ad4bdbb): an indented listing pinned to 3.10 before the valid recipe."""
    text = HOST_SETUP.read_text(encoding="utf-8")
    block = _section_block(text)
    indented = "".join(f"    {ln}\n" for ln in f"```bash\n{block.replace('3.11', '3.10')}```".splitlines())
    bad = text.replace(f"```bash\n{block}```", indented + "\n" + f"```bash\n{block}```", 1)
    assert bad != text
    with pytest.raises(AssertionError):
        _check_22_04(bad)


@pytest.mark.parametrize("fence", ["```sh", "```"])
def test_a_conflicting_recipe_under_another_fence_label_is_refused(fence):
    """Negative control (T1 rv-70ae358262): a 3.10 copy in an sh or unlabelled fence above the valid recipe."""
    text = HOST_SETUP.read_text(encoding="utf-8")
    block = _section_block(text)
    other = f"{fence}\n{block.replace('3.11', '3.10')}```\n"
    bad = text.replace(f"```bash\n{block}```", other + "\n" + f"```bash\n{block}```", 1)
    assert bad != text
    with pytest.raises(AssertionError):
        _check_22_04(bad)


def test_a_recipe_in_raw_html_is_refused():
    """Negative control (T1 rv-59f510b8fd): a 3.10 recipe in a <pre> block before the valid recipe."""
    text = HOST_SETUP.read_text(encoding="utf-8")
    block = _section_block(text)
    pre = f"<pre>\n<!-- ```bash -->\n{block.replace('3.11', '3.10')}<!-- ``` -->\n</pre>\n"
    bad = text.replace(f"```bash\n{block}```", pre + "\n" + f"```bash\n{block}```", 1)
    assert bad != text
    with pytest.raises(AssertionError, match="raw HTML"):
        _check_22_04(bad)


def _check_22_04(text: str) -> None:
    _section_block(text)
    # The top-level recipe and any other rendered recipe in the section (a quoted copy a reader can also copy) must
    # each meet the floor: a conflicting recipe is refused (T1 rv-f6b9a3df36).
    for block in list(_ALL_22_04_BLOCKS):
        installs = re.findall(r"uv python install (\d+\.\d+)", block)
        venvs = re.findall(r"uv venv\b[^\n]*--python (\d+\.\d+)[^\n]*\.venv", block)
        assert installs and all(_ver(v) >= FLOOR for v in installs), block
        assert venvs and all(_ver(v) >= FLOOR for v in venvs), block
        assert "--seed" in block, "without --seed the venv has no pip and `pip install` reaches the system Python"
        assert "sudo" not in block and "add-apt-repository" not in block


@pytest.mark.parametrize("release", sorted(DEFAULT_PYTHON3))
def test_every_default_python3_venv_line_names_the_22_04_replacement_where_python3_is_too_old(release):
    # The README and every guide it links (T1 rv-045fb119b4: a board guide's venv line counts as much as the README's).
    guides = sorted((p for p in (ROOT / "docs" / "getting_started").rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".markdown")))
    assert HOST_SETUP in guides
    text = "\n".join(p.read_text(encoding="utf-8") for p in [README, *guides])
    lines = [ln for ln in text.splitlines() if re.match(r"\s*python3 -m venv \.venv\b", ln)]
    # ... and the same lines as each file renders them (block-quoted or indented code included), each file parsed
    # on its own, so a rendered venv line is held to the 22.04 rule too (T1 rv-6661ed93e7).
    lines += [ln for p in [README, *guides] for tok in MarkdownIt("commonmark").parse(p.read_text(encoding="utf-8"))
              if tok.type in ("fence", "code_block") for ln in tok.content.splitlines()
              if re.match(r"\s*python3 -m venv \.venv\b", ln)]
    assert lines, "the README has no `python3 -m venv .venv` step"
    # Each recipe creates its own environment: the Simulation quick start and the host setup (T1 rv-8977f8b564).
    # Owner 2026-11-25: the README's Simulation starts from the host setup recipe, which creates the environment.
    assert _has_venv_step(HOST_SETUP.read_text(encoding="utf-8")), "host_setup.md has no `python3 -m venv .venv` step"
    # The link is in Simulation, before its first command block (T1 rv-b5f52f1a37).
    tokens = MarkdownIt("commonmark").parse(README.read_text(encoding="utf-8"))
    at = next(i for i, t in enumerate(tokens) if t.type == "heading_open" and tokens[i + 1].content == "Simulation")
    before = []
    for tok in tokens[at + 3:]:
        if tok.type in ("fence", "code_block", "heading_open"):
            break
        before += [c.attrGet("href") for c in tok.children or [] if c.type == "link_open"]
    assert "docs/getting_started/host_setup.md#install-routertl-and-clone-the-repository" in before, \
        "README.md no longer starts its Simulation from the host setup recipe"
    if DEFAULT_PYTHON3[release] < FLOOR:
        for ln in lines:
            assert f"Ubuntu {release}" in ln and SECTION.lstrip("# ") in ln, ln


def _has_venv_step(markdown: str) -> bool:
    """The venv command in a rendered code block (fence or indented code), not in a comment (T1 rv-85f9bcd6cf)."""
    return any(re.match(r"\s*python3 -m venv \.venv\b", line)
               for tok in MarkdownIt("commonmark").parse(markdown) if tok.type in ("fence", "code_block")
               for line in tok.content.splitlines())


def test_a_hidden_22_04_recipe_is_not_found():
    """Negative control (T1 rv-66f326750f): the uv recipe inside an HTML comment is not the recipe under the heading."""
    text = HOST_SETUP.read_text(encoding="utf-8")
    block = _section_block(text)
    hidden = text.replace(f"```bash\n{block}```", f"<!--\n```bash\n{block}```\n-->", 1)
    assert hidden != text
    with pytest.raises(AssertionError, match="no bash block"):
        _section_block(hidden)


def test_the_22_04_heading_must_keep_its_rank():
    """Negative control (T1 rv-bd5b14ed4e): demoted to h3 with the recipe under a sibling h3, it is not found."""
    text = HOST_SETUP.read_text(encoding="utf-8")
    block = _section_block(text)
    moved = text.replace(SECTION + "\n", "#" + SECTION + "\n", 1)
    moved = moved.replace(f"```bash\n{block}```", f"### Other setup\n\n```bash\n{block}```", 1)
    assert moved != text
    with pytest.raises(AssertionError, match="no rendered heading"):
        _section_block(moved)


def test_a_quoted_copy_cannot_stand_in_for_the_recipe():
    """Negative control (T1 rv-e17d84f5c3): a block-quoted copy pinned to 3.11 before a recipe pinned to 3.10."""
    text = HOST_SETUP.read_text(encoding="utf-8")
    block = _section_block(text)
    quoted = "".join(f"> {ln}\n" for ln in f"~~~bash\n{block}~~~".splitlines())
    moved = text.replace(f"```bash\n{block}```", quoted + "\n" + f"```bash\n{block.replace('3.11', '3.10')}```", 1)
    assert moved != text
    got = _section_block(moved)
    assert "3.10" in got and "3.11" not in got   # the top-level recipe is the one judged, and it now fails the floor


def test_a_commented_out_venv_step_does_not_count():
    """Negative control: the step moved into an HTML comment is not a step a reader runs."""
    assert _has_venv_step("```bash\npython3 -m venv .venv\n```\n")
    assert not _has_venv_step("<!--\npython3 -m venv .venv\n-->\n\n```bash\nsource .venv/bin/activate\n```\n")


def test_every_declared_ubuntu_release_has_a_known_default_python3():
    os_decl = yaml.safe_load(DECL.read_text(encoding="utf-8"))["host"]["os"]
    declared = [str(os_decl["version"])] + [str(v["version"]) for v in os_decl.get("also_supported") or []]
    assert set(declared) <= set(DEFAULT_PYTHON3), declared
