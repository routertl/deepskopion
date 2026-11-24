# SPDX-License-Identifier: MPL-2.0
"""DS-P3.316: the DE25 image ships no SSH files, and no init script that names a
program the image does not hold.

Measured on a DE25 rootfs with debugfs: S49dropbear and deepskopion-authorize-key
shipped, the dropbear binary did not, so S49dropbear failed at every boot.

Scope of the init-script scan (the same words as the tool's docstring and sw/linux/README.md,
checked by test_the_scan_scope_is_worded_identically_everywhere):
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "sw" / "linux" / "rootfs_overlay"
# A finding outside this scope is a WARN line, never a refusal.
SCOPE = (
    "Parsed: each script is parsed by the ash of BusyBox 1.36.1 (tools/ash_dump.py), built from the same "
    "source and with the same ash options as the image's busybox, not the same binary, in a sandbox with "
    "no network; nothing in a script is executed. Judged: every simple command the parse yields, wherever "
    "it sits: lists, pipelines and && / || chains, if, while, until, for and case (patterns included), "
    "subshells ( ), brace groups { }, redirected compound commands, function bodies, and the $( ) and "
    "backtick substitutions (compound commands inside them included) in any word, redirection target, for "
    "list, case pattern or here-document body that the shell expands; the literal command words, quotes, "
    "escapes and line continuations as the shell resolves them; literal NAME=value assignments, prefix "
    "assignments included; a literal PATH assignment at the top level of the file, unconditional only; "
    "function definitions, visible in their own subshell scope, to calls after the definition, and inside "
    "function bodies (command NAME bypasses a function); the -x operand of [ and test; the #! "
    "interpreter; and a command word that is exactly $NAME or ${NAME} for a variable the script assigns, "
    "judged by each literal value assigned. At each judged program position a path under a bin directory, "
    "or any other absolute command word whose every segment is a plain name of letters, digits and . _ + "
    "- (and not . or ..), except the runtime and computed paths named below, must be an executable file "
    "in the image, and a bare name must resolve through the PATH in effect. Not judged, and reported as "
    "one WARN line naming script:line and the construct, and refused by the image gate unless it is on the reviewed accept list: command -p; a PATH "
    "assignment that is conditional or inside a compound command, a subshell, a function body, a pipeline "
    "or the background (it is not applied); a PATH entry that is not a literal absolute directory; an "
    "absolute command path under tmp, run, var, dev, proc, sys, mnt or media (the file is created at "
    "boot), and an absolute command path that is computed (a variable, substitution, arithmetic "
    "expansion, unquoted glob or tilde, or a character outside the plain-name set in it) or has a . or .. "
    "segment; a command word that is not a literal name or absolute path, such as a relative path; a "
    "command word that joins a variable with other text; eval; alias; a here-document fed to a shell; "
    "process substitution; sh -c strings; . and source; a command word held in a variable the script does "
    "not assign or assigns a value that is not a literal; a command word produced by a substitution; and "
    "a program run through a wrapper (env, nice, nohup, setsid, timeout, xargs, chroot, su, flock, or "
    "start-stop-daemon with --exec or --startas), whose own name is judged. A script that ash refuses is "
    "an error, never skipped. What the scan cannot know is what a variable, a substitution or a sourced "
    "file holds when the script runs. An accepted WARN passes with its reason; a clean run is not a proof."
)
SSH_FILES = {Path("etc/init.d/S49dropbear"), Path("usr/local/bin/deepskopion-authorize-key")}
# An overlay judged before the layers merge: the base root file system (rr linux rootfs, BusyBox)
# supplies the init scripts' interpreter and the applets they run by name, so such checks pass
# BASE_PROVIDES and bare=False; the image check judges both on the merged image.
BASE_PROVIDES = frozenset({"/bin/sh"})
APPLETS = ("cat", "chmod", "chown", "cp", "mkdir")      # what the shared S49dropbear runs by name


def _tool():
    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("de25_linux_image_p3_316", ROOT / "tools" / "de25_linux_image.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(tmp_path: Path, files: dict[str, str], executable: tuple[str, ...] = (), *, shell: bool = True) -> Path:
    """An image tree; it holds an executable /bin/sh (the init scripts' interpreter) unless *shell* is False."""
    if shell:
        files = {"bin/sh": "", **files}
        executable = (*executable, "bin/sh")
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755 if rel in executable or rel.startswith("etc/init.d/") else 0o644)
    return tmp_path


@pytest.mark.parametrize("ref", ['DROPBEAR="${ROOT_PREFIX}/usr/sbin/dropbear"\n[ -x "$DROPBEAR" ]\n',
                                 '/usr/sbin/dropbear -R\n', '"$ROOT_PREFIX/usr/sbin/dropbear" -R\n',
                                 'X="${ROOT_PREFIX:-}/usr/sbin/dropbear"\n',
                                 "echo 'state#1'; /usr/sbin/dropbear -R\n",
                                 'if [ -x /usr/sbin/dropbear ]; then exec /usr/sbin/dropbear; fi\n',
                                 'true && /usr/sbin/dropbear -R  # start it\n',
                                 'if /usr/sbin/dropbear -R; then :; fi\n', '! /usr/sbin/dropbear -R\n',
                                 'X=1 /usr/sbin/dropbear -R\n', 'local D="${ROOT_PREFIX}/usr/sbin/dropbear"\n',
                                 '/usr/sbin/dropbear \\\n  -R\n'])
