# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P1.125: every AI-written source file carries the RouteRTL provenance header line.

The header is "<comment> rr <model> (AI-generated)" in the file's own comment syntax within its first 20 lines
(tools/ai_provenance_stamp.py). AI_PROVENANCE_EXCEPTIONS.txt lists the files that do not carry it (vendored,
tool-generated, frozen evidence whose sha256 a record quotes, human-written) and the VHDL directories that cannot
hold the .routertl-ai-generated sentinel. These tests keep that file honest in both directions.
"""
from __future__ import annotations

import fnmatch
import hashlib
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import ai_provenance_stamp as aps  # noqa: E402
import export_github  # noqa: E402

def export_drops(pattern: str) -> bool:
    """True when the public export (tools/export_github.py DROP_LIST) drops the paths ``pattern`` names, so an
    exported checkout has no file for the entry to match."""
    return bool(export_github._matches_drop_list(pattern.replace("**", "x").replace("*", "x")))


# The guard reads the git index snapshot: policy and source list as staged.
SEC = aps.load_exceptions(ROOT, from_index=True)
TRACKED = {p: pre for p, (pre, _data) in aps.indexed(ROOT).items()}
CHECKSUM_ROW, row_pins = aps.CHECKSUM_ROW, aps.row_pins


def test_every_non_excluded_source_file_carries_the_header():
    missing = aps.missing(ROOT)
    assert missing == [], f"{len(missing)} source file(s) without the AI-generated header, first: {missing[:10]}"


@pytest.mark.parametrize("section", ["vendored", "generated", "frozen-evidence", "human-written"])
def test_every_exception_entry_matches_a_tracked_source_file(section):
    stale = [pat for pat in SEC[section]
             if not any(fnmatch.fnmatchcase(p, pat) for p in TRACKED) and not export_drops(pat)]
    assert stale == [], f"[{section}] entries that match no tracked source file: {stale}"


def test_the_no_sentinel_list_is_exactly_the_blocked_directories():
    _, blocked = aps.sentinel_dirs(ROOT, from_index=True)
    # An entry the public export drops has no directory in an exported checkout; everywhere else the list is exact.
    expected = [d for d in SEC["no-sentinel"] if (ROOT / d).is_dir() or not export_drops(d + "/x")]
    assert sorted(expected) == sorted(blocked)


def test_sentinels_sit_in_every_all_ai_vhdl_directory_and_nowhere_else():
    ok, _ = aps.sentinel_dirs(ROOT, from_index=True)
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True,
                           check=True).stdout.splitlines()
    have = sorted(str(Path(p).parent) for p in files if Path(p).name == aps.SENTINEL)
    assert have == sorted(ok)
    modes = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "--", *[f"{d}/{aps.SENTINEL}" for d in ok]],
                           capture_output=True, text=True, check=True).stdout.split("\n")
    assert all(m.split()[0] == "100644" for m in modes if m), "a sentinel must be a regular file"
    assert all((ROOT / d / aps.SENTINEL).is_file() and not (ROOT / d / aps.SENTINEL).is_symlink() for d in ok)


def _native_sources_pinned() -> bool:
    """sw/stream/native/README.md quotes the hash over edge_drain.c and edge_drain.h (native_edge_source_sha256),
    computed here as native_edge_lib.source_sha256 does, from the index bytes of the sources and of the README."""
    import native_edge_lib as nel
    d = hashlib.sha256()
    for rel in nel.SOURCES:
        d.update(rel.encode("utf-8") + b"\0" + (aps.index_bytes(ROOT, rel) or b"") + b"\0")
    doc = (aps.index_bytes(ROOT, "sw/stream/native/README.md") or b"").decode("utf-8")
    return f"`native_edge_source_sha256`\n`{d.hexdigest()[:8]}…`" in doc


# Frozen-evidence files pinned by a hash over a set of files rather than by their own sha256.
SET_PINNED = {"sw/stream/native/edge_drain.c": ("sw/stream/native/README.md", _native_sources_pinned),
              "sw/stream/native/edge_drain.h": ("sw/stream/native/README.md", _native_sources_pinned)}


def test_each_frozen_evidence_file_is_pinned_by_the_record_its_entry_names():
    """Policy, file bytes and record bytes all from the git index (aps.frozen_problems)."""
    assert aps.frozen_problems(ROOT, SET_PINNED) == []


def test_a_stale_staged_checksum_fails_even_when_the_worktree_matches(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[frozen-evidence]\nw/h.py  # pinned-by: ./SUMS\n", encoding="utf-8")
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "h.py").write_text("x = 1\n", encoding="utf-8")
    good = hashlib.sha256(b"x = 1\n").hexdigest()
    (tmp_path / "w" / "SUMS").write_text(f"{'0' * 64}  h.py\n", encoding="utf-8")       # stale, staged
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    (tmp_path / "w" / "SUMS").write_text(f"{good}  h.py\n", encoding="utf-8")            # correct, unstaged only
    assert aps.frozen_problems(tmp_path) != []
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.frozen_problems(tmp_path) == []


def test_ai_files_in_sentinel_less_directories_are_still_guarded():
    """A directory whose subtree holds tool output gets no sentinel (V-019 would flag the untouched output; MC
    ruling 4). The header guard still covers every stamped source file in it."""
    _, blocked = aps.sentinel_dirs(ROOT, from_index=True)
    checked = []
    for d in blocked:
        for p, pre in TRACKED.items():
            if str(Path(p).parent) == d and not aps.excluded(p, SEC):
                assert aps.has_tag((ROOT / p).read_bytes(), pre), p
                checked.append(p)
    assert "hw/rtl/deepskopion_core.vhd" in checked


def test_a_human_written_glob_is_refused(tmp_path):
    (tmp_path / aps.EXCEPTIONS).write_text("[human-written]\n*\n", encoding="utf-8")
    with pytest.raises(ValueError):
        aps.load_exceptions(tmp_path)


def test_a_tracked_symlink_is_never_a_stamp_target(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[vendored]\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(tmp_path / "outside.py")
    subprocess.run(["git", "-C", str(tmp_path), "add", "link.py"], check=True)
    assert "link.py" not in aps.tracked(tmp_path)


def test_a_symlinked_parent_directory_is_never_a_stamp_target(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[vendored]\n", encoding="utf-8")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "real/a.py"], check=True)
    assert "real/a.py" in aps.tracked(tmp_path)
    (tmp_path / "real").rename(tmp_path / "moved")
    (tmp_path / "real").symlink_to(tmp_path / "moved")
    assert "real/a.py" not in aps.tracked(tmp_path)


@pytest.mark.parametrize("src", [
    b"#!/usr/bin/env python3\n# -*- coding: latin-1 -*-\nx = '\xe9'\n",
    b"# a plain comment\n# -*- coding: latin-1 -*-\nx = '\xe9'\n",
    b"\n# -*- coding: latin-1 -*-\nx = '\xe9'\n",
    b"# -*- coding: latin-1 -*-\nx = '\xe9'\n",
    b"\xef\xbb\xbfx = 1\n",
    b"\xef\xbb\xbf#!/usr/bin/env python3\nx = 1\n",
])
def test_a_stamped_latin1_source_still_compiles(src):
    compile(src, "src.py", "exec")
    out = aps.stamp_bytes(src, "#", "m1", python=True)
    compile(out, "src.py", "exec")
    assert aps.has_tag(out, "#", python=True) and out.count(b"(AI-generated)") == 1


def test_the_verdict_reads_the_index_not_the_working_tree(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[vendored]\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", aps.EXCEPTIONS, "a.py"], check=True)
    (tmp_path / "a.py").write_text("# rr m1 (AI-generated)\nx = 1\n", encoding="utf-8")   # unstaged fix only
    assert aps.missing(tmp_path) == ["a.py"]
    (tmp_path / "a.py").unlink()                                                     # gone from the worktree
    assert aps.missing(tmp_path) == ["a.py"]


@pytest.mark.parametrize("row,want", [
    ("gate_p1_79.py", True),
    ("unrelated-gate_p1_79.py", False),
    ("../run1/gate_p1_79.py", False),          # another run's file with the same name
    ("../harness/gate_p1_79.py", True),        # the same path, spelled through its parent
])
def test_a_checksum_row_must_resolve_to_the_file_itself(row, want):
    h, rec = "a" * 64, "tools/tests/data/w/run2/harness/HARNESS_SHA256"
    assert row_pins(rec, f"{h}  {row}\n", "tools/tests/data/w/run2/harness/gate_p1_79.py", h) is want


def test_an_unstaged_policy_change_does_not_change_the_verdict(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[human-written]\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", aps.EXCEPTIONS, "a.py"], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[human-written]\na.py\n", encoding="utf-8")   # unstaged
    assert aps.missing(tmp_path) == ["a.py"]


def test_the_repository_root_gets_no_sentinel_over_an_excluded_file(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[vendored]\nvendor/*\n", encoding="utf-8")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "new.vhd").write_text("-- rr m1 (AI-generated)\n", encoding="utf-8")
    (tmp_path / "vendor" / "untagged.vhd").write_text("library ieee;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    ok, blocked = aps.sentinel_dirs(tmp_path)
    assert "." not in ok and "." in blocked


def test_the_header_goes_after_a_python_encoding_declaration():
    src = "#!/usr/bin/env python3\n# -*- coding: latin-1 -*-\nx = 1\n"
    out = aps.stamp_text(src, "#", "m1", python=True)
    assert out.splitlines()[1] == "# -*- coding: latin-1 -*-" and out.splitlines()[2] == "# rr m1 (AI-generated)"
    assert aps.has_tag(out, "#", python=True) and not aps.has_tag("#!/bin/sh\n# rr m1 (AI-generated)\n# coding: x\n".replace(
        "# rr m1 (AI-generated)\n# coding: x", "# coding: x\n\n# rr m1 (AI-generated)"), "#")


#: Interpreters an extensionless #! file may name without being shell; anything else must be listed in [shell].
NON_SHELL_INTERPRETERS = ("python", "perl", "tclsh", "wish", "node", "ruby")


def test_every_shell_script_not_classified_by_suffix_is_listed_by_path():
    """Independent of the classifier: every tracked #! file whose suffix is not in the comment map names a known
    non-shell interpreter or is listed under [shell], so deleting its #! line cannot take it out of the check."""
    listed, unlisted = set(SEC["shell"]), []
    for e in subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "-z"], capture_output=True,
                            check=True).stdout.split(b"\0"):
        head, _, raw = e.partition(b"\t")
        p = raw.decode()
        if not e or PurePosixPath(p).suffix in aps.COMMENT or head.split()[0] not in (b"100644", b"100755"):
            continue
        first = (aps.index_bytes(ROOT, p) or b"").split(b"\n", 1)[0]
        if not first.startswith(b"#!"):
            continue
        words = first[2:].decode("utf-8", "replace").split()
        interp = PurePosixPath(words[0]).name if words else ""
        if interp == "env":
            interp = next((w for w in words[1:] if not w.startswith("-")), "")
        if not interp.startswith(NON_SHELL_INTERPRETERS) and p not in listed:
            unlisted.append(p)
    assert listed and unlisted == []


