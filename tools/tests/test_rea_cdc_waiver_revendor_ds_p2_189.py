"""DS-P2.189 — the REA RR-CDC-002 waiver survives a re-vendor of REA.

DS-T1.21 waived rr_rea_sync_word s1/s2 (quasi-static MCP, REA-REQ-959) inline
in the vendored libs/routertl/rea/rtl/rr_rea_cdc.vhd. Upstream rr-rea has no
such pragma, so the re-vendor 30c53d04 (DS-P3.174) overwrote it and the
DS-T1.21 whole-design CDC gate went red on main. The waiver now lives in the
project-root cdc_lint_waivers.yml and is enforced by tools/cdc_lint_waivers.py.

DS-P2.193: rr-rea 1.12.3 (REA-P2.26) ships that waiver inline upstream, so the
local cdc_lint_waivers.yml entry is gone and a re-vendor reinstalls the pragma
with the pinned bytes. The gate's own rules (scope, stale, evidence) are still
judged here, on the pinned file with the upstream pragma stripped, i.e. a REA
release that carries none.

Every case runs that CLI (the command the pre-commit leg runs) against a
scratch project root, and judges its exit code and output:

* re-vendor: the scratch REA file is exactly the bytes ip.lock pins for the
  installed commit, i.e. what `rr pkg install` lays down, shifted down by an
  upstream-style header edit. With the real cdc_lint_waivers.yml it must stay
  clean (exit 0), and the same tree with the upstream pragma stripped must
  report s1/s2 (exit 1), so the green is not vacuous.
* scope: the waiver covers rr_rea_sync_word s1/s2 only. The same crossing in
  another entity, or on another signal, is still reported.
* stale / evidence: a waiver that matches no finding, or whose evidence file
  no longer says what it cites, is refused.
* the pre-commit gate runs the CLI and refuses the commit on red.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "tools" / "cdc_lint_waivers.py"
HOOK = ROOT / "hooks" / "pre-commit.local"
REA = Path("libs/routertl/rea")
REA_CDC = REA / "rtl" / "rr_rea_cdc.vhd"
EVIDENCE = (REA / "requirements.yml", REA / "constraints" / "rr_rea_scoped.xdc",
            REA / "constraints" / "rr_rea_scoped.sdc")
WAIVERS = "cdc_lint_waivers.yml"
UPSTREAM_CHURN = "".join(f"-- upstream header line {i}\n" for i in range(7))


def _locked_rea_cdc_bytes() -> bytes:
    """The rr_rea_cdc.vhd bytes a re-vendor installs: pinned by ip.lock."""
    lock = yaml.safe_load((ROOT / "ip.lock").read_text(encoding="utf-8"))
    want = lock["routertl/rea"]["file_hashes"][REA_CDC.as_posix()]
    data = (ROOT / REA_CDC).read_bytes()
    assert hashlib.sha256(data).hexdigest() == want, (
        f"{REA_CDC} is not the ip.lock-pinned upstream file; a vendored file must "
        "stay byte-identical to upstream, so its waiver cannot live inline"
    )
    return data


def _scratch(tmp: Path, rea_cdc: str, waivers: str | None) -> Path:
    root = tmp / "proj"
    for rel in EVIDENCE:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, root / rel)
    (root / REA_CDC).parent.mkdir(parents=True, exist_ok=True)
    (root / REA_CDC).write_text(rea_cdc, encoding="utf-8")
    if waivers is not None:
        (root / WAIVERS).write_text(waivers, encoding="utf-8")
    return root


def _gate(root: Path) -> tuple[int, str]:
    out = subprocess.run([sys.executable, str(CLI), "check", "--root", str(root)],
                         cwd=ROOT, text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, check=False)
    return out.returncode, out.stdout


def _revendored() -> str:
    return UPSTREAM_CHURN + _locked_rea_cdc_bytes().decode("utf-8")


_PRAGMA_RE = re.compile(r"\s*--\s*rr-lint-disable-line\s+RR-CDC-002\b[^\n]*")


def _unannotated() -> str:
    """The re-vendored file as a REA release without the upstream s1/s2 pragma."""
    text = _revendored()
    stripped, n = _PRAGMA_RE.subn("", text)
    assert n == 2, (
        f"expected the upstream RR-CDC-002 pragma on s1 and s2 only, found {n}; "
        "the pinned rr_rea_cdc.vhd changed shape"
    )
    return stripped


def _real_waivers() -> str:
    path = ROOT / WAIVERS
    assert path.is_file(), (
        f"REA waiver lost on re-vendor: no {WAIVERS} at the project root, so the "
        "only waiver left is the inline pragma a re-vendor overwrites"
    )
    return path.read_text(encoding="utf-8")


# The rr_rea_sync_word waiver as a fixture, so the scope / stale / evidence
# cases judge the gate's rules, not whether the real file still holds it.
_SYNC_WORD_WAIVER = (
    "version: 1\n"
    "waivers:\n"
    "  - id: fixture-sync-word\n"
    "    rule: RR-CDC-002\n"
    f"    path: {REA_CDC.as_posix()}\n"
    "    entity: rr_rea_sync_word\n"
    "    signals: [s1, s2]\n"
    "    reason: quasi-static MCP (REA-REQ-959)\n"
    "    evidence:\n"
    f"      - file: {EVIDENCE[0].as_posix()}\n"
    "        contains: REA-REQ-959\n"
    f"      - file: {EVIDENCE[1].as_posix()}\n"
    "        contains: rr_rea_sync_word\n"
)


def test_revendored_rea_keeps_its_cdc_waiver(tmp_path) -> None:
    text = _revendored()
    s1 = 1 + next(i for i, line in enumerate(text.splitlines())
                  if line.lstrip().startswith("signal s1 "))

    rc, out = _gate(_scratch(tmp_path / "waived", text, _real_waivers()))
    assert rc == 0, (
        "REA waiver lost on re-vendor: the gate reports the ip.lock-pinned "
        f"rr_rea_cdc.vhd with cdc_lint_waivers.yml applied (exit {rc}):\n{out}"
    )
    # A fixture copy of the waiver, applied on top of the upstream pragma, is stale.
    rc, out = _gate(_scratch(tmp_path / "doubled", text, _SYNC_WORD_WAIVER))
    assert rc == 1 and "fixture-sync-word: stale" in out, (
        "a local waiver duplicating the upstream pragma was not refused as stale "
        f"(exit {rc}):\n{out}"
    )

    # And on the real tree, as the pre-commit leg runs it.
    rc, out = _gate(ROOT)
    assert rc == 0, f"REA waiver lost on re-vendor: real tree gate exit {rc}:\n{out}"

    # Non-vacuity: the same re-vendored tree without the upstream pragma is red.
    bare_rc, bare_out = _gate(_scratch(tmp_path / "bare", _unannotated(), _real_waivers()))
    assert bare_rc == 1 and f"rr_rea_cdc.vhd:{s1}:" in bare_out \
        and f"rr_rea_cdc.vhd:{s1 + 1}:" in bare_out, (
            "re-vendored REA without the pragma must report s1/s2 at their new "
            f"lines {s1}/{s1 + 1} (exit 1), else this check proves nothing:\n{bare_out}"
        )


def test_waiver_does_not_cover_other_entities_or_signals(tmp_path) -> None:
    text = _unannotated()
    start = text.index("entity rr_rea_sync_word is")
    end = text.index("end architecture;", start) + len("end architecture;")
    # The same synchronizer under another entity name, placed FIRST: RR-CDC-002
    # reports a signal name once per file, at its first declaration, so the s1/s2
    # finding now sits in rr_rea_sync_word_copy, which the waiver does not name.
    copy = text[start:end].replace("rr_rea_sync_word", "rr_rea_sync_word_copy")
    other_entity = text[:start] + copy + "\n\n" + text[start:]
    # A second multi-bit 2-FF pair (w1/w2) inside rr_rea_sync_word itself; s1/s2
    # stay, so the waiver is live and only w1/w2 can fail the gate.
    s2_decl = next(line for line in text.splitlines()
                   if line.lstrip().startswith("signal s2 "))
    other_signal = (
        text.replace(s2_decl, s2_decl + "\n" + s2_decl.replace("s2", "w1")
                     + "\n" + s2_decl.replace("s2", "w2"), 1)
        .replace('    attribute ASYNC_REG of s2 : signal is "TRUE";\n',
                 '    attribute ASYNC_REG of s2 : signal is "TRUE";\n'
                 '    attribute ASYNC_REG of w1 : signal is "TRUE";\n'
                 '    attribute ASYNC_REG of w2 : signal is "TRUE";\n', 1)
        .replace("            s2 <= s1;\n",
                 "            s2 <= s1;\n            w1 <= din_i;\n            w2 <= w1;\n", 1)
    )
    assert other_signal.count("w2 <= w1;") == 1, "fixture edit did not apply"
    copy_s1 = 1 + next(i for i, line in enumerate(other_entity.splitlines())
                       if line.lstrip().startswith("signal s1 "))
    w1 = 1 + next(i for i, line in enumerate(other_signal.splitlines())
                  if line.lstrip().startswith("signal w1 "))
    for name, variant, line in (("entity", other_entity, copy_s1),
                                ("signal", other_signal, w1)):
        rc, out = _gate(_scratch(tmp_path / name, variant, _SYNC_WORD_WAIVER))
        assert rc == 1 and f"UNWAIVED {REA_CDC.as_posix()}:{line}:" in out, (
            "a waiver is a blanket file waiver: the "
            f"same crossing on another {name} passed the gate (exit {rc}):\n{out}"
        )


def test_stale_waiver_is_refused(tmp_path) -> None:
    dead = (
        "  - id: dead-pulse-xfer\n"
        "    rule: RR-CDC-002\n"
        f"    path: {REA_CDC.as_posix()}\n"
        "    entity: rr_rea_pulse_xfer\n"
        "    signals: [s1]\n"
        "    reason: matches nothing\n"
        "    evidence:\n"
        f"      - file: {EVIDENCE[0].as_posix()}\n"
        "        contains: REA-REQ-959\n"
    )
    rc, out = _gate(_scratch(tmp_path, _unannotated(), _SYNC_WORD_WAIVER + dead))
    assert rc == 1 and "dead-pulse-xfer: stale" in out, (
        f"a waiver that matches no RR-CDC-002 finding was not refused (exit {rc}):\n{out}"
    )


def test_waiver_with_broken_evidence_is_refused(tmp_path) -> None:
    root = _scratch(tmp_path, _unannotated(), _SYNC_WORD_WAIVER)
    req = root / EVIDENCE[0]
    req.write_text(req.read_text(encoding="utf-8").replace("REA-REQ-959", "REA-REQ-XXX"),
                   encoding="utf-8")
    rc, out = _gate(root)
    assert rc == 1 and "no longer contains 'REA-REQ-959'" in out, (
        "a waiver whose cited requirement is gone from its evidence file was "
        f"not refused (exit {rc}):\n{out}"
    )


_LEG_MARKER = "pre-commit does not gate on the RR-CDC-002 design closure"


def _leg() -> str:
    lines = HOOK.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith("# ── DS-P2.189")), None)
    assert start is not None, f"{_LEG_MARKER}: hooks/pre-commit.local has no DS-P2.189 leg"
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("# ── ")), len(lines))
    return "\n".join(lines[start:end])


def test_precommit_runs_the_cdc_gate_and_refuses_on_red(tmp_path) -> None:
    for gate_rc, want_rc in ((0, 0), (1, 1), (2, 1)):
        work = tmp_path / f"rc_{gate_rc}"
        work.mkdir()
        calls = work / "argv.log"
        stand_in = work / "python3"
        stand_in.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexit {gate_rc}\n')
        stand_in.chmod(0o755)
        script = f'DS_GUARD_PY="{stand_in}"\n{_leg()}\nexit 0\n'
        out = subprocess.run(["bash", "-c", script], cwd=ROOT, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        argv = calls.read_text().splitlines() if calls.is_file() else []
        assert "tools/cdc_lint_waivers.py check" in argv, (
            f"{_LEG_MARKER}: the DS-P2.189 leg never runs `tools/cdc_lint_waivers.py "
            f"check` (stand-in saw {argv})\n{out.stdout}"
        )
        assert out.returncode == want_rc, (
            f"{_LEG_MARKER}: gate exit {gate_rc} -> hook exit {out.returncode}, "
            f"want {want_rc}\n{out.stdout}"
        )
