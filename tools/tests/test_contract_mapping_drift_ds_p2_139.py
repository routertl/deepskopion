# rr unknown-llm (AI-generated)
"""DS-P2.139 — pytest gate: requirement contracts match the HDL and their tests.

The demo health sweep found contracts that no longer described their RTL and
REQs whose tests the coverage gate could not see:

* uart_datalogger: requirements.yml did not load at all (an unknown
  ``status:`` key on UART-REQ-16), so contract-check and coverage-map both
  died on it. Behind that, coverage was 8/13.
* arbiter: the contract declared record ports DIN/DOUT; the entity has the
  flattened DIN_DATA/DIN_VALID/DOUT_DATA/DOUT_VALID/DOUT_EMPTY/DOUT_FULL.
* datalogger_gather: the contract declared DOUT_DATA; the entity has
  DOUT_PAYLOAD plus DOUT_DATALOGGER_ID/DOUT_MSG_TYPE and the edge taps.

This gate runs the real ``rr sim contract-check`` / ``rr sim coverage-map``
CLIs (the surface every caller uses) and pins what a fix must not give back:

* the UART contract loads, and the UART/arbiter/gather contracts match their
  entities (plus the arbiter generics, which contract-check does not compare);
* the exact UART declared REQ set (deleting or renaming a REQ block is RED);
* a strict UART verdict: ``missing`` is exactly the open gaps UART-REQ-16
  (late enable) and UART-REQ-17 (baud band) that are still declared, and
  nothing is orphaned, vacuous, unreferenced or parked (stripping a
  ``@requires`` tag is RED). UART-REQ-14 left the open set when DS-P2.179
  landed its two witnesses;
* the UART-REQ-14 text keeps both halves of its obligation (DS-P2.182):
  8N1 is never detected as has-parity, and 8E1/8O1 is still detected
  without a spurious error. coverage-map never reads ``desc``, so only
  these pins see the 8E1/8O1 sentence deleted;
* the named witness test(s) for each repaired REQ, credited as covered
  (removing one witness's tag is RED even while another keeps the REQ
  covered);
* a strict arbiter verdict over its exact 13 REQs.

Every pin above goes red on a mutant that still loads as a valid
requirements.yml / ``@requires`` set; none relies on breaking the schema.

DS-P2.183: the three UART pins are independent, so each one answers only its
own question. Before, the REQ-set, strict-coverage and witness pins all read
coverage-map of the real contract: a contract that failed to load reddened
every one of them with the same "failed to load" text, and a deleted REQ
reddened the coverage pin too. Now:

* ``test_uart_contract_loads`` alone runs the real loader on the real file;
* ``test_uart_declared_reqs_are_pinned`` reads the REQ ids from the YAML;
* the coverage and witness pins run coverage-map on a projection of the
  contract that keeps only what those verdicts read (``ip``, ``version`` and
  each REQ's ``id``/``kind``/``desc``), next to the real tests. A schema
  defect elsewhere in the file is the load pin's finding, not theirs, and the
  coverage pin expects only the open gaps the contract still declares.

When the file is not even YAML, or a REQ has no ``id``/``desc``, the
projection cannot be built; the dependent pins then fail with a
``PRECONDITION:`` message, never with their own invariant's text.

Static analysis only: no simulator. It needs ``rr`` on PATH, as every
deepskopion gate does.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
UART = "hw/rtl/datalogger/uart"
ARBITER = "hw/rtl/datalogger/infra/arbiter"
GATHER = "hw/rtl/datalogger/infra/datalogger_gather"
UART_TESTS = "hw/sim/cocotb/tests/datalogger/uart"

UART_REQS = [
    "UART-REQ-1", "UART-REQ-2", "UART-REQ-3", "UART-REQ-4",
    "UART-REQ-10", "UART-REQ-11", "UART-REQ-12", "UART-REQ-13",
    "UART-REQ-14", "UART-REQ-15", "UART-REQ-16", "UART-REQ-17",
    "UART-REQ-18", "UART-REQ-19",
]

# Known-open implementation gaps, stated in their REQ text: no witness test
# exists, so coverage-map must report exactly these as "missing".
UART_OPEN_REQS = ["UART-REQ-16", "UART-REQ-17"]

# REQ -> the test functions that must carry its tag (file stem, function).
UART_WITNESSES = {
    "UART-REQ-13": {("test_uart_sparse_flush", "single_byte_flushes"),
                    ("test_uart_sparse_flush", "byte_gap_byte_both_flush")},
    "UART-REQ-14": {("test_uart_no_spurious_parity", "no_spurious_parity_on_clean_8n1"),
                    ("test_uart_parity_detected", "parity_detected_on_8e1_and_8o1")},
    "UART-REQ-15": {("test_uart_reset_reachable", "reset_reachable_from_sw")},
}

ARBITER_REQS = sorted([
    "ARB-REQ-1", "ARB-REQ-2", "ARB-REQ-10", "ARB-REQ-11", "ARB-REQ-20",
    "ARB-REQ-21", "ARB-REQ-22", "ARB-REQ-30", "ARB-REQ-31", "ARB-REQ-40",
    "ARB-REQ-41", "ARB-REQ-50", "ARB-REQ-51",
])


def _rr_json(*args: str) -> tuple[int, dict]:
    rr = shutil.which("rr")
    assert rr, "rr is not on PATH; this gate cannot run without it"
    proc = subprocess.run(
        [rr, "sim", *args, "--json"], cwd=ROOT, text=True,
        capture_output=True, timeout=300,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"unparsed_stdout": proc.stdout[-2000:],
                   "stderr": proc.stderr[-2000:]}
    return proc.returncode, payload


def _contract(ip_dir: str) -> tuple[int, dict]:
    return _rr_json("contract-check", ip_dir, "--fail-undeclared")


def _coverage_payload(ip_dir: str, *extra: str) -> tuple[int, dict]:
    return _rr_json("coverage-map", ip_dir, *extra)


def _coverage_report(ip_dir: str) -> dict:
    rc, payload = _coverage_payload(ip_dir)
    reports = (payload.get("data") or {}).get("ip_reports") or []
    assert len(reports) == 1, (
        f"coverage-map {ip_dir} produced no IP report (rc={rc}); the contract "
        f"failed to load: {json.dumps(payload)[:1500]}"
    )
    report = reports[0]
    report["_rc"] = rc
    return report


def _uart_contract() -> dict:
    """The UART requirements.yml as plain YAML, with no schema validation."""
    try:
        contract = yaml.safe_load((ROOT / UART / "requirements.yml").read_text())
    except yaml.YAMLError as exc:
        pytest.fail(f"PRECONDITION: UART requirements.yml is not YAML: {exc}")
    reqs = (contract or {}).get("functional_contract") if isinstance(
        contract, dict) else None
    if not isinstance(reqs, list) or not all(
            isinstance(r, dict) and r.get("id") and r.get("desc") for r in reqs):
        pytest.fail("PRECONDITION: UART requirements.yml has no functional_contract "
                    "list of REQs that each carry an id and a desc")
    return contract


@pytest.fixture(scope="module")
def uart_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """coverage-map of the UART contract's REQ projection over the real tests.

    The projection is what the missing/orphan/vacuous/unreferenced/
    undetermined verdicts read; the rest of the file (ports, parameters, any
    unknown key) is for the loader and contract-check, pinned separately.
    """
    contract = _uart_contract()
    projected = {
        "ip": contract.get("ip"),
        "version": contract.get("version"),
        "functional_contract": [
            {k: r[k] for k in ("id", "kind", "desc") if k in r}
            for r in contract["functional_contract"]
        ],
    }
    ip_dir = tmp_path_factory.mktemp("uart_req_projection")
    (ip_dir / "requirements.yml").write_text(yaml.safe_dump(projected, sort_keys=False))
    rc, payload = _coverage_payload(str(ip_dir), "--tests", str(ROOT / UART_TESTS))
    reports = (payload.get("data") or {}).get("ip_reports") or []
    if len(reports) != 1:
        pytest.fail(
            f"PRECONDITION: coverage-map could not read the UART REQ projection "
            f"(rc={rc}): {json.dumps(payload)[:1500]}")
    return reports[0]


def test_uart_contract_loads() -> None:
    """The UART requirements.yml must pass schema validation and load.

    Before DS-P2.139 UART-REQ-16 carried an unknown ``status:`` key, so the
    loader rejected the whole file and every downstream gate (contract-check,
    coverage-map) died before looking at a single REQ.
    """
    rc, payload = _coverage_payload(UART)
    load_errors = payload.get("errors") or []
    reports = (payload.get("data") or {}).get("ip_reports") or []
    assert not load_errors and len(reports) == 1, (
        f"UART requirements.yml failed to load (rc={rc}): "
        f"{load_errors or json.dumps(payload)[:1500]}"
    )


@pytest.mark.parametrize("ip_dir", [UART, ARBITER, GATHER])
def test_contract_matches_hdl(ip_dir: str) -> None:
    rc, payload = _contract(ip_dir)
    issues = [
        f"[{i.get('category')}] {i.get('message')}"
        for r in (payload.get("data") or {}).get("ip_reports") or []
        for i in r.get("issues", []) if i.get("severity") != "warn"
    ]
    assert rc == 0 and payload.get("ok") is True, (
        f"contract-check {ip_dir} FAILED (rc={rc}): requirements.yml does not "
        f"match the HDL entity: {issues or json.dumps(payload)[:1500]}"
    )


def _vhdl_generics(vhd: Path, entity: str) -> list[str]:
    text = re.sub(r"--[^\n]*", "", vhd.read_text())
    m = re.search(rf"entity\s+{entity}\s+is\s+generic\s*\((.*?)\)\s*;\s*port\b",
                  text, re.IGNORECASE | re.DOTALL)
    assert m, f"no generic clause found for entity {entity} in {vhd}"
    return [g.split(":")[0].strip().upper() for g in m.group(1).split(";")]


def test_arbiter_parameters_match_entity_generics() -> None:
    """contract-check compares ports only; it never reads ``parameters:``.

    DS-P2.139 added G_DATA_WIDTH (DS-P3.29 made the arbiter width
    parametric). Dropping it again leaves contract-check green, so the
    generic list is compared to the entity here.
    """
    declared = [p["name"].upper() for p in yaml.safe_load(
        (ROOT / ARBITER / "requirements.yml").read_text())["parameters"]]
    hdl = _vhdl_generics(ROOT / ARBITER / "arbiter.vhd", "arbiter")
    assert sorted(declared) == sorted(hdl), (
        f"arbiter parameters do not match the entity generics: "
        f"undeclared={sorted(set(hdl) - set(declared))} "
        f"not-in-HDL={sorted(set(declared) - set(hdl))}"
    )


def test_uart_declared_reqs_are_pinned() -> None:
    declared = [r["id"] for r in _uart_contract()["functional_contract"]]
    assert declared == UART_REQS, (
        "UART declared REQ set changed: a requirement was deleted or renamed "
        f"instead of reconciled: {declared}"
    )


def test_uart_coverage_is_strict(uart_report: dict) -> None:
    """Every declared UART REQ is covered except the still-declared open gaps.

    Which REQs are declared is test_uart_declared_reqs_are_pinned's question;
    this pin judges the coverage of whatever set is declared, so a deleted
    open REQ reddens that pin and not this one.
    """
    expected = [r for r in UART_OPEN_REQS if r in uart_report["declared_reqs"]]
    gaps = {k: uart_report[k] for k in
            ("orphan", "vacuous", "unreferenced", "undetermined") if uart_report[k]}
    assert uart_report["missing"] == expected and not gaps, (
        f"UART coverage-map not strict: missing={uart_report['missing']} "
        f"(expected exactly the open gaps {expected}), other gaps={gaps}"
    )


@pytest.mark.parametrize("req_id", sorted(UART_WITNESSES))
def test_uart_req_has_named_witness(uart_report: dict, req_id: str) -> None:
    tagged = {
        (Path(t["test_path"]).stem, t["function"])
        for t in uart_report["tags"]
        if t["req_id"] == req_id and t["assert_count"] > 0
    }
    missing = UART_WITNESSES[req_id] - tagged
    assert not missing, (
        f"{req_id} lost its witness test(s) {sorted(missing)}; "
        f"tagged now: {sorted(tagged)}"
    )
    assert req_id in uart_report["covered"], (
        f"{req_id} is tagged but NOT covered: coverage-map rejects the tag "
        f"(missing={uart_report['missing']}, "
        f"unreferenced={uart_report['unreferenced']}, "
        f"vacuous={uart_report['vacuous']})"
    )


def _uart_req_desc(req_id: str) -> str:
    """The REQ's ``desc``, whitespace-collapsed, as the loader sees it."""
    descs = [r.get("desc", "") for r in _uart_contract()["functional_contract"]
             if r.get("id") == req_id]
    assert len(descs) == 1, f"{req_id} declared {len(descs)} times"
    return " ".join(descs[0].split())


