# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P1.120: the resource ceilings are a post-implementation check.

The caps were measured on routed/fitted builds, but RouteRTL judges resources at
synth by default, where the same design measures higher before optimisation (a
Zybo synth of 8d38e651: LUT 21471, LUTRAM 376, SRL 426 against routed 19231, 282,
241). Every clean Zybo build therefore failed at synth. The contract moves the
hard check to impl, and each cap is the reference post-implementation figure plus
15%, rounded up (DS-D.57).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MARGIN_PERCENT = 15

#: Reference post-implementation totals (DS-P1.114 builds): zybo from the routed
#: reports/utilization.rpt of 80abd18d, de25 from the fit reports of 6a3cb558.
REFERENCE = {
    "zybo_datalogger": {"lut": 19231, "ff": 27210, "bram": 38, "dsp": 2, "lutram": 282, "srl": 241, "pll": 0},
    "de25_datalogger": {"lut": 21117, "ff": 37130, "bram": 87, "dsp": 1, "lutram": 977, "pll": 2, "alut": 28206},
}


def _technical() -> dict:
    return yaml.safe_load((ROOT / "requirements.yml").read_text(encoding="utf-8"))["technical"]


def test_resources_are_judged_after_implementation():
    assert _technical()["measured_at"] == {"resources": "impl"}


def _caps() -> dict[str, dict[str, int]]:
    rows = _technical()["resources"]
    targets = [entry["target"] for entry in rows]
    assert len(targets) == len(set(targets)), f"duplicate target rows: {targets}"
    return {entry["target"]: {k: v["max"] for k, v in entry.items() if k != "target"} for entry in rows}


def _expected(reference: int) -> int:
    return math.ceil(reference * (100 + MARGIN_PERCENT) / 100)


@pytest.mark.parametrize("target", sorted(REFERENCE))
def test_each_cap_is_the_post_impl_reference_plus_the_margin(target):
    caps = _caps()[target]
    assert set(caps) == set(REFERENCE[target])
    assert caps == {dim: _expected(ref) for dim, ref in REFERENCE[target].items()}


def test_the_margin_rule_rounds_up():
    assert [_expected(x) for x in (19231, 2, 0, 1)] == [22116, 3, 0, 2]


def test_a_synth_overrun_of_the_measured_size_stays_inside_no_cap():
    """The measured synth figures exceed the post-impl caps on LUTRAM and SRL, which is
    why the hard check cannot run at synth."""
    caps = _caps()["zybo_datalogger"]
    synth = {"lut": 21471, "ff": 29268, "lutram": 376, "srl": 426}
    assert synth["lutram"] > caps["lutram"]
    assert synth["srl"] > caps["srl"]


# ── The production path: RouteRTL's own loader, verdict and stage planner ────────
#: Figures measured on the bench at 8d38e651 (rr queue jobs 4ffca018a5f2 synth,
#: afe0452a76a9 impl), in rr's build_report.json resource keys.
SYNTH = {"LUTs": 21471, "FFs": 29268, "LUTRAM": 376, "SRL": 426, "BRAM": 38, "DSPs": 2, "PLLs": 0}
ROUTED = {"LUTs": 19243, "FFs": 27183, "LUTRAM": 282, "SRL": 241, "BRAM": 38, "DSPs": 2, "PLLs": 0}
COUNTED = {"LUTs": "LUT as Logic", "LUTRAM": "LUT as Distributed RAM", "SRL": "LUT as Shift Register"}


def _zybo_technical(path: Path = ROOT / "requirements.yml"):
    from routertl_core.resource_budget import resolve_technical
    from sdk.cli.requirements import load_requirements

    req = load_requirements(path)
    return resolve_technical(req.technical, getattr(req, "parameters", ()), target="zybo_datalogger")


def _resource_violations(technical, figures: dict, stage: str, tmp_path: Path) -> tuple[list, list]:
    """(judged, deferred) resource violation names for *figures* at *stage*."""
    from routertl_core.ppa_verdict import build_ppa_verdict
    from sdk.engine.technical_stage import defer_outer, plan_stage

    reports = tmp_path / f"reports-{stage}-{figures['LUTs']}"
    reports.mkdir()
    (reports / "build_report.json").write_text(json.dumps({"resources": {
        k: {"used": v, "available": 999999, **({"counted": COUNTED[k]} if k in COUNTED else {})}
        for k, v in figures.items()}}), encoding="utf-8")
    verdict = build_ppa_verdict(technical, reports, "deepskopion")
    verdict = verdict.to_dict() if hasattr(verdict, "to_dict") else verdict
    kept, deferred = defer_outer(verdict, plan_stage(technical, stage, "xilinx"))
    return ([v["name"] for v in kept["violations"] if v.get("kind") == "resource"],
            [v["name"] for v in deferred if v.get("kind") == "resource"])


