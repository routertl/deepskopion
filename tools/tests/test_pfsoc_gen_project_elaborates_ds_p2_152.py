# rr unknown-llm (AI-generated)
"""DS-P2.152 (PF-H0): the PFSoC reference-design baseline must ELABORATE.

This is the test-power guard the ticket demands: it exercises the ACTUAL
gen_project stage (rr's microchip dispatcher -> Libero SCRIPT mode -> the
hw/libero/pfsoc/pfsoc_mss_subsys.tcl hook, sd_connect_pins and all) via
`rr project create` (RR_TARGET=targets/pfsoc_datalogger.yml), which regenerates the
project and sources every gen_project hook.

DS-P2.278: until 2026-11-12 this ran `rr synth run --dry-run`. routertl 1c273045f
(2026-10-18, RTL-P2.1806) made --dry-run print a synthesis plan and launch no vendor
tool ("Project regeneration remains available as `rr project create`"), so from
then on the test started no Libero and passed whenever no FIC marker appeared. A
positive control now proves Libero ran: a project file (*.prjx) under
build/pfsoc_datalogger/project/ written after the test started. It is deliberately NOT a
source-substring check — the DS-P2.151 land refusal proved those are vacuous
against this failure mode: the historical abort ("Parameter 'to' is missing /
The command 'sd_connect_pins' failed", log 6b7c603647ba) only reproduces by
running Libero.

WRONG-REASON-RED discipline (the DS-P2.152 land refusal): a Libero gen_project
can abort for many reasons (a CCC spirit-parameter typo dies at
create_and_configure_core BEFORE any FIC connect), and a receipt whose
intended assertion is a generic "it failed" string accepts any of them. The
failure message below therefore CLASSIFIES the red: the _FIC_RED_MARKER
string is emitted ONLY when Libero's own output
carries the FIC connect failure ("The command 'sd_connect_pins' failed" /
"Parameter 'to' is missing"); every other abort is reported as a NON-FIC red
so fort_test_power rejects it as wrong-reason.

Full-pipeline mode (ticket done-when: gen_project -> synthesis -> netlist):
set DS_P2_152_E2E_SYNTH=1 and the same test drops --dry-run, runs Synplify to
completion (~5 min on this bench), and asserts the netlist
build/pfsoc_datalogger/project/synthesis/deepskopion_pfsoc_top.vm exists.
The default dry-run mode stays the receipt/regression form so a red is cheap.

Environment contract: needs the bench Libero install (targets yml
hardware.tool_path) and its local FlexLM daemon. Where Libero is absent
(cron/CI/fresh clone without EDA tools) the test SKIPS with an explicit
message rather than failing — the always-on static twin lives in
test_pfsoc_mss_builder_contract.py::test_fic_rows_match_the_frozen_mss_verilog.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "pfsoc_datalogger.yml"

# The bench Microchip FlexLM daemon (lmgrd -c ~/.config/microchip/License.dat,
# SERVER line port 1702). Only used as a default when the caller has not
# already exported a license path — an unset LM_LICENSE_FILE otherwise aborts
# Libero before it even reads the script ("Cannot locate license file").
_BENCH_LICENSE = "1702@localhost"

# Libero's own error lines for the DS-P2.152 defect: an invented MSS pin name
# in a sd_connect_pins list is silently dropped, the connect degenerates to one
# element, and the command aborts with a missing-'to'-parameter error. These
# are the ONLY two lines that identify the ticket's abort.
_FIC_ABORT_MARKERS = (
    "The command 'sd_connect_pins' failed",
    "Parameter 'to' is missing",
)

# The receipt's intended-assertion marker. Deliberately assembled by
# concatenation so the contiguous string exists NOWHERE in this source file:
# pytest tracebacks echo test source lines into stdout, and a source-echoed
# marker would let a wrong-reason red still match the receipt's intended
# assertion. Only a genuine FIC-classified failure message carries it.
_FIC_RED_MARKER = "pfsoc FIC sd_connect_pins" + " abort"


def _libero_exe() -> Path:
    cfg = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    tool_path = Path(cfg["hardware"]["tool_path"])
    return tool_path / "Designer" / "bin64" / "libero"


def test_gen_project_elaborates_on_the_real_libero() -> None:
    rr = shutil.which("rr")
    if rr is None:
        pytest.skip("NOT EXERCISED: rr CLI not on PATH — cannot drive the Libero gen_project")
    libero = _libero_exe()
    if not libero.exists():
        pytest.skip(f"NOT EXERCISED: Libero not installed at {libero} — e2e elaboration needs the bench EDA install")

    full_synth = os.environ.get("DS_P2_152_E2E_SYNTH") == "1"
    project_dir = ROOT / "build" / "pfsoc_datalogger" / "project"
    t_start = time.time()

    # A fresh clone has no generated/ yet (its content is produced by the
    # pre_build hooks, the whole dir is gitignored, and the project-create
    # path does not run the pre_build hooks), but the gen_project source list
    # imports generated/rr_rea_build_id_pkg.vhd. Produce it exactly the way
    # the target's own rr_rea_build_id hook does before invoking rr.
    (ROOT / "generated").mkdir(exist_ok=True)
    gen = subprocess.run(
        [
            "python3", str(ROOT / "tools" / "hooks" / "run_sdk_generator.py"),
            "rea-build-id", "--design-identity", "deepskopion",
            "--out", "generated/rr_rea_build_id_pkg.vhd",
        ],
        cwd=ROOT, capture_output=True, text=True, errors="replace", timeout=60,
    )
    assert gen.returncode == 0, (
        f"rea-build-id pre-step failed (rc={gen.returncode}):\n{gen.stdout}\n{gen.stderr}"
    )
    env = os.environ.copy()
    env.setdefault("LM_LICENSE_FILE", _BENCH_LICENSE)
    # Force the direct synchronous path: the queue is for real builds; this
    # guard must not depend on (or occupy) the shared bench queue, and the
    # submit-and-exit path would return before Libero ran.
    env["RR_QUEUE_BUILDS"] = "0"

    if full_synth:
        cmd = [rr, "synth", "run", "--target", "targets/pfsoc_datalogger.yml"]
    else:
        cmd = [rr, "project", "create"]
        env["RR_TARGET"] = "targets/pfsoc_datalogger.yml"
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        # Project create: gen_project only (headroom for bench load).
        # Full mode must contain a real Synplify mapper run (~4-5 min here).
        timeout=2400 if full_synth else 600,
    )
    combined = proc.stdout + "\n" + proc.stderr
    tail = "\n".join(combined.splitlines()[-60:])
    if any(marker in combined for marker in _FIC_ABORT_MARKERS):
        reason = _FIC_RED_MARKER + (
            " — gen_project died wiring a FIC AXI pin into the SmartDesign "
            "(Libero: missing-'to'-parameter on a connect list; an invented MSS "
            "pin name was dropped from it — DS-P2.152's named defect)"
        )
    else:
        reason = (
            "pfsoc gen_project/elaboration FAILED for a NON-FIC reason — this "
            "red does NOT reproduce DS-P2.152's named defect (the FIC connect "
            "abort); diagnose the actual Libero error below before touching "
            "the FIC wiring"
        )
    assert proc.returncode == 0, f"{reason} (rc={proc.returncode}); last output:\n{tail}"

    # Positive control (DS-P2.278): Libero's gen_project ran in THIS test.
    fresh = [p for p in project_dir.glob("**/*.prjx") if p.stat().st_mtime >= t_start - 1]
    assert fresh, (
        f"rr exited 0 but no Libero project file under {project_dir} was written by this run, "
        "so gen_project did not run: this test exercised nothing (DS-P2.278)")

    if full_synth:
        netlist = (
            ROOT / "build" / "pfsoc_datalogger" / "project" / "synthesis"
            / "deepskopion_pfsoc_top.vm"
        )
        assert netlist.exists(), (
            "rr synth run reported success but the Synplify netlist "
            f"{netlist} was not produced — synthesis did not complete"
        )
