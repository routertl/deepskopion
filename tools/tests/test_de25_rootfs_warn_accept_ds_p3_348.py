# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P3.348: a WARN of the DE25 init-script scan passes only from a reviewed accept list.

check-rootfs and the image gate used to print each WARN (a construct the scan does not judge) and
pass. Now a WARN not on sw/linux/de25/rootfs_warn_accept.json makes check-rootfs exit non-zero and
the image gate refuse, naming the WARN; an accepted one is printed with its reason. The list must
equal the WARNs of the shipped init scripts exactly, so a moved or removed line is reviewed again.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.tests.test_de25_kernel_trio_p1_55 import _fdt_fixture
from tools.tests.test_de25_no_ssh_ds_p3_316 import ROOT, _ext4, _image_tree, _tool

PLANTED = "S50x:2: eval"   # measured: the scan names the construct only
SOURCED = ". (a sourced file is not judged)"   # the construct the committed entry accepts


def _sourcing_tree(tmp_path: Path, script: str, line: int, times: int = 1, text: str = '. "/etc/none"') -> Path:
    """An image whose *script* holds *text* at *line* (*times* copies on that line), with the
    shipped init script present; nothing else in it raises a WARN."""
    tree = _image_tree(tmp_path, False, False)
    body = "#!/bin/sh\n" + "# filler\n" * (line - 2) + "; ".join([text] * times) + "\n"
    (tree / "etc/init.d" / script).write_text(body, encoding="utf-8")
    return tree


def _shipped_tree(tmp_path: Path) -> Path:
    """The real shipped S99deepskopion-stream in an image whose every program it names exists
    (as an empty executable), so only its WARN remains to be judged."""
    tool = _tool()
    tree = _image_tree(tmp_path, False, False)
    script = tree / "etc/init.d/S99deepskopion-stream"
    script.write_bytes((ROOT / "sw/linux/rootfs_overlay/etc/init.d/S99deepskopion-stream").read_bytes())
    for missing in tool.init_script_missing_binaries(tree, warnings=[]):
        named = missing.split(": ", 1)[1]
        rel = named.lstrip("/") if named.startswith("/") else "usr/bin/" + named.split(" (", 1)[0]
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text("", encoding="utf-8")
        (tree / rel).chmod(0o755)
    assert tool.init_script_missing_binaries(tree, warnings=[]) == []
    return tree


def _card(tmp_path: Path, ext4: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "sdcard.img", "wb") as card, open(ext4, "rb") as fs:
        card.seek(_tool().SDCARD_ROOTFS_OFFSET_BYTES)
        shutil.copyfileobj(fs, card)


def _both_entry_points(tmp_path: Path, tree: Path) -> tuple[int, str, str | None]:
    """check-rootfs's exit code and stderr, and the image gate's refusal (None when it passes)."""
    import contextlib
    import io
    tool = _tool()
    ext4 = _ext4(tmp_path, tree)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = tool.main(["check-rootfs", str(ext4)])
    _card(tmp_path, ext4, tmp_path / "out")
    try:
        tool.assert_card_rootfs(tmp_path / "out")
        refusal = None
    except RuntimeError as exc:
        refusal = str(exc)
    return rc, err.getvalue(), refusal


def test_the_committed_acceptance_passes_both_entry_points(tmp_path):
    """The real committed entry on the real shipped script (S99deepskopion-stream:141 sources the
    operator's expectation file), no monkeypatch."""
    rc, err, refusal = _both_entry_points(tmp_path, _shipped_tree(tmp_path))
    assert (rc, refusal) == (0, None), err + str(refusal)
    assert f"WARN S99deepskopion-stream:141: {SOURCED} (accepted: " in err


@pytest.mark.parametrize("script,line", [("S99deepskopion-stream", 142), ("S50x", 141)])
def test_the_same_construct_elsewhere_is_refused_by_both_entry_points(tmp_path, script, line):
    """The entry accepts one script, one line and one construct: the same construct one line
    lower, or at the same line of another script, is refused by check-rootfs and by the gate."""
    rc, err, refusal = _both_entry_points(tmp_path, _sourcing_tree(tmp_path, script, line))
    want = f"WARN {script}:{line}: {SOURCED} is not on the reviewed accept list"
    assert rc != 0 and want in err, err
    assert refusal is not None and want in refusal, refusal