def test_the_synth_figures_are_deferred_not_failed_at_synth(tmp_path):
    judged, deferred = _resource_violations(_zybo_technical(), SYNTH, "synth", tmp_path)
    assert judged == []
    assert {"lutram", "srl"} <= set(deferred)


@pytest.mark.parametrize("stage", ["impl", "bitstream"])
def test_the_routed_figures_pass_after_implementation(tmp_path, stage):
    assert _resource_violations(_zybo_technical(), ROUTED, stage, tmp_path) == ([], [])


@pytest.mark.parametrize("stage", ["impl", "bitstream"])
def test_a_routed_overrun_fails_after_implementation(tmp_path, stage):
    over = dict(ROUTED, LUTs=_caps()["zybo_datalogger"]["lut"] + 1)
    judged, _ = _resource_violations(_zybo_technical(), over, stage, tmp_path)
    assert judged == ["lut"]


def test_the_contract_loader_refuses_a_duplicate_target_row(tmp_path):
    """Control: RouteRTL's loader is in the path, and it rejects what _caps() refuses."""
    from sdk.cli.requirements import RequirementsValidationError

    text = (ROOT / "requirements.yml").read_text(encoding="utf-8")
    row = "    - target: de25_datalogger\n"
    assert text.count(row) == 1
    dup = tmp_path / "requirements.yml"
    dup.write_text(text.replace(row, "    - target: zybo_datalogger\n", 1), encoding="utf-8")
    with pytest.raises(RequirementsValidationError):
        _zybo_technical(dup)


# ── The build evaluator itself (what rr synth/impl/bitstream run calls) ──────────
def _evaluate(figures: dict, stage: str, tmp_path: Path) -> tuple[int, str]:
    """rr's build-stage technical evaluator on a target reports dir holding *figures*:
    (return code, printed output)."""
    import io

    from rich.console import Console
    from sdk.engine.build_technical_eval import evaluate_build_technical_contracts

    reports = tmp_path / f"eval-{stage}-{figures['LUTs']}" / "reports"
    reports.mkdir(parents=True)
    (reports / "build_report.json").write_text(json.dumps({"resources": {
        k: {"used": v, "available": 999999, **({"counted": COUNTED[k]} if k in COUNTED else {})}
        for k, v in figures.items()}}), encoding="utf-8")
    out = io.StringIO()
    rc = evaluate_build_technical_contracts(ROOT, reports_dir=reports, console=Console(file=out, width=200),
                                            target="zybo_datalogger", stage=stage)
    return rc, out.getvalue()


def test_the_build_evaluator_passes_a_synth_overrun_with_the_clauses_deferred(tmp_path):
    rc, out = _evaluate(SYNTH, "synth", tmp_path)
    assert rc == 0
    assert "technical.resources.lutram: DEFERRED to impl" in out
    assert "Binding constraint" not in out


def test_the_build_evaluator_passes_the_routed_figures_at_impl(tmp_path):
    rc, out = _evaluate(ROUTED, "impl", tmp_path)
    assert rc == 0
    assert "Technical contract met for deepskopion" in out
    assert "DEFERRED" not in out


@pytest.mark.parametrize("stage,judged", [("synth", False), ("impl", True), ("bitstream", True)])
def test_every_declared_resource_is_judged_from_impl_on(stage, judged):
    from sdk.engine.technical_stage import plan_stage

    plan = plan_stage(_zybo_technical(), stage, "xilinx")
    declared = {f"resources.{dim}" for dim in REFERENCE["zybo_datalogger"]}
    assert declared <= (set(plan.judged) if judged else {c for c, _ in plan.deferred})


#: build_report.json key -> contract dimension.
DIM_KEY = {"lut": "LUTs", "ff": "FFs", "lutram": "LUTRAM", "srl": "SRL", "bram": "BRAM", "dsp": "DSPs"}


@pytest.mark.parametrize("dim", sorted(DIM_KEY))
def test_the_build_evaluator_fails_each_routed_overrun_at_impl(tmp_path, dim):
    over = dict(ROUTED, **{DIM_KEY[dim]: _caps()["zybo_datalogger"][dim] + 1})
    rc, out = _evaluate(over, "impl", tmp_path)
    assert rc != 0
    assert f"technical.resources.{dim}" in out
    assert f"{dim} used {over[DIM_KEY[dim]]}" in out