def test_a_script_naming_a_missing_program_is_reported(tmp_path, ref):
    tree = _tree(tmp_path, {"etc/init.d/S49x": "#!/bin/sh\n" + ref})
    assert _tool().init_script_missing_binaries(tree) == ["S49x: /usr/sbin/dropbear"]


def test_a_held_or_provided_program_and_data_paths_are_not_reported(tmp_path):
    tree = _tree(tmp_path, {
        "etc/init.d/S49x": '#!/bin/sh\nD="${ROOT_PREFIX}/usr/sbin/dropbear"\nP="${ROOT_PREFIX}/usr/bin/python3"\n'
                           'L="${ROOT_PREFIX}/var/log/x.log"\nC="${ROOT_PREFIX}/etc/deepskopion/network.conf"\n'
                           '# /usr/sbin/commented-out\n/bin/true # /usr/sbin/after-a-comment\n'
                           'echo "dropbear: /usr/sbin/in-a-message missing"\n'
                           'echo "To restart, exec /usr/sbin/in-a-message -R"\n'
                           'cat /usr/bin/an-argument-not-a-program > /dev/null\n',
        "usr/sbin/dropbear": "", "bin/true": "", "bin/cat": "",
    }, executable=("usr/sbin/dropbear", "bin/true", "bin/cat"))
    tool = _tool()
    assert tool.init_script_missing_binaries(tree) == ["S49x: /usr/bin/python3"]
    assert tool.init_script_missing_binaries(tree, provided=frozenset({"/usr/bin/python3"})) == []


@pytest.mark.parametrize("ref", ["true;(/usr/sbin/dropbear); true\n", "(/usr/sbin/dropbear)\n",
                                 "true&&(/usr/sbin/dropbear -R)||true\n"])
def test_a_program_after_a_combined_operator_is_reported(tmp_path, ref):
    """shlex emits ";(" and ");" as one token; they are operators, not words."""
    tree = _tree(tmp_path, {"etc/init.d/S49x": "#!/bin/sh\n" + ref})
    assert _tool().init_script_missing_binaries(tree) == ["S49x: /usr/sbin/dropbear"]


@pytest.mark.parametrize("line", ['echo ";" /usr/sbin/example\n', "echo ';' /usr/sbin/example\n",
                                  'echo "a;(b" /usr/sbin/example\n'])
def test_a_quoted_operator_is_text_and_rejects_no_valid_image(tmp_path, line):
    tree = _tree(tmp_path, {"etc/init.d/S49x": "#!/bin/sh\n" + line})
    assert _tool().init_script_missing_binaries(tree) == []


def test_a_missing_interpreter_is_reported(tmp_path):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n:\n"}, shell=False)
    assert _tool().init_script_missing_binaries(tree) == ["S99x: /bin/sh (interpreter)"]
    held = _tree(tmp_path / "held", {"etc/init.d/S99x": "#!/bin/sh -e\n:\n"})
    assert _tool().init_script_missing_binaries(held) == []


@pytest.mark.parametrize("line", ["python3 -m sw.stream\n", "(PYTHONUNBUFFERED=1 python3 -m sw.stream) &\n",
                                  'if ! python3 -c "print(1)"; then exit 1; fi\n',
                                  ">/dev/null python3 -c 1\n"])
def test_a_command_by_name_missing_from_the_path_is_reported(tmp_path, line):
    """The shipped S99 starts the daemon as bare `python3`: an image without it must fail."""
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + line})
    assert _tool().init_script_missing_binaries(tree) == ["S99x: python3 (not in PATH)"]
    held = _tree(tmp_path / "held", {"etc/init.d/S99x": "#!/bin/sh\n" + line, "usr/bin/python3": ""},
                 executable=("usr/bin/python3",))
    assert _tool().init_script_missing_binaries(held) == []


def test_builtins_functions_case_patterns_and_arithmetic_are_not_commands(tmp_path):
    script = ("#!/bin/sh\nstart() {\n  i=$((i + 1))\n  command -v udhcpc >/dev/null 2>&1 || true\n"
              '  echo "x" >&2; [ -n "$i" ] && set -e\n}\ncase "$1" in\n  start)   start ;;\n'
              "  restart|reload) start ;;\n  *) echo usage >&2; exit 1 ;;\nesac\n")
    tree = _tree(tmp_path, {"etc/init.d/S99x": script})
    assert _tool().init_script_missing_binaries(tree) == []


@pytest.mark.parametrize("line", [">/dev/null /usr/sbin/dropbear -R\n", "2>&1 /usr/sbin/dropbear -R\n",
                                  "> /dev/null /usr/sbin/dropbear -R\n"])
def test_an_absolute_command_after_a_leading_redirection_is_reported(tmp_path, line):
    tree = _tree(tmp_path, {"etc/init.d/S49x": "#!/bin/sh\n" + line})
    assert _tool().init_script_missing_binaries(tree) == ["S49x: /usr/sbin/dropbear"]


