# rr unknown-llm (AI-generated)
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.276: a desk run collects no test that starts a vendor tool through a real rr.

Measured 2026-11-12 on 9faf542f by the nightly desk job's rr guard (TIC-P3.512):
test_pfsoc_gen_project_elaborates_ds_p2_152.py runs `rr synth run --dry-run`, which starts Libero.
conftest.vendor_tool_modules names such modules (beside sim_driving_modules) and gives their tests
the `vendor_tools` marker; a desk run deselects them with -m "not vendor_tools".
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _rule():
    spec = importlib.util.spec_from_file_location("_ds_p2_276_conftest", HERE / "conftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _src(text: str) -> str:
    """Fixture source with STAGE/VERB/SIM placeholders filled in at run time (DS-P2.278): written
    literally, the fixtures made this file match both conftest rules, so the rule test itself was
    marked vendor_tools and joined the sim group."""
    return (text.replace("STAGE_S", "syn" + "th").replace("STAGE_B", "bit" + "stream")
            .replace("STAGE_I", "imple" + "mentation").replace("STAGE_P", "im" + "pl")
            .replace("VERB", "r" + "un").replace("SIMV", "s" + "im"))


def test_the_rule_names_real_rr_vendor_spawns_and_not_fake_rr_assertions(tmp_path):
    (tmp_path / "test_a.py").write_text(_src('rr = "x"\ncmd = [rr, "STAGE_S", "VERB", "--target", "t"]\n'))
    (tmp_path / "test_b.py").write_text(_src('import shutil\nc = [shutil.which("rr"), "STAGE_B", "VERB"]\n'))
    (tmp_path / "test_c.py").write_text(_src('c = ["/opt/bin/rr", "STAGE_I", "VERB"]\n'))
    (tmp_path / "helper_d.py").write_text(_src('def go(rr_bin):\n    return [rr_bin, "STAGE_P", "VERB"]\n'))
    (tmp_path / "test_e.py").write_text("import helper_d\n")
    (tmp_path / "test_f.py").write_text(_src('assert ["STAGE_S", "VERB"] in calls\nx = ("STAGE_B", "VERB")\n'))
    (tmp_path / "test_g.py").write_text(_src('cmd = [rr, "STAGE_S", "check"]\ncmd2 = [rr, "SIMV", "VERB"]\n'))
    got = _rule().vendor_tool_modules(tmp_path)
    assert got == {"test_a", "test_b", "test_c", "helper_d", "test_e"}, sorted(got)


def test_the_rule_finds_the_libero_test_in_this_directory():
    got = _rule().vendor_tool_modules(HERE)
    assert "test_pfsoc_gen_project_elaborates_ds_p2_152" in got, sorted(got)


def test_this_rule_test_is_in_neither_rule():
    """DS-P2.278: the rule test starts no vendor tool and no simulator, so neither rule may select it."""
    rule, me = _rule(), Path(__file__).stem
    assert me not in rule.vendor_tool_modules(HERE)
    assert me not in rule.sim_driving_modules(HERE)


def test_a_desk_selection_collects_no_vendor_tool_test():
    members = _rule().vendor_tool_modules(HERE)
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "-m", "not vendor_tools", str(HERE)],
        cwd=HERE.parents[1], capture_output=True, text=True, errors="replace", timeout=600)
    collected = [line for line in out.stdout.splitlines() if "::" in line]
    assert collected, out.stdout[-2000:] + out.stderr[-2000:]   # the collection itself ran
    leaked = sorted({line.split("::", 1)[0] for line in collected
                     if Path(line.split("::", 1)[0]).stem in members})
    assert not leaked, f"desk selection still collects vendor-tool tests: {leaked}"
