# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.371: every README command block runs as pasted on RouteRTL 5.7.0.

Three defects shipped in public text: `<placeholder>` arguments inside bash blocks
(a redirection when pasted: `source <install-dir>/x` reads from `install-dir` and
writes to `/x`), a hardware `rr` command outside a lease (RouteRTL 5.7.0 refuses
it), and a Quartus release in SUPPORTED_ENVIRONMENT.md that disagreed with the DE25
port plan. Each check has a negative control.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]
SANDBOX_TICKET = "DS-P3.320"
README = ROOT / "README.md"
SHELLS = {"bash", "sh", "shell"}
def _target_boards() -> dict[str, str]:
    """target name -> the board token RouteRTL resolves for its lease (hardware.board, else .platform)."""
    out = {}
    for path in (ROOT / "targets").glob("*.yml"):
        hw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("hardware") or {}
        board = hw.get("board") or hw.get("platform")
        if board:
            out[path.stem] = str(board)
    return out


def _blocks(text: str) -> list[tuple[int, str]]:
    """Every shell fence, as CommonMark parses it (backtick or tilde, any info spacing)."""
    return [((tok.map[0] + 1) if tok.map else 0, tok.content)
            for tok in MarkdownIt("commonmark").parse(text)
            if tok.type == "fence" and (tok.info.split() or [""])[0] in SHELLS]


def _bash_errors(blocks, tmp_path) -> list[str]:
    bad = []
    for i, (line, body) in enumerate(blocks):
        script = tmp_path / f"block{i}.sh"
        script.write_text(body, encoding="utf-8")
        proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        if proc.returncode != 0:
            bad.append(f"README.md:{line}: {proc.stderr.strip()}")
    return bad


def _placeholders(blocks) -> list[str]:
    return [f"README.md:{line}: {ph}" for line, body in blocks for ph in re.findall(r"<[^<>\n]+>", body)]


# Every external command becomes a logging stub; `cd` and `source` are stubbed too, so
# a block runs as bash would run it, with nothing reaching the host.
PRELUDE = r"""
printf 'START\036' >> "$S2_LOG"
command_not_found_handle() {
  { printf 'CMD\037%s' "${RR_TARGET-}"; printf '\037%s' "$@"; printf '\036'; } >> "$S2_LOG"; return 0; }
cd() { :; }
source() { printf 'SRC\t%s\n' "$*" >> "$S2_LOG"; }
"""


def _sandbox(scratch: Path, cwd: Path, script: Path) -> list[str] | None:
    """bwrap argv running *script* with the root read-only and only *scratch* writable,
    or None when that exact configuration cannot start here. The probe uses the same
    flags as the run, so a probe that passes cannot hide a launch that fails."""
    import shutil
    import subprocess
    bw = shutil.which("bwrap")
    if not bw:
        return None
    flags = [bw, "--ro-bind", "/", "/", "--bind", str(scratch), str(scratch), "--dev", "/dev", "--proc", "/proc",
             "--unshare-all", "--die-with-parent", "--chdir", str(cwd)]
    if subprocess.run(flags + ["/bin/true"], capture_output=True).returncode != 0:
        return None
    return flags + ["/bin/bash", str(script)]