def test_the_shipped_s99_needs_every_command_it_runs_including_inside_substitutions(tmp_path):
    """The shipped S99 runs dirname inside quoted $( ... ). With every name it runs held but one,
    exactly that one is reported."""
    s99 = (SHARED / "etc/init.d/S99deepskopion-stream").read_text(encoding="utf-8")
    names = sorted(_tool()._bare_commands(s99, "S99deepskopion-stream"))
    assert {"dirname", "python3", "start-stop-daemon"} <= set(names), names
    for gone in ("dirname", "python3"):
        held = [n for n in names if n != gone]
        tree = _tree(tmp_path / gone, {"etc/init.d/S99deepskopion-stream": s99, **{f"usr/bin/{n}": "" for n in held}},
                     executable=tuple(f"usr/bin/{n}" for n in held))
        assert _tool().init_script_missing_binaries(tree) == [f"S99deepskopion-stream: {gone} (not in PATH)"]


@pytest.mark.parametrize("line", ['D="$(dirname "$x")"\n', "D=`dirname $x`\n", 'echo "$(dirname "$(readlink -f "$0")")"\n',
                                  'python3 -V; case "$1" in start) : ;; esac\n'])
def test_commands_inside_substitutions_and_before_a_case_are_judged(tmp_path, line):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + line})
    missing = _tool().init_script_missing_binaries(tree)
    assert missing and all(m.endswith("(not in PATH)") for m in missing), missing
    assert any(("dirname" in m) or ("python3" in m) for m in missing), missing


@pytest.mark.parametrize("line", ['D="$(/usr/bin/dirname "$x")"\n', "D=`/usr/bin/dirname $x`\n",
                                  'echo "$(${ROOT_PREFIX}/usr/bin/dirname "$(readlink -f "$0")")"\n'])
def test_an_absolute_path_inside_a_substitution_is_reported(tmp_path, line):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + line, "usr/bin/readlink": ""},
                 executable=("usr/bin/readlink",))
    assert _tool().init_script_missing_binaries(tree) == ["S99x: /usr/bin/dirname"]
    held = _tree(tmp_path / "held", {"etc/init.d/S99x": "#!/bin/sh\n" + line, "usr/bin/readlink": "",
                                     "usr/bin/dirname": ""}, executable=("usr/bin/readlink", "usr/bin/dirname"))
    assert _tool().init_script_missing_binaries(held) == []


@pytest.mark.parametrize("line", ["echo '$(missing-tool)'\n", "echo '`missing-tool`'\n",
                                  "echo 'x $(/usr/sbin/missing) y'\n", "printf '%s' '$(a; missing-tool)'\n"])
def test_a_substitution_inside_single_quotes_is_text(tmp_path, line):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + line})
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == []
    assert warnings == []


def test_the_shipped_s99_with_absolute_paths_needs_each_one(tmp_path):
    """The shipped S99 with its dirname and python3 calls written as absolute paths:
    each absent executable is reported by its path."""
    s99 = (SHARED / "etc/init.d/S99deepskopion-stream").read_text(encoding="utf-8")
    absolute = s99.replace("$(dirname ", "$(/usr/bin/dirname ").replace("python3 -", "/usr/bin/python3 -")
    assert absolute.count("/usr/bin/dirname") >= 2 and absolute.count("/usr/bin/python3 -") >= 3
    names = sorted(_tool()._bare_commands(absolute, "S99deepskopion-stream"))
    assert "dirname" not in names and "python3" not in names, names
    for gone in ("usr/bin/dirname", "usr/bin/python3"):
        held = [f"usr/bin/{n}" for n in names] + [p for p in ("usr/bin/dirname", "usr/bin/python3") if p != gone]
        tree = _tree(tmp_path / gone.rsplit("/", 1)[-1], {"etc/init.d/S99deepskopion-stream": absolute,
                                                          **{p: "" for p in held}}, executable=tuple(held))
        assert _tool().init_script_missing_binaries(tree) == [f"S99deepskopion-stream: /{gone}"]


@pytest.mark.parametrize("line,construct", [
    ('eval "$X"\n', "eval"),
    ("alias ll='ls -l'\n", "alias"),
    ("sh <<EOF\nmissing-in-the-body\nEOF\n", "a here-document run by sh (its body is not judged)"),
    ('sh -c "missing-in-the-string"\n', "sh -c (the command string is not judged)"),
    (". /etc/x.conf\n", ". (a sourced file is not judged)"),
    ("source /etc/x.conf\n", "source (a sourced file is not judged)"),
    ('"$UNSET_CMD" -x\n', "a command word held in '$UNSET_CMD', which the script does not assign"),
    ('$(pick) -x\n', "a command word produced by a substitution"),
    ("env A=1 missing-under-env\n", "a program run through env (its arguments are not judged)"),
    ("start-stop-daemon --start --exec /usr/sbin/x\n",
     "a program run through start-stop-daemon (its arguments are not judged)"),
])
def test_an_unmodelled_construct_is_one_warn_line_and_no_refusal(tmp_path, line, construct):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + line,
                            **{f"bin/{a}": "" for a in ("cat", "sh", "env", "start-stop-daemon", "pick")}},
                 executable=tuple(f"bin/{a}" for a in ("cat", "sh", "env", "start-stop-daemon", "pick")))
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == []
    assert warnings == [f"S99x:2: {construct}"]


def test_a_variable_the_script_assigns_is_judged_where_it_is_assigned(tmp_path):
    script = '#!/bin/sh\nD="${ROOT_PREFIX}/usr/sbin/dropbear"\n"$D" -R\n'
    warnings: list[str] = []
    tree = _tree(tmp_path, {"etc/init.d/S49x": script})
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == ["S49x: /usr/sbin/dropbear"]
    assert warnings == []


