# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""The deployment contract's ban on forbidden copyable commands, applied to the getting-started guides.

The README's board and Linux instructions moved to docs/getting_started/ (owner 2026-11-25). The contract's ban on
copyable commands from a flow no image uses (a `journalctl -u ...`, a PetaLinux command) applies to the documents it
lists; a guide left off that list would carry such a command unchecked (T1 rv-e9a8685cd5).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_LINUX_DIR = Path(__file__).resolve().parents[1]
_ROOT = _LINUX_DIR.parents[1]
sys.path.insert(0, str(_LINUX_DIR))
import deployment_contract as C  # noqa: E402



def _copy_bound(root: Path) -> dict:
    decl = C.load_declaration()
    for rel in decl["docs_bound_by_this_contract"]:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_ROOT / rel, dst)
    return decl


def test_the_bound_guides_pass_as_committed(tmp_path):
    """Positive control for the test below: the unmodified copies carry no forbidden command."""
    decl = _copy_bound(tmp_path)
    assert C.check_bound_docs(decl, tmp_path) == []


def test_prose_in_an_html_block_is_not_a_command(tmp_path):
    """Control (T1 rv-836fde037c, rv-90079c9ffe): raw HTML is prose, which may discuss systemd."""
    decl = _copy_bound(tmp_path)
    guide = tmp_path / "docs" / "getting_started" / "zybo_z7_20.md"
    guide.write_text(guide.read_text(encoding="utf-8")
                     + "\n<div>\nThere is no systemd here, so journalctl cannot work.\n</div>\n"
                     # T1 rv-a00ad460bc: prose beside a listing, and a commented-out listing, are not commands.
                     + "\n<div>\nNo journalctl here. <pre>echo ready</pre>\n</div>\n"
                     + "\n<!-- <pre>journalctl -u deepskopion-stream -f</pre> -->\n"
                     + "\n<div>There is no <code>journalctl</code> on this image.</div>\n"
                     # T1 rv-0b759f3955: a shell comment in a block-quoted fence is not a command.
                     + "\n> ```sh\n> # On this board journalctl is unavailable.\n> tail -f /var/log/messages\n> ```\n",
                     encoding="utf-8")
    assert C.check_bound_docs(decl, tmp_path) == []


_GAP = "DS-P3.364: the contract reads only backtick fences, as main does"


@pytest.mark.parametrize("block", [
    "```sh\njournalctl -u deepskopion-stream -f\n```\n",
    "```sh\njournalctl -u deepskopion-stream -f\n",                     # fence left open at the end of the file
    "    ```sh\n    journalctl -u deepskopion-stream -f\n    ```\n",   # indented backtick fence (T1 rv-4b55c5529d)
    "<pre>\n```sh\njournalctl -u deepskopion-stream -f\n```\n</pre>\n",  # backtick fence in <pre> (T1 rv-f2fc046c84)
    # Forms main's scanner misses too, tracked by DS-P3.364: a tilde fence, a fence in a block quote (also after a
    # quoted #), and a plain <pre> listing (T1 rv-b0295d1941, rv-6bb005685e, rv-45e967244a).
    pytest.param("~~~sh\njournalctl -u deepskopion-stream -f\n~~~\n", marks=pytest.mark.xfail(strict=True, reason=_GAP)),
    pytest.param("> ```sh\n> journalctl -u deepskopion-stream -f\n> ```\n",
                 marks=pytest.mark.xfail(strict=True, reason=_GAP)),
    pytest.param("> ```sh\n> printf '%s' 'startup # check'; journalctl -u deepskopion-stream -f\n> ```\n",
                 marks=pytest.mark.xfail(strict=True, reason=_GAP)),
    pytest.param("<pre>journalctl -u deepskopion-stream -f</pre>\n", marks=pytest.mark.xfail(strict=True, reason=_GAP)),
])
def test_a_journalctl_command_in_a_guide_fails(tmp_path, block):
    decl = _copy_bound(tmp_path)
    guide = tmp_path / "docs" / "getting_started" / "zybo_z7_20.md"
    guide.write_text(guide.read_text(encoding="utf-8") + "\n## Checking the daemon\n\n" + block, encoding="utf-8")
    fails = C.check_bound_docs(decl, tmp_path)
    assert any("journalctl" in f.detail and f.what.startswith("docs/getting_started/zybo_z7_20.md:") for f in fails), \
        [str(f) for f in fails]