@pytest.mark.parametrize("text,want", [
    ('. "/etc/none"', "is accepted for the line"),                       # same construct, other operation
    ('eval "$X"', "S99deepskopion-stream:141: eval is not on the reviewed accept list"),  # other construct
])
def test_another_operation_on_the_accepted_line_is_refused_by_both_entry_points(tmp_path, text, want):
    """Script and line held fixed: a different sourced file, or a different construct, is refused."""
    rc, err, refusal = _both_entry_points(tmp_path, _sourcing_tree(tmp_path, "S99deepskopion-stream", 141, text=text))
    assert rc != 0 and want in err, err
    assert refusal is not None and want in refusal, refusal


def test_a_second_occurrence_on_the_accepted_line_is_refused(tmp_path):
    rc, err, refusal = _both_entry_points(tmp_path, _sourcing_tree(tmp_path, "S99deepskopion-stream", 141, times=2))
    want = f"WARN S99deepskopion-stream:141: {SOURCED} occurs 2 times"
    assert rc != 0 and want in err, err
    assert refusal is not None and want in refusal, refusal


def test_build_refuses_a_card_with_an_unaccepted_warn(tmp_path, monkeypatch):
    """build() itself, with every rr step recorded instead of run, and `rr linux sdcard`
    writing a card whose rootfs carries the planted WARN; the real image scan judges it."""
    image = _tool()
    ext4 = _ext4(tmp_path, _planted_tree(tmp_path))
    root = tmp_path / "repo"
    out = root / "sw/linux/de25/out/de25_datalogger"
    calls: list[list[str]] = []

    real_run = subprocess.run

    def record(cmd, **kwargs):
        # only the rr build steps are recorded; every other program (debugfs, the scan's own
        # parse-only ash, run under bubblewrap or directly as CI does) runs for real
        if str(cmd[0]) != "rr":
            return real_run(cmd, **kwargs)
        calls.append(list(cmd))
        if list(cmd[:3]) == ["rr", "linux", "sdcard"]:
            _card(tmp_path, ext4, out)

        class _Done:
            returncode = 0
            stdout = stderr = ""
        return _Done()
    (root / "project.yml").parent.mkdir(parents=True, exist_ok=True)
    (root / "project.yml").write_text("target: targets/de25_datalogger.yml\n", encoding="utf-8")
    for rel in ("targets/de25_datalogger.yml", "sw/linux/de25/system_memory_map.yml",
                "hw/rtl/de25/deepskopion_de25_top.vhd", "hw/rtl/de25/axil_demux3.vhd"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes((ROOT / rel).read_bytes())
    out.mkdir(parents=True, exist_ok=True)
    (out / image.DTB_ARTIFACT).write_bytes(_fdt_fixture.dtb_realising(root / "targets/de25_datalogger.yml"))
    monkeypatch.setattr(image.subprocess, "run", record)
    monkeypatch.setattr(image, "deployment_bootargs", lambda _root: "console=ttyS1,115200 ds_ip=192.0.2.5/24")
    monkeypatch.setattr(image, "_stage_target_stem_sof", lambda _root: None)
    monkeypatch.setattr(image, "rebuild_uboot_with_emac0_phy_fix", lambda _root: None)
    monkeypatch.setattr(image, "assert_uboot_itb_default_bootargs", lambda *_a: None, raising=False)
    monkeypatch.setattr(image, "normalize_provenance", lambda *_a, **_k: None)
    monkeypatch.setattr(image, "normalize_artifact_mtimes", lambda *_a, **_k: None)
    monkeypatch.setattr(image, "_emit_uboot_env", lambda _root, out, _env: out / "uboot.env", raising=False)
    monkeypatch.setattr(image, "_stage_uboot_env_on_sdcard", lambda *_a, **_k: None, raising=False)
    with pytest.raises(RuntimeError, match=f"WARN {PLANTED} is not on the reviewed accept list"):
        image.build(root)
    assert ["rr", "linux", "sdcard"] == [c for c in calls if c[:3] == ["rr", "linux", "sdcard"]][0][:3]


def _planted_tree(tmp_path: Path) -> Path:
    tree = _image_tree(tmp_path, False, False)
    (tree / "etc/init.d/S50x").write_text('#!/bin/sh\neval "$X"\n', encoding="utf-8")
    return tree


def _warns(tool, tree: Path) -> list[str]:
    found: list[str] = []
    tool.init_script_missing_binaries(tree, warnings=found)
    return found


def test_the_planted_construct_is_a_warn(tmp_path):
    """Positive control: the planted eval is reported as exactly this WARN."""
    assert _warns(_tool(), _planted_tree(tmp_path)) == [PLANTED]


def test_check_rootfs_exits_non_zero_on_an_unaccepted_warn(tmp_path, capsys):
    tool = _tool()
    rc = tool.main(["check-rootfs", str(_ext4(tmp_path, _planted_tree(tmp_path)))])
    err = capsys.readouterr().err
    assert rc != 0, err
    assert f"WARN {PLANTED} is not on the reviewed accept list" in err


def test_check_rootfs_passes_an_accepted_warn_and_prints_its_reason(tmp_path, capsys, monkeypatch):
    tool = _tool()
    monkeypatch.setattr(tool, "rootfs_warn_accepts",
                        lambda path=None: {PLANTED: {"text": 'eval "$X"', "reason": "reviewed: test reason"}})
    assert tool.main(["check-rootfs", str(_ext4(tmp_path, _planted_tree(tmp_path)))]) == 0
    assert f"WARN {PLANTED} (accepted: reviewed: test reason)" in capsys.readouterr().err


def test_the_accept_list_holds_exactly_the_shipped_warns(tmp_path):
    """The tracked DE25 overlay, scanned as the image ships it: every WARN is accepted and every
    entry is still reported, so an edited script line is reviewed again."""
    tool = _tool()
    tree = tmp_path / "overlay"
    for rel in tool.de25_overlay_files(ROOT):
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "sw/linux/rootfs_overlay" / rel, tree / rel)
    found = _warns(tool, tree)
    assert found, "the shipped scripts produce the reviewed WARN; an empty scan is no evidence"
    accepts = tool.rootfs_warn_accepts()
    assert sorted(accepts) == sorted(found)
    assert {w: tool.script_line(tree, w) for w in found} == {w: accepts[w]["text"] for w in found}