def test_the_shipped_scripts_warn_only_where_s99_sources_its_expectation_file():
    """Every shipped init script through the scan: the one construct outside its scope is
    S99's `. "${EXPECT_FILE}"` (an optional operator file of NAME=value expectations)."""
    tool = _tool()
    found = []
    for script in sorted((SHARED / "etc/init.d").glob("S*")):
        found += tool._scan_script(script.read_text(encoding="utf-8"), script.name)[1]
    # Pinned exactly: an edit that brings an unjudged construct into a shipped script turns this red.
    assert found == ["S99deepskopion-stream:140: . (a sourced file is not judged)"], found


def test_check_rootfs_prints_a_warn_and_refuses_it_unless_accepted(tmp_path, capsys):
    """DS-P3.348 reverses this test's old outcome ("prints a WARN and still passes"): a WARN not on
    the reviewed accept list now fails check-rootfs; the accepted case is in the DS-P3.348 tests."""
    tree = _image_tree(tmp_path, False, False)
    (tree / "etc/init.d/S50x").write_text('#!/bin/sh\neval "$X"\n', encoding="utf-8")
    (tree / "bin/sh").parent.mkdir(parents=True, exist_ok=True)
    (tree / "bin/sh").write_text("", encoding="utf-8")
    (tree / "bin/sh").chmod(0o755)
    assert _tool().main(["check-rootfs", str(_ext4(tmp_path, tree))]) != 0
    assert "WARN S50x:2: eval" in capsys.readouterr().err


def test_the_scan_scope_is_worded_identically_everywhere():
    flat = " ".join(SCOPE.split())
    tool = _tool()
    assert tool.INIT_SCRIPT_SCAN_SCOPE == SCOPE
    assert flat in " ".join(tool.init_script_missing_binaries.__doc__.split())
    assert flat in " ".join((ROOT / "sw/linux/README.md").read_text(encoding="utf-8").split())


@pytest.mark.parametrize("body", ["(\n/usr/sbin/missing-tool\n)\n", "(\n  cd /tmp\n  /usr/sbin/missing-tool -R)\n",
                                  "{\n/usr/sbin/missing-tool\n}\n", "case $1 in\n  start)\n    /usr/sbin/missing-tool\n    ;;\nesac\n",
                                  'case "$1" in\n(start) /usr/sbin/missing-tool ;;\nesac\n'])
def test_subshells_and_case_arms_across_lines_are_judged(tmp_path, body):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + body})
    assert _tool().init_script_missing_binaries(tree) == ["S99x: /usr/sbin/missing-tool"]


@pytest.mark.parametrize("body", ['case "$1" in\n(start) : ;;\nesac\n', "case $1 in\n  start|\\\n  restart)\n    :\n    ;;\nesac\n",
                                  "case $1 in\n  start)\n    : ;;\n  (stop|reload) : ;;\n  *) exit 1 ;;\nesac\n",
                                  "case $1 in\n  a) case $2 in\n       b) : ;;\n     esac ;;\nesac\n"])
def test_case_patterns_across_lines_are_not_commands(tmp_path, body):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + body})
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == []
    assert warnings == []


def test_a_command_word_held_in_an_assigned_variable_is_judged_by_its_value(tmp_path):
    script = '#!/bin/sh\nRUN=missing-tool\n"$RUN" -x\n'
    assert _tool().init_script_missing_binaries(_tree(tmp_path, {"etc/init.d/S99x": script})) == [
        "S99x: missing-tool (not in PATH)"]
    held = _tree(tmp_path / "held", {"etc/init.d/S99x": script, "usr/bin/missing-tool": ""},
                 executable=("usr/bin/missing-tool",))
    assert _tool().init_script_missing_binaries(held) == []
    warnings: list[str] = []
    odd = _tree(tmp_path / "odd", {"etc/init.d/S99x": '#!/bin/sh\nRUN="$1"\n$RUN -x\n'})
    assert _tool().init_script_missing_binaries(odd, warnings=warnings) == []
    assert len(warnings) == 1 and "not a literal" in warnings[0], warnings


@pytest.mark.parametrize("with_tool,expected", [(False, 1), (True, 0)])
def test_check_rootfs_judges_a_command_word_held_in_a_variable(tmp_path, capsys, with_tool, expected):
    tree = _image_tree(tmp_path, False, False)
    for rel, text in (("etc/init.d/S50x", '#!/bin/sh\nRUN=missing-tool\n"$RUN" -x\n'), ("bin/sh", "")) + (
            (("usr/bin/missing-tool", ""),) if with_tool else ()):
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text(text, encoding="utf-8")
        (tree / rel).chmod(0o755)
    assert _tool().main(["check-rootfs", str(_ext4(tmp_path, tree))]) == expected
    assert ("S50x: missing-tool (not in PATH)" in capsys.readouterr().err) is (not with_tool)


@pytest.mark.parametrize("script,where,missing", [
    ("PATH=/opt/tools\npresent-tool\n", "usr/bin", ["S99x: present-tool (not in PATH)"]),
    ("export PATH=/opt/tools\npresent-tool\n", "usr/bin", ["S99x: present-tool (not in PATH)"]),
    ("PATH=/opt/tools\npresent-tool\n", "opt/tools", []),
    ('PATH="/opt/tools:$PATH"\npresent-tool\n', "usr/bin", []),
    ("present-tool\nPATH=/opt/tools\n", "usr/bin", []),
    ("PATH=/opt/tools present-tool\npresent-tool\n", "usr/bin", ["S99x: present-tool (not in PATH)"]),
])
def test_a_literal_path_assignment_sets_the_directories_searched(tmp_path, script, where, missing):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + script, f"{where}/present-tool": ""},
                 executable=(f"{where}/present-tool",))
    assert _tool().init_script_missing_binaries(tree) == missing


