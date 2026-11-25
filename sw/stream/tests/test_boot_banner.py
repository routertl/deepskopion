# rr unknown-llm (AI-generated)
"""Unit tests for the boot-identity banner (DS-P3.85). No board needed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boot_banner import (  # noqa: E402
    BitstreamIdentity, format_boot_banner, read_bitstream_identity, log_boot_banner,
)


def _joined(lines):
    return "\n".join(lines)


def test_no_expectation_is_unknown():
    ident = BitstreamIdentity(git_hash=0x7c3614aa, system_version=0x12301, abi_ver=0x20000)
    lines, verdict = format_boot_banner("v0.3.0-5-gabc", ident)
    assert verdict == "UNKNOWN"
    assert "0x7c3614aa" in _joined(lines)
    assert "no expected manifest" in _joined(lines).lower()


def test_matching_manifest_is_pass():
    ident = BitstreamIdentity(git_hash=0x7c3614aa)
    lines, verdict = format_boot_banner(
        "v0.3.0", ident,
        expected_daemon_ver="v0.3.0", expected_git_hash="0x7c3614aa")
    assert verdict == "PASS"
    assert "matches" in _joined(lines).lower()


def test_hash_mismatch_is_stale():
    ident = BitstreamIdentity(git_hash=0xdeadbeef)
    lines, verdict = format_boot_banner(
        "v0.3.0", ident, expected_git_hash="0x7c3614aa")
    assert verdict == "STALE"
    assert "STALE DEPLOYMENT" in _joined(lines)


def test_sw_mismatch_is_stale():
    ident = BitstreamIdentity(git_hash=0x1)
    _, verdict = format_boot_banner(
        "v0.2.0-stale", ident, expected_daemon_ver="v0.3.0")
    assert verdict == "STALE"


def test_hex_normalisation_forms():
    ident = BitstreamIdentity(git_hash=0x00abcdef)
    # plain-hex (no 0x) and 0x-prefixed must both match
    _, v1 = format_boot_banner("x", ident, expected_git_hash="abcdef")
    _, v2 = format_boot_banner("x", ident, expected_git_hash="0x00abcdef")
    assert v1 == "PASS" and v2 == "PASS"


def test_missing_identity_renders_dashes():
    lines, verdict = format_boot_banner("x", BitstreamIdentity())
    assert "0x--------" in _joined(lines)
    assert verdict == "UNKNOWN"


def test_read_identity_best_effort():
    class FlakyRegbank:
        def read_u32(self, off):
            if off == 0x0000:
                return 0x7c3614aa
            raise OSError("boom")
    ident = read_bitstream_identity(FlakyRegbank())
    assert ident.git_hash == 0x7c3614aa
    assert ident.system_version is None and ident.abi_ver is None


def test_log_boot_banner_emits_and_returns_verdict():
    out = []
    v = log_boot_banner(out.append, "v0.3.0", regbank=None)
    assert v == "UNKNOWN" and any("boot identity" in l for l in out)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