def _clauses(text: str) -> list[str]:
    return [c for c in re.split(r"[.;]\s", text) if c]


def test_uart_req14_keeps_8n1_never_has_parity() -> None:
    """UART-REQ-14 half 1: an 8N1 stream MUST NEVER be detected as has-parity.

    Either wording the contract has used states it: "auto-detected as
    has-parity" (origin/main) or "produce a parity_error" (5acd9e7f).
    """
    desc = _uart_req_desc("UART-REQ-14")
    hits = [c for c in _clauses(desc)
            if "8N1" in c and "MUST NEVER" in c
            and ("has-parity" in c or "parity_error" in c)]
    assert hits, (
        "UART-REQ-14 lost its 8N1 obligation: no clause says an 8N1 stream "
        f"MUST NEVER be detected as has-parity. desc={desc!r}"
    )


def test_uart_req14_keeps_8e1_8o1_detected() -> None:
    """UART-REQ-14 half 2: 8E1/8O1 MUST still be detected, without a spurious error.

    DS-P2.139's 5acd9e7f head rewrote REQ-14 to the 8N1 side only and dropped
    this sentence; the 8E1/8O1 witness (DS-P2.179) then had no stated
    obligation to witness. The normative clause is pinned, not a mention:
    the observable or the witness name alone does not satisfy it.
    """
    desc = _uart_req_desc("UART-REQ-14")
    hits = [c for c in _clauses(desc)
            if re.search(r"8E1\s*/\s*8O1\s+MUST\b", c)
            and "detected" in c and re.search(r"must not spuriously error", c)]
    assert hits, (
        "UART-REQ-14 lost its 8E1/8O1 obligation: no clause says 8E1/8O1 "
        "MUST still be detected and must not spuriously error. "
        f"desc={desc!r}"
    )


def test_arbiter_coverage_is_strict() -> None:
    """Arbiter coverage over its exact 13 REQs, nothing missing or orphaned.

    A regression lock from the health sweep, not a guard on the port
    reconciliation: coverage-map does not read port_contract.
    """
    report = _coverage_report(ARBITER)
    gaps = {k: report[k] for k in
            ("missing", "orphan", "vacuous", "unreferenced", "undetermined")
            if report[k]}
    assert report["_rc"] == 0 and sorted(report["covered"]) == ARBITER_REQS \
        and not gaps, (
        f"arbiter coverage-map not strict (rc={report['_rc']}): "
        f"covered={len(report['covered'])}/13 gaps={gaps}"
    )