def test_a_path_entry_that_is_not_literal_is_a_warn(tmp_path):
    warnings: list[str] = []
    tree = _tree(tmp_path, {"etc/init.d/S99x": '#!/bin/sh\nPATH="$TOOLS:/usr/bin"\n:\n'})
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == []
    assert warnings == ["S99x:2: a PATH entry that is not a literal absolute directory ('$TOOLS')"]


@pytest.mark.parametrize("where", ["sbin", "bin", "usr/bin", "usr/local/sbin"])
def test_a_dropbear_binary_anywhere_on_the_path_is_refused(tmp_path, where):
    tree = _image_tree(tmp_path, False, False)
    (tree / where).mkdir(parents=True, exist_ok=True)
    (tree / where / "dropbear").write_text("", encoding="utf-8")
    assert _tool().rootfs_image_problems(_ext4(tmp_path, tree)) == [f"/{where}/dropbear is in the image"]


@pytest.mark.parametrize("script", [
    ">|/dev/null missing-tool\n",                          # >| is one redirection
    "missing-\\\ntool\n",                                 # \-newline joins with nothing between
    'OUT="$(\n  printf hi\n)"\nmissing-tool\n',            # a substitution across lines is one command
    "echo \"it's\" # don't\nmissing-tool\n",                # an apostrophe in a comment opens nothing
    "# don't\nmissing-tool\n",
])
def test_round_8_forms_reach_the_command_the_shell_runs(tmp_path, script):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + script, "opt/tools/missing-tool": "",
                            "usr/bin/missing-": ""}, executable=("opt/tools/missing-tool", "usr/bin/missing-"))
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == ["S99x: missing-tool (not in PATH)"]
    assert warnings == []


@pytest.mark.parametrize("script", ['OUT="$(\n  printf hi\n)"\n', "OUT=$(\n  printf hi\n)\n",
                                    "command -v missing-tool >/dev/null || exit 0\n",
                                    'case "$1"\nin\n  start|restart) : ;;\nesac\n'])
def test_round_8_valid_forms_are_not_refused(tmp_path, script):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + script})
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == []
    assert warnings == []


@pytest.mark.parametrize("script,warning", [
    ("command -p review_tool_missing\n", "S99x:2: command -p (not judged)"),
])
def test_round_9_constructs_are_one_warn_and_no_refusal(tmp_path, script, warning):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + script})
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == []
    assert warnings == [warning]


def test_a_case_inside_a_substitution_does_not_end_it_early(tmp_path):
    """The substitution's end is found past its case patterns, so the rest of the line is judged."""
    tree = _tree(tmp_path, {"etc/init.d/S99x": '#!/bin/sh\nOUT="$(case x in x) : ;; esac)"; missing-tool\n'})
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == ["S99x: missing-tool (not in PATH)"]
    assert warnings == []


def test_a_function_definition_inside_quoted_data_defines_nothing(tmp_path):
    script = "#!/bin/sh\nprintf '%s\\n' '\nreview_tool_missing() { :; }\n'\nreview_tool_missing\n"
    assert _tool().init_script_missing_binaries(_tree(tmp_path, {"etc/init.d/S99x": script})) == [
        "S99x: review_tool_missing (not in PATH)"]
    defined = "#!/bin/sh\nreview_tool_missing() { :; }\nreview_tool_missing\n"
    assert _tool().init_script_missing_binaries(_tree(tmp_path / "d", {"etc/init.d/S99x": defined})) == []


@pytest.mark.parametrize("script", ["false && PATH=/opt/review-tools\n", "true || PATH=/opt/review-tools\n",
                                    "if true; then PATH=/opt/review-tools; fi\n", "f() {\n  PATH=/opt/review-tools\n}\n",
                                    "(PATH=/opt/review-tools)\n", "PATH=/opt/review-tools | cat\n",
                                    "case $1 in x) PATH=/opt/review-tools ;; esac\n"])
def test_a_path_assignment_that_may_not_run_is_a_warn_and_not_applied(tmp_path, script):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + script + "review_tool_missing\n",
                            "opt/review-tools/review_tool_missing": "", "bin/cat": ""},
                 executable=("opt/review-tools/review_tool_missing", "bin/cat"))
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == ["S99x: review_tool_missing (not in PATH)"]
    assert len(warnings) == 1 and "a PATH assignment that is conditional" in warnings[0], warnings


@pytest.mark.parametrize("script,name", [
    ("missing-tool>/dev/null\n", "missing-tool"), (">> /var/log/test missing-tool\n", "missing-tool"),
    ("2>&1 missing-tool\n", "missing-tool"), (">/dev/null 2>&1 missing-tool\n", "missing-tool"),
    ("( missing_tool() { :; }; ); missing_tool\n", "missing_tool"),
    ("missing_tool() { :; }; command missing_tool\n", "missing_tool"),
    ("OUT=\"$(printf '%s' 'if'; missing-tool)\"\n", "missing-tool"), ("123\n", "123"),
])
def test_round_10_forms_reach_the_command_the_shell_runs(tmp_path, script, name):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + script})
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == [f"S99x: {name} (not in PATH)"]
    assert warnings == []