def test_a_bash_suffix_is_shell_without_a_shebang():
    assert aps.prefix_of("tools/new_helper.bash", b"echo hi") == "#"


def test_a_cr_only_python_source_gets_the_header_on_its_own_line():
    src = b"#!/usr/bin/env python3\r# -*- coding: latin-1 -*-\rx = '\xe9'\r"
    compile(src, "s.py", "exec")
    out = aps.stamp_bytes(src, "#", "m1", python=True)
    assert out.split(b"\r")[2] == b"# rr m1 (AI-generated)"
    compile(out, "s.py", "exec")
    assert aps.has_tag(out, "#", python=True)
    assert not aps.has_tag(b"#!/usr/bin/env python3\rx = 1  # rr m1 (AI-generated)\r", "#", python=True)


def test_verify_diff_refuses_a_fragment_dropped_from_the_shell_list(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[shell]\nfrag\n", encoding="utf-8")
    (tmp_path / "frag").write_text("# rr m1 (AI-generated)\necho a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"],
                   check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[shell]\n", encoding="utf-8")
    (tmp_path / "frag").write_text("echo b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.verify_diff(tmp_path, "HEAD") == 1


def _repo_with(tmp_path, files):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"],
                   check=True)


def test_verify_diff_refuses_dropping_a_fragment_from_the_shell_list_alone(tmp_path):
    _repo_with(tmp_path, {aps.EXCEPTIONS: "[shell]\nfrag\n", "frag": "# rr m1 (AI-generated)\necho a\n"})
    (tmp_path / aps.EXCEPTIONS).write_text("[shell]\n", encoding="utf-8")          # manifest only
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.verify_diff(tmp_path, "HEAD") == 1


def test_verify_diff_refuses_lifting_an_exclusion_and_stamping_in_one_change(tmp_path):
    _repo_with(tmp_path, {aps.EXCEPTIONS: "[generated]\ng.py\n", "g.py": "x = 1\n"})
    (tmp_path / aps.EXCEPTIONS).write_text("[generated]\n", encoding="utf-8")
    (tmp_path / "g.py").write_bytes(aps.stamp_bytes(b"x = 1\n", "#", "m1", python=True))
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.verify_diff(tmp_path, "HEAD") == 1


def test_verify_diff_refuses_a_new_exclusion_over_an_existing_unmarked_source(tmp_path):
    _repo_with(tmp_path, {aps.EXCEPTIONS: "[generated]\n", "g.py": "x = 1\n"})
    (tmp_path / aps.EXCEPTIONS).write_text("[generated]\ng.py\n", encoding="utf-8")   # manifest only
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.missing(tmp_path) == []          # the header check now skips it, so the change gate must refuse
    assert aps.verify_diff(tmp_path, "HEAD") == 1


def test_verify_diff_accepts_a_fragment_entering_scope_by_a_new_shell_entry_with_its_header(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "frag").write_text("echo a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"],
                   check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[shell]\nfrag\n", encoding="utf-8")
    (tmp_path / "frag").write_text("# rr m1 (AI-generated)\necho a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.verify_diff(tmp_path, "HEAD") == 0
    (tmp_path / "frag").write_text("# rr m1 (AI-generated)\necho CHANGED\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.verify_diff(tmp_path, "HEAD") == 1


@pytest.mark.parametrize("first", [b"#!/bin/busybox sh", b"#!/bin/ash", b"#!/usr/local/bin/bash", b"#!/bin/busybox ash"])
def test_busybox_and_ash_shebangs_are_shell(first):
    assert aps.prefix_of("x", first) == "#"


def test_verify_diff_refuses_a_file_moving_into_source_scope(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[vendored]\n", encoding="utf-8")
    (tmp_path / "tool").write_text("plain data\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"],
                   check=True)
    (tmp_path / "tool").write_text("#!/bin/sh\n# rr m1 (AI-generated)\nrm -rf x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.verify_diff(tmp_path, "HEAD") == 1


def test_a_first_party_file_beside_the_vendored_package_needs_the_header():
    sp = "sw/linux/rootfs_overlay/usr/lib/python3.12/site-packages/"
    assert aps.excluded(sp + "smbus2/smbus2.py", SEC)
    assert not aps.excluded(sp + "deepskopion_local.py", SEC)


def test_a_listed_script_without_its_shebang_line_still_needs_the_header(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[shell]\netc/init.d/S40x\n", encoding="utf-8")
    (tmp_path / "etc/init.d").mkdir(parents=True)
    f = tmp_path / "etc/init.d/S40x"
    f.write_text("#!/bin/sh\n# rr m1 (AI-generated)\necho up\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.missing(tmp_path) == []
    f.write_text("echo up\n", encoding="utf-8")                   # shebang and header removed
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.missing(tmp_path) == ["etc/init.d/S40x"]


def test_the_encoding_line_exception_applies_to_python_only():
    sh = b"#!/bin/sh\n# coding: utf-8\necho\n"
    assert aps.stamp_bytes(sh, "#", "m1").split(b"\n")[1] == b"# rr m1 (AI-generated)"
    assert not aps.has_tag(b"#!/bin/sh\n# coding: utf-8\n# rr m1 (AI-generated)\n", "#")
    assert aps.has_tag(b"#!/usr/bin/env python3\n# coding: utf-8\n# rr m1 (AI-generated)\n", "#", python=True)


def test_verify_diff_refuses_any_change_to_an_excluded_file(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[vendored]\nv/*\n", encoding="utf-8")
    (tmp_path / "v").mkdir()
    (tmp_path / "v" / "x.vhd").write_text("library ieee;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"],
                   check=True)
    (tmp_path / "v" / "x.vhd").write_bytes(aps.stamp_bytes(b"library ieee;\n", "--", "m1"))
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.verify_diff(tmp_path, "HEAD") == 1


def test_a_sourced_shell_fragment_listed_under_shell_is_in_scope():
    assert TRACKED.get("hooks/pre-commit.local") == "#"
    assert aps.has_tag((ROOT / "hooks/pre-commit.local").read_bytes(), "#")


@pytest.mark.parametrize("first,want", [
    (b"#!/bin/sh\n", "#"), (b"#!/bin/bash\n", "#"), (b"#!/usr/bin/env bash\n", "#"),
    (b"#!/usr/bin/env -S bash -e\n", "#"),
    (b"#!/usr/bin/env python3\n", None), (b"plain text\n", None),
])
def test_an_extensionless_shell_source_is_in_scope(first, want):
    assert aps.prefix_of("sw/linux/rootfs_overlay/init", first) == want


def test_the_rootfs_init_scripts_are_in_scope_and_stamped():
    for rel in ("sw/linux/rootfs_overlay/init", "sw/linux/rootfs_overlay/etc/init.d/S99deepskopion-stream"):
        assert TRACKED.get(rel) == "#", rel
        assert aps.has_tag((ROOT / rel).read_bytes(), "#"), rel


@pytest.mark.parametrize("text,prefix,want", [
    ("x = 1\n", "#", "# rr m1 (AI-generated)\nx = 1\n"),
    ("#!/usr/bin/env python3\nx = 1\n", "#", "#!/usr/bin/env python3\n# rr m1 (AI-generated)\nx = 1\n"),
    ("", "#", "# rr m1 (AI-generated)\n"),
    ("library ieee;\n", "--", "-- rr m1 (AI-generated)\nlibrary ieee;\n"),
    ("-- rr other (AI-generated)\nlibrary ieee;\n", "--", "-- rr other (AI-generated)\nlibrary ieee;\n"),
    ("a\r\nb\r\n", "#", "# rr m1 (AI-generated)\r\na\r\nb\r\n"),
    ("#!/bin/sh\r\necho\r\n", "#", "#!/bin/sh\r\n# rr m1 (AI-generated)\r\necho\r\n"),
])
def test_stamp_text_places_the_header_once(text, prefix, want):
    assert aps.stamp_text(text, prefix, "m1") == want
    assert aps.stamp_text(want, prefix, "m1") == want


def test_a_header_in_the_wrong_comment_syntax_does_not_count():
    assert not aps.has_tag("# rr m1 (AI-generated)\nlibrary ieee;\n", "--")


@pytest.mark.parametrize("text", [
    "x = 1\n# rr m1 (AI-generated)\n",                                  # one line down, no shebang
    "#!/usr/bin/env python3\n# SPDX-License-Identifier: MPL-2.0\n# rr m1 (AI-generated)\n",   # line 3 after a shebang
    "\n" * 19 + "# rr m1 (AI-generated)\n",                              # line 20
    "#!/bin/sh\n\n# rr m1 (AI-generated)\n",                              # one line down after a shebang
])
def test_a_header_one_line_below_its_position_does_not_count(text):
    assert not aps.has_tag(text, "#")


def test_the_stamp_tool_and_this_test_carry_the_header_at_line_1():
    for rel in ("tools/ai_provenance_stamp.py", "tools/tests/test_ai_provenance_header_ds_p1_125.py"):
        assert (ROOT / rel).read_text(encoding="utf-8").splitlines()[0] == "# rr unknown-llm (AI-generated)", rel


def test_an_excluded_vhdl_file_staged_but_deleted_from_the_worktree_still_blocks_its_sentinel(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[vendored]\nd/v.vhd\n", encoding="utf-8")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a.vhd").write_text("-- rr m1 (AI-generated)\n", encoding="utf-8")
    (tmp_path / "d" / "v.vhd").write_text("library ieee;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    (tmp_path / "d" / "v.vhd").unlink()                                   # gone from the worktree only
    ok, blocked = aps.sentinel_dirs(tmp_path, from_index=True)
    assert "d" in blocked and "d" not in ok


@pytest.mark.parametrize("model", ["m\u00f8del", "mod\u00e8le", "\u0442\u0435\u0441\u0442"])
def test_a_non_ascii_model_id_is_refused(model):
    with pytest.raises(ValueError):
        aps.tag_line("#", model)


def test_a_model_id_that_would_form_an_encoding_declaration_is_refused():
    with pytest.raises(ValueError):
        aps.tag_line("#", "coding:bogus")


def test_verify_diff_accepts_a_bom_source_it_stamped(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / aps.EXCEPTIONS).write_text("[vendored]\n", encoding="utf-8")
    (tmp_path / "a.py").write_bytes(b"\xef\xbb\xbfx = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"],
                   check=True)
    (tmp_path / "a.py").write_bytes(aps.stamp_bytes((tmp_path / "a.py").read_bytes(), "#", "m1"))
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert aps.verify_diff(tmp_path, "HEAD") == 0


def test_a_model_id_outside_the_v019_alphabet_is_refused():
    with pytest.raises(ValueError):
        aps.tag_line("#", "bad model")
