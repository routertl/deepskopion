"""DS-P2.187 — pytest gate: the rest of the DS-P2.139 contract audit.

The 2026-09-10 audit (733cd8ce), re-measured on origin/main e2096d2d before
this fix, found four contracts the gates could not hold:

* stimulus, emio, responder, de25_interconnect_shell: ``ip:`` named no HDL
  entity, so ``rr sim contract-check`` stopped at ``no-matching-hdl`` and
  never compared a port.
* edge_datalogger: ``test_dir`` was the shared ``nonstd/`` parent, so
  coverage-map walked level_datalogger's suite and reported LEVEL-REQ-10/50/
  51/52 as edge orphans. EDGE-REQ-51/52 were tagged by test_edge_datalogger
  since DS-P3.24 but never declared (two more orphans), and the entity's
  ED_MSG_LIFECYCLE / ED_MAX_INTERVAL / ED_MAX_NEW ports were undeclared.
* stimulus: ``test_dir`` was the whole cocotb tree, giving 143 orphans from
  other IPs' suites.
* zs_msg_framer: ZSF-REQ-50/51/52 (runtime PAYLOAD_WORDS_LIVE) had no
  witness. The witness written for ZSF-REQ-52 found a DUT defect: the clamp
  was dead code (its variable's range rejected the very values it clamps).

Each static check runs the real ``rr sim contract-check`` /
``rr sim coverage-map`` CLI, the surface every caller uses. The framer clamp
check runs the cocotb witness through ``rr sim run`` (about a minute) and fails
with the cocotb failure text.

Host requirements: ``rr`` on PATH with nvc. A fresh clone has no vendored
packages under ``libs/``, so the sim check runs ``rr workspace sync`` first.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STIMULUS = "hw/rtl/stimulus"
EMIO = "hw/rtl/emio"
RESPONDER = "hw/rtl/responder"
DE25 = "hw/rtl/de25"
EDGE = "hw/rtl/datalogger/nonstd/edge_datalogger"
FRAMER = "hw/rtl/datalogger/infra/zs_msg_framer"

EDGE_REQS = [
    "EDGE-REQ-50", "EDGE-REQ-51", "EDGE-REQ-52", "EDGE-REQ-53", "EDGE-REQ-54",
    "EDGE-REQ-55", "EDGE-REQ-56", "EDGE-REQ-57", "EDGE-REQ-58",
    # DS-P2.16: Stream-RLE mode (test_edge_streamer.py).
    "EDGE-REQ-59", "EDGE-REQ-60", "EDGE-REQ-61", "EDGE-REQ-62", "EDGE-REQ-63",
    # DS-P3.117: the max-frequency confirmation gate (G_MAXFREQ_CONFIRM).
    "EDGE-REQ-64",
]
STIMULUS_REQS = sorted([
    "STIM-REQ-1", "STIM-REQ-2", "STIM-REQ-3", "STIM-REQ-4", "STIM-REQ-5",
    "STIM-REQ-6", "STIM-REQ-7", "STIM-REQ-8", "STIM-REQ-9", "STIM-REQ-10",
    "STIM-REQ-11", "STIM-REQ-12",
    # DS-P2.257 (fe691f0a): the UART break pattern; DS-P2.285 adds it here.
    "STIM-REQ-13",
    "STIM-SEAM-REQ-1", "STIM-SEAM-REQ-2",
    "STIM-SEAM-REQ-3", "STIM-SEAM-REQ-4",
    # DS-P1.90 (8ebcce01): the Zybo wiring, G_STIM_DRIVES_EMIO true.
    "STIM-SEAM-REQ-5", "STIM-SEAM-REQ-6", "STIM-SEAM-REQ-7",
])
FRAMER_REQS = sorted([
    "ZSF-REQ-1", "ZSF-REQ-2", "ZSF-REQ-3", "ZSF-REQ-4", "ZSF-REQ-5",
    "ZSF-REQ-6", "ZSF-REQ-7", "ZSF-REQ-8", "ZSF-REQ-10", "ZSF-REQ-11",
    "ZSF-REQ-20", "ZSF-REQ-30", "ZSF-REQ-40", "ZSF-REQ-50", "ZSF-REQ-51",
    "ZSF-REQ-52",
])
FRAMER_LIVE = "test_zs_msg_framer_live_payload"
FRAMER_WITNESSES = {
    "ZSF-REQ-50": {"test_live_count_selects_frame_length",
                   "test_live_count_latched_on_handshake"},
    "ZSF-REQ-51": {"test_undriven_live_count_emits_full_frame"},
    "ZSF-REQ-52": {"test_live_count_clamps_to_port_capacity"},
}

# Every sim rewrites this pid file. It is untracked and ignored since
# DS-P3.188; restoring it keeps the checkout as the run found it.
WORK_LOCK = ROOT / "hw" / "sim" / "work.lock"
NVC_WORK_DIRS = (ROOT / "hw" / "sim" / "work" / "nvc", ROOT / "sim" / "work" / "nvc")


def _rr() -> str:
    rr = shutil.which("rr")
    assert rr, "rr is not on PATH; this gate cannot run without it"
    return rr


def _rr_json(*args: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [_rr(), "sim", *args, "--json"], cwd=ROOT, text=True,
        capture_output=True, timeout=300,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"unparsed_stdout": proc.stdout[-2000:],
                   "stderr": proc.stderr[-2000:]}
    return proc.returncode, payload


def _contract_issues(ip_dir: str) -> tuple[int, dict, list[str]]:
    rc, payload = _rr_json("contract-check", ip_dir, "--fail-undeclared")
    issues = [
        f"[{i.get('category')}] {i.get('message')}"
        for r in (payload.get("data") or {}).get("ip_reports") or []
        for i in r.get("issues", []) if i.get("severity") != "warn"
    ]
    return rc, payload, issues


def _coverage_report(ip_dir: str) -> dict:
    rc, payload = _rr_json("coverage-map", ip_dir)
    reports = (payload.get("data") or {}).get("ip_reports") or []
    assert len(reports) == 1, (
        f"coverage-map {ip_dir} produced no IP report (rc={rc}); the contract "
        f"failed to load: {json.dumps(payload)[:1500]}"
    )
    return reports[0]


# ── Contracts that named no entity ──────────────────────────────────────────

@pytest.mark.parametrize(
    "ip_dir", [STIMULUS, EMIO, RESPONDER, DE25],
    ids=["stimulus", "emio", "responder", "de25"],
)
def test_contract_names_its_entity(ip_dir: str) -> None:
    """Each contract resolves to a real entity and matches its ports.

    Before DS-P2.187 all four failed with no-matching-hdl: contract-check
    never got as far as comparing a single port. Each now pins ``hdl_file``.
    """
    rc, payload, issues = _contract_issues(ip_dir)
    assert rc == 0 and payload.get("ok") is True and not issues, (
        f"contract-check {ip_dir} FAILED (rc={rc}): the contract does not "
        f"resolve to, or match, its HDL entity: "
        f"{issues or json.dumps(payload)[:1500]}"
    )


# ── edge_datalogger ─────────────────────────────────────────────────────────

def test_edge_contract_declares_every_port() -> None:
    """ED_MSG_LIFECYCLE / ED_MAX_INTERVAL / ED_MAX_NEW are declared."""
    rc, payload, issues = _contract_issues(EDGE)
    assert rc == 0 and payload.get("ok") is True and not issues, (
        f"contract-check {EDGE} FAILED (rc={rc}): edge_datalogger ports "
        f"undeclared or mismatched: {issues or json.dumps(payload)[:1500]}"
    )


def test_edge_scope_excludes_foreign_suites() -> None:
    """coverage-map for edge_datalogger reads the edge suite only.

    With test_dir at the shared nonstd/ parent, level_datalogger's LEVEL-REQ
    tags showed up as edge orphans.
    """
    report = _coverage_report(EDGE)
    foreign = sorted(o for o in report["orphan"] if not o.startswith("EDGE-"))
    assert not foreign, (
        f"edge_datalogger coverage scope reaches a foreign suite: orphan tags "
        f"{foreign} belong to other IPs (test_dir too wide)"
    )


def test_edge_overflow_reqs_declared_and_covered() -> None:
    """EDGE-REQ-51/52 are declared, and the whole edge contract is covered.

    test_edge_datalogger has tagged EDGE-REQ-51/52 since DS-P3.24; the REQs
    were never declared, so both tags were orphans and the ED_ERRORS overflow
    behaviour sat outside the contract.
    """
    report = _coverage_report(EDGE)
    edge_orphans = sorted(o for o in report["orphan"] if o.startswith("EDGE-"))
    gaps = {k: report[k] for k in
            ("missing", "vacuous", "unreferenced", "undetermined") if report[k]}
    assert report["declared_reqs"] == EDGE_REQS and not edge_orphans \
        and sorted(report["covered"]) == sorted(EDGE_REQS) and not gaps, (
        f"edge_datalogger contract not reconciled to its tests: "
        f"declared={report['declared_reqs']} (want {EDGE_REQS}), "
        f"EDGE orphans={edge_orphans}, covered={len(report['covered'])}/{len(EDGE_REQS)}, "
        f"gaps={gaps}"
    )


# ── stimulus ────────────────────────────────────────────────────────────────

def test_stimulus_scope_is_its_own_suite() -> None:
    """Zero orphans, and the core-seam REQs still in reach.

    test_dir was the whole cocotb tree (143 orphans). It is now
    tests/stimulus, with the core-seam suite moved under it, so narrowing the
    scope did not drop STIM-SEAM-REQ-1..4 out of reach.
    """
    report = _coverage_report(STIMULUS)
    gaps = {k: report[k] for k in
            ("missing", "vacuous", "unreferenced", "undetermined") if report[k]}
    assert not report["orphan"] and sorted(report["covered"]) == STIMULUS_REQS \
        and not gaps, (
        f"stimulus coverage scope wrong: {len(report['orphan'])} orphan tags "
        f"(first: {report['orphan'][:5]}), covered={len(report['covered'])}/{len(STIMULUS_REQS)}, "
        f"gaps={gaps}"
    )


# ── zs_msg_framer ───────────────────────────────────────────────────────────

def test_framer_live_payload_reqs_witnessed() -> None:
    """Regression lock: ZSF-REQ-50/51/52 keep named, asserting witnesses and
    the framer contract stays fully covered."""
    report = _coverage_report(FRAMER)
    tagged: dict[str, set[str]] = {}
    for t in report["tags"]:
        if Path(t["test_path"]).stem == FRAMER_LIVE and t["assert_count"] > 0:
            tagged.setdefault(t["req_id"], set()).add(t["function"])
    lost = {req: sorted(want - tagged.get(req, set()))
            for req, want in FRAMER_WITNESSES.items()
            if want - tagged.get(req, set())}
    gaps = {k: report[k] for k in
            ("missing", "orphan", "vacuous", "unreferenced", "undetermined")
            if report[k]}
    assert not lost and sorted(report["covered"]) == FRAMER_REQS and not gaps, (
        f"zs_msg_framer live-payload witnesses lost={lost}; "
        f"covered={len(report['covered'])}/16 gaps={gaps}"
    )


@pytest.fixture(scope="module")
def synced_workspace() -> str:
    rr = _rr()
    proc = subprocess.run([rr, "workspace", "sync"], cwd=ROOT, text=True,
                          capture_output=True, timeout=300)
    if proc.returncode != 0:
        pytest.fail(f"rr workspace sync failed (exit {proc.returncode}):\n"
                    f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    return rr


def _run_cocotb(rr: str, module: str, testcase: str) -> str | None:
    """Run one cocotb testcase; return its failure message, or None if it passed."""
    results = ROOT / "sim" / "results" / module / "results.xml"
    if results.exists():
        results.unlink()
    for work in NVC_WORK_DIRS:
        shutil.rmtree(work, ignore_errors=True)
    lock = WORK_LOCK.read_bytes() if WORK_LOCK.exists() else None
    env = os.environ.copy()
    env["RR_SIM_QUEUE_WAIT"] = "1"
    env["COCOTB_TEST_FILTER"] = rf"(?:^|\.){testcase}$"
    try:
        proc = subprocess.run(
            [rr, "sim", "run", module, "--pass-env", "COCOTB_TEST_FILTER"],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=1200)
    finally:
        if lock is not None:
            WORK_LOCK.write_bytes(lock)
    if not results.is_file():
        pytest.fail(f"SIM DID NOT RUN: rr sim run {module} (exit {proc.returncode}) "
                    f"wrote no results.xml\n{proc.stdout[-3000:]}\n{proc.stderr[-1500:]}")
    for case in ET.parse(results).iter("testcase"):
        if case.get("name") != testcase:
            continue
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if bad is None:
            if case.find("skipped") is not None:
                pytest.fail(f"SIM DID NOT RUN: {module}.{testcase} was skipped")
            return None
        log = ROOT / "hw" / "sim" / "cocotb" / "logs" / f"{module}.log"
        fatal = ""
        if log.is_file():
            fatal = "\n".join(line for line in log.read_text(errors="replace")
                              .splitlines() if "Fatal" in line)
        return f"{bad.get('message') or ''}\n{bad.text or ''}\n{fatal}"
    pytest.fail(f"SIM DID NOT RUN: {module}.{testcase} is not in {results}")


def test_framer_clamps_live_count_in_sim(synced_workspace: str) -> None:
    """ZSF-REQ-52: PAYLOAD_WORDS_LIVE 0 and >G_PAYLOAD_WORDS are clamped.

    Before DS-P2.187 the clamp's variable was ``natural range 1 to
    G_PAYLOAD_WORDS``: assigning 0 or 5 to it is a range violation, so nvc
    stopped with a Fatal before the clamp ran.
    """
    failure = _run_cocotb(synced_workspace, FRAMER_LIVE,
                          "test_live_count_clamps_to_port_capacity")
    assert failure is None, f"ZSF-REQ-52 framer clamp sim FAILED: {failure}"
