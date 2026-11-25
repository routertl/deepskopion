# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DS-P3.341 part 2: the init-script scan reads each script's parse tree from the image's own BusyBox ash.

The DS-P3.316 review rounds 8-11 and the DS-P3.342 rounds 1-3 found the hand-written lexical scanner reading text
the shell does not read: a quote or backslash dropped, $(( )) folded to 0, a here-document body skipped, a
compound command inside a substitution given up on. Every case here is red on that scanner and green on the parse.
A WARN stays a WARN: it is reported in the scan result and is never counted as a pass.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("de25_scan_fixture", HERE / "test_de25_no_ssh_ds_p3_316.py")
_base = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _base
_spec.loader.exec_module(_base)


def _check(tmp_path, script, files=("bin/cat", "bin/sh", "bin/uname")):
    held = {rel: "" for rel in files}
    tree = _base._tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + script, **held}, executable=tuple(files))
    warnings: list[str] = []
    return _base._tool().init_script_missing_binaries(tree, warnings=warnings), warnings


# ----------------------------------------------------------------------------- structure the lexer lost
@pytest.mark.parametrize("script", [
    "true &&\n  missing-tool\n",                          # a line ending in && continues on the next
    "false ||\n  missing-tool\n",
    "echo x |\n  missing-tool\n",
    ">/dev/null <&0 missing-tool\n",                      # redirections before the command word
    "cat 2>&1 </dev/null; missing-tool\n",
    "case x in x) missing-tool ;; esac\n",
    "exec missing-tool\n",                                # exec is a prefix, the program after it is judged
    "if true; then missing-tool; fi\n",
    "for d in a b; do missing-tool; done\n",
    "while false; do missing-tool; done\n",
    "{ missing-tool; }\n",
    "( missing-tool )\n",
    "! missing-tool\n",
    "missing-tool &\n",
    "f() { :; }; missing-tool\n",
    "99\n",                                               # a numeric command name is a command
    "'if'\n",                                             # a quoted reserved word is a command word
    "\"missing-tool\"\n",
    "'missing-tool'\n",
    "missing\\-tool\n",                                   # an escaped hyphen is the same word
])
def test_the_command_the_shell_runs_is_judged(tmp_path, script):
    problems, warnings = _check(tmp_path, script)
    expected = {"99\n": "99", "'if'\n": "if"}.get(script, "missing-tool")
    assert problems == [f"S99x: {expected} (not in PATH)"], (problems, script)
    assert warnings == [], (warnings, script)


def test_a_function_defined_in_a_subshell_is_not_in_scope_outside_it(tmp_path):
    problems, _ = _check(tmp_path, "( missing_tool() { :; } )\nmissing_tool\n")
    assert problems == ["S99x: missing_tool (not in PATH)"]


def test_a_function_called_after_it_is_defined_is_not_looked_up(tmp_path):
    problems, _ = _check(tmp_path, "missing_tool() { :; }\nmissing_tool\n")
    assert problems == []


def test_a_function_body_sees_a_function_defined_after_it_but_before_the_call(tmp_path):
    problems, _ = _check(tmp_path, "outer() { inner; }\ninner() { :; }\nouter\n")
    assert problems == []


# ------------------------------------------------------------ substitutions, here-documents, escapes
@pytest.mark.parametrize("script", [
    'OUT="$(case x in x) /usr/bin/missing-tool ;; esac)"\n',        # a compound command inside a substitution
    'OUT="$(if true; then /usr/bin/missing-tool; fi)"\n',
    'OUT="$( (/usr/bin/missing-tool) )"\n',
    "OUT=`/usr/bin/missing-tool`\n",
    "echo \"$(/usr/bin/missing-tool)\"\n",
    "echo \"$(echo \"$(/usr/bin/missing-tool)\")\"\n",              # nested, quotes inside a substitution
    "echo $(echo ')' ; /usr/bin/missing-tool)\n",                  # a quoted ) does not end it
    "cat <<EOF\n$(/usr/bin/missing-tool)\nEOF\n",                  # a here-document body is read for substitutions
    "cat <<EOF\n`/usr/bin/missing-tool`\nEOF\n",
    "echo x\\ # ; /usr/bin/missing-tool\n",                        # an escaped blank keeps # inside the word
    "cat <<EOF\n  EOF\n$(/usr/bin/missing-tool)\nEOF\n",            # an indented EOF is body text; the body goes on
])
def test_a_program_the_shell_runs_inside_these_forms_is_judged(tmp_path, script):
    problems, warnings = _check(tmp_path, script)
    assert problems == ["S99x: /usr/bin/missing-tool"], (problems, script)
    assert warnings == [], (warnings, script)       # judged, not given up on: no leftover give-up WARN