def test_a_conditional_chain_continued_on_the_next_line_keeps_its_path_assignment_conditional(tmp_path):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\nfalse &&\nPATH=/opt/tools\nmissing-tool\n",
                            "opt/tools/missing-tool": "", "bin/false": ""}, executable=("opt/tools/missing-tool", "bin/false"))
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == ["S99x: missing-tool (not in PATH)"]
    assert warnings == ["S99x:3: a PATH assignment that is conditional or inside a compound command (not applied)"]


@pytest.mark.parametrize("script", ["if true; then case x in x) : ;; other) : ;; esac; fi\n",
                                    "while false; do case $1 in\n  a|b) : ;;\n  esac\ndone\n",
                                    "missing_tool() { :; }; missing_tool\n", "cat <<EOF\n'\nEOF\n"])
def test_round_10_valid_forms_are_not_refused(tmp_path, script):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + script, "bin/false": "", "bin/cat": ""},
                 executable=("bin/false", "bin/cat"))
    assert _tool().init_script_missing_binaries(tree) == []


def test_a_here_document_body_with_an_apostrophe_ends_at_its_delimiter(tmp_path):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\ncat <<EOF\n'\nEOF\nmissing-tool\n", "bin/cat": ""},
                 executable=("bin/cat",))
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == ["S99x: missing-tool (not in PATH)"]
    assert warnings == []


@pytest.mark.parametrize("script,warning", [
    ("./missing-tool\n", "a command word that is not a literal name or absolute path ('./missing-tool', not judged)"),
    ("tools/missing-tool\n", "a command word that is not a literal name or absolute path ('tools/missing-tool', not judged)"),
])
def test_an_unjudgeable_command_word_is_a_warn(tmp_path, script, warning):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + script})
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == []
    assert warnings == [f"S99x:2: {warning}"]


@pytest.mark.parametrize("script,name,warning", [
    ("printf '%s\\n' '<<EOF'\nmissing-tool\n", "missing-tool", None),             # a quoted << opens nothing
    ("cat <<EOF\n  EOF\nnot_run\nEOF\nmissing-tool\n", "missing-tool", None),    # an indented EOF is body text
    ("cat <<-EOF\n\tbody\n\tEOF\nmissing-tool\n", "missing-tool", None),       # <<- removes leading tabs
    ("missing_tool\nmissing_tool() { :; }\n", "missing_tool", None),              # called before it is defined
    ("outer()( missing_tool() { :; }; ); missing_tool\n", "missing_tool", None),   # f()( ) keeps its subshell
    ("nohup missing-tool\n", "nohup", "S99x:2: a program run through nohup (its arguments are not judged)"),
])
def test_round_11_forms_reach_the_command_the_shell_runs(tmp_path, script, name, warning):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + script, "bin/cat": "", "bin/printf": ""},
                 executable=("bin/cat", "bin/printf"))
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == [f"S99x: {name} (not in PATH)"]
    assert warnings == ([warning] if warning else [])


def test_a_function_body_may_call_a_function_defined_later(tmp_path):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\nf() { g; }\ng() { :; }\nf\n"})
    assert _tool().init_script_missing_binaries(tree) == []


def test_a_command_word_joining_a_variable_with_text_is_a_warn(tmp_path):
    tree = _tree(tmp_path, {"etc/init.d/S99x": '#!/bin/sh\nRUN=printf\n"$RUN"-missing\n"${RUN}" x\n'})
    warnings: list[str] = []
    assert _tool().init_script_missing_binaries(tree, warnings=warnings) == []
    assert warnings == ["S99x:3: a command word that joins a variable with other text ('$RUN-missing', not judged)"]


@pytest.mark.parametrize("rel,held", [("usr/bin/absent-dir/../true", False), ("usr/bin/../bin/true", True),
                                      ("usr/bin/true", True)])
def test_dot_dot_needs_the_directory_it_leaves(tmp_path, rel, held):
    tree = _tree(tmp_path, {"usr/bin/true": "", "bin/true": ""}, executable=("usr/bin/true", "bin/true"))
    assert _tool()._executable_in(tree, rel) is held
    script = f"#!/bin/sh\n/{rel}\n"
    tree2 = _tree(tmp_path / "s", {"etc/init.d/S99x": script, "usr/bin/true": "", "bin/true": ""},
                  executable=("usr/bin/true", "bin/true"))
    assert _tool().init_script_missing_binaries(tree2) == ([] if held else [f"S99x: /{rel}"])


def test_a_quoted_reserved_word_is_a_command(tmp_path):
    tree = _tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n'if'\n"})
    assert _tool().init_script_missing_binaries(tree) == ["S99x: if (not in PATH)"]


def test_a_line_that_cannot_be_split_fails_instead_of_being_skipped(tmp_path):
    tree = _tree(tmp_path, {"etc/init.d/S49x": "#!/bin/sh\necho 'unterminated\n/usr/sbin/dropbear\n"})
    with pytest.raises(RuntimeError, match="cannot inspect"):
        _tool().init_script_missing_binaries(tree)


def test_every_shipped_init_script_can_be_inspected():
    tool = _tool()
    for script in sorted((SHARED / "etc/init.d").glob("S*")):
        tool._program_refs(script.read_text(encoding="utf-8"), script.name)


