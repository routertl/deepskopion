# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P1.119: the README builds and runs the DE25 from a fresh clone.

The README's DE25 entry once held only `rr target set` and `rr pkg install`, so the
image build stopped without a bitstream and without the deployment descriptor it
reads. These tests hold the section to the commands the image tool needs, in order,
its paths to the ones the tool computes, and each command block to stopping at its
first failure.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
# The DE25 section moved from the README to its guide (docs/getting_started/), where it is the page heading;
# First boot moved to its own guide.
DE25_GUIDE = ROOT / "docs" / "getting_started" / "de25.md"
FIRST_BOOT = ROOT / "docs" / "getting_started" / "first_boot.md"
HEADING = "# Build and run on the Terasic DE25"
HEADING_TAG = f"h{len(HEADING) - len(HEADING.lstrip('#'))}"
HPS_SOF = "sw/linux/de25/out/de25_datalogger/de25_datalogger_hps.sof"

# The non-comment lines of each command block, in order.
EXPECTED = (
    (
        "( set -e",
        "rr target set de25_datalogger",
        "rr pkg install",
        "pip install -r requirements.txt",
        "pip install -r tools/de25_uboot_host_requirements.txt",
        "python3 tools/preflight/ds_preflight.py --stage build",
        "rr synth run",
        "rr implementation run",
        "rr bitstream run )",
    ),
    ("[ -e runtimes/hw.local.yml ] || cp runtimes/hw.example.yml runtimes/hw.local.yml",),
    ("python3 tools/de25_linux_image.py build",),
    (
        "( set -e",
        "CARD=/dev/sdX",
        'rr linux flash-sd --image --device "$CARD" )',
    ),
    (f"rr lease run de25-standard -- rr program run -b {HPS_SOF}",),
    (
        "( set -e",
        "BOARD_IP=board-ip",
        "RW=~/.routertl/bin/routewave/routewave",  # DS-P3.370: RouteWave as rr ws update-viewer installs it
        'python3 tools/wait_for_port.py "$BOARD_IP" 7475 --timeout 180',
        '"$RW" -c "stream attach $BOARD_IP:7475" )',
    ),
)
# Directions given as prose between the blocks, which a command list cannot hold.
OPERATOR_STEPS = (
    "A block runs only after the previous one\nreturned 0.",
    "in `runtimes/hw.local.yml` before the\nnext block.",
    "Put the card in the board and power it on. The board starts Linux from the card only after step 9, at every\npower-on.",
    "Step 9 runs once per power-on: before it is repeated, the board is powered off and on again.",
    "Each block runs on its own, pasted alone and not joined to another with `&&` or `;`.",
)


