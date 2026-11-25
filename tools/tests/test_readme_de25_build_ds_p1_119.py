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
HEADING = "### Build and run on the Terasic DE25"
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
        'python3 tools/wait_for_port.py "$BOARD_IP" 7475 --timeout 180',
        'routewave -c "stream attach $BOARD_IP:7475" )',
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
    return README.read_text(encoding="utf-8")


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
    start = next((i for i, tok in enumerate(tokens) if tok.type == "heading_open" and tok.tag == "h3"
                  and tok.level == 0 and tokens[i + 1].content == title), None)
    if start is None:
        raise AssertionError(f"README has no rendered heading {HEADING!r}")
    out, fences, pos = [HEADING + "\n\n"], [], len(HEADING) + 2
    for tok in tokens[start + 3:]:
        if tok.type == "heading_open" and tok.tag in ("h1", "h2", "h3") and tok.level == 0:
            break
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
    end = text.index("\n### ", start + len(HEADING))
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
    start = text.index(HEADING)
    return text.index("\n### ", start + len(HEADING)) + 1


def test_a_nested_heading_does_not_end_the_section():
    """A blockquoted h3 is not a section boundary: a bash fence after it is still read."""
    text = _readme()
    end = _next_heading(text)
    moved = text[:end] + "> ### Aside\n\n```bash\nrr program run\n```\n\n" + text[end:]
    assert moved != text
    with pytest.raises(AssertionError, match="commands differ"):
        _assert_commands_in_order(_commands(_section(moved)))


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


def test_the_first_boot_text_matches_the_baked_address():
    """The DE25 image carries ds_ip= on its kernel command line, so First boot may
    not say every image ships with no address."""
    text = _readme()
    first = text[text.index("### First boot"):text.index(HEADING)]
    flat = " ".join(first.split())
    assert "The image ships with no network address" not in first
    assert ("The DE25 image differs: it ships with the static address of its deployment descriptor "
            "on its kernel command line and no SSH server") in flat
    # The DE25 note follows the numbered steps, so step 1 stays the first ds_ip= mention.
    assert first.index("ds_ip=") > first.index("1. Address.")
    assert first.index("The DE25 image differs") > first.index("/etc/init.d/S49dropbear start")
    # Static first, then DHCP, as S40network does it.
    s40 = (ROOT / "sw/linux/rootfs_overlay/etc/init.d/S40network").read_text(encoding="utf-8")
    assert s40.index("cmdline_value ds_ip") < s40.index("udhcpc -q")
    assert first.index("ds_ip=<address>/<prefix>") < first.index("over DHCP")


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
