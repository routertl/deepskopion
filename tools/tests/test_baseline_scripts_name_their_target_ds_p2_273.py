# rr unknown-llm (AI-generated)
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.273: each sim-baseline script names the target its tag lives on.

Measured 2026-11-12 by the nightly ds-sim-baseline job at 0457eb45: run_xilinx_baseline.py ran
`rr sim run --tag xilinx_baseline` with no --target, so rr resolved the tag on project.yml's active
target (de25_datalogger) and failed "Tag 'xilinx_baseline' not found". The test runs each script's
real main() with subprocess.Popen replaced by a recorder, so it checks the command the script would
actually run, not its source text.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
# DS-P1.103 param split: run_de25_core_baseline drives a bench target and runs in the _bench.py sibling.
SCRIPTS = {
    "run_xilinx_baseline": ROOT / "tools" / "sim" / "run_xilinx_baseline.py",
}


class _Captured(Exception):
    pass


def _command_of(path: Path, monkeypatch) -> list[str]:
    spec = importlib.util.spec_from_file_location(f"_ds_p2_273_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = []

    def popen(argv, *a, **k):
        seen.append(list(argv))
        raise _Captured

    monkeypatch.setattr(subprocess, "Popen", popen)
    if hasattr(mod, "subprocess"):
        monkeypatch.setattr(mod.subprocess, "Popen", popen)
    with pytest.raises(_Captured):
        mod.main()
    assert seen, f"{path.name}: main() started no subprocess"
    return seen[0]


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_baseline_command_names_the_target_that_declares_its_tag(name, monkeypatch):
    path = SCRIPTS[name]
    argv = _command_of(path, monkeypatch)
    assert argv[:3] == ["rr", "sim", "run"], argv
    assert "--target" in argv, f"{path.name} runs {argv} with no --target"
    target = argv[argv.index("--target") + 1]
    tag = argv[argv.index("--tag") + 1]
    # DS-P1.103: a product is named (targets/<name>.yml); a bench target is passed as its path.
    target_path = ROOT / target if target.endswith(".yml") else ROOT / "targets" / f"{target}.yml"
    tags = (yaml.safe_load(target_path.read_text(encoding="utf-8"))
            .get("simulation") or {}).get("tags") or {}
    assert tag in tags, f"{path.name}: tag {tag!r} is not declared on --target {target!r}"