def _image_tool():
    spec = importlib.util.spec_from_file_location("de25_linux_image_p1_119", ROOT / "tools" / "de25_linux_image.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _readme() -> str:
    return DE25_GUIDE.read_text(encoding="utf-8")


def _section_end(text: str) -> int:
    """Offset of the next rendered heading of HEADING's level or higher after HEADING (a `# ` comment line inside a
    fence is not a heading), or the end of the text."""
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    after = text.index(HEADING) + len(HEADING)
    for tok in MarkdownIt("commonmark").parse(text):
        if (tok.type == "heading_open" and tok.level == 0 and int(tok.tag[1]) <= int(HEADING_TAG[1])
                and starts[tok.map[0]] >= after):
            return starts[tok.map[0]] - 1
    return len(text)


_COMMENT = r"<!--(?:(?!-->)[\s\S])*-->"  # one comment: it cannot run past its own -->


def _visible_inline(tok) -> str:
    """An inline token's text as a reader sees it, from its parsed children: text, code
    spans (kept in backticks) and line breaks. Link targets and titles are not shown and
    are dropped; an image shows its alt text. HTML comments are dropped, any other inline
    HTML is refused."""
    out = []
    for child in tok.children or []:
        if child.type == "text":
            out.append(child.content)
        elif child.type == "code_inline":
            out.append(f"`{child.content}`")
        elif child.type in ("softbreak", "hardbreak"):
            out.append("\n")
        elif child.type == "image":
            out.append("".join(c.content for c in child.children or []))
        elif child.type == "html_inline":
            if not re.fullmatch(_COMMENT, child.content):
                raise AssertionError(f"the DE25 section has visible HTML the checks do not read: {child.content[:60]!r}")
    return "".join(out)


class _Section(str):
    """The section text, carrying each bash fence's (start, end, body) as the README parse
    gave it, so no check re-parses the reconstructed text."""
    fences: list[tuple[int, int, str]]


def _section(text: str) -> _Section:
    """The DE25 section as a reader sees it (DS-P3.319): parsed as CommonMark, from its
    rendered top-level heading to the next top-level h1-h3, keeping the bash fences (bodies
    exactly as parsed) and the visible inline text. HTML comments are not shown to a reader
    and are dropped. Anything else a reader sees that the checks would not read (another
    fence, an indented code block, visible HTML) fails, so the exact command list cannot
    be bypassed by moving a command into it. A section that is not a rendered heading fails."""
    tokens = MarkdownIt("commonmark").parse(text)
    title = HEADING.lstrip("# ")
    start = next((i for i, tok in enumerate(tokens) if tok.type == "heading_open" and tok.tag == HEADING_TAG
                  and tok.level == 0 and tokens[i + 1].content == title), None)
    if start is None:
        raise AssertionError(f"the DE25 guide has no rendered heading {HEADING!r}")
    # The title opens the page: anything a reader sees above it would sit outside the checks (T1 rv-4b55c5529d).
    before = [tok for tok in tokens[:start]
              if not (tok.type == "html_block" and re.fullmatch(rf"\s*(?:{_COMMENT}\s*)+", tok.content))]
    if before:
        raise AssertionError(f"the DE25 guide has visible content before {HEADING!r} that the checks do not read")
    out, fences, pos = [HEADING + "\n\n"], [], len(HEADING) + 2
    for tok in tokens[start + 3:]:
        # The section is the whole guide page: subsections stay in scope, and a second heading of the page's own
        # level is refused rather than read as the end of the section (T1 rv-b0295d1941).
        if tok.type == "heading_open" and int(tok.tag[1]) <= int(HEADING_TAG[1]) and tok.level == 0:
            raise AssertionError(f"the DE25 guide has a second top-level heading after {HEADING!r}; "
                                 "the checks do not read past it")
        if tok.type == "fence" and (tok.info.split() or [""])[0] == "bash":
            piece = f"```bash\n{tok.content}```\n\n"
            fences.append((pos, pos + len(piece), tok.content))
        elif tok.type == "inline":
            piece = _visible_inline(tok) + "\n\n"
        elif tok.type in ("fence", "code_block"):
            raise AssertionError(f"the DE25 section has a {tok.type} ({tok.info!r}) the checks do not read")
        elif tok.type == "html_block" and not re.fullmatch(rf"\s*(?:{_COMMENT}\s*)+", tok.content):
            raise AssertionError(f"the DE25 section has visible HTML the checks do not read: {tok.content[:60]!r}")
        else:
            continue
        out.append(piece)
        pos += len(piece)
    section = _Section("".join(out))
    section.fences = fences
    return section


def _fences(section: _Section) -> list[tuple[int, int, str]]:
    """(start offset, end offset, body) of each bash fence of a `_section`, from the README
    parse. A plain string is refused: re-parsing reconstructed text can misread it."""
    if not isinstance(section, _Section):
        raise TypeError("_fences needs the result of _section()")
    return section.fences


def _blocks(section: _Section) -> list[str]:
    return [body for _, _, body in _fences(section)]


def _commands(section: str) -> list[list[str]]:
    return [[ln.strip() for ln in b.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
            for b in _blocks(section)]


def _assert_commands_in_order(cmds: list[list[str]]) -> None:
    assert cmds == [list(b) for b in EXPECTED], f"DE25 commands differ from the expected blocks: {cmds}"


@pytest.mark.parametrize("wrap", [("<!--\n", "\n-->\n"), ("~~~text\n", "\n~~~\n")])
def test_a_hidden_section_fails(wrap):
    """DS-P3.319: the whole DE25 section inside an HTML comment or a code fence is not a
    section a reader sees, so every check built on `_section` fails."""
    text = _readme()
    start = text.index(HEADING)
    end = _section_end(text)
    hidden = text[:start] + wrap[0] + text[start:end] + wrap[1] + text[end:]
    assert hidden != text
    with pytest.raises(AssertionError, match="no rendered heading"):
        _section(hidden)


@pytest.mark.parametrize("construct", [
    "```sh\nrr program run\n```\n", "~~~console\nrr program run\n~~~\n", "```\nrr program run\n```\n",
    "    rr program run\n", "<pre>\nrr program run\n</pre>\n",
])
def test_a_command_in_a_construct_the_checks_do_not_read_fails(construct):
    """DS-P3.319: the exact command list is an absence check for every other command, so
    a command a reader can see and paste may not sit where the checks do not read it."""
    text = _readme()
    anchor = text.index("```bash", text.index(HEADING))
    moved = text[:anchor] + construct + "\n" + text[anchor:]
    assert moved != text
    with pytest.raises(AssertionError, match="the checks do not read"):
        _section(moved)


def test_a_command_in_an_html_comment_is_not_shown_and_is_dropped():
    text = _readme()
    anchor = text.index("```bash", text.index(HEADING))
    moved = text[:anchor] + "<!--\nrr program run\n-->\n\n" + text[anchor:]
    assert moved != text
    assert _commands(_section(moved)) == [list(b) for b in EXPECTED]


def _next_heading(text: str) -> int:
    return min(_section_end(text) + 1, len(text))


def test_a_nested_heading_does_not_end_the_section():
    """A blockquoted heading of the section's own level (h1 on the guide page) is not a section boundary: a bash
    fence after it is still read."""
    text = _readme()
    end = _next_heading(text)
    moved = text[:end] + "> # Aside\n\n```bash\nrr program run\n```\n\n" + text[end:]
    assert moved != text
    with pytest.raises(AssertionError, match="commands differ"):
        _assert_commands_in_order(_commands(_section(moved)))


@pytest.mark.parametrize("sub", ["## Writing the card again", "### Writing the card again"])
def test_a_command_under_a_subsection_is_still_judged(sub):
    """The section is the whole page under its h1: a seventh block under a subsection heading is one of its
    commands, so the exact command list refuses it (T1 rv-752b3828fb)."""
    text = _readme()
    end = _section_end(text)
    moved = text[:end] + f"\n\n{sub}\n\n```bash\nrr linux flash-sd --image --device /dev/sdX\n```\n" + text[end:]
    assert moved != text
    with pytest.raises(AssertionError, match="commands differ"):
        _assert_commands_in_order(_commands(_section(moved)))


def test_content_before_the_title_is_refused():
    """A seventh block placed above the guide's title would sit outside the checked section (T1 rv-4b55c5529d)."""
    text = _readme()
    moved = "# Before\n\n```bash\nrr linux flash-sd --image --device /dev/sdX\n```\n\n" + text
    with pytest.raises(AssertionError, match="visible content before"):
        _section(moved)


def test_a_second_top_level_heading_is_refused():
    """A seventh block under a second h1 on the guide page would sit outside the checked section (T1 rv-b0295d1941)."""
    text = _readme()
    moved = text + "\n# Writing the card again\n\n```bash\nrr linux flash-sd --image --device /dev/sdX\n```\n"
    with pytest.raises(AssertionError, match="second top-level heading"):
        _section(moved)


def test_a_fence_body_is_read_whole_past_a_backtick_run_inside_it():
    """Three backticks in a shell comment do not end a fence, so the command after them is
    still a command of the block."""
    text = _readme()
    first = text.index("```bash\n", text.index(HEADING))
    close = text.index("\n```\n", first + 8) + 1
    moved = text[:close] + "# ``` not a fence\nrr program run\n" + text[close:]
    assert moved != text
    with pytest.raises(AssertionError, match="commands differ"):
        _assert_commands_in_order(_commands(_section(moved)))


def test_a_longer_fence_keeps_a_backtick_line_inside_its_body():
    """A ````bash fence may hold a line of three backticks; the section keeps the body whole,
    so the command after that line is still a command of the block. The last block of the
    section is used: there a body cut short at that line would read as the expected block."""
    text = _readme()
    first = text.rindex("```bash\n", 0, _next_heading(text))
    close = text.index("\n```\n", first + 8) + 1
    body = text[first + len("```bash\n"):close]
    moved = (text[:first] + "````bash\n" + body + "```\nrr program run\n````\n"
             + text[close + len("```\n"):])
    assert moved != text
    with pytest.raises(AssertionError, match="commands differ"):
        _assert_commands_in_order(_commands(_section(moved)))


def test_a_heading_that_looks_like_a_fence_does_not_hide_a_block():
    """A heading whose text starts with backticks is prose; the bash fence after it is
    still one of the section's blocks (bodies come from the README parse, not a re-parse)."""
    text = _readme()
    anchor = text.index("```bash", text.index(HEADING))
    moved = text[:anchor] + "#### ```text is the Markdown fence marker\n\n```bash\nrr program run\n```\n\n" + text[anchor:]
    assert moved != text
    assert len(_blocks(_section(moved))) == len(EXPECTED) + 1
    with pytest.raises(AssertionError, match="commands differ"):
        _assert_commands_in_order(_commands(_section(moved)))


def test_link_metadata_is_not_visible_text():
    """A link's title and target are not shown to a reader, so they cannot satisfy a
    prose check."""
    text = _readme()
    phrase = "The DE25 image has no SSH server"
    moved = text.replace(phrase, f'[The image has one](#ssh "{phrase}")', 1)
    assert moved != text
    assert phrase not in _section(moved)
    assert "The image has one" in _section(moved)


def test_an_inline_comment_hides_text_from_the_section():
    text = _readme()
    phrase = "The DE25 image has no SSH server"
    hidden = text.replace(phrase, f"<!-- {phrase} -->", 1)
    assert hidden != text
    assert phrase in _section(text)
    assert phrase not in _section(hidden)


@pytest.mark.parametrize("html", ["<b>rr program run</b>", "<!-- a --> <b>rr program run</b> <!-- c -->"])
def test_visible_html_in_the_section_fails(html):
    text = _readme()
    anchor = text.index("```bash", text.index(HEADING))
    for moved in (text[:anchor] + html + "\n\n" + text[anchor:],
                  text.replace("The DE25 image has no SSH server", "The DE25 image " + html + " has no SSH server", 1)):
        assert moved != text
        with pytest.raises(AssertionError, match="the checks do not read"):
            _section(moved)


def test_a_block_hidden_in_a_comment_is_not_a_command_block():
    text = _readme()
    first = text.index("```bash", text.index(HEADING))
    hidden = text[:first] + "<!--\n" + text[first:].replace("```\n", "```\n-->\n", 1)
    assert hidden != text
    assert len(_blocks(_section(hidden))) == len(EXPECTED) - 1


def test_the_section_gives_every_command_in_order():
    _assert_commands_in_order(_commands(_section(_readme())))


def test_the_order_check_refuses_a_missing_moved_or_changed_command():
    fpga, descriptor, image, card, program, attach = _commands(_section(_readme()))
    bad_lists = (
        [[c for c in fpga if c != "rr implementation run"], descriptor, image, card, program, attach],
        [fpga, image, descriptor, card, program, attach],
        [fpga, descriptor, [image[0] + " --help"], card, program, attach],
        [fpga, descriptor + image, card, program, attach],
        [fpga, descriptor, image, card, program + attach],
    )
    for bad in bad_lists:
        with pytest.raises(AssertionError):
            _assert_commands_in_order(bad)


def test_the_programmed_file_is_the_one_the_image_tool_writes(monkeypatch):
    tool = _image_tool()
    monkeypatch.setattr(tool, "active_target_stem", lambda root: "de25_datalogger")
    expected = (tool.linux_out_dir(ROOT) / tool.active_sof_hps_artifact(ROOT)).relative_to(ROOT).as_posix()
    assert expected == HPS_SOF
    # The _hps.sof is a JTAG object; the card carries only the FAT set.
    assert tool.active_sof_hps_artifact(ROOT) not in tool.SDCARD_CONTENTS
    # The lease token is the target's board.
    target = yaml.safe_load((ROOT / "targets" / "de25_datalogger.yml").read_text(encoding="utf-8"))
    assert target["hardware"]["platform"] == "de25-standard"


def test_the_template_needs_the_edit_the_readme_asks_for(monkeypatch, tmp_path):
    """The image tool, reading runtimes/hw.local.yml by default (RR_BENCH_CONFIG
    unset), refuses the unedited template; with the documented key
    bench.resources.de25_address given an addr and its prefix line deleted, as
    the README says, it bakes ds_ip=<addr>/24."""
    tool = _image_tool()
    monkeypatch.delenv("RR_BENCH_CONFIG", raising=False)
    root = tmp_path / "clone"
    (root / "targets").mkdir(parents=True)
    (root / "runtimes").mkdir()
    shutil.copy(ROOT / "targets" / "de25_datalogger.yml", root / "targets" / "de25_datalogger.yml")
    template = (ROOT / "runtimes" / "hw.example.yml").read_text(encoding="utf-8")
    local = root / tool.DEPLOYMENT_HW_REL
    assert tool.DEPLOYMENT_HW_REL == "runtimes/hw.local.yml"
    with pytest.raises(RuntimeError, match="deployment descriptor missing"):
        tool.deployment_bootargs(root, target_rel="targets/de25_datalogger.yml")
    local.write_text(template, encoding="utf-8")
    with pytest.raises(RuntimeError):
        tool.deployment_bootargs(root, target_rel="targets/de25_datalogger.yml")
    hw = yaml.safe_load(template)
    resource = hw["bench"]["resources"]["de25_address"]  # the key the README names
    resource["addr"] = "192.0.2.10"
    del resource["prefix"]
    local.write_text(yaml.safe_dump(hw), encoding="utf-8")
    bootargs = tool.deployment_bootargs(root, target_rel="targets/de25_datalogger.yml")
    assert [tok for tok in bootargs.split() if tok.startswith("ds_ip=")] == ["ds_ip=192.0.2.10/24"], bootargs
    assert "bench.resources.de25_address" in _section(_readme())


#: Marks a rendered ordered-list number in _visible_text. Markdown text cannot carry it, so prose that spells
#: "1. Address." cannot stand in for the list's first item (T1 rv-47763b7493).
LIST_MARK = "\x00"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _alt_text(image) -> str:
    """An image's alt text as rendered: every text and code descendant, nested images included, breaks as spaces."""
    parts = []
    for child in image.children or []:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        elif child.type == "image":
            parts.append(_alt_text(child))
    return "".join(parts)


def _reader_view(markdown: str) -> tuple[str, list[tuple[int, str]]]:
    """Markdown as a reader reads it, taken from the CommonMark tokens: the body text (text, inline code, code
    blocks; emphasis markers and comments are not text: T1 rv-a00ad460bc), and separately the metadata a reader can
    also see, link and image titles (tooltips) and image alt text, each with the length of the body text before it.
    Metadata is not spliced into the body, so a phrase stays intact around a link (T1 rv-45e967244a). Raw HTML
    other than a comment is refused, as the DE25 section refuses it (T1 rv-5960ee971c)."""
    meta: list[tuple[int, str]] = []
    out, lists = [], []        # lists: the next item number of each open ordered list, None for a bullet list
    items = []                 # per open list item: its number until the item's own text has taken it
    for tok in MarkdownIt("commonmark").parse(markdown):
        # An ordered list item shows its number as the reader sees it: the list's start, then +1 per item
        # (T1 rv-6543102979: '2. Address.' is not step 1).
        if tok.type == "ordered_list_open":
            start = tok.attrGet("start")      # absent means 1; a start of 0 is 0 (T1 rv-3b05fb442d)
            lists.append(int(start) if start is not None else 1)
        elif tok.type == "bullet_list_open":
            lists.append(None)
        elif tok.type in ("ordered_list_close", "bullet_list_close"):
            lists.pop()
        elif tok.type == "list_item_open":
            # The number belongs to the item's own text only: an empty item followed by a paragraph does not give
            # that paragraph the number (T1 rv-7dd66a9d2e).
            if lists and lists[-1] is not None:
                items.append(f"{LIST_MARK}{lists[-1]}. ")
                lists[-1] += 1
            else:
                items.append("")
        elif tok.type == "list_item_close":
            if items.pop():
                out.append(f"{LIST_MARK}(empty list item)")
        if tok.type == "html_block" and not re.fullmatch(rf"\s*(?:{_COMMENT}\s*)+", tok.content):
            raise AssertionError(f"raw HTML the checks do not read: {tok.content[:60]!r}")
        if tok.type in ("fence", "code_block"):
            out.append(tok.content)
        elif tok.type == "inline":
            parts = []
            for child in tok.children or []:
                here = len(_flat(" ".join(out) + " " + (items[-1] if items else "") + "".join(parts)))
                if child.type in ("link_open", "image") and child.attrGet("title"):
                    meta.append((here, _flat(child.attrGet("title"))))       # a tooltip (T1 rv-0b759f3955)
                if child.type == "image":
                    # The parsed alt text (emphasis markers are not text: T1 rv-6bb005685e) is metadata, not the
                    # body text a required label must be (T1 rv-0c60199f6a, rv-a89f9b8661). It is read through
                    # nested images and line breaks (T1 rv-5f7cf6c980).
                    meta.append((here, _flat(_alt_text(child))))
                if child.type in ("text", "code_inline"):
                    parts.append(child.content)
                elif child.type in ("softbreak", "hardbreak"):
                    parts.append(" ")
                elif child.type == "html_inline" and not re.fullmatch(rf"\s*{_COMMENT}\s*", child.content):
                    raise AssertionError(f"raw HTML the checks do not read: {child.content[:60]!r}")
            text = "".join(parts)
            if items and items[-1] and text.strip():   # only visible text takes the number (T1 rv-e7fd2a1fde)
                text, items[-1] = items[-1] + text, ""
            out.append(text)
    return _flat(" ".join(out)), meta



def _check_first_boot(raw: str) -> None:
    # Scoped to the rendered First boot section: from its heading to the next heading of any level, so a note
    # moved to an appendix of the page no longer counts (T1 rv-4b55c5529d).
    # Judged as a reader sees it: _visible_text drops only tokens CommonMark parses as HTML comments, so a comment
    # written inside a code span stays visible (T1 rv-836fde037c, rv-011e9bc77c).
    page = raw
    # main's own assertions, on the raw page from its first byte, kept beside the rendered checks below so the pair is
    # never weaker than main's check (T1 rv-5f7cf6c980).
    raw_flat = " ".join(raw.split())
    assert "The image ships with no network address" not in raw
    assert ("The DE25 image differs: it ships with the static address of its deployment descriptor "
            "on its kernel command line and no SSH server") in raw_flat
    for needed in ("ds_ip=", "1. Address.", "The DE25 image differs", "/etc/init.d/S49dropbear start",
                   "ds_ip=<address>/<prefix>", "over DHCP"):
        assert needed in raw, f"first_boot.md lost {needed!r}"
    assert raw.index("ds_ip=") > raw.index("1. Address.")
    assert raw.index("The DE25 image differs") > raw.index("/etc/init.d/S49dropbear start")
    assert raw.index("ds_ip=<address>/<prefix>") < raw.index("over DHCP")
    tokens = MarkdownIt("commonmark").parse(page)
    heads = [tok.map[0] for tok in tokens if tok.type == "heading_open" and tok.level == 0]
    lines = page.splitlines(keepends=True)
    assert heads and lines[heads[0]].startswith("# First boot"), "first_boot.md does not open with its First boot heading"
    # Nothing a reader sees sits above the title (T1 rv-5f7cf6c980).
    above = [t for t in tokens if t.map and t.map[0] < heads[0] and t.level == 0
             and not (t.type == "html_block" and re.fullmatch(rf"\s*(?:{_COMMENT}\s*)+", t.content))]
    assert not above, "first_boot.md has visible content above its First boot heading"
    first = "".join(lines[heads[0]:heads[1] if len(heads) > 1 else len(lines)])
    flat, meta = _reader_view(first)   # required text is body text; metadata is checked on its own
    # The contradicting claim is refused anywhere on the page, not only in the section (T1 rv-f2fc046c84), in the
    # body text and in every tooltip or alt text (wrapped lines, emphasis: T1 rv-836fde037c, rv-a00ad460bc).
    page_body, page_meta = _reader_view(page)
    banned = "The image ships with no network address"
    assert banned not in page_body and not any(banned in text for _, text in page_meta)
    assert ("The DE25 image differs: it ships with the static address of its deployment descriptor "
            "on its kernel command line and no SSH server") in flat
    # The DE25 note follows the numbered steps, so step 1 stays the first ds_ip= mention. Positions are taken in the
    # same visible text the note is found in (T1 rv-1f018c1160).
    step1 = f"{LIST_MARK}1. Address."
    assert step1 in flat, "First boot no longer opens its numbered steps with '1. Address.'"
    # ... and it is the section's first numbered item: no other ordered list comes before it (T1 rv-296b8dac2b).
    assert flat.index(LIST_MARK) == flat.index(step1), "an ordered list item comes before '1. Address.'"
    # The label must be body text; an earlier ds_ip= mention counts in the body and in any tooltip or alt text
    # before the label (T1 rv-6bb005685e, rv-45e967244a).
    assert flat.index("ds_ip=") > flat.index(step1)
    # Metadata up to the end of the label counts as before it: a tooltip inside item 1 ahead of 'Address.' sits
    # after the list number but before the label (T1 rv-6825d4f6e9).
    assert not any("ds_ip=" in text for at, text in meta if at < flat.index(step1) + len(step1))
    assert flat.index("The DE25 image differs") > flat.index("/etc/init.d/S49dropbear start")
    # Static first, then DHCP, as S40network does it.
    s40 = (ROOT / "sw/linux/rootfs_overlay/etc/init.d/S40network").read_text(encoding="utf-8")
    assert s40.index("cmdline_value ds_ip") < s40.index("udhcpc -q")
    assert flat.index("ds_ip=<address>/<prefix>") < flat.index("over DHCP")


def test_the_first_boot_text_matches_the_baked_address():
    """The DE25 image carries ds_ip= on its kernel command line, so First boot may
    not say every image ships with no address."""
    _check_first_boot(FIRST_BOOT.read_text(encoding="utf-8"))


DE25_NOTE = ("The DE25 image differs: it ships with the static address of its deployment descriptor on its kernel "
             "command line and\nno SSH server")


@pytest.mark.parametrize("mutate", [
    # The required note only in a link title, while the visible text says the opposite (T1 rv-a00ad460bc).
    lambda t: t.replace(DE25_NOTE, f'[The DE25 image requests DHCP](de25.md "{" ".join(DE25_NOTE.split())}")', 1),
    # The banned claim, split by emphasis.
    lambda t: t + "\nThe image ships with **no network address**.\n",
    # The banned claim as an image's alt text (T1 rv-0c60199f6a).
    lambda t: t + "\n![The image ships with no network address](../images/self_test.svg)\n",
    # Step 1 renumbered: the address step is no longer the first item (T1 rv-6543102979).
    lambda t: "<!-- 1. Address. -->\n" + t.replace("\n1. Address.", "\n2. Address.", 1),
    # An empty first item, then the address text as a paragraph outside the list (T1 rv-7dd66a9d2e).
    lambda t: "<!-- 1. Address. -->\n" + t.replace("\n1. Address.", "\n1.\n\nAddress.", 1),
    # An item holding only an empty link, then the address text outside the list (T1 rv-e7fd2a1fde).
    lambda t: "<!-- 1. Address. -->\n" + t.replace("\n1. Address.", "\n1. [](#first-boot)\n\nAddress.", 1),
    # The banned claim around a link whose tooltip would split it if spliced (T1 rv-45e967244a).
    lambda t: t + '\nThe image ships with [no network address](de25.md "Network setup").\n',
    # ds_ip= in a nested image's alt text before step 1, and the ban split over two alt-text lines (T1 rv-5f7cf6c980).
    # (Each leading '<!-- 1. Address. -->' satisfies main's raw ordering, so the rendered checks are what refuse it:
    # T1 rv-6825d4f6e9.)
    lambda t: "<!-- 1. Address. -->\n" + t.replace(
        "\n1. Address.", "\n![See ![ds_ip=](../images/self_test.svg)](../images/architecture.svg)\n\n1. Address.", 1),
    lambda t: t + "\n![The image ships with\nno network address](../images/self_test.svg)\n",
    # A ds_ip= mention above the title (T1 rv-5f7cf6c980).
    lambda t: "<!-- 1. Address. -->\n\nUse ds_ip= before following this guide.\n\n" + t,
    # The list renumbered to start at 0 (T1 rv-3b05fb442d).
    lambda t: "<!-- 1. Address. -->\n" + t.replace("\n1. Address.", "\n0. Address.", 1),
    # A separate ordered list before the steps (T1 rv-296b8dac2b).
    lambda t: "<!-- 1. Address. -->\n" + t.replace("\n1. Address.", "\n1) Put SSH setup before addressing.\n\n1. Address.", 1),
    # A tooltip or alt text holding ds_ip= inside item 1, ahead of the label (T1 rv-6825d4f6e9).
    lambda t: "<!-- 1. Address. -->\n" + t.replace("\n1. Address.", '\n1. [](#a "ds_ip=") Address.', 1),
    lambda t: "<!-- 1. Address. -->\n" + t.replace("\n1. Address.", "\n1. ![ds_ip=](../images/self_test.svg) Address.", 1),
    # ds_ip= rendered across a link before step 1 (T1 rv-45e967244a).
    lambda t: t.replace("\n1. Address.", '\nds_[ip=](de25.md "Addressing")\n\n1. Address.', 1),
    # A ds_ip= mention before step 1, only in a link tooltip (T1 rv-6bb005685e).
    lambda t: t.replace("\n1. Address.", '\n[address](#first-boot "Use ds_ip= as the boot argument")\n\n1. Address.', 1),
    # The banned claim as emphasised alt text (T1 rv-6bb005685e).
    lambda t: t + "\n![The image ships with **no network address**](../images/self_test.svg)\n",
    # The banned claim as a link title (tooltip) (T1 rv-0b759f3955).
    lambda t: t + '\n[DE25](de25.md "The image ships with no network address")\n',
    # The step label replaced by an image whose alt text is the label (T1 rv-a89f9b8661).
    lambda t: "<!-- 1. Address. -->\n" + t.replace("\n1. Address.", "\n1. ![Address.](../images/self_test.svg)", 1),
    # Prose spelling the label before a renumbered list (T1 rv-47763b7493).
    lambda t: "<!-- 1. Address. -->\n" + t.replace(
        "\n1. Address.", "\nExpected label: 1. **Address.**\n\n2. Address.", 1),
    # The required note only in a raw HTML title whose text holds '>' (T1 rv-5960ee971c).
    lambda t: t.replace(DE25_NOTE, f'<span title="> {" ".join(DE25_NOTE.split())}">The DE25 image requests DHCP</span>', 1),
])
def test_the_first_boot_check_reads_the_visible_text(mutate):
    raw = FIRST_BOOT.read_text(encoding="utf-8")
    bad = mutate(raw)
    assert bad != raw
    with pytest.raises(AssertionError):
        _check_first_boot(bad)


def test_metadata_after_the_step1_label_is_allowed():
    """Positive control (T1 rv-a1ac530f21): a tooltip that follows the whole label is not before it."""
    raw = FIRST_BOOT.read_text(encoding="utf-8")
    ok = raw.replace("\n1. Address.", '\n1. Address. [Details](#first-boot "ds_ip= boot argument")', 1)
    assert ok != raw
    _check_first_boot(ok)


def test_every_command_block_is_valid_bash(tmp_path):
    blocks = _blocks(_section(_readme()))
    assert len(blocks) == len(EXPECTED)
    for i, block in enumerate(blocks):
        script = tmp_path / f"de25_{i}.sh"
        script.write_text(block, encoding="utf-8")
        proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, f"block {i}: {proc.stderr}"


def _run_block(tmp_path: Path, block: str, fail_on: str) -> tuple[int, list[str]]:
    """Run one block with every external command stubbed; the stub whose call
    starts with *fail_on* exits 1. Returns the status and the calls made.

    The block must be one of the EXPECTED blocks, checked before Bash starts, and
    PATH holds only the stubs: no README edit can make this test run a real
    command."""
    lines = [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if tuple(lines) not in EXPECTED:  # not an assert: it must hold under python -O too
        pytest.fail(f"refusing to execute an unexpected block: {lines}")
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    for name in ("rr", "pip", "python3", "routewave", "cp"):
        stub = stubs / name
        stub.write_text(
            "#!/bin/sh\n"
            f'echo "{name} $*" >> "{log}"\n'
            f'case "{name} $*" in "{fail_on}"*) exit 1;; esac\n'
            "exit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    # DS-P3.370: the attach block calls RouteWave where rr ws update-viewer installs it under HOME, not on PATH.
    rw = tmp_path / ".routertl" / "bin" / "routewave" / "routewave"
    rw.parent.mkdir(parents=True, exist_ok=True)
    rw.write_text((stubs / "routewave").read_text(encoding="utf-8"), encoding="utf-8")
    rw.chmod(0o755)
    script = tmp_path / "block.sh"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")  # the approved text, not the raw block
    env = {"PATH": str(stubs), "HOME": str(tmp_path)}
    proc = subprocess.run([shutil.which("bash") or "/bin/bash", str(script)], env=env,
                          capture_output=True, text=True, cwd=tmp_path)
    return proc.returncode, log.read_text(encoding="utf-8").splitlines()


def test_each_block_stops_at_its_first_failure(tmp_path):
    fpga, _descriptor, _image, card, program, attach = _blocks(_section(_readme()))
    rc, calls = _run_block(tmp_path, fpga, "rr synth")
    assert rc != 0 and calls[-1] == "rr synth run", calls
    rc, calls = _run_block(tmp_path, card, "rr linux")
    assert rc != 0 and calls[-1].startswith("rr linux flash-sd"), calls
    rc, calls = _run_block(tmp_path, program, "rr lease")
    assert rc != 0 and calls == [f"rr lease run de25-standard -- rr program run -b {HPS_SOF}"], calls
    rc, calls = _run_block(tmp_path, attach, "python3 tools/wait_for_port.py")
    assert rc != 0 and not any(c.startswith("routewave") for c in calls), calls
    # Positive control: with nothing failing, every command runs and the block returns 0.
    rc, calls = _run_block(tmp_path, attach, "nothing")
    assert rc == 0 and calls[-1].startswith("routewave"), calls


def test_the_stub_runner_refuses_an_unexpected_block(tmp_path):
    with pytest.raises(pytest.fail.Exception, match="refusing to execute"):
        _run_block(tmp_path, "dd if=/dev/zero of=/dev/sdX\n", "nothing")


def test_the_operator_steps_are_stated():
    section = _section(_readme())
    missing = [s for s in OPERATOR_STEPS if s not in section]
    assert missing == [], missing


def _between_blocks(section: str, text: str) -> tuple[int, int]:
    """(i, j): *text* lies after command block i and before block j (0-based)."""
    pos = section.index(text)
    ends = [end for _, end, _ in _fences(section)]
    starts = [start for start, _, _ in _fences(section)]
    after = max((i for i, e in enumerate(ends) if e <= pos), default=-1)
    before = min((j for j, s in enumerate(starts) if s >= pos), default=len(starts))
    return after, before


def test_the_operator_steps_sit_between_the_blocks_they_govern():
    section = _section(_readme())
    # The descriptor edit comes after the copy (block 1) and before the image build (block 2).
    assert _between_blocks(section, "Set `addr` (and `prefix`, or delete it)") == (1, 2)
    # Card insertion and power-on come after the card write (block 3) and before programming (block 4).
    assert _between_blocks(section, "Put the card in the board and power it on.") == (3, 4)


def test_the_section_states_the_de25_image_has_no_ssh_server():
    """Measured on the d2447f7e DE25 rootfs: S49dropbear and the key tool ship, the
    dropbear binary does not, so the shared SSH steps cannot start a server."""
    assert "The DE25 image has no SSH server" in _section(_readme())


def _wait_tool():
    spec = importlib.util.spec_from_file_location("wait_for_port_p1_119", ROOT / "tools" / "wait_for_port.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_port_wait_returns_on_the_first_accepted_connection():
    tool = _wait_tool()
    attempts = []

    class _Conn:
        def close(self):
            pass

    def connect(addr, timeout):
        attempts.append(addr)
        if len(attempts) < 3:
            raise ConnectionRefusedError
        return _Conn()

    now = [0.0]
    assert tool.wait_for_port("192.0.2.10", 7475, 30, connect=connect, clock=lambda: now[0],
                              sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert attempts == [("192.0.2.10", 7475)] * 3


def test_the_port_wait_gives_up_at_its_timeout():
    """No connection attempt or sleep runs past the deadline, with slow
    connection attempts that each use their whole timeout."""
    tool = _wait_tool()
    now = [0.0]
    attempts = []

    def slow_refuse(addr, timeout):
        attempts.append((now[0], timeout))
        now[0] += timeout
        raise OSError("unreachable")

    assert not tool.wait_for_port("192.0.2.10", 7475, 9, connect=slow_refuse, clock=lambda: now[0],
                                  sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert now[0] <= 9, now[0]
    assert all(start + t <= 9 for start, t in attempts), attempts


def test_the_port_wait_cli_reports_a_refused_port_within_its_budget(monkeypatch, capsys):
    tool = _wait_tool()
    calls = []

    def refuse(addr, timeout):
        calls.append(addr)
        raise ConnectionRefusedError

    monkeypatch.setattr(tool.socket, "create_connection", refuse)
    monkeypatch.setattr(tool.time, "sleep", lambda s: None)
    start = tool.time.monotonic()
    assert tool.main(["192.0.2.10", "7475", "--timeout", "0.2"]) == 1
    assert tool.time.monotonic() - start < 5
    assert calls and set(calls) == {("192.0.2.10", 7475)}
    assert "did not accept a connection within 0.2 s" in capsys.readouterr().err


def test_the_port_wait_cli_refuses_a_name_or_an_unbounded_timeout(capsys):
    tool = _wait_tool()
    assert tool.main(["board.example", "7475"]) == 2
    assert tool.main(["192.0.2.10", "7475", "--timeout", "nan"]) == 2
    assert tool.main(["192.0.2.10", "7475", "--timeout", "inf"]) == 2
    assert tool.main(["192.0.2.10", "7475", "--timeout", "-1"]) == 2


def test_the_readme_names_the_effective_descriptor_resource_as_the_resolver_reads_it(monkeypatch, tmp_path):
    """DS-P3.319: RR_BENCH_CONFIG wins over runtimes/hw.local.yml, and the address comes
    from the resource bench.targets.de25_bench.bench_ip names, as the README says."""
    tool = _image_tool()
    root = tmp_path / "clone"
    (root / "targets").mkdir(parents=True)
    (root / "runtimes").mkdir()
    shutil.copy(ROOT / "targets" / "de25_datalogger.yml", root / "targets" / "de25_datalogger.yml")
    hw = yaml.safe_load((ROOT / "runtimes" / "hw.example.yml").read_text(encoding="utf-8"))
    hw["bench"]["resources"]["de25_address"]["addr"] = "192.0.2.10"
    del hw["bench"]["resources"]["de25_address"]["prefix"]
    other = dict(hw["bench"]["resources"]["de25_address"], addr="192.0.2.20")
    hw["bench"]["resources"]["lab_address"] = other
    local = root / "runtimes" / "hw.local.yml"
    local.write_text(yaml.safe_dump(hw), encoding="utf-8")
    monkeypatch.delenv("RR_BENCH_CONFIG", raising=False)

    def ds_ip() -> list[str]:
        args = tool.deployment_bootargs(root, target_rel="targets/de25_datalogger.yml")
        return [tok for tok in args.split() if tok.startswith("ds_ip=")]

    assert ds_ip() == ["ds_ip=192.0.2.10/24"]
    # A retained descriptor whose bench_ip names another resource ignores de25_address.
    hw["bench"]["targets"]["de25_bench"]["bench_ip"] = "lab_address"
    hw["bench"]["targets"]["de25_bench"]["uses"] = ["lab_address"]
    local.write_text(yaml.safe_dump(hw), encoding="utf-8")
    assert ds_ip() == ["ds_ip=192.0.2.20/24"]
    # RR_BENCH_CONFIG, when set, replaces the local file.
    env_file = tmp_path / "bench.yml"
    hw["bench"]["resources"]["lab_address"]["addr"] = "192.0.2.30"
    env_file.write_text(yaml.safe_dump(hw), encoding="utf-8")
    monkeypatch.setenv("RR_BENCH_CONFIG", str(env_file))
    assert ds_ip() == ["ds_ip=192.0.2.30/24"]
    section = _section(_readme())
    row = next(ln for ln in section.splitlines() if ln.startswith("| Deployment descriptor |"))
    assert ("The image build reads the file named by `RR_BENCH_CONFIG` when that variable is set, and "
            "`runtimes/hw.local.yml` otherwise.") in row
    assert ("In that file, `bench.targets.de25_bench.bench_ip` names the resource that holds the board's static "
            "address, `de25_address` in the template;") in row
    # Step 6 follows the effective descriptor, and sits before the block it governs.
    step6 = ("The image build reads the file `RR_BENCH_CONFIG` names when it is set, and `runtimes/hw.local.yml`\n"
             "otherwise. In that file, `addr` and `prefix` are set under the `bench.resources` entry that\n"
             "`bench.targets.de25_bench.bench_ip` names; in a copy of the template that entry is `de25_address`.")
    assert step6 in section
    assert _between_blocks(section, step6) == (1, 2)


def test_the_readme_gives_the_serial_console_settings_the_target_uses():
    """DS-P3.319: the console the image's bootargs name, and the line settings the
    DE-G0 provider applies (Haz.107: DTR and RTS asserted or the port reads nothing)."""
    target = yaml.safe_load((ROOT / "targets" / "de25_datalogger.yml").read_text(encoding="utf-8"))
    bootargs = target["linux"]["bootargs"].split()
    assert "console=ttyS1,115200" in bootargs
    assert any(tok.startswith("earlycon=uart8250,mmio32,0x10c02100,") for tok in bootargs)  # HPS UART1
    provider = (ROOT / "tools" / "golden_path" / "providers_deg0_de25.py")
    if provider.is_file():  # private; the export drops tools/golden_path/
        assert "TIOCM_DTR | termios.TIOCM_RTS" in provider.read_text(encoding="utf-8")
    row = next(ln for ln in _section(_readme()).splitlines() if ln.startswith("| Serial console |"))
    for needed in ("HPS UART1", "`ttyS1`", "on the USB-Blaster III's USB serial port", "115200 8N1",
                   "DTR and RTS asserted"):
        assert needed in row, needed