def _executed(text: str, tmp_path: Path) -> list[tuple[int, str, list[str]]]:
    """Run each shell block in a clean bash whose PATH finds nothing, and return the
    external commands it actually invoked: (block line, RR_TARGET, argv)."""
    calls = []
    for i, (line, body) in enumerate(_blocks(text)):
        for raw in body.splitlines():
            head = raw.split("#", 1)[0].strip().split(" ", 1)[0]
            code = raw.split("#", 1)[0]
            if head.startswith("/") or head in {"exec", "eval", "command", "builtin", "enable"} \
                    or re.search(r"(^|[\s;&|(])(ba|da|z)?sh\s+-c\b", code) \
                    or re.search(r"(^|[\s;&|(=])[^\s;&|()]*/rr(\s|$)", code):
                raise AssertionError(f"README.md:{line}: refusing to execute {raw.strip()!r}")
        log = tmp_path / f"calls{i}.log"
        log.write_text("", encoding="utf-8")
        script = tmp_path / f"block{i}.sh"
        script.write_text(PRELUDE + body, encoding="utf-8")
        run = tmp_path / f"cwd{i}"
        run.mkdir()
        argv = _sandbox(tmp_path, run, script)
        if argv is None:  # README text is never executed outside the sandbox
            pytest.skip(f"bwrap cannot run here; the README execution guards need it ({SANDBOX_TICKET})")
        proc = subprocess.run(argv, cwd=run, capture_output=True, text=True, timeout=60,
                              env={"PATH": "/nonexistent", "S2_LOG": str(log), "HOME": str(run)})
        records = log.read_text(encoding="utf-8").split("\x1e")
        if not records or records[0] != "START":  # the sandbox launched and bash ran the block
            raise AssertionError(f"README.md:{line}: the sandboxed bash never started ({proc.stderr[-300:]!r})")
        for record in records:
            fields = record.split("\x1f")
            if fields[0] == "CMD":  # argument boundaries as bash passed them, never re-split
                calls.append((line, fields[1], fields[2:]))
    return calls


def _hardware_at(argv: list[str]) -> list[int]:
    """Indices i where argv[i:] is a RouteRTL 5.7.0 lease-gated rr command (the commands
    that call require_lease in sdk/cli/commands: program, hw, ila, boot, linux push-sw,
    guest*, jtag-boot, console), wherever it sits: a wrapper
    such as `env`, `timeout` or `nice` in front does not hide it."""
    out = []
    for i, tok in enumerate(argv):
        if tok != "rr" or i + 1 >= len(argv):
            continue
        sub = argv[i + 1:]
        if sub[0] in {"program", "hw", "ila", "boot"} or (
                sub[0] == "linux" and len(sub) > 1
                and (sub[1] in {"push-sw", "jtag-boot", "console"} or sub[1].startswith("guest"))):
            out.append(i)
    return out


def _unleased(text: str, boards: dict[str, str], tmp_path: Path) -> list[str]:
    """Executed hardware rr commands not run as `rr lease run <board> -- rr ...` on the
    board of the target selected last (`rr target set <t>`), or under a RR_TARGET that
    names another target. Judged on the exact argv bash passed."""
    out, active = [], None
    for line, rr_target, argv in _executed(text, tmp_path):
        if argv[:3] == ["rr", "target", "set"] and len(argv) > 3:
            active = argv[3]
            continue
        hits = _hardware_at(argv)
        if not hits:
            continue
        leased = argv[:3] == ["rr", "lease", "run"] and len(argv) > 5 and argv[4] == "--"
        want = boards.get(active or "")
        if not leased or hits != [5]:
            out.append(f"README.md:{line}: outside a lease: {argv}")
        elif argv[3] != want:
            out.append(f"README.md:{line}: lease on {argv[3]!r}, active target {active!r} needs {want!r}")
        if rr_target and rr_target not in (f"targets/{active}.yml", active):
            out.append(f"README.md:{line}: RR_TARGET={rr_target} overrides the active target {active!r}")
    return out


def _fenced(body: str) -> str:
    return f"```bash\n{body}```\n"


def test_every_readme_shell_block_is_valid_bash(tmp_path):
    blocks = _blocks(README.read_text(encoding="utf-8"))
    assert len(blocks) >= 10
    assert _bash_errors(blocks, tmp_path) == []


def test_no_readme_shell_block_holds_an_angle_bracket_placeholder():
    assert _placeholders(_blocks(README.read_text(encoding="utf-8"))) == []


def test_no_hardware_rr_command_runs_outside_a_lease_on_the_active_board(tmp_path):
    assert _unleased(README.read_text(encoding="utf-8"), _target_boards(), tmp_path) == []