@pytest.mark.parametrize("entry,why", [
    ({"script": "S1", "line": 1, "construct": "eval", "text": "eval x", "reason": " "}, "needs script, line, construct, text and reason"),
    ({"script": "S1", "line": 1, "construct": "eval", "reason": "r"}, "needs script, line, construct, text and reason"),
    ({"script": "S1", "line": 0, "construct": "eval", "reason": "r"}, "needs script, line, construct, text and reason"),
    ({"script": "S1", "construct": "eval", "reason": "r"}, "needs script, line, construct, text and reason"),
    ({"script": "S1", "line": True, "construct": "eval", "reason": "r"}, "needs script, line, construct, text and reason"),
])
def test_a_malformed_accept_entry_is_refused(tmp_path, entry, why):
    path = tmp_path / "accept.json"
    path.write_text(json.dumps({"accept": [entry]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match=why):
        _tool().rootfs_warn_accepts(path)


def test_a_duplicate_accept_entry_is_refused(tmp_path):
    entry = {"script": "S1", "line": 1, "construct": "eval", "text": "eval x", "reason": "r"}
    path = tmp_path / "accept.json"
    path.write_text(json.dumps({"accept": [entry, entry]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="accepted twice"):
        _tool().rootfs_warn_accepts(path)


def test_the_text_compare_cannot_be_skipped_by_a_caller():
    """The accepted line's text is always compared: a caller that gives no image tree is an error,
    never the weaker check without the text."""
    tool = _tool()
    accepts = {"S1:1: eval": {"text": "eval x", "reason": "r"}}
    with pytest.raises(TypeError):
        tool.unaccepted_warnings(["S1:1: eval"], accepts)


def test_debugfs_is_present():
    """The image tests read ext4 images with debugfs; its absence must not look like a pass."""
    assert subprocess.run([_tool().sbin_tool("debugfs"), "-V"], capture_output=True).returncode == 0
