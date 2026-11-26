# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""The README links its long instructions in docs/getting_started/; every such link must reach a page and heading.

The board guides, host setup, first boot, self-test, datalogger mix and stream client moved out of the README into
docs/getting_started/ (owner 2026-11-25). A renamed file or heading would leave the README pointing at nothing, and
GitHub shows a missing anchor as the top of the page, without an error. These checks resolve every link from the
README into that directory, and every link between its pages: the file must exist, and a #fragment must be the
GitHub anchor of one of the target's headings.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
GUIDES = ROOT / "docs" / "getting_started"
GUIDES_REL = "docs/getting_started/"
#: Every page the README must reach: one per guide in the directory.
EXPECTED_PAGES = {"README.md", "host_setup.md", "zybo_z7_20.md", "de25.md", "polarfire_soc_discovery.md",
                  "first_boot.md", "self_test.md", "datalogger_mix.md", "reading_the_stream.md"}


def _anchor(heading: str) -> str:
    """The GitHub anchor of a heading: lower case, punctuation other than '-' removed, spaces to '-'."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _headings(text: str) -> set[str]:
    tokens = MarkdownIt("commonmark").parse(text)
    return {_anchor(tokens[i + 1].content) for i, tok in enumerate(tokens) if tok.type == "heading_open"}


def _links(text: str) -> list[str]:
    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    out = []
    for tok in tokens:
        for child in tok.children or []:
            if child.type == "link_open":
                out.append(child.attrs["href"])
    return out


def _problems(doc_rel: str, text: str, files: dict[str, str]) -> list[str]:
    """Links from ``doc_rel`` into docs/getting_started/ that miss their page or heading. ``files`` maps each
    repository path that exists to its text."""
    bad = []
    base = PurePosixPath(doc_rel).parent
    for href in _links(text):
        if re.match(r"[a-z]+:", href):
            continue
        path, _, frag = href.partition("#")
        target = doc_rel if not path else str(PurePosixPath(*(base / path).parts))
        parts = []
        for part in PurePosixPath(target).parts:
            if part == "..":
                parts = parts[:-1]
            elif part != ".":
                parts.append(part)
        target = "/".join(parts)
        if not target.startswith(GUIDES_REL) and not (doc_rel.startswith(GUIDES_REL) and target == doc_rel):
            continue
        if target not in files:
            bad.append(f"{doc_rel}: {href} -> no file {target}")
        elif frag and frag not in _headings(files[target]):
            bad.append(f"{doc_rel}: {href} -> no heading #{frag} in {target}")
    return bad


def _guide_files() -> list[Path]:
    """Every Markdown page under docs/getting_started/, in subdirectories too, any extension case."""
    return sorted(p for p in GUIDES.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".markdown"))


def _tree() -> dict[str, str]:
    files = {f"{GUIDES_REL}{p.relative_to(GUIDES).as_posix()}": p.read_text(encoding="utf-8") for p in _guide_files()}
    files["README.md"] = README.read_text(encoding="utf-8")
    return files


def test_the_readme_links_every_guide():
    """Positive control: the check below has links to judge, and no guide is unreachable from the README."""
    reached = {PurePosixPath(h.partition("#")[0]).name for h in _links(README.read_text(encoding="utf-8"))
               if h.startswith(GUIDES_REL)}
    assert reached == EXPECTED_PAGES
    assert {p.relative_to(GUIDES).as_posix() for p in _guide_files()} == EXPECTED_PAGES


def test_every_readme_link_into_the_guides_resolves():
    files = _tree()
    assert _problems("README.md", files["README.md"], files) == []


@pytest.mark.parametrize("name", sorted(EXPECTED_PAGES))
def test_every_link_between_the_guides_resolves(name):
    files = _tree()
    assert _problems(f"{GUIDES_REL}{name}", files[f"{GUIDES_REL}{name}"], files) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda t: t.replace("](docs/getting_started/de25.md)", "](docs/getting_started/de-25.md)", 1), "no file"),
    (lambda t: t.replace("host_setup.md#install-routertl-and-clone-the-repository",
                         "host_setup.md#install-routertl", 1), "no heading"),
])
def test_a_broken_readme_link_is_reported(mutate, expect):
    files = _tree()
    bad = mutate(files["README.md"])
    assert bad != files["README.md"]
    assert any(expect in p for p in _problems("README.md", bad, files))


def _leaves_docs(doc_rel: str, href: str) -> bool:
    """True when a relative link from ``doc_rel`` resolves outside docs/, which MkDocs (strict) refuses."""
    if re.match(r"[a-z]+:", href) or href.startswith("#"):
        return False
    parts: list[str] = []
    for part in (PurePosixPath(doc_rel).parent / href.partition("#")[0]).parts:
        if part == "..":
            parts = parts[:-1]
        elif part != ".":
            parts.append(part)
    return not "/".join(parts).startswith("docs/")


def test_no_guide_links_outside_docs_with_a_relative_path():
    """The site is built from docs/ with `mkdocs build --strict`, which aborts on a relative link that leaves docs/;
    a guide links a repository file outside docs/ by its GitHub URL, as the other pages under docs/ do."""
    files = _tree()
    bad = [f"{rel}: {href}" for rel, text in files.items() if rel.startswith(GUIDES_REL)
           for href in _links(text) if _leaves_docs(rel, href)]
    assert bad == []


def test_a_relative_link_out_of_docs_is_reported():
    """Negative control for the test above."""
    assert _leaves_docs(f"{GUIDES_REL}self_test.md", "../../hw/rtl/emio/ds_emio_mux.vhd")
    assert _leaves_docs(f"{GUIDES_REL}host_setup.md", "../../README.md")
    assert not _leaves_docs(f"{GUIDES_REL}self_test.md", "../images/self_test.svg")
    assert not _leaves_docs(f"{GUIDES_REL}de25.md", "https://github.com/routertl/deepskopion/blob/main/README.md")


GH_BLOB = "https://github.com/routertl/deepskopion/blob/main/"


def _repo_problems(doc_rel: str, text: str, read=lambda rel: (ROOT / rel).read_text(encoding="utf-8")) -> list[str]:
    """Links from ``doc_rel`` to any file of this repository, written relative or as this repository's GitHub URL,
    that miss their file or heading (T5 rv-cff2f0dbd7: links out of docs/getting_started/ were not checked)."""
    bad = []
    base = PurePosixPath(doc_rel).parent
    for href in _links(text):
        if href.startswith(GH_BLOB):
            rel = href[len(GH_BLOB):]
        elif re.match(r"[a-z]+:", href) or href.startswith("#"):
            continue
        else:
            rel = str(base / href)
        path, _, frag = rel.partition("#")
        parts: list[str] = []
        for part in PurePosixPath(path).parts:
            if part == "..":
                parts = parts[:-1]
            elif part != ".":
                parts.append(part)
        target = "/".join(parts)
        if not (ROOT / target).exists():
            bad.append(f"{doc_rel}: {href} -> no file {target}")
        elif frag and target.lower().endswith(".md") and frag not in _headings(read(target)):
            bad.append(f"{doc_rel}: {href} -> no heading #{frag} in {target}")
    return bad


@pytest.mark.parametrize("name", sorted(EXPECTED_PAGES))
def test_every_link_from_a_guide_into_the_repository_resolves(name):
    rel = f"{GUIDES_REL}{name}"
    assert _repo_problems(rel, (ROOT / rel).read_text(encoding="utf-8")) == []


def test_every_readme_link_into_the_repository_resolves():
    assert _repo_problems("README.md", README.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("doc,old,new,expect", [
    ("reading_the_stream.md", "README.md#captures", "README.md#captures-moved", "no heading"),
    ("first_boot.md", "../SUPPORTED_ENVIRONMENT.md", "../SUPPORTED_ENVIRONMENTS.md", "no file"),
])
def test_a_broken_link_out_of_the_guides_is_reported(doc, old, new, expect):
    """Negative controls (T5 rv-cff2f0dbd7): a GitHub-URL anchor and a relative doc link, each broken."""
    rel = f"{GUIDES_REL}{doc}"
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert old in text, f"{doc} no longer links {old}"
    assert any(expect in p for p in _repo_problems(rel, text.replace(old, new, 1)))