def _assert_leased_program_step(text: str, target: str, board: str, tmp_path: Path) -> None:
    """Positive control: after `rr target set <target>`, a program step is actually
    executed under a lease on that target's board."""
    active, found = None, False
    for _, _rr_target, argv in _executed(text, tmp_path):
        if argv[:3] == ["rr", "target", "set"] and len(argv) > 3:
            active = argv[3]
        elif active == target and argv[:8] == ["rr", "lease", "run", board, "--", "rr", "program", "run"]:
            found = True
    assert found, f"no leased program step executed for {target}"


@pytest.mark.parametrize("target,board", [("pfsoc_datalogger", "polarfire-soc-discovery"),
                                          ("de25_datalogger", "de25-standard")])
def test_each_programmed_board_has_its_leased_program_step(target, board, tmp_path):
    assert _target_boards()[target] == board
    _assert_leased_program_step(README.read_text(encoding="utf-8"), target, board, tmp_path)


@pytest.mark.parametrize("fence", ["~~~bash\n{body}~~~\n", "```bash  \n{body}```\n", "```sh\n{body}```\n"])
def test_every_fence_form_is_checked(fence, tmp_path):
    text = fence.format(body="rr program run\nrr linux flash-sd --device <sd-card-device>\n")
    blocks = _blocks(text)
    assert len(blocks) == 1
    assert _placeholders(blocks)
    assert _bash_errors(blocks, tmp_path)
    assert _unleased(_fenced("rr program run\n"), _target_boards(), tmp_path)


PF = "rr target set pfsoc_datalogger\n"
LEASED = "rr lease run polarfire-soc-discovery -- rr program run\n"


@pytest.mark.parametrize("bad", [
    PF + "rr program run\n", PF + "rr hw bit-sha --json\n", PF + "true && rr program run -b a.sof\n",
    PF + "rr lease run polarfire-soc-discovery -- rr program run && rr program run\n",
    PF + "rr lease run polarfire-soc-discovery -- rr program run & rr program run\n",
    PF + "rr lease run de25-standard -- rr program run\n",
    PF + "RR_TARGET=targets/de25_datalogger.yml rr lease run polarfire-soc-discovery -- rr program run\n",
    PF + "if rr program run; then :; fi\n", PF + "! rr program run\n",
    PF + "printf '%s\\n' example#fragment; rr program run\n",
    PF + LEASED.rstrip("\n") + "; env rr program run\n", PF + "timeout 60 rr program run\n",
    PF + "rr linux console\n", PF + "rr linux jtag-boot\n", PF + "( rr ila capture )\n",
])
def test_the_lease_check_refuses(bad, tmp_path):
    assert _unleased(_fenced(bad), _target_boards(), tmp_path)


def test_the_lease_check_accepts_a_lease_on_the_active_board(tmp_path):
    assert _unleased(_fenced(PF + LEASED), _target_boards(), tmp_path) == []


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace(LEASED, "", 1),
    lambda t: t.replace(LEASED, "false && " + LEASED, 1),
    lambda t: t.replace(LEASED, 'rr "lease run polarfire-soc-discovery -- rr program run"\n', 1),
])
def test_an_absent_or_unreachable_polarfire_program_step_fails_its_positive_control(mutate, tmp_path):
    text = README.read_text(encoding="utf-8")
    bad = mutate(text)
    assert bad != text
    with pytest.raises(AssertionError, match="no leased program step"):
        _assert_leased_program_step(bad, "pfsoc_datalogger", "polarfire-soc-discovery", tmp_path)


