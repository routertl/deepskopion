# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""A pasted kernel banner must not publish the account and host that built the kernel.

A pasted kernel banner names the account and host that built the kernel, in brackets after the release. The public export gate
refuses it in exported text and accepts the placeholder `builder@buildhost`; the first-boot guide shows the
placeholder.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIRST_BOOT = ROOT / "docs" / "getting_started" / "first_boot.md"

_spec = importlib.util.spec_from_file_location("public_artifact_hygiene", ROOT / "tools" / "public_artifact_hygiene.py")
assert _spec and _spec.loader
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

KIND = "kernel build account and host"


def _findings(data: bytes) -> list[str]:
    return [f for f in H.inspect_bytes("docs/getting_started/first_boot.md", data, export_gate=True) if KIND in f]


def test_the_first_boot_guide_shows_the_placeholder():
    data = FIRST_BOOT.read_bytes()
    assert b"Linux version " in data, "the first-boot guide no longer shows a kernel banner"
    assert b"(" + H.KERNEL_BUILD_ID_PLACEHOLDER + b")" in data
    assert _findings(data) == []


def test_a_real_build_account_and_host_is_refused():
    """Control: the guide's banner with a real build identity in place of the placeholder (the shape the guide had
    before the fix) is refused, and the finding names it."""
    data = FIRST_BOOT.read_bytes().replace(H.KERNEL_BUILD_ID_PLACEHOLDER, b"alice@labhost", 1)
    found = _findings(data)
    assert len(found) == 1 and "alice@labhost" in found[0], found


def test_the_check_runs_only_at_the_export_gate():
    # Built from parts, so this file does not itself carry a banner the export gate refuses.
    data = b"Linux version 6.6.80 (" + b"alice@labhost" + b") #1 SMP\n"
    assert [f for f in H.inspect_bytes("x.md", data, export_gate=False) if KIND in f] == []
    assert [f for f in H.inspect_bytes("x.md", data, export_gate=True) if KIND in f]


def _banner_block() -> bytes:
    """The guide's fenced kernel-banner block alone: the export gate judges it with nothing else on the page."""
    text = FIRST_BOOT.read_bytes()
    start = text.index(b"```text\nLinux version ")
    end = text.index(b"```", start + 8) + 3
    return b"# First boot\n\n" + text[start:end] + b"\n"


def test_the_export_entry_point_refuses_the_mutated_guide(tmp_path):
    """Effect, not detector (T1 rv-c019f9c064, T5 rv-f2ce0eacb0): the hygiene gate the exporter runs on an export
    tree FAILS on the banner with a real build identity and names it, and PASSES (exit 0) on the placeholder."""
    sys.path.insert(0, str(ROOT / "tools"))
    import export_github as E

    block = _banner_block()
    # A synthetic restricted-name roster, as the other export tests pass: a public checkout has no roster, and
    # without one the gate stops before it reads the banner (T5 rv-12734a275c).
    names = tmp_path / "names.yml"
    names.write_text("- name: Example Synthetic Client\n", encoding="utf-8")
    for name, data, expect_fail in (
        ("bad", block.replace(H.KERNEL_BUILD_ID_PLACEHOLDER, b"alice@labhost", 1), True),
        ("good", block, False),
    ):
        tree = tmp_path / name
        dst = tree / "docs" / "getting_started" / "first_boot.md"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(data)
        rc, output = E.run_hygiene_gate(tree, root=ROOT, restricted_names_file=names)
        assert "SCAN INCOMPLETE" not in output, output
        if expect_fail:
            assert rc != 0, output
            assert KIND in output and "alice@labhost" in output, output
        else:
            assert rc == 0, output


def test_any_identity_spelling_is_refused_and_prose_addresses_are_not():
    """T5 rv-f2ce0eacb0: the whole bracketed field is judged; the placeholder passes; an address in prose with no
    kernel banner around it is not a banner."""
    banner = b"Linux version 6.6.80 (" + b"ci+linux@lab-host.local" + b") #1 SMP\n"
    assert [f for f in H.inspect_bytes("x.md", banner, export_gate=True) if KIND in f]
    ok = b"Linux version 6.6.80 (" + H.KERNEL_BUILD_ID_PLACEHOLDER + b") #1 SMP\n"
    assert [f for f in H.inspect_bytes("x.md", ok, export_gate=True) if KIND in f] == []
    prose = b"Questions go to the maintainers (dev@example.org) or the issue tracker.\n"
    assert [f for f in H.inspect_bytes("x.md", prose, export_gate=True) if KIND in f] == []
