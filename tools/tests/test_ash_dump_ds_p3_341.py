# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DS-P3.341 part 1: the parse tree of a shell script from the image's ash, not from a hand tokenizer.

Evidence that the built ash is the image's ash (same source, same options; not the same binary), the decode of every
word class the DS-P3.316 and DS-P3.342 review rounds found the hand lexer mis-reading, and the proof that parsing
runs nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ash_dump  # noqa: E402


SKIP_REASON = ("DS-P3.350: GitHub Actions: no user namespaces and no DS_ASH_PARSE_UNSANDBOXED opt-out in that workflow, "
               "which builds no parser; the parser tests run in the bench gate and the Forgejo CI")


def parse_environment(env, bwrap_usable: bool) -> str:
    """"ok" when a sanctioned parse exists, "skip" on GitHub proper, else "fail".

    A Forgejo job (measured with forgejo-runner 13.1.0 `exec`, python:3.11 container: CI=true, FORGEJO_ACTIONS=true,
    GITHUB_ACTIONS=true) sets GITHUB_ACTIONS too, so only the absence of FORGEJO_ACTIONS/GITEA_ACTIONS means GitHub
    proper. Any other CI missing both bwrap and the opt-out is a broken workflow, never a skip."""
    in_ci = env.get("CI") in ("true", "1") or env.get("GITHUB_ACTIONS") == "true"
    if not in_ci or bwrap_usable or env.get(ash_dump.UNSANDBOXED_ENV) == ash_dump.UNSANDBOXED_VALUE:
        return "ok"
    github_proper = (env.get("GITHUB_ACTIONS") == "true" and not env.get("FORGEJO_ACTIONS")
                     and not env.get("GITEA_ACTIONS"))
    return "skip" if github_proper else "fail"


@pytest.fixture(autouse=True)
def _a_sanctioned_parse_environment_exists():
    verdict = parse_environment(os.environ, ash_dump._bwrap_usable() is not None)
    if verdict == "skip":
        pytest.skip(SKIP_REASON)
    if verdict == "fail":
        pytest.fail(f"this CI has no user namespaces and no {ash_dump.UNSANDBOXED_ENV}={ash_dump.UNSANDBOXED_VALUE} "
                    "line in its workflow env: add it to the Desk tests step, or the parser tests would not run")


def _ci_optout(monkeypatch) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv(ash_dump.UNSANDBOXED_ENV, ash_dump.UNSANDBOXED_VALUE)


@pytest.fixture(scope="module")
def binary() -> Path:
    return ash_dump.build()


def _words(script: str, *, cmd: int = 0) -> list[ash_dump.Word]:
    cmds = list(ash_dump.simple_commands(ash_dump.dump(script)))
    return cmds[cmd]["args"][1:]            # the words after the command word


# ------------------------------------------------------------------------------------- the equivalence evidence
def test_the_source_and_the_config_are_the_pinned_ones(binary):
    assert hashlib.sha256(ash_dump.tarball().read_bytes()).hexdigest() == ash_dump.TARBALL_SHA256
    assert hashlib.sha256(ash_dump.CONFIG.read_bytes()).hexdigest() == ash_dump.CONFIG_SHA256
    assert ash_dump.TARBALL_SHA256 == "b8cc24c9574d809e7279c3be349795c5d5ceb6fdf19ca709f80cde50e47de314"


def test_the_built_binary_is_busybox_1_36_1(binary):
    out = subprocess.run([str(binary)], capture_output=True, text=True, check=False).stdout
    assert out.startswith("BusyBox v1.36.1 ")


def test_the_built_config_keeps_every_ash_option_of_the_pinned_config(binary):
    """The config is rewritten only for a native static build; every option that shapes the shell is as pinned."""
    keep = re.compile(r"^(?:# )?CONFIG_(?:ASH\w*|SH_IS_\w+|FEATURE_SH_\w+|SHELL_\w+|HUSH\w*|FEATURE_EDITING\w*)[ =]")

    def lines(path: Path) -> list[str]:
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if keep.match(ln)]

    built = lines(binary.parent / "config")
    pinned = lines(ash_dump.CONFIG)
    assert pinned and built == pinned
    assert "CONFIG_SH_IS_ASH=y" in pinned and "CONFIG_ASH_BASH_COMPAT=y" in pinned