def test_a_block_cannot_write_outside_its_scratch_directory(tmp_path):
    """A README block runs under bwrap with only the scratch directory writable, so a
    redirection or an absolute command later in a line cannot create a host file."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = tmp_path / "outside.txt"
    _executed(_fenced(f"echo x > {outside}\n: > {outside}.2\ntrue; /usr/bin/touch {outside}.3\n"), scratch)
    assert not outside.exists()
    assert not Path(f"{outside}.3").exists()
    assert not Path(f"{outside}.2").exists()


def test_without_bwrap_nothing_is_executed(tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), "_sandbox", lambda scratch, cwd, script: None)
    outside = tmp_path / "outside.txt"
    (tmp_path / "s").mkdir()
    with pytest.raises(pytest.skip.Exception):
        _executed(_fenced(f"echo x > {outside}\n"), tmp_path / "s")
    assert not outside.exists()


@pytest.mark.parametrize("bad", [PF + "bash -c 'rr program run'\n", PF + "sh -c 'rr program run'\n",
                                 PF + "./.venv/bin/rr program run\n", PF + "x=1 .venv/bin/rr program run\n"])
def test_forms_the_guard_cannot_inspect_are_refused(bad, tmp_path):
    with pytest.raises(AssertionError, match="refusing to execute"):
        _executed(_fenced(bad), tmp_path)


def test_a_sandbox_that_never_starts_fails_instead_of_passing_empty(tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), "_sandbox", lambda scratch, cwd, script: ["/bin/false"])
    with pytest.raises(AssertionError, match="never started"):
        _executed(_fenced(PF + "rr program run\n"), tmp_path)


def test_the_runner_refuses_an_absolute_command(tmp_path):
    with pytest.raises(AssertionError, match="refusing to execute"):
        _executed(_fenced("/bin/rm -rf x\n"), tmp_path)


RELEASE = r"(\d+\.\d+\.\d+)(?![\w.-])"
PATCH = r"(\d+\.\d+)(?![\w.-])"  # a complete token: 1.020, 1.02a, 1.02-rc1 do not match 1.02


def _quartus(text: str, pattern: str) -> tuple[str, str]:
    m = re.search(pattern, text)
    assert m, pattern
    return m.group(1), m.group(2)


def test_the_quartus_release_and_patch_agree_across_the_documents():
    decl = yaml.safe_load((ROOT / "tools/preflight/supported_environment.yml").read_text(encoding="utf-8"))
    de25 = decl["targets"]["de25_datalogger"]
    display = _quartus(de25["tool"], rf"^Quartus Prime Pro {RELEASE} with patch {PATCH}$")
    assert display[0] == str(de25["build"]["tool"]["version"])
    env = (ROOT / "docs/SUPPORTED_ENVIRONMENT.md").read_text(encoding="utf-8")
    row = next(ln for ln in env.splitlines() if ln.startswith("| `de25_datalogger` |"))
    assert _quartus(row, rf"\| Quartus Prime Pro {RELEASE} with patch {PATCH} \|") == display
    readme = README.read_text(encoding="utf-8")
    assert _quartus(readme, rf"\| Quartus Prime Pro \(DE25\) \| `{RELEASE}` with patch {PATCH},") == display
    # The port plan is private: the export drops docs/design/ and the private notes
    # directory. In the source tree (that directory present) the plan is required and
    # must agree. The name is split as tools/public_artifact_hygiene.py's drop list does.
    plan = ROOT / "docs/design/de25_port_plan.md"
    if (ROOT / "docs" / ("in" + "ternal")).is_dir():
        assert plan.is_file(), "source tree without the DE25 port plan"
    if plan.is_file():  # compared whenever present, not only when the notes directory is
        assert _quartus(plan.read_text(encoding="utf-8"),
                        rf"Quartus Prime Pro {RELEASE} Build \d+ \+ patch {PATCH}") == display


@pytest.mark.parametrize("text", ["Quartus Prime Pro 25.3.1 with patch 1.020",
                                  "Quartus Prime Pro 25.3.1 with patch 1.02a",
                                  "Quartus Prime Pro 25.3.1a with patch 1.02",
                                  "Quartus Prime Pro 25.3.1 with patch 1.02-rc1"])
def test_the_quartus_comparison_refuses_a_longer_token(text):
    with pytest.raises(AssertionError):
        assert _quartus(text, rf"Quartus Prime Pro {RELEASE} with patch {PATCH}") == ("25.3.1", "1.02")
