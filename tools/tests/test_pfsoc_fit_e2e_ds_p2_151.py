"""PF-F0 (DS-P2.151) end-to-end Libero fit guard.

Kept SEPARATE from test_pfsoc_sdc_scope_ds_p2_151.py (the always-on static
twins) because this test SKIPS where the bench Libero/license is absent, and a
skip must not dilute the static suite's green bar.

Full done-when: a clean `rr synth run` (whose gen_project now derives + associates
the CCC clk_sys generated clock) followed by an SDC-ASSOCIATED, timing-driven
`rr synth check` (PLACEROUTE) completes with NO SDC parse abort.

What this leg asserts, and what it deliberately does NOT:

  * BOTH steps must return 0 — `rr synth run` AND `rr synth check` (the earlier
    revision checked only `syn.returncode`; a non-zero `chk` — e.g. the derive
    hook now ABORTS gen_project on a derive miss, DS-P2.151 land-refusal fix —
    must fail this test, not pass silently).
  * clk_sys must be CONSTRAINED — the place-and-route constraint-coverage XML
    MUST exist and show the derived generated clock associated (the earlier
    revision only checked coverage `if cov.is_file()`, so a missing XML meant no
    check at all).
  * it does NOT assert timing MET. Measured on the bench 2026-09-20 the design
    does not close 100 MHz (WNS -5.5..-6.2 ns, worst path u_core/u_edge_agg) —
    see docs/evidence/pf-f0/DS-P2.151-pf-f0-fit-timing.md. PF-F0's timing-met
    done-when is HONESTLY still open (closure is a named follow-on: shared-core
    edge_batcher pipelining, or the §2 clk_sys-tuning design decision). Asserting
    "met" here would be a false green. What this leg guards is that the fit
    REACHES a genuine, constrained timing verdict at all (the three SDC walls
    PF-F0 fixed), not the vacuous unconstrained run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"


def _cfg() -> dict:
    return yaml.safe_load(TARGET.read_text(encoding="utf-8"))


@pytest.mark.skipif(
    os.environ.get("DS_P2_151_E2E_IMPL") != "1",
    reason="e2e Libero fit, opt-in: set DS_P2_151_E2E_IMPL=1 (needs the bench Libero + license)",
)
def test_pfsoc_fit_reaches_constrained_timing() -> None:
    rr = shutil.which("rr")
    if rr is None:
        pytest.skip("rr CLI not on PATH")
    libero = Path(_cfg()["hardware"]["tool_path"]) / "Designer" / "bin64" / "libero"
    if not libero.exists():
        pytest.skip(f"Libero not installed at {libero}")
    env = os.environ.copy()
    env.setdefault("LM_LICENSE_FILE", "1702@localhost")
    env["RR_QUEUE_BUILDS"] = "0"
    subprocess.run([rr, "target", "set", "pfsoc_datalogger"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=300)
    syn = subprocess.run([rr, "synth", "run", "--target", str(TARGET)], cwd=ROOT,
                         env=env, capture_output=True, text=True, timeout=1800)
    assert syn.returncode == 0, f"synth failed:\n{syn.stdout[-2000:]}\n{syn.stderr[-2000:]}"
    env["RR_ALLOW_STALE_SYNTH"] = "1"
    chk = subprocess.run([rr, "synth", "check"], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=1200)
    blob = chk.stdout + "\n" + chk.stderr
    for bad in ("SDC0061", "SDC0023", "SDC0001", "read_sdc failed",
                "The command 'run_tool' failed"):
        assert bad not in blob, (
            "pfsoc SDC-associated timing run hit "
            f"'{bad}':\n" + "\n".join(blob.splitlines()[-40:])
        )
    # The timing-driven PLACEROUTE itself must succeed. gen_project now ABORTS on
    # a derive miss (pfsoc_derive_timing.tcl re-raises), and a failed
    # PLACEROUTE/VERIFYTIMING is not a timing verdict at all — either way chk
    # returns non-zero and this fit did NOT reach a real, constrained result.
    assert chk.returncode == 0, (
        "rr synth check (SDC-associated PLACEROUTE / VERIFYTIMING) failed "
        f"(rc={chk.returncode}):\n" + "\n".join(blob.splitlines()[-40:])
    )
    # The derived generated clock must have made clk_sys constrained
    # (anti-vacuity). The coverage XML is a MANDATORY artifact of a real timing
    # run — its absence means the fit never associated the derived SDC, which is
    # itself the failure, so require the file rather than skipping when missing.
    cov = (ROOT / "build" / "pfsoc_datalogger" / "project" / "designer"
           / "deepskopion_pfsoc_top"
           / "deepskopion_pfsoc_top_place_and_route_constraint_coverage.xml")
    assert cov.is_file(), (
        f"place-and-route constraint-coverage XML absent ({cov}) — a real "
        "SDC-associated timing run always emits it; its absence means the fit "
        "did not reach a constrained timing verdict"
    )
    import re
    rows = re.findall(r"<row>(.*?)</row>", cov.read_text(errors="replace"), re.S)
    setup = next((r for r in rows if "Setup" in r), "")
    nums = [int(x) for x in re.findall(r"<cell>\s*(\d+)\s*</cell>", setup)]
    # nums = [constrained, unconstrained, total]; clk_sys constrained means
    # the overwhelming majority are constrained (measured 78403/3).
    assert nums and nums[0] > 1000 and (len(nums) < 2 or nums[1] < 100), (
        f"clk_sys still unconstrained (setup coverage {nums}) — the CCC "
        "derived generated clock was not associated; 'timing met' would be "
        "vacuous"
    )