def test_the_patch_is_the_only_change_to_the_pinned_source():
    text = ash_dump.PATCH.read_text(encoding="utf-8")
    touched = re.findall(r"^\+\+\+ (\S+)", text, re.M)
    assert touched == ["b/shell/ash.c"]


def test_the_patch_only_adds_lines_to_ash():
    body = ash_dump.PATCH.read_text(encoding="utf-8").split("\n--- ", 1)[1]
    removed = [ln for ln in body.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    assert removed == ["-\tif (nflag)", "-\t\tn1 = list(0);"], removed   # evaltree's -n test, and the brace-group parse


# ------------------------------------------------------------------------------------------------ word classes
def test_a_plain_word_is_literal():
    arg = _words("echo /opt/x/tool\n")[0]
    assert arg.literal == "/opt/x/tool" and arg.is_plain and not arg.quoted and not arg.expands


@pytest.mark.parametrize("text,literal", [
    ('"a b"', "a b"), ("'c$d'", "c$d"), (r"e\ f", "e f"), (r'"/opt/team'"'"'s/tool"', "/opt/team's/tool"),
    (r'"/opt/x/to\ol"', r"/opt/x/to\ol"), (r'"/opt/x/tool\""', '/opt/x/tool"'), ("a'b'c", "abc"), ('""', ""),
])
def test_quoting_is_resolved_by_ash_itself(text, literal):
    word = _words(f"echo {text}\n")[0]
    assert word.literal == literal and word.is_plain and word.quoted == any(c in text for c in "'\"\\")


@pytest.mark.parametrize("text,kinds", [
    ("$X", ("var",)), ("${X}", ("var",)), ('"$X"', ("var",)), ("/opt/$MODEL/tool", ("var",)),
    ("${X:-/opt/x}", ("var",)), ("/opt/x/tool$((1+1))", ("arith",)), ("$(uname -m)", ("cmd",)),
    ("`uname -m`", ("cmd",)), ("/opt/$(uname -m)/t$((2))", ("cmd", "arith")), ('"/opt/${A}/x"', ("var",)),
])
def test_every_expansion_makes_the_word_not_literal(text, kinds):
    word = _words(f"echo {text}\n")[0]
    assert word.literal is None and word.expands == kinds and not word.is_plain


@pytest.mark.parametrize("text", ["/opt/*/tool", "/opt/x/to?l", "/opt/x/[a]ool", "*.sh"])
def test_an_unquoted_glob_is_not_plain_but_a_quoted_one_is(text):
    assert _words(f"echo {text}\n")[0].glob and not _words(f"echo {text}\n")[0].is_plain
    assert _words(f"echo '{text}'\n")[0].is_plain


def test_a_leading_tilde_is_not_plain_unless_quoted():
    assert _words("echo ~/x\n")[0].tilde and not _words("echo ~/x\n")[0].is_plain
    assert _words("echo '~/x'\n")[0].is_plain and _words("echo a~b\n")[0].is_plain


def test_whitespace_in_a_variable_value_is_not_hidden_in_a_word():
    cmds = list(ash_dump.simple_commands(ash_dump.dump("RUN='/opt/x/tool start'\n$RUN\n\"$RUN\"\n")))
    assign = cmds[0]["assign"][0]
    assert assign.literal == "RUN=/opt/x/tool start"
    assert [c["args"][0].expands for c in cmds[1:]] == [("var",), ("var",)]
    assert [c["args"][0].quoted for c in cmds[1:]] == [False, True]      # field splitting applies to the first only


def test_command_substitutions_are_parsed_and_listed_in_order():
    cmd = next(ash_dump.simple_commands(ash_dump.dump("touch $(a1 x) $(b2 y)\n")))
    subs = [w.subs for w in cmd["args"][1:]]
    inner = [[c["args"][0].literal for c in ash_dump.simple_commands(list(s))] for s in subs]
    assert inner == [["a1"], ["b2"]]


# ------------------------------------------------------------------------------------------ the tree structure
SCRIPT = """#!/bin/sh
PATH=/usr/bin:/bin
f() { command /opt/x/h "$@" >/dev/null 2>&1 || exit 1; }
case "$1" in
  start) /opt/x/tool a ;;
  stop|restart) kill 1; : ;;
esac
cat <<EOF2
$(inner1 arg)
EOF2
(cd / && /opt/y/z) &
if [ -x /a ] && /b; then :; else /c; fi
for d in /x /y; do /opt/z "$d"; done
while false; do :; done | wc -l
! /bin/false
"""


EXPECTED_HEADS = ["command", "exit", "/opt/x/tool", "kill", ":", "cat", "inner1", "cd", "/opt/y/z", "[", "/b", ":",
                  "/c", "/opt/z", "false", ":", "wc", "/bin/false"]


def test_every_compound_form_is_walked_and_every_simple_command_found():
    trees = ash_dump.dump(SCRIPT)
    assert [t["t"] for t in trees] == ["cmd", "defun", "case", "cmd", "bg", "if", "for", "pipe", "not"]
    heads = [c["args"][0].literal for c in ash_dump.simple_commands(trees) if c["args"]]
    assert heads == EXPECTED_HEADS, heads
    assert [c["line"] for c in ash_dump.simple_commands(trees) if c["args"] and c["args"][0].literal == "/opt/y/z"] == [11]


def test_a_function_definition_is_a_node_with_its_name_and_body():
    [fn] = [t for t in ash_dump.dump(SCRIPT) if t["t"] == "defun"]
    assert fn["name"] == "f" and fn["line"] == 3
    assert [c["args"][0].literal for c in ash_dump.simple_commands(fn)] == ["command", "exit"]


def test_redirections_are_listed_with_their_target_words():
    [cmd] = [c for c in ash_dump.simple_commands(ash_dump.dump(SCRIPT)) if c["args"] and c["args"][0].literal == "command"]
    assert [r["to"].literal for r in cmd["redir"] if "to" in r] == ["/dev/null"]
    assert [r.get("dup") for r in cmd["redir"] if "dup" in r] == [1]


def test_a_here_document_body_with_a_substitution_is_parsed_not_skipped():
    cmds = [c["args"][0].literal for c in ash_dump.simple_commands(ash_dump.dump("cat <<EOF\n$(touch /x)\nEOF\n"))]
    assert cmds == ["cat", "touch"]


def test_a_quoted_here_document_delimiter_makes_the_body_text():
    cmds = [c["args"][0].literal for c in ash_dump.simple_commands(ash_dump.dump("cat <<'EOF'\n$(touch /x)\nEOF\n"))]
    assert cmds == ["cat"]


def test_prefix_assignments_are_separate_from_the_command_words():
    [cmd] = ash_dump.simple_commands(ash_dump.dump("A=1 PATH=/opt/bin /opt/bin/run x\n"))
    assert [w.literal for w in cmd["assign"]] == ["A=1", "PATH=/opt/bin"]
    assert [w.literal for w in cmd["args"]] == ["/opt/bin/run", "x"]


# ---------------------------------------------------------------------------------------------- never executes
# Every planted command is a shell builtin redirecting into the fixture directory, so it needs no PATH and no
# applet: if the evaluator reached it, the file would appear.
PLANT = """#!/bin/sh
: > {p}/A
echo $(: > {p}/B) `: > {p}/C` "$(: > {p}/D)"
f() {{ : > {p}/E; }}
f
cat <<EOF
$(: > {p}/F)
EOF
( : > {p}/G ) &
x=$(: > {p}/H)
: $((0))
if : > {p}/I; then : > {p}/J; fi
"""
PLANTED = set("ABCDEFGHIJ")


def test_the_plant_does_create_its_markers_when_the_same_script_is_executed(binary, tmp_path):
    """Control: run (not parse) the plant with the built shell; every marker appears, so an empty directory after
    the parse means the parse did not run them."""
    script = tmp_path / "plant.sh"
    script.write_text(PLANT.format(p=tmp_path))
    subprocess.run([str(binary), "sh", str(script)], capture_output=True, check=False, cwd=tmp_path, timeout=30,
                   env={"PATH": "/nonexistent"})
    assert {p.name for p in tmp_path.iterdir()} >= PLANTED


def test_parsing_runs_nothing_without_the_sandbox(tmp_path, monkeypatch):
    _ci_optout(monkeypatch)
    trees = ash_dump.dump(PLANT.format(p=tmp_path))
    assert ash_dump.LAST_MODE == "unsandboxed"
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    seen = {r["to"].literal[-1] for c in ash_dump.simple_commands(trees) for r in c["redir"]
            if "to" in r and r["to"].literal and r["to"].literal.startswith(str(tmp_path))}
    assert seen == PLANTED                                              # every planted command was SEEN, none run


def test_the_sandboxed_parse_is_the_bwrap_path(tmp_path, monkeypatch):
    if ash_dump._bwrap_usable() is None:
        pytest.skip("DS-P3.350: user namespaces are refused here (the Forgejo runner's container, GitHub Actions); "
                    "this bwrap leg runs in the land retest on the bench host, not in the commit hook")
    monkeypatch.delenv(ash_dump.UNSANDBOXED_ENV, raising=False)
    assert ash_dump.dump(PLANT.format(p=tmp_path))
    assert ash_dump.LAST_MODE == "bwrap"


def test_inside_the_sandbox_the_parse_creates_nothing_and_the_same_script_executed_does(binary, tmp_path):
    """The witness is a writable directory INSIDE the sandbox (a tmpfs the plant targets); the control runs the same
    script under the same mounts and must create every marker, the parse must create none."""
    bwrap = ash_dump._bwrap_usable()
    if bwrap is None:
        pytest.skip("DS-P3.350: user namespaces are refused here (the Forgejo runner's container, GitHub Actions); "
                    "this bwrap leg runs in the land retest on the bench host, not in the commit hook")
    script = tmp_path / "plant.sh"
    script.write_text(PLANT.format(p="/tmp"))
    check = "for m in A B C D E F G H I J; do test -e /tmp/$m && n=$((n+1)); done; echo ${n:-0}"
    prefix = ash_dump._bwrap_prefix(bwrap, binary, script)

    def markers(run: str) -> int:
        out = subprocess.run(prefix + ["/busybox", "sh", "-c", run + "; " + check], capture_output=True, text=True,
                             check=False, timeout=60)
        return int(out.stdout.strip().splitlines()[-1])

    assert markers("/busybox sh /script.sh") == 10                    # control: executing creates them all
    assert markers("DS_ASH_DUMP=1 /busybox sh -n /script.sh >/dev/null") == 0


def test_without_bwrap_and_without_the_opt_out_the_parse_refuses(monkeypatch):
    monkeypatch.setattr(ash_dump, "_bwrap_usable", lambda: None)
    monkeypatch.delenv(ash_dump.UNSANDBOXED_ENV, raising=False)
    with pytest.raises(ash_dump.AshDumpError, match="bubblewrap"):
        ash_dump.dump("echo hi\n")


def test_with_the_ci_opt_out_the_parse_runs_and_says_it_was_unsandboxed(monkeypatch, capsys):
    monkeypatch.setattr(ash_dump, "_bwrap_usable", lambda: None)
    _ci_optout(monkeypatch)
    assert ash_dump.dump("echo hi\n")
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "ran UNSANDBOXED" in err and ash_dump.UNSANDBOXED_VALUE in err


@pytest.mark.parametrize("ci,value", [("true", "1"), ("true", "yes"), (None, "ci-no-userns"), ("false", "ci-no-userns")])
def test_the_opt_out_is_refused_outside_ci_or_with_any_other_value(monkeypatch, ci, value):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv(ash_dump.UNSANDBOXED_ENV, value)
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    with pytest.raises(ash_dump.AshDumpError, match="accepted value"):
        ash_dump.dump("echo hi\n")


# ---------------------------------------------------------------------------------------------------- errors
@pytest.mark.parametrize("script", ["if true; then\n", "echo 'unclosed\n", "case x in\n", "echo $(\n", "f() {\n"])
def test_a_script_ash_refuses_raises_and_is_never_skipped(script, monkeypatch):
    _ci_optout(monkeypatch)
    with pytest.raises(ash_dump.AshSyntaxError):
        ash_dump.dump(script)


def test_a_missing_pinned_tarball_is_a_loud_error(tmp_path, monkeypatch):
    monkeypatch.setenv(ash_dump.CACHE_ENV, str(tmp_path))
    monkeypatch.setenv(ash_dump.TARBALL_ENV, str(tmp_path / "wrong.tar.bz2"))
    (tmp_path / "wrong.tar.bz2").write_bytes(b"not busybox")
    with pytest.raises(ash_dump.AshDumpError, match="pinned"):
        ash_dump.build()


# ------------------------------------------------------------------------------- what the dump must keep
def test_the_braces_of_a_group_are_kept_so_a_group_is_not_the_top_level(monkeypatch):
    _ci_optout(monkeypatch)
    [group] = ash_dump.dump("{ PATH=/opt/bin; }\n")
    [plain] = ash_dump.dump("PATH=/opt/bin\n")
    assert group["t"] == "redir" and group["redir"] == [] and plain["t"] == "cmd"
    assert [c["assign"][0].literal for c in ash_dump.simple_commands(group)] == ["PATH=/opt/bin"]


def test_a_script_of_comments_is_an_empty_tree_not_an_error():
    assert ash_dump.dump("# nothing\n") == [] and ash_dump.dump("") == []


@pytest.mark.parametrize("script", ["cat <<EOF\nbody\n", "echo a <<'X'\n"])
def test_a_script_that_swallows_the_end_marker_is_refused(script):
    with pytest.raises(ash_dump.AshDumpError, match="marker"):
        ash_dump.dump(script)


def test_a_binary_that_is_not_the_recorded_build_is_refused(tmp_path, monkeypatch, binary):
    stock = tmp_path / "stock"
    stock.mkdir()
    monkeypatch.setenv(ash_dump.CACHE_ENV, str(stock))
    slot = stock / ash_dump.build_key()
    slot.mkdir()
    (slot / "busybox").write_bytes(binary.read_bytes() + b"\0")            # not the recorded build
    (slot / "busybox.sha256").write_text(hashlib.sha256(binary.read_bytes()).hexdigest() + "\n")
    with pytest.raises(ash_dump.AshDumpError, match="checksum"):
        ash_dump.build()


def test_a_carriage_return_and_non_utf8_bytes_reach_ash_unchanged():
    [cmd] = ash_dump.simple_commands(ash_dump.dump(b"echo '/opt/a\rb\xff'\n"))
    assert cmd["args"][1].literal == "/opt/a\rb\udcff"


@pytest.mark.parametrize("text", ["PATH=~/bin", "PATH=/a:~/bin", "~/x", "A=~", "export P=/a:~/b"])
def test_a_tilde_in_an_assignment_or_word_start_is_not_plain(text):
    [cmd] = ash_dump.simple_commands(ash_dump.dump(text + "\n"))
    word = (cmd["assign"] or cmd["args"])[-1]
    assert word.tilde and not word.is_plain


def test_a_quoted_tilde_after_equals_is_plain():
    [cmd] = ash_dump.simple_commands(ash_dump.dump("PATH='~/bin'\n"))
    assert cmd["assign"][0].is_plain


@pytest.mark.parametrize("word", ["[", "/opt/x/tool[", "/opt/x/pa:~th", "a]b", "my[file*"[:-1]])
def test_a_literal_word_ash_would_not_expand_stays_plain(word):
    """ash globs [ only when a closing ] follows, and expands ~ after a colon only in an assignment."""
    [cmd] = ash_dump.simple_commands(ash_dump.dump(f"{word}\n"))
    assert cmd["args"][0].is_plain, word


@pytest.mark.parametrize("word", ["/opt/[ab]/tool", "/opt/x/to*", "/opt/x/t?"])
def test_a_closed_bracket_or_wildcard_is_a_glob(word):
    [cmd] = ash_dump.simple_commands(ash_dump.dump(f"{word}\n"))
    assert cmd["args"][0].glob and not cmd["args"][0].is_plain


@pytest.mark.parametrize("tail", ["/opt/x/tool &&", "/opt/x/tool |"])
def test_a_script_ending_inside_a_command_never_loses_that_command(tail):
    """The marker proves the END of the script was parsed; a trailing command that absorbs it is refused, not
    dropped."""
    with pytest.raises(ash_dump.AshDumpError):
        ash_dump.dump("echo before\n" + tail)


def test_the_sandbox_has_no_host_files_no_network_and_no_environment(binary, tmp_path):
    bwrap = ash_dump._bwrap_usable()
    if bwrap is None:
        pytest.skip("DS-P3.350: user namespaces are refused here (the Forgejo runner's container, GitHub Actions); "
                    "this bwrap leg runs in the land retest on the bench host, not in the commit hook")
    script = tmp_path / "s.sh"
    script.write_text(": \n")
    # builtins only (the build has no standalone applets): only the loopback line may be in /proc/net/dev
    probe = ('test ! -e /home && test ! -e /etc/passwd && test -z "$HOME" && n=0 && '
             'while read l; do n=$((n+1)); done < /proc/net/dev && test $n -le 3 && echo SANDBOX-OK')
    out = subprocess.run(ash_dump._bwrap_prefix(bwrap, binary, script) + ["/busybox", "sh", "-c", probe],
                         capture_output=True, text=True, check=False, timeout=30)
    assert out.stdout.strip() == "SANDBOX-OK", out
    # and the production argv is that same prefix, nothing weaker
    assert ash_dump._bwrap_prefix(bwrap, binary, script)[1:4] == ["--unshare-all", "--die-with-parent", "--new-session"]


def test_the_child_output_limit_really_stops_a_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(ash_dump, "MAX_DUMP_BYTES", 4096)
    target = tmp_path / "big"
    with target.open("wb") as fh:
        subprocess.run(["sh", "-c", "head -c 100000 /dev/zero"], stdout=fh, check=False,
                       preexec_fn=ash_dump._cap_output)
    assert target.stat().st_size <= 4096


def test_dump_installs_the_child_output_limit(monkeypatch):
    _ci_optout(monkeypatch)
    seen = {}
    real = subprocess.run

    def spy(*a, **kw):
        seen["preexec"] = kw.get("preexec_fn")
        return real(*a, **kw)

    monkeypatch.setattr(ash_dump.subprocess, "run", spy)
    ash_dump.dump("echo hi\n")
    assert seen["preexec"] is ash_dump._cap_output


def test_an_oversized_tree_is_refused(monkeypatch):
    _ci_optout(monkeypatch)
    monkeypatch.setattr(ash_dump, "MAX_DUMP_BYTES", 4096)
    with pytest.raises(ash_dump.AshDumpError):
        ash_dump.dump("echo " + "a " * 4000 + "\n")


def test_a_deeply_chained_line_is_a_controlled_refusal_not_a_recursion_error(monkeypatch):
    _ci_optout(monkeypatch)
    with pytest.raises(ash_dump.AshDumpError, match="deeply"):
        ash_dump.dump("; ".join([":"] * 1500) + "\n")


def test_a_final_backslash_without_a_newline_is_refused_by_name_and_with_one_it_parses(monkeypatch):
    _ci_optout(monkeypatch)
    with pytest.raises(ash_dump.AshDumpError, match="ends in a backslash"):
        ash_dump.dump("echo a \\")
    assert ash_dump.dump("echo a \\\\")                              # an even run is text, not a continuation


def test_an_assignment_is_not_a_glob_but_a_command_word_is(monkeypatch):
    [cmd] = ash_dump.simple_commands(ash_dump.dump("PATH=/opt/*/bin\n"))
    assert cmd["assign"][0].is_plain and cmd["assign"][0].literal == "PATH=/opt/*/bin"
    [cmd] = ash_dump.simple_commands(ash_dump.dump("/opt/*/bin\n"))
    assert cmd["args"][0].glob


@pytest.mark.parametrize("word,glob", [("/opt/a[/b]/tool", False), ("/opt/a[b]/tool", True), ("a]b[", False)])
def test_a_bracket_pair_globs_only_within_one_path_component(word, glob):
    [cmd] = ash_dump.simple_commands(ash_dump.dump(word + "\n"))
    assert cmd["args"][0].glob is glob


# ----------------------------------------------------------------- the record of what the image's busybox is
RECORD = json.loads((Path(ash_dump.__file__).parent / "ash_dump" / "build_record.json").read_text(encoding="utf-8"))


def test_the_build_record_names_the_files_this_tool_pins():
    build = RECORD["image_build"]
    assert build["busybox_config_sha256"] == ash_dump.CONFIG_SHA256
    assert build["source_tarball_sha256"] == ash_dump.TARBALL_SHA256 and build["busybox_version"] == "1.36.1"
    assert build["BR2_PACKAGE_BUSYBOX_CONFIG_FRAGMENT_FILES"] == ""      # no fragment changes an option
    assert len(RECORD["buildroot_patches_to_busybox"]) == 16


def test_only_one_buildroot_patch_touches_ash_and_only_a_comment(tmp_path):
    legal = Path.home() / ".cache" / "ds208-br" / "legal-info"
    if not (legal / "buildroot.config").is_file():
        pytest.skip("the image build's legal-info is not on this machine")
    assert hashlib.sha256((legal / "buildroot.config").read_bytes()).hexdigest() == \
        RECORD["image_build"]["buildroot_config_sha256"]
    touching = []
    for name, sha in RECORD["buildroot_patches_to_busybox"].items():
        patch = legal / "sources" / "busybox-1.36.1" / name
        assert hashlib.sha256(patch.read_bytes()).hexdigest() == sha, name
        if "+++ b/shell/ash.c" in patch.read_text(encoding="utf-8", errors="replace"):
            touching.append(name)
            hunk = patch.read_text(encoding="utf-8", errors="replace").split("+++ b/shell/ash.c", 1)[1]
            hunk = hunk.split("diff --git", 1)[0]
            changed = [ln for ln in hunk.splitlines() if re.match(r"^[+-](?![+-])", ln)]
            assert changed and all(ln[1:].lstrip().startswith("*") for ln in changed), changed   # comment lines only
    assert touching == ["0008-shell-fix-SIGWINCH-and-SIGCHLD-in-hush-interrupting-.patch"]


def test_a_command_ending_in_a_continuation_line_is_kept_with_the_marker_separate(monkeypatch):
    _ci_optout(monkeypatch)
    heads = [c["args"][0].literal for c in ash_dump.simple_commands(
        ash_dump.dump("echo b\n: ds-ash-dump-end-8d0b1f; /opt/x/tool \\\n"))]
    assert heads == ["echo", ":", "/opt/x/tool"]


# ------------------------------------------------------------- which CI may skip: GitHub proper only
FORGEJO = {"CI": "true", "GITHUB_ACTIONS": "true", "FORGEJO_ACTIONS": "true"}
GITHUB = {"CI": "true", "GITHUB_ACTIONS": "true"}
OPT_OUT = {ash_dump.UNSANDBOXED_ENV: ash_dump.UNSANDBOXED_VALUE}


def test_a_forgejo_job_without_the_opt_out_and_without_bwrap_fails_not_skips():
    assert parse_environment(FORGEJO, False) == "fail"
    assert parse_environment({**FORGEJO, "FORGEJO_ACTIONS": ""} | {"GITEA_ACTIONS": "true"}, False) == "fail"
    assert parse_environment({"CI": "true"}, False) == "fail"            # some other CI


def test_a_forgejo_job_with_the_opt_out_or_with_bwrap_runs():
    assert parse_environment({**FORGEJO, **OPT_OUT}, False) == "ok"
    assert parse_environment(FORGEJO, True) == "ok"


def test_github_proper_without_the_opt_out_skips_with_the_named_reason():
    assert parse_environment(GITHUB, False) == "skip"
    assert "GitHub Actions" in SKIP_REASON and ash_dump.UNSANDBOXED_ENV in SKIP_REASON


def test_off_ci_without_bwrap_is_left_to_the_harness_refusal():
    assert parse_environment({}, False) == "ok"                          # dump() itself then refuses (tested above)


def test_the_forgejo_workflow_still_sets_the_opt_out_for_the_desk_tests():
    text = (Path(__file__).resolve().parents[2] / ".forgejo" / "workflows" / "ci.yml")
    if not text.is_file():
        pytest.skip("the exported tree has no .forgejo workflow here")
    assert f'{ash_dump.UNSANDBOXED_ENV}: "{ash_dump.UNSANDBOXED_VALUE}"' in text.read_text(encoding="utf-8")
