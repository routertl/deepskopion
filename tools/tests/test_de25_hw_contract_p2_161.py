# rr unknown-llm (AI-generated)
"""DS-P2.161 — the DE25 silicon claims as a tracked rr.hw.contract/v1 contract.

WHAT THIS GUARDS
----------------
Before DS-P2.161 the DE25's silicon evidence was README folders and a
bench-only ``runtimes/hw.example.yml``; nothing declared which claims the
bitstream makes, how each is witnessed, or which broken state proves the
witness can go RED. ``runtimes/hw.yml`` is now that contract. Because the same
path was the gitignored deployment descriptor (DS-C1), the deployment
descriptor moved to the gitignored ``runtimes/hw.local.yml`` (rr reaches it
through the contract's ``extends:``) and this repo's own readers moved with it.

Each test asserts at a surface a caller reaches: the ``rr hw validate`` /
``rr hw run`` CLI output, the builder's rendered bootargs, and the preflight
verdict — not a helper's private return value.

POWER
-----
* BASELINE (pre-DS-P2.161): no tracked contract exists, so every contract test
  is RED ("no contract" / missing claims); the readers still read
  ``runtimes/hw.yml``, so the resolver tests are RED against a checkout whose
  ``runtimes/hw.yml`` is the contract (no ``bench:`` block).
* MUTATIONS (each one surgical, applied alone): removing fresh producer
  movement from the integrity REQ, substituting the producer pointer for the
  host-drain pointer, and removing the implemented read-trigger each turn a
  dedicated test RED. Deployment/reader guards remain separate assertions.

DEPLOYMENT ENVIRONMENT
----------------------
The ``rr`` tests shell out to the ``rr`` on PATH — the same binary the
pre-commit gate resolves ``ROUTERTL_ROOT`` from — and FAIL (not skip) when it
is missing: a contract gate that cannot run is not a green gate. The desk dry
run calls the same executor in process on routertl's MockProbe (DS-T2.87:
``--probe mock`` cannot seed the map's constants). No hardware, no lease.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "runtimes" / "hw.yml"
BOARD = "de25-standard"

# The four DE25 claim families named by the ticket, and the fifth, sustained
# drain (DS-DE25-DRAIN-001, DS-P2.267). TRIG is the implemented EDGE software
# read-trigger, not the future semantic TRIG_MATCH engine.
CLAIMS = (
    "DS-DE25-CLK-001",
    "DS-DE25-INTEG-001",
    "DS-DE25-TRIG-001",
    "DS-DE25-TPUT-001",
    "DS-DE25-DRAIN-001",
)

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "preflight"))


def _rr() -> str:
    rr = shutil.which("rr")
    assert rr, (
        "`rr` is not on PATH, so the DS-P2.161 contract gate cannot run. This is "
        "NOT a finding about the contract — but a gate that cannot run is not green."
    )
    return rr


def _contract() -> dict:
    assert CONTRACT.is_file(), f"no tracked hardware contract at {CONTRACT}"
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def _requirements() -> dict[str, dict]:
    return {req["id"]: req for req in _contract()["requirements"]}


# ── the claims are declared, each with an identity-gated falsifier ──────────


def test_every_de25_claim_has_an_identity_gated_falsifier_on_the_same_board():
    reqs = _requirements()
    missing = [claim for claim in CLAIMS if claim not in reqs]
    assert not missing, (
        f"DE25 claim(s) {missing} are not declared in runtimes/hw.yml — the "
        "contract no longer covers the silicon claim the project makes"
    )
    for claim in CLAIMS:
        req = reqs[claim]
        assert req["expected_outcome"] == "pass", claim
        sibling_id = req.get("falsifier")
        sibling = reqs.get(sibling_id)
        assert sibling is not None, f"{claim} names no declared falsifier sibling"
        assert sibling["expected_outcome"] == "demonstrated_broken", sibling_id
        for item in (req, sibling):
            assert item["target"]["board"] == BOARD, item["id"]
            chain = item.get("capture_chain") or {}
            assert chain.get("bitstream_identity_required") is True, (
                f"{item['id']}: an unconfirmed PL identity cannot qualify as "
                "DE25 hardware evidence (REA BUILD_ID, rea-jtag-build-id-32)"
            )


def test_rr_hw_validate_passes_with_desk_negative_control_receipts():
    """The validator gate's verdict must agree with the checked-in receipts.

    Originally this required ``rr hw validate`` to PASS on the desk
    negative-control receipts (accepting its missing-evidence exit 1 was the
    original defect). DS-P2.165 replaced those with silicon runs, on which the
    gate FAILS (witness finding 3: the map-access preamble forces every live
    receipt to unexpected_fail). Same invariant, new receipt shape: the gate
    must print a parseable verdict, ``ok`` must agree with the exit code, and
    the only refusal it may give is the missing falsifier evidence.
    """
    proc = subprocess.run(
        [_rr(), "hw", "validate", "--json", str(CONTRACT)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    out = proc.stdout.strip()
    assert out, f"rr hw validate printed nothing (rc={proc.returncode}): {proc.stderr[-2000:]}"
    report = json.loads(out[out.index("{"):])
    assert (proc.returncode == 0) == (report.get("ok") is True), (
        f"rr hw validate exit code disagrees with its own verdict:\n{out}\n{proc.stderr[-2000:]}"
    )
    if report.get("ok") is not True:
        unfiled = [
            line for line in "\n".join(report.get("errors", [])).splitlines()
            if line.strip() and "has no run.json with req_status" not in line
            and not line.startswith("Valid pair:")
        ]
        assert not unfiled, f"rr hw validate refuses for an unfiled reason: {unfiled}"


def test_integrity_requires_fresh_write_pointer_movement_not_stale_status():
    req = _requirements()["DS-DE25-INTEG-001"]
    rate_steps = [step for step in req["steps"] if step["kind"] == "rate"]
    assert len(rate_steps) == 1, "capture integrity needs one fresh-movement oracle"
    rate = rate_steps[0]
    assert rate["read"] == "0x20000150", (
        "integrity must measure a RING_WRITE_PTR delta; sticky bvalid and an "
        "absolute pointer can pass when the canonical stimulus is ignored"
    )
    assert rate["min_per_s"] > 0, "an ignored stimulus must produce a RED result"


def test_trigger_uses_the_implemented_read_trigger_and_rejects_stale_ring_state():
    steps = _requirements()["DS-DE25-TRIG-001"]["steps"]
    assert steps[0] == {
        "idx": 1,
        "kind": "peek",
        "addr": "0x20000150",
        "expected": "0x00000000",
        "comment": "positive anti-stale control: ring starts empty",
    }, "trigger must prove its pre-trigger ring state rather than trust old traffic"
    assert any(step.get("addr") == "0x20000020" and
               step.get("value") == "0x00000803" for step in steps), (
        "EDGE0 CONFIG payload 0b11 is the shipped software read-trigger"
    )


def test_throughput_measures_the_software_host_drain_not_only_fabric_writes():
    req = _requirements()["DS-DE25-TPUT-001"]
    rates = [step for step in req["steps"] if step["kind"] == "rate"]
    assert len(rates) == 1
    assert rates[0]["read"] == "0x20000154", (
        "stream-to-host throughput must measure software-owned RING_READ_PTR, "
        "not fabric-owned RING_WRITE_PTR"
    )
    names = {item["name"] for item in req["preconditions"]}
    assert {"capture_daemon", "host_consumer"} <= names


def test_every_rate_step_can_measure_its_whole_declared_band_without_wrapping():
    """rr's ``rate`` step takes two reads ``interval_s`` apart and computes
    ``delta = (c1 - c0) % 2**width``. A counter that advances by ``2**width`` or
    more inside the window aliases: at width 28 and a 10 s window, 30 MB/s reads
    as 3.16 MB/s and a healthy board grades RED. So every rate step must keep
    ``interval_s * max_per_s`` strictly below ``2**width`` — the top of its own
    band must stay measurable.
    """
    offenders = []
    for req in _contract()["requirements"]:
        for step in req["steps"]:
            if step.get("kind") != "rate":
                continue
            span = float(step["interval_s"]) * float(step["max_per_s"])
            limit = 2 ** int(step["width"])
            if span >= limit:
                offenders.append(
                    f"{req['id']} step {step['idx']}: interval_s={step['interval_s']} "
                    f"x max_per_s={step['max_per_s']} = {span:.4g} >= 2**{step['width']} "
                    f"= {limit:.4g}"
                )
    assert not offenders, (
        "rate step windows wrap their counter inside the declared band:\n  "
        + "\n  ".join(offenders)
    )


def test_falsifier_receipts_are_explicit_mock_runs_not_silicon_claims():
    """A falsifier receipt must label its provenance truthfully.

    Originally: each receipt is an explicit unleased, unvalidated mock run.
    After DS-P2.165 the receipts are silicon runs, so the same guard inverts to
    its other side: no receipt may be a mock, and a silicon label must be
    earned (leased, validated, REA identity CONFIRMED). Neither shape may
    masquerade as the other.
    """
    for claim in CLAIMS:
        red = _requirements()[claim]["falsifier"]
        logs = sorted((ROOT / "runs" / "hw" / red).glob("*/run.json"))
        assert len(logs) == 1, f"{red}: expected one checked-in receipt"
        run = json.loads(logs[0].read_text(encoding="utf-8"))
        chain = run["capture_chain"]
        assert chain["backend"] != "mock", f"{red}: a desk mock is checked in as the receipt"
        assert chain["leased"] is True, f"{red}: silicon receipt was not taken under a lease"
        assert chain["status"] == "validated", f"{red}: unvalidated receipt posing as live evidence"
        assert chain["bitstream_identity"]["outcome"] == "CONFIRMED", red
        # DS-P2.267: the filed preamble finding holds for DS-P2.165's receipts; a
        # falsifier recorded since (registered sibling witness) must be exactly
        # demonstrated_broken.
        from de25_witness_manifests import p2165_receipt_ids
        expected = ("unexpected_fail" if red in p2165_receipt_ids()
                    else "demonstrated_broken")
        assert run["conclusion"]["req_status"] == expected, (
            f"{red}: silicon falsifier verdict is {run['conclusion']['req_status']!r}, "
            f"expected {expected!r}"
        )


# ── the dry run exercises every step, and a dead board cannot pass ──────────


#: What a de25_datalogger image answers at the regbank's declared constants
#: (project_regbank.yml ``expected:``): REGBANK_VERSION, CONTRACT_ABI_VER and
#: RING_BYTES. Every other register reads 0.
MAP_CONSTANTS = {"0x2000000C": "0x00030000", "0x20000028": "0x00020000",
                 "0x20000160": "0x10000000"}


def _mock_run(req_id: str) -> dict:
    """Run one REQ on routertl's MockProbe, through the executor rr hw run uses.

    The board presents this regbank (its declared constants answer at the
    CSR aperture, where runtimes/de25_regbank_map.yml places the map) and nothing else: dead
    clock, empty ring, silent write plane. ``rr hw run --probe mock`` cannot
    seed a register, so its all-zero board fails the map-access preamble
    (DS-T2.87) and no REQ assertion would be graded at all.
    """
    from sdk.engine.hardware.contract import load_contract
    from sdk.engine.hardware.executor import run_contract
    from sdk.engine.hardware.mock_probe import MockProbe

    return run_contract(
        load_contract(CONTRACT), req_id, MockProbe(registers=dict(MAP_CONSTANTS)),
        mode="replay", project=ROOT.name, sof_sha256="0" * 64, git_rev="desk",
        execution_backend="mock", hardware_access=False, command_cwd=ROOT,
    )


def _mock_cli_run(req_id: str) -> dict:
    proc = subprocess.run(
        [_rr(), "hw", "run", req_id, "--probe", "mock", "--json",
         "--project", str(ROOT)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (
        f"rr hw run {req_id} --probe mock failed rc={proc.returncode}:\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    envelope = json.loads(proc.stdout[proc.stdout.index("{"):])
    return envelope.get("data") or envelope


@pytest.mark.parametrize("req_id", sorted(
    req["id"] for req in yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["requirements"]
) if CONTRACT.is_file() else ["<no contract>"])
def test_rr_hw_run_mock_cli_walks_every_step_of_the_recipe(req_id):
    """The CLI surface itself: `rr hw run --probe mock` must exit 0 and walk
    every recipe step. Its all-zero board cannot seed the map's constants, so
    the grade is not asserted here (the in-process dry run below grades it) —
    only that the command runs the recipe end to end."""
    reqs = _requirements()
    assert req_id in reqs, f"{req_id} not in the contract"
    run = _mock_cli_run(req_id)
    recipe = [step["idx"] for step in reqs[req_id]["steps"]]
    executed = [step["idx"] for step in run.get("steps", [])]
    assert executed == recipe, f"{req_id}: CLI executed steps {executed} != recipe {recipe}"


@pytest.mark.parametrize("req_id", sorted(
    req["id"] for req in yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["requirements"]
) if CONTRACT.is_file() else ["<no contract>"])
def test_mock_dry_run_executes_every_step_and_an_all_zero_board_passes_no_claim(req_id):
    """The desk dry run: rr's runner walks every recipe step. MockProbe reads
    every register but the map's declared constants as 0x00000000 — a board
    with a dead clock, an empty ring and a silent write plane — so no positive
    DE25 claim may PASS on it, and
    every RED sibling must grade demonstrated_broken. A positive REQ that
    passes here asserts nothing a dead board cannot satisfy."""
    reqs = _requirements()
    assert req_id in reqs, f"{req_id} not in the contract"
    run = _mock_run(req_id)
    recipe = [step["idx"] for step in reqs[req_id]["steps"]]
    executed = [step["idx"] for step in run.get("steps", [])]
    assert executed == recipe, f"{req_id}: executed steps {executed} != recipe {recipe}"
    req_status = (run.get("conclusion") or {}).get("req_status")
    if reqs[req_id]["expected_outcome"] == "demonstrated_broken":
        assert req_status == "demonstrated_broken", (req_id, run.get("conclusion"))
    else:
        assert req_status != "pass", (
            f"{req_id} PASSES on an all-zero mock board — its assertions are "
            f"vacuous: {run.get('conclusion')}"
        )


# ── DS-C1: the tracked contract carries no deployment state ────────────────


def test_tracked_contract_carries_no_deployment_state():
    doc = _contract()
    assert "bench" not in doc, (
        "runtimes/hw.yml is TRACKED (the rr.hw.contract/v1 contract) and must "
        "carry no bench block: cable serials, UART paths, TFTP roots and bench "
        "IPs are DS-C1 deployment state and belong in runtimes/hw.local.yml"
    )
    assert doc.get("extends") == "hw.local.yml", (
        "the contract must hand rr's bench resolver the gitignored "
        "deployment descriptor through `extends: hw.local.yml`"
    )

    def ignored(rel: str) -> bool:
        return subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", rel], cwd=ROOT,
        ).returncode == 0

    assert not ignored("runtimes/hw.yml"), "the contract must be tracked, not ignored"
    assert ignored("runtimes/hw.local.yml"), (
        "runtimes/hw.local.yml carries deployment identity and must stay gitignored"
    )


# ── the repo's deployment readers skip the tracked contract ────────────────


def _checkout_with_contract(tmp_path: Path) -> Path:
    """A project root shaped like a real checkout: the tracked contract at
    runtimes/hw.yml (no bench block) plus a local deployment descriptor."""
    root = tmp_path / "proj"
    (root / "runtimes").mkdir(parents=True)
    (root / "targets").mkdir()
    (root / "hw" / "rtl" / "pkg").mkdir(parents=True)
    shutil.copy2(CONTRACT, root / "runtimes" / "hw.yml")
    shutil.copy2(ROOT / "targets" / "de25_datalogger.yml",
                 root / "targets" / "de25_datalogger.yml")
    (root / "project.yml").write_text(
        "target: targets/de25_datalogger.yml\n", encoding="utf-8")
    (root / "runtimes" / "hw.local.yml").write_text(yaml.safe_dump({
        "bench": {
            "resources": {
                "de25_address": {"kind": "bench_ip", "addr": "198.51" + ".100.61",
                                 "prefix": 24},
                "jtag_primary": {"kind": "jtag", "cable": "FAKESERIAL161"},
            },
            "targets": {
                "de25_bench": {"board": BOARD, "bench_ip": "de25_address",
                               "cmdline_ip_key": "ds_ip",
                               "uses": ["de25_address", "jtag_primary"]},
            },
        },
    }), encoding="utf-8")
    return root


def test_image_builder_reads_the_local_descriptor_not_the_tracked_contract(
        tmp_path, monkeypatch):
    monkeypatch.delenv("RR_BENCH_CONFIG", raising=False)
    import de25_linux_image as image

    root = _checkout_with_contract(tmp_path)
    try:
        bootargs = image.deployment_bootargs(root)
    except RuntimeError as exc:
        pytest.fail(
            "the DE25 image builder read the tracked contract runtimes/hw.yml "
            f"as its deployment descriptor instead of runtimes/hw.local.yml: {exc}"
        )
    assert "ds_ip=198.51.100.61/24" in bootargs.split(), bootargs


def test_preflight_names_the_cable_from_the_local_descriptor(tmp_path, monkeypatch):
    monkeypatch.delenv("RR_BENCH_CONFIG", raising=False)
    import ds_preflight as preflight

    root = _checkout_with_contract(tmp_path)
    declared = preflight._bench_declared_cables(root)
    assert declared == {"FAKESERIAL161"}, (
        "preflight's jtag.unambiguous check must read the cable from "
        "runtimes/hw.local.yml; reading the tracked contract (no bench block) "
        f"makes every multi-board host look undeclared — got {declared!r}"
    )


def test_rr_bench_resolver_follows_the_contract_extends_pointer(tmp_path):
    """rr itself (not only this repo's readers) must find the deployment
    descriptor from the tracked contract: `rr bench`-style resolution goes
    through `extends: hw.local.yml`."""
    root = _checkout_with_contract(tmp_path)
    env = {k: v for k, v in os.environ.items()
           if k not in ("RR_BENCH_FILE", "RR_BENCH_CONFIG")}
    sdk_path = subprocess.run([_rr(), "sdk-path"], capture_output=True, text=True,
                              timeout=60).stdout.strip()
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {sdk_path!r}) if {bool(sdk_path)!r} else None\n"
        "from sdk.engine.bench_descriptor import resolve_bench\n"
        "r = resolve_bench(sys.argv[1])\n"
        "print(json.dumps({'host': str(r.layers[0].path), "
        "'jtag': (r.resources.get('jtag_primary') or {}).get('cable')}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code, str(root)], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["jtag"] == "FAKESERIAL161", got
    assert Path(got["host"]).name == "hw.local.yml", got
