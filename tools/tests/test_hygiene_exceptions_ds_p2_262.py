"""DS-P2.262 class F: the reviewed exact-match exception list of public_artifact_hygiene.

Owner ruling 2026-11-11: an exception names one archive member and one exact string.
A listed string passes only in its listed member; the same string in another member
fails; an unlisted string in a listed member fails. Literals are assembled from parts
so this module is itself clean for the scanner.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "public_artifact_hygiene.py"
SPEC = importlib.util.spec_from_file_location("public_artifact_hygiene_fexc", TOOL)
assert SPEC and SPEC.loader
H = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(H)

IMG = "img/uImage!uboot-payload!gzip[2]@1"
DROPBEAR = IMG + "!usr/sbin/dropbear"
RSA_HDR = ("-----BEGIN RSA " + "PRIVATE KEY-----").encode()
PKCS8_HDR = ("-----BEGIN " + "PRIVATE KEY-----").encode()
IPADDR = IMG + "!usr/lib/python3.12/ipaddress.pyc"
NET_A = ("10.0" + ".0.0").encode()
NET_OTHER = ("10.1" + ".2.3").encode()
WHEEL = IMG + "!usr/lib/python3.12/ensurepip/_bundled/pip-25.0.1-py3-none-any.whl"
IDNA = WHEEL + "!pip/_vendor/idna/idnadata.py"
IDNA_CONST = ("21" + "2600002127").encode()


def _scan(name: str, literal: bytes) -> list[str]:
    return H.inspect_bytes(name, b"prefix " + literal + b" suffix\n")


def test_a_listed_string_passes_in_its_listed_member():
    assert _scan(DROPBEAR, RSA_HDR) == []
    assert _scan(IPADDR, NET_A) == []
    assert _scan(IDNA, IDNA_CONST) == []


@pytest.mark.parametrize("other", [
    IMG + "!usr/sbin/sshd",               # another file
    IMG + "!usr/sbin/dropbear.bak",       # the listed path plus a suffix
    IMG + "!xusr/sbin/dropbear",          # the listed path inside a longer name
    "usr/sbin/dropbear",                  # a repository file, not an archive member
])
def test_the_same_string_in_another_member_fails(other):
    found = _scan(other, RSA_HDR)
    assert len(found) == 1 and "private key" in found[0], found


def test_the_same_string_in_another_wheel_member_fails():
    found = _scan(WHEEL + "!pip/_vendor/idna/core.py", IDNA_CONST)
    assert len(found) == 1 and "bench cable serial" in found[0], found


def test_an_unlisted_string_in_a_listed_member_fails():
    found = _scan(DROPBEAR, PKCS8_HDR)
    assert len(found) == 1 and "private key" in found[0], found
    found = _scan(IPADDR, NET_OTHER)
    assert len(found) == 1 and "private-subnet assumption" in found[0], found


def test_every_entry_of_the_reviewed_file_takes_effect():
    entries = H.load_reviewed_exceptions()
    assert entries, "the reviewed exceptions file is empty"
    for member, kind, literal in sorted(entries):
        name = IMG + "!" + member
        found = [f for f in _scan(name, literal) if f": {kind}: " in f]
        assert found == [], f"entry {member} / {kind} does not excuse its literal: {found}"


@pytest.mark.parametrize("body, why", [
    ("entries:\n  - {member: 'usr/*', kind: k, literal: [a], source: s}\n", "glob"),
    ("entries:\n  - {member: usr/x, kind: k, literal: abc, source: s}\n", "literal not a list"),
    ("entries:\n  - {member: usr/x, kind: k, literal: [a]}\n", "missing source"),
    ("entries:\n  - {member: usr/x, kind: k, literal: [a], source: s, extra: 1}\n", "extra key"),
    ("entries: {}\n", "entries not a list"),
])
def test_a_malformed_exceptions_file_is_an_error(tmp_path, body, why):
    f = tmp_path / f"exc_{abs(hash(why))}.yml"
    f.write_text(body, encoding="utf-8")
    with pytest.raises(H.InspectionError):
        H.load_reviewed_exceptions(f)


def test_a_missing_exceptions_file_excuses_nothing(tmp_path, monkeypatch):
    assert H.load_reviewed_exceptions(tmp_path / "absent.yml") == frozenset()
    monkeypatch.setattr(H, "EXCEPTIONS_FILE", tmp_path / "absent.yml")
    found = _scan(DROPBEAR, RSA_HDR)
    assert len(found) == 1 and "private key" in found[0], found


def test_the_exceptions_file_itself_passes_the_scanner():
    rel = "tools/public_artifact_hygiene_exceptions.yml"
    data = (ROOT / rel).read_bytes()
    assert H.inspect_bytes(rel, data) == []
    assert H.inspect_bytes(rel, data, export_gate=True, rel_path=rel) == []
    # Positive control: the same file with one literal written whole is caught.
    whole = data.replace(b'["-----BEGIN RSA ", "PRIVATE KEY-----"]',
                         b'["' + RSA_HDR + b'"]', 1)
    assert whole != data
    found = H.inspect_bytes(rel, whole)
    assert len(found) == 1 and "private key" in found[0], found