def test_a_program_through_symlinks_inside_the_image_is_held(tmp_path):
    tree = _tree(tmp_path, {"etc/init.d/S40x": "#!/bin/sh\n/sbin/ip link\n/usr/sbin/udhcpc -q\n",
                            "bin/busybox": ""}, executable=("bin/busybox",))
    (tree / "sbin").mkdir()
    (tree / "sbin/ip").symlink_to("../bin/busybox")
    (tree / "usr/sbin").mkdir(parents=True)
    (tree / "usr/sbin/udhcpc").symlink_to("/bin/busybox")  # absolute: resolved inside the image
    assert _tool().init_script_missing_binaries(tree) == []


@pytest.mark.parametrize("make", ["dangling", "not_executable", "directory", "escapes"])
def test_a_reference_that_cannot_run_is_reported(tmp_path, make):
    tree = _tree(tmp_path, {"etc/init.d/S49x": "#!/bin/sh\n/usr/sbin/dropbear -R\n"})
    target = tree / "usr/sbin/dropbear"
    target.parent.mkdir(parents=True, exist_ok=True)
    if make == "dangling":
        target.symlink_to("../../bin/none")
    elif make == "not_executable":
        target.write_text("", encoding="utf-8")
        target.chmod(0o644)
    elif make == "directory":
        target.mkdir()
    else:  # a link out of the image: resolved against the image root, where nothing is
        target.symlink_to("/../../../usr/bin/env")
    assert _tool().init_script_missing_binaries(tree) == ["S49x: /usr/sbin/dropbear"]


def test_the_shared_overlay_alone_has_the_measured_defect(tmp_path):
    """Positive control: every tracked shared-overlay file, as the DE25 stage copied them
    before this fix, gives S49dropbear without its binary."""
    tool = _tool()
    for rel in tool._tracked_overlay_files(ROOT):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((SHARED / rel).read_bytes())
        dest.chmod((SHARED / rel).stat().st_mode & 0o777)
    assert tool.init_script_missing_binaries(tmp_path, provided=BASE_PROVIDES, bare=False) == [
        "S49dropbear: /usr/sbin/dropbear"]


def test_the_de25_selection_leaves_out_exactly_the_ssh_files():
    tool = _tool()
    tracked = set(tool._tracked_overlay_files(ROOT))
    assert SSH_FILES <= tracked
    assert tracked - set(tool.de25_overlay_files(ROOT)) == SSH_FILES
    assert tool.DE25_OVERLAY_EXCLUDE == SSH_FILES


def test_an_exclusion_that_no_longer_matches_a_file_is_refused(monkeypatch):
    tool = _tool()
    monkeypatch.setattr(tool, "DE25_OVERLAY_EXCLUDE", frozenset({Path("etc/init.d/S49renamed")}))
    with pytest.raises(RuntimeError, match="does not track"):
        tool.de25_overlay_files(ROOT)


def _stub_stage(tool, monkeypatch):
    """Replace stage()'s downloads and builds; keep its overlay assembly and publish."""
    monkeypatch.setattr(tool, "_fetch_pinned", lambda *a, **k: None)
    monkeypatch.setattr(tool.de25_toolchain_stage, "stage", lambda *a, **k: None)
    monkeypatch.setattr(tool, "build_native_edge_lib", lambda *a, **k: "0" * 64)
    monkeypatch.setattr(tool, "build_can_tools", lambda *a, **k: {})
    monkeypatch.setattr(tool, "_stage_msgpack", lambda *a, **k: None)
    monkeypatch.setattr(tool, "_stage_python", lambda *a, **k: None)


def test_stage_publishes_a_de25_overlay_without_the_ssh_files(tmp_path, monkeypatch):
    tool = _tool()
    _stub_stage(tool, monkeypatch)
    out = tmp_path / "rootfs_overlay"
    tool.stage(ROOT, out, tmp_path / "python_overlay", tmp_path / "cache", toolchain_dir=tmp_path / "tc")
    assert (out / "etc/init.d/S99deepskopion-stream").is_file()  # the overlay was published
    for rel in SSH_FILES:
        assert not (out / rel).exists(), rel
    assert tool.init_script_missing_binaries(out, provided=BASE_PROVIDES, bare=False) == []


def test_stage_no_longer_judges_programs_before_the_layers_merge(tmp_path, monkeypatch):
    """The program check needs the merged image (the Python overlay arrives later), so
    stage() only leaves the SSH files out; build() judges the card."""
    tool = _tool()
    _stub_stage(tool, monkeypatch)
    monkeypatch.setattr(tool, "init_script_missing_binaries",
                        lambda *a, **k: pytest.fail("stage() must not run the program check"))
    out = tmp_path / "rootfs_overlay"
    tool.stage(ROOT, out, tmp_path / "python_overlay", tmp_path / "cache", toolchain_dir=tmp_path / "tc")
    assert (out / "etc/init.d/S99deepskopion-stream").is_file()


def _ext4(tmp_path: Path, tree: Path) -> Path:
    """An ext4 image of *tree*, made the way rr makes the rootfs (mke2fs -d)."""
    tool = _tool()
    image = tmp_path / "rootfs.ext4"
    subprocess.run([tool.sbin_tool("mke2fs"), "-q", "-t", "ext4", "-d", str(tree), str(image), "8M"],
                   check=True, capture_output=True)
    return image


