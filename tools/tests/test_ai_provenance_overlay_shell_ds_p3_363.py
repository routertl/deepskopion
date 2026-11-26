# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P3.363: the init and command scripts a rootfs overlay ships stay under the AI-provenance header guard by path.

Before this, an extensionless script was a shell source only through its #! line or its [shell] entry in
AI_PROVENANCE_EXCEPTIONS.txt. Removing the header, the #! line and the [shell] entry together took the script out of
scope, so the guard (tools/ai_provenance_stamp.py missing()) reported nothing, while the unchanged body still ran from
the overlay's /init loop. The shipped set now comes from the overlay layout (overlay_shell_paths).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import ai_provenance_stamp as aps  # noqa: E402

S40 = "sw/linux/rootfs_overlay/etc/init.d/S40network"

#: Every script the overlays ship, named one by one: a script added, removed or moved changes this list on purpose.
SHIPPED = {
    "sw/linux/de25/p2writer/overlay/init",
    "sw/linux/de25/p2writer/overlay/usr/sbin/p2write.sh",
    "sw/linux/rootfs_overlay/init",
    "sw/linux/rootfs_overlay/etc/init.d/S01hostname",
    "sw/linux/rootfs_overlay/etc/init.d/S40network",
    "sw/linux/rootfs_overlay/etc/init.d/S49dropbear",
    "sw/linux/rootfs_overlay/etc/init.d/S99deepskopion-stream",
    "sw/linux/rootfs_overlay/usr/local/bin/deepskopion-authorize-key",
    "sw/linux/rootfs_overlay/usr/local/bin/deepskopion-self-test",
}


def _git(repo: Path, *args: str, input_: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], input=input_, capture_output=True, check=True).stdout


def _index_paths(repo: Path) -> list[str]:
    return _git(repo, "ls-files", "-z").decode("utf-8", "surrogateescape").split("\0")[:-1]


def test_the_shipped_script_set_comes_from_the_overlay_layout():
    assert aps.overlay_shell_paths(_index_paths(ROOT)) == SHIPPED


def test_the_layout_rule_does_not_read_the_shell_list_or_the_first_line():
    paths = ["ov/init", "ov/etc/init.d/S10x", "ov/usr/local/bin/tool", "ov/etc/profile", "ov/usr/lib/x/y",
             "other/init", "etc/init.d/S01top", "init"]
    assert aps.overlay_shell_paths(paths) == {"ov/init", "ov/etc/init.d/S10x", "ov/usr/local/bin/tool",
                                              "etc/init.d/S01top", "init"}


def _copy_of_head(tmp_path: Path) -> Path:
    """A throwaway repository whose index is this checkout's HEAD tree; objects are shared, nothing is checked out."""
    head = _git(ROOT, "rev-parse", "HEAD").decode().strip()
    copy = tmp_path / "copy"
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(copy)], check=True,
                   capture_output=True)
    _git(copy, "read-tree", head)
    return copy


def _stage(copy: Path, path: str, data: bytes, mode: str) -> None:
    sha = _git(copy, "hash-object", "-w", "--stdin", input_=data).decode().strip()
    _git(copy, "update-index", "--cacheinfo", f"{mode},{sha},{path}")


def _strip_s40(copy: Path) -> None:
    """The T1 #15 shape: S40network loses its header line, its #! line and its [shell] entry; the body is unchanged."""
    mode = _git(copy, "ls-files", "-s", "--", S40).split()[0].decode()
    body = _git(copy, "show", f":{S40}").split(b"\n")
    assert body[0].startswith(b"#!") and aps.has_tag(b"\n".join(body), "#"), "fixture: S40network changed shape"
    kept = [ln for ln in body[1:] if not aps.tag_re("#").fullmatch(ln.decode("utf-8", "replace"))]
    assert len(kept) == len(body) - 2, "fixture: expected exactly one #! line and one header line to remove"
    _stage(copy, S40, b"\n".join(kept), mode)
    policy = _git(copy, "show", f":{aps.EXCEPTIONS}").split(b"\n")
    assert S40.encode() in policy, "fixture: S40network is no longer listed under [shell]"
    _stage(copy, aps.EXCEPTIONS, b"\n".join(ln for ln in policy if ln != S40.encode()), "100644")
    assert S40 not in aps.load_exceptions(copy, from_index=True)["shell"]


def test_the_guard_reports_s40network_without_header_shebang_and_shell_entry(tmp_path):
    copy = _copy_of_head(tmp_path)
    assert S40 not in aps.missing(copy), "positive control: the unmodified copy must pass for S40network"
    _strip_s40(copy)
    assert S40 in aps.missing(copy)


def test_without_the_layout_rule_the_stripped_script_escapes_the_guard(tmp_path, monkeypatch):
    """Control: the fixture reproduces the T1 #15 escape, so the test above is green because of the layout rule."""
    copy = _copy_of_head(tmp_path)
    _strip_s40(copy)
    monkeypatch.setattr(aps, "overlay_shell_paths", lambda paths: frozenset())
    assert S40 not in aps.missing(copy)
