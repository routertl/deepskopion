# rr unknown-llm (AI-generated)
"""Unit tests for sw.stream.testbench.provenance.

Covers the PS-side provenance + ABI-contract helpers behind RTL-P3.636
(verdict self-describes the testbench), RTL-P3.637 (deploy stamp +
RTC-less verdict filenames) and RTL-P2.654 (ABI self-check).
"""

from __future__ import annotations

import datetime
import json

from sw.stream.testbench.provenance import (
    CONTRACT_ABI_VER,
    abi_verdict,
    clock_is_trustworthy,
    gather_provenance,
    read_deploy_stamp,
    verdict_filename,
)
from sw.stream.testbench.provenance import testbench_git_rev as _testbench_git_rev


# ── RTL-P3.637: RTC trustworthiness ──────────────────────────────────────
def test_clock_untrustworthy_at_epoch():
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    assert clock_is_trustworthy(epoch) is False


def test_clock_trustworthy_when_set():
    now = datetime.datetime(2026, 5, 30, tzinfo=datetime.timezone.utc)
    assert clock_is_trustworthy(now) is True


# ── RTL-P3.637: collision-free verdict filenames ─────────────────────────
def test_filename_uses_iso_when_clock_ok(tmp_path):
    p = verdict_filename(tmp_path, "2026-05-30T12-00-00Z", clock_ok=True, monotonic_ns=123)
    assert p.name == "2026-05-30T12-00-00Z.json"


def test_filename_nortc_when_clock_bad(tmp_path):
    p = verdict_filename(tmp_path, "1970-01-01T00-00-00Z", clock_ok=False, monotonic_ns=999)
    assert p.name == "nortc-0000-up999.json"


def test_nortc_sequence_increments_no_collision(tmp_path):
    """The bug: RTC-less boards stamped every run 1970-… and overwrote each
    other. Successive runs must get distinct, ordered names."""
    first = verdict_filename(tmp_path, "1970", clock_ok=False, monotonic_ns=1)
    first.write_text("{}")
    second = verdict_filename(tmp_path, "1970", clock_ok=False, monotonic_ns=2)
    assert first != second
    assert second.name.startswith("nortc-0001-")
    # And they sort in creation order by the zero-padded sequence.
    assert sorted([first.name, second.name]) == [first.name, second.name]


# ── RTL-P2.654: ABI self-check ───────────────────────────────────────────
def test_abi_match():
    v = abi_verdict(CONTRACT_ABI_VER)
    assert v["state"] == "match"


def test_abi_unversioned_when_zero():
    v = abi_verdict(0)
    assert v["state"] == "unversioned"


def test_abi_mismatch_when_different_nonzero():
    v = abi_verdict(CONTRACT_ABI_VER + 0x0001_0000)  # bump major
    assert v["state"] == "mismatch"
    assert v["hw"] != v["expected"]


# ── RTL-P3.636: deploy stamp + provenance assembly ───────────────────────
def test_read_deploy_stamp_missing(tmp_path):
    assert read_deploy_stamp(tmp_path / "nope.json") is None


def test_read_deploy_stamp_valid(tmp_path):
    stamp = {"linux_lock_digest": "abc123", "sw_git_rev": "deadbee"}
    p = tmp_path / "build.json"
    p.write_text(json.dumps(stamp))
    assert read_deploy_stamp(p) == stamp


def test_read_deploy_stamp_corrupt(tmp_path):
    p = tmp_path / "build.json"
    p.write_text("{not valid json")
    assert read_deploy_stamp(p) is None


def test_testbench_git_rev_none_outside_git(tmp_path):
    assert _testbench_git_rev(tmp_path) is None


def test_gather_provenance_records_both_halves():
    identity = {"git_hash": 0x1234, "system_version": 0x02050001,
                "build_date": 0x6a19, "regbank_version": 0x01000000}
    stamp = {"linux_lock_digest": "lockdigest", "sw_git_rev": "swrev123"}
    prov = gather_provenance(identity, deploy_stamp=stamp, tb_git_rev=None, clock_ok=False)
    # Bitstream half preserved + dirty bit decoded from low byte (0x01 → True).
    assert prov["bitstream"]["git_hash"] == 0x1234
    assert prov["bitstream"]["dirty"] is True
    # Testbench half: git_rev falls back to the deploy stamp's sw_git_rev.
    assert prov["testbench"]["git_rev"] == "swrev123"
    assert prov["testbench"]["contract_abi_ver"] == f"{CONTRACT_ABI_VER:#010x}"
    # Lock digest surfaced for at-a-glance pairing.
    assert prov["lock_digest"] == "lockdigest"
    assert prov["clock_trustworthy"] is False


def test_gather_provenance_clean_dirty_bit():
    identity = {"git_hash": 0x1, "system_version": 0x02050000}  # low byte 0
    prov = gather_provenance(identity, deploy_stamp=None, tb_git_rev="abc", clock_ok=True)
    assert prov["bitstream"]["dirty"] is False
    assert prov["testbench"]["git_rev"] == "abc"
    assert prov["lock_digest"] is None