def _image_tree(tmp_path: Path, with_ssh: bool, with_dropbear: bool) -> Path:
    files = {"etc/init.d/S99deepskopion-stream": "#!/bin/sh\nexit 0\n", "bin/busybox": "",
             **{f"bin/{a}": "" for a in APPLETS}}
    if with_ssh:
        files["etc/init.d/S49dropbear"] = (SHARED / "etc/init.d/S49dropbear").read_text(encoding="utf-8")
        files["usr/local/bin/deepskopion-authorize-key"] = "#!/bin/sh\n"
    if with_dropbear:
        files["usr/sbin/dropbear"] = ""
    return _tree(tmp_path / "tree", files, executable=("bin/busybox", "usr/sbin/dropbear",
                                                       "usr/local/bin/deepskopion-authorize-key",
                                                       *(f"bin/{a}" for a in APPLETS)))


@pytest.mark.parametrize("with_ssh,with_dropbear,expected", [
    (True, False, ["/etc/init.d/S49dropbear is in the image", "/usr/local/bin/deepskopion-authorize-key is in the image",
                   "S49dropbear: /usr/sbin/dropbear"]),
    (True, True, ["/etc/init.d/S49dropbear is in the image", "/usr/local/bin/deepskopion-authorize-key is in the image",
                  "/usr/sbin/dropbear is in the image"]),
    (False, True, ["/usr/sbin/dropbear is in the image"]),
    (False, False, []),
])
def test_the_image_check_reads_the_ext4_artifact(tmp_path, with_ssh, with_dropbear, expected):
    image = _ext4(tmp_path, _image_tree(tmp_path, with_ssh, with_dropbear))
    assert _tool().rootfs_image_problems(image) == expected


def test_the_image_check_reads_a_partition_at_an_offset(tmp_path):
    image = _ext4(tmp_path, _image_tree(tmp_path, True, False))
    card = tmp_path / "card.img"
    card.write_bytes(b"\0" * 4096 + image.read_bytes())
    assert "S49dropbear: /usr/sbin/dropbear" in _tool().rootfs_image_problems(card, 4096)


def test_an_image_without_the_shipped_init_script_fails_loudly(tmp_path):
    """A partial or wrong extraction (here an image holding only etc/) is never judged clean."""
    tree = tmp_path / "tree"
    (tree / "etc").mkdir(parents=True)
    (tree / "etc/hostname").write_text("x\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="cannot inspect"):
        _tool().rootfs_image_problems(_ext4(tmp_path, tree))


def test_an_unreadable_image_fails_loudly(tmp_path):
    bad = tmp_path / "not-ext4.img"
    bad.write_bytes(b"\0" * 65536)
    with pytest.raises(RuntimeError, match="cannot inspect"):
        _tool().rootfs_image_problems(bad)


def test_the_cli_verb_exits_non_zero_on_a_bad_image(tmp_path, capsys):
    image = _ext4(tmp_path, _image_tree(tmp_path, True, False))
    assert _tool().main(["check-rootfs", str(image)]) == 1
    assert "S49dropbear: /usr/sbin/dropbear" in capsys.readouterr().err


def _stub_build(tool, monkeypatch, tmp_path, problems):
    calls = []
    for name in ("active_target_stem", "assert_lwh2f_dt_nodes_are_decoded", "rebuild_uboot_with_emac0_phy_fix",
                 "assert_uboot_itb_default_bootargs", "_stage_target_stem_sof", "normalize_provenance",
                 "assert_dtb_realises_dt_nodes", "assert_ctucanfd_probe_is_bounded", "normalize_artifact_mtimes"):
        monkeypatch.setattr(tool, name, lambda *a, **k: None)
    monkeypatch.setattr(tool, "deployment_bootargs", lambda *a, **k: "console=ttyS1,115200")
    monkeypatch.setattr(tool, "linux_out_dir", lambda root: tmp_path)
    monkeypatch.setattr(tool.subprocess, "run", lambda argv, **k: calls.append(list(argv)))
    monkeypatch.setattr(tool, "rootfs_image_problems",
                        lambda image, offset=0, warnings=None: calls.append(["check", str(image), offset]) or problems)
    return calls


def test_build_refuses_a_card_whose_rootfs_cannot_boot(tmp_path, monkeypatch):
    """Effect, not detection: after `rr linux sdcard`, build() checks the card's P2 and
    fails on a problem."""
    tool = _tool()
    calls = _stub_build(tool, monkeypatch, tmp_path, ["S49dropbear: /usr/sbin/dropbear"])
    with pytest.raises(RuntimeError, match="not bootable as built: S49dropbear"):
        tool.build(ROOT)
    assert calls[-2][:3] == ["rr", "linux", "sdcard"]
    assert calls[-1] == ["check", str(tmp_path / "sdcard.img"), tool.SDCARD_ROOTFS_OFFSET_BYTES]


def test_build_passes_a_card_with_no_problem(tmp_path, monkeypatch):
    tool = _tool()
    calls = _stub_build(tool, monkeypatch, tmp_path, [])
    tool.build(ROOT)
    assert calls[-1][0] == "check"


def test_the_readme_states_the_measured_image():
    """Done-when 3: the README's DE25 SSH statement names the inspected image by sha256."""
    flat = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "The DE25 image has no SSH server" in flat
    assert ("A DE25 rootfs built with `rr linux rootfs` was read with debugfs (image sha256 "
            "`14d16638fa751228a9e1df229b1bd4d23a62f3c7caaa5f898c4d79220f1aff87`): it holds no dropbear init "
            "script, no key tool and no dropbear binary.") in flat