@pytest.mark.parametrize("script", [
    "cat <<'EOF'\n$(/usr/bin/missing-tool)\nEOF\n",                # a quoted delimiter makes the body text
    "echo '$(/usr/bin/missing-tool)'\n",                           # nothing inside single quotes runs
    "echo x # $(/usr/bin/missing-tool)\n",                         # a comment
])
def test_text_that_does_not_run_is_not_judged(tmp_path, script):
    problems, warnings = _check(tmp_path, script)
    assert problems == [] and warnings == [], (problems, warnings, script)


def test_a_here_document_run_by_a_shell_is_a_warn_not_a_pass(tmp_path):
    problems, warnings = _check(tmp_path, "sh <<EOF\n/usr/bin/missing-tool\nEOF\n")
    assert problems == [] and warnings == ["S99x:2: a here-document run by sh (its body is not judged)"]


def test_a_process_substitution_is_a_warn(tmp_path):
    """It runs: the commands inside are judged, and the construct is reported as one WARN as well."""
    problems, warnings = _check(tmp_path, "cat <(/usr/bin/missing-tool)\n")
    assert problems == ["S99x: /usr/bin/missing-tool"]
    assert warnings == ["S99x:2: a process substitution (not judged)"]


# ------------------------------------------------------------------ the DS-P3.342 corpus, on the parse
def test_arithmetic_is_evaluated_by_the_shell_so_a_path_holding_it_is_computed(tmp_path):
    """The lexer folded $((1+1)) to 0 and judged tool0; the shell runs tool2."""
    problems, warnings = _check(tmp_path, '"/opt/x/tool$((1+1))"\n', files=("bin/cat", "bin/sh", "opt/x/tool0"))
    assert problems == [] and len(warnings) == 1 and "computed" in warnings[0], (problems, warnings)


def test_a_lone_arithmetic_constant_in_a_bin_path_is_judged_as_that_number(tmp_path):
    """$((0)) is 0: the file the shell runs is helper0, so held passes and missing refuses (as the 342 test pins)."""
    held = _check(tmp_path / "held", '"/usr/bin/helper$((0))"\n', files=("bin/cat", "bin/sh", "usr/bin/helper0"))
    missing = _check(tmp_path / "missing", '"/usr/bin/helper$((0))"\n')
    assert held == ([], []) and missing[0] == ["S99x: /usr/bin/helper0"], (held, missing)


def test_an_arithmetic_expression_in_a_bin_path_is_computed_not_folded_to_zero(tmp_path):
    """$((1+1)) is 2: with helper0 present and helper2 absent, the hand lexer folded it to 0 and passed. The
    parse cannot name the file, so it is one computed WARN (never a pass on helper0, never a guessed refusal)."""
    problems, warnings = _check(tmp_path, '"/usr/bin/helper$((1+1))"\n', files=("bin/cat", "bin/sh", "usr/bin/helper0"))
    assert problems == [] and len(warnings) == 1 and "computed" in warnings[0], (problems, warnings)


@pytest.mark.parametrize("script", ['"/opt/x/to\\ol"\n', '"/opt/team\'s/tool"\n', "/opt/x/tool\\ name\n",
                                    "/opt/'x y'/tool\n", '"/opt/x/tool\\""\n'])
def test_a_path_with_a_quote_a_backslash_or_a_blank_is_the_text_the_shell_resolves(tmp_path, script):
    """The lexer dropped the backslash or the quote and judged a path the shell never runs; the parse hands over
    the resolved word, so a character outside the plain-name set makes it a WARN, never a refusal."""
    problems, warnings = _check(tmp_path, script, files=("bin/cat", "bin/sh", "opt/x/tool", "opt/teams/tool"))
    assert problems == [] and len(warnings) == 1 and "not judged" in warnings[0], (problems, warnings)


