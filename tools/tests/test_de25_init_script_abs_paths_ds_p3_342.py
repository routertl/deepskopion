# SPDX-License-Identifier: MPL-2.0
"""DS-P3.342: an absolute command path outside the bin directories is judged, not WARNed.

DS-P3.316's scan judged a command path only under a bin directory and reported any other absolute command path as
a WARN, so an init script running a program installed under /opt or /usr/lib that the image does not hold passed.
A path under a directory the system fills at boot (/tmp, /run, /var, /dev, /proc, /sys, /mnt, /media) stays a WARN:
the image is not expected to hold it. Each judged case is red on the previous scan (a missing program was not
reported) and each valid case checks the judgement does not refuse a program the image holds.
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


def _check(tmp_path, script, files=(), provided=frozenset()):
    files = (*files, "bin/cat")                       # a bare cat in the fixtures resolves
    held = {rel: "" for rel in files}
    tree = _base._tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n" + script, **held}, executable=tuple(files))
    warnings: list[str] = []
    return _base._tool().init_script_missing_binaries(tree, provided, warnings=warnings), warnings


@pytest.mark.parametrize("path", ["/opt/x/tool", "/usr/lib/x/tool", "/etc/init.d/helper", "/usr/share/x/run.sh"])
def test_a_missing_program_outside_the_bin_dirs_is_reported(tmp_path, path):
    problems, warnings = _check(tmp_path, f"{path} start\n")
    assert problems == [f"S99x: {path}"] and warnings == []


def test_a_held_program_outside_the_bin_dirs_is_not_reported(tmp_path):
    problems, warnings = _check(tmp_path, "/opt/x/tool start\n", files=("opt/x/tool",))
    assert problems == [] and warnings == []


def test_a_program_another_layer_provides_is_not_reported(tmp_path):
    problems, _ = _check(tmp_path, "/opt/x/tool start\n", provided=frozenset({"/opt/x/tool"}))
    assert problems == []


def test_a_present_but_not_executable_file_is_reported(tmp_path):
    tree = _base._tree(tmp_path, {"etc/init.d/S99x": "#!/bin/sh\n/opt/x/tool\n", "opt/x/tool": ""})
    assert _base._tool().init_script_missing_binaries(tree) == ["S99x: /opt/x/tool"]


@pytest.mark.parametrize("script", ["cat /opt/x/data\n", "echo /opt/x/tool\n", "X=/opt/x/tool\n",
                                    "cat <<EOF\n/opt/x/tool\nEOF\n", "# /opt/x/tool\n"])
def test_a_path_that_is_data_and_not_a_command_is_not_judged(tmp_path, script):
    problems, warnings = _check(tmp_path, script)
    assert problems == []


@pytest.mark.parametrize("path", ["/tmp/x", "/run/x/tool", "/var/lib/x/tool", "/dev/x", "/proc/x", "/sys/x",
                                  "/mnt/x/tool", "/" + "media" + "/x/tool"])
def test_a_path_the_system_fills_at_boot_is_a_warn_and_no_refusal(tmp_path, path):
    problems, warnings = _check(tmp_path, f"{path} start\n")
    assert problems == []
    assert warnings == [f"S99x:2: an absolute command path under a directory the system fills at boot "
                        f"('{path}', not judged)"]


def test_a_missing_program_inside_a_substitution_and_after_a_pipe_is_reported(tmp_path):
    problems, _ = _check(tmp_path, 'X="$(/opt/x/probe)"\ncat /dev/null | /opt/x/filter\n')
    assert problems == ["S99x: /opt/x/filter", "S99x: /opt/x/probe"]


def test_the_root_prefix_is_removed_before_judging(tmp_path):
    problems, _ = _check(tmp_path, '"${ROOT_PREFIX}/opt/x/tool" start\n')
    assert problems == ["S99x: /opt/x/tool"]


_COMPUTED = "an absolute command path that is computed or has a ./.. segment"


@pytest.mark.parametrize("script,word", [
    ('MODEL=x\n"/opt/$MODEL/tool" start\n', "/opt/$MODEL/tool"),
    ('/opt/$(uname -m)/tool\n', None),
    ("/opt/*/tool\n", "/opt/*/tool"),
])
def test_a_computed_command_path_is_a_warn_even_when_the_resolved_program_exists(tmp_path, script, word):
    problems, warnings = _check(tmp_path, script, files=("opt/x/tool", "bin/uname"))
    assert problems == []
    assert len(warnings) == 1 and _COMPUTED in warnings[0], warnings
    if word:
        assert repr(word) in warnings[0], warnings


@pytest.mark.parametrize("path", ["/./run/tools/helper", "/var/../opt/x/tool", "/opt/./x/tool", "/opt/x/../x/tool"])
def test_a_path_with_a_dot_segment_is_not_judged_by_its_spelling(tmp_path, path):
    problems, warnings = _check(tmp_path, f"{path}\n")
    assert problems == []
    assert len(warnings) == 1 and _COMPUTED in warnings[0], warnings


def test_a_runtime_path_held_in_a_variable_warns_at_the_invocation(tmp_path):
    problems, warnings = _check(tmp_path, 'RUN=/run/tools/helper\n"$RUN" start\n')
    assert problems == []
    assert warnings == ["S99x:3: an absolute command path under a directory the system fills at boot "
                        "('/run/tools/helper' held in '$RUN', not judged)"]


def test_a_missing_judged_path_held_in_a_variable_is_reported(tmp_path):
    problems, warnings = _check(tmp_path, 'RUN=/opt/x/tool\n"$RUN" start\n')
    assert problems == ["S99x: /opt/x/tool"] and warnings == []


def test_a_helper_under_var_lib_is_a_warn_not_a_pass(tmp_path):
    """Stated limit: a directory the system fills at boot is never judged, so a helper the image should ship
    under /var/lib that is lost is a WARN. A WARN is never a pass (the scope text says so)."""
    problems, warnings = _check(tmp_path, "/var/lib/x/helper\n")
    assert problems == [] and len(warnings) == 1 and "fills at boot" in warnings[0]


@pytest.mark.parametrize("script", ['"/opt/x/tool$((1+1))"\n', 'RUN=/opt/x/tool$((1+1))\n"$RUN" start\n'])
def test_an_arithmetic_expansion_in_a_command_path_is_computed_not_zero(tmp_path, script):
    problems, warnings = _check(tmp_path, script, files=("opt/x/tool2",))
    assert problems == [] and warnings and all("not judged" in w for w in warnings), (problems, warnings)


def test_a_quote_inside_a_command_path_is_not_removed_before_judging(tmp_path):
    problems, warnings = _check(tmp_path, '"/opt/team\'s/tool"\n', files=("opt/teams/tool",))
    assert problems == [] and warnings, (problems, warnings)    # neither refused nor passed on /opt/teams/tool


@pytest.mark.parametrize("script", ["RUN='/opt/x/tool start'\n$RUN\n", "RUN='/opt/x/tool start'\n\"$RUN\"\n"])
def test_a_variable_value_with_a_space_is_advisory(tmp_path, script):
    problems, warnings = _check(tmp_path, script, files=("opt/x/tool",))
    assert problems == [] and warnings, (problems, warnings)


_NOT_LITERAL = ["/opt/x\\ y/tool", "/opt/{a,b}/tool", "/opt/x/to?l", "/opt/x/[a]ool",
                "/opt/~x/tool", "/opt/x/tool!", "/opt/x/to%ol", "/opt/x/to@ol", "/opt/x/to=ol", "/opt/x/to:ol",
                "/opt/x/t,ol", "/opt/x/tôol", "/opt/x/$X", "/opt/x/${X}", "/opt/x/`true`", "/opt//x/tool"]


@pytest.mark.parametrize("path", _NOT_LITERAL)
def test_a_path_that_is_not_a_plain_literal_is_never_a_refusal(tmp_path, path):
    """The allow-list: a refusal needs a positive literal match, so no construct, listed or not, can make a path
    a false refusal. Each of these is absent from the image and still only a WARN."""
    tree = _base._tree(tmp_path, {"etc/init.d/S99x": f"#!/bin/sh\n{path}\n", "bin/cat": ""}, executable=("bin/cat",))
    warnings: list[str] = []
    assert _base._tool().init_script_missing_binaries(tree, warnings=warnings) == []
    assert len(warnings) == 1 and warnings[0].startswith("S99x:2: "), warnings


@pytest.mark.parametrize("path", ["/opt/x/tool", "/opt/x-y/t.o+l_", "/usr/lib/x/A1.2-b"])
def test_a_plain_literal_path_is_judged(tmp_path, path):
    problems, _ = _check(tmp_path, f"{path}\n")
    assert problems == [f"S99x: {path}"]


@pytest.mark.parametrize("script", ['"/opt/x/to\\ol"\n', '"/opt/x/tool\\""\n', '"/opt/x/tool$((0))"\n'])
def test_a_line_the_tokenizer_altered_never_refuses_an_outside_path(tmp_path, script):
    """The tokenizer drops a backslash and folds $(( )) to 0, so the text it hands over is not the path the shell
    runs: the image may hold the real one. Never a refusal, always a WARN naming the line."""
    problems, warnings = _check(tmp_path, script, files=("opt/x/tool", "opt/x/tool0", "opt/x/toool"))
    assert problems == [] and len(warnings) == 1 and warnings[0].startswith("S99x:2: "), (problems, warnings)


def test_an_arithmetic_expansion_in_a_bin_path_is_still_judged_as_before(tmp_path):
    """Base behaviour kept: $(( )) folds to 0 and the bin-directory lookup follows it."""
    held = _check(tmp_path / "held", '"/usr/bin/helper$((0))"\n', files=("usr/bin/helper0",))[0]
    missing = _check(tmp_path / "missing", '"/usr/bin/helper$((0))"\n')[0]
    assert held == [] and missing == ["S99x: /usr/bin/helper0"]