@pytest.mark.parametrize("script", ["/opt/./x/tool\n", "/opt/x/../x/tool\n", "/opt//x/tool\n"])
def test_a_dot_segment_is_a_warn(tmp_path, script):
    problems, warnings = _check(tmp_path, script, files=("bin/cat", "bin/sh", "opt/x/tool"))
    assert problems == [] and len(warnings) == 1 and "./.. segment" in warnings[0], (problems, warnings)


@pytest.mark.parametrize("script", ["/opt/*/tool\n", "/usr/bin/*\n", "/usr/bin/t?ol\n", "/opt/[ab]/tool\n"])
def test_a_glob_in_a_command_path_is_a_warn_even_under_a_bin_directory(tmp_path, script):
    problems, warnings = _check(tmp_path, script, files=("bin/cat", "bin/sh", "opt/a/tool", "usr/bin/tool"))
    assert problems == [] and len(warnings) == 1 and "computed" in warnings[0], (problems, warnings)


def test_a_bracket_with_no_closing_one_is_a_literal_character_outside_the_plain_set_so_a_warn(tmp_path):
    """ash does not glob it (the word is the text it spells), but a [ is not a plain-name character, so the
    allow-list reports it instead of judging a file of that name."""
    problems, warnings = _check(tmp_path, "/usr/bin/missing[\n")
    assert problems == [] and len(warnings) == 1 and "computed" in warnings[0], (problems, warnings)


@pytest.mark.parametrize("script,expect", [("~/tool\n", "not a literal name or absolute path"),
                                           ("tool*\n", "not a literal name or absolute path")])
def test_a_tilde_or_a_glob_in_a_bare_command_word_is_a_warn(tmp_path, script, expect):
    problems, warnings = _check(tmp_path, script)
    assert problems == [] and len(warnings) == 1 and expect in warnings[0], (problems, warnings)


# ------------------------------------------------------------------------------------ what stays the same
def test_command_dash_v_runs_nothing_and_command_p_is_a_warn(tmp_path):
    assert _check(tmp_path, "command -v missing-tool\n") == ([], [])
    problems, warnings = _check(tmp_path, "command -p missing-tool\n")
    assert problems == [] and warnings == ["S99x:2: command -p (not judged)"]


def test_a_script_ash_refuses_is_an_error_not_a_skip(tmp_path):
    tree = _base._tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\nif true; then\n"})
    with pytest.raises(RuntimeError, match="cannot inspect"):
        _base._tool().init_script_missing_binaries(tree)


def test_a_warn_is_reported_in_the_result_and_never_makes_a_problem(tmp_path):
    problems, warnings = _check(tmp_path, 'eval "$X"\n. /etc/x.conf\n')
    assert problems == [] and len(warnings) == 2


# --------------------------------------------------------------------------- nothing is executed
def test_scanning_a_script_with_planted_commands_creates_nothing(tmp_path):
    """The scan only parses: builtin redirections into a directory the test owns are SEEN as commands and none
    runs (ash_dump's own tests hold the positive control that the same plant creates its markers when run)."""
    plant = tmp_path / "plant"
    plant.mkdir()
    script = (f": > {plant}/A\necho $(: > {plant}/B) `: > {plant}/C`\nf() {{ : > {plant}/D; }}\nf\n"
              f"cat <<EOF\n$(: > {plant}/E)\nEOF\n")
    problems, _ = _check(tmp_path / "image", script + "/usr/bin/ds341_reached_marker\n")
    assert sorted(p.name for p in plant.iterdir()) == []
    assert problems == ["S99x: /usr/bin/ds341_reached_marker"]      # the scan read the whole planted script


def test_the_module_imports_from_a_clone_that_holds_only_its_declared_siblings(tmp_path):
    """The golden-path provider imports tools/de25_linux_image.py by path from a clone that stages a few repo files;
    importing must not need ash_dump.py (it is loaded when a script is first scanned), or the provider's stager
    comes back None (11 provider tests red on the first part-2 commit)."""
    tools = tmp_path / "tools"
    tools.mkdir()
    src = _base.ROOT / "tools"
    for name in ("de25_linux_image.py", "de25_toolchain_stage.py"):
        (tools / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("de25_clone_import", tools / "de25_linux_image.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module._stage_target_stem_sof)
