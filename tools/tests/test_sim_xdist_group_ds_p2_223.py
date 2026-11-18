"""DS-P2.223: sim-driving tools/tests run as one xdist group, one at a time.

Measured 2026-11-06 on origin/main 16443b1f, tools/tests at -n 8: 10 tests
failed that each pass alone. The two failing sweeps both overlapped a plain
``rr sim run`` from the same checkout ("failed to open
hw/rtl/regbank/regbank_schema.hex"); no sweep outside that window failed.
tools/tests/conftest.py puts every module that drives a simulator in one
xdist group and switches -n runs to ``--dist loadgroup``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import conftest as C  # noqa: E402

#: The modules whose tests failed only under xdist on 2026-11-06.
MEASURED_RACERS = {
    "test_datalogger_mix_contract",
    "test_datalogger_mix_mechanisms",
    "test_ip_standalone_closure_long_ds_p2_137",
    "test_uart_rearm_powerup_state_ds_t1_16",
    "test_edge_maxfreq_confirm_sim_ds_p3_117",
}


def test_every_measured_racer_is_in_the_sim_group():
    assert MEASURED_RACERS <= C.sim_driving_modules()


def test_a_module_that_drives_no_sim_stays_out_of_the_group():
    assert "test_witness_pin_gate_ds_p3_184" not in C.sim_driving_modules()


@pytest.mark.parametrize("body,driving", [
    ('import subprocess\nsubprocess.run(["rr", "sim", "run", "t"])\n', True),
    ('CMD = [rr, "sim", "sweep", "x"]\n', True),
    ('import sys\nT = ROOT / "hw" / "sim" / "cocotb" / "t.py"\nrun([sys.executable, str(T)])\n', True),
    ('CMD = ["rr", "lint", "run"]\n', False),
    ('"""Docs mention rr sim run in prose only."""\n', False),
])
def test_the_detector_reads_each_spawn_form(tmp_path, body, driving):
    (tmp_path / "mod.py").write_text(body, encoding="utf-8")
    assert ("mod" in C.sim_driving_modules(tmp_path)) is driving


def test_importing_a_sim_driver_joins_the_group_transitively(tmp_path):
    (tmp_path / "helper.py").write_text('CMD = ["rr", "sim", "run", "t"]\n')
    (tmp_path / "middle.py").write_text("from helper import CMD\n")
    (tmp_path / "test_top.py").write_text("import middle\n")
    (tmp_path / "test_other.py").write_text("import json\n")
    assert C.sim_driving_modules(tmp_path) == {"helper", "middle", "test_top"}


@pytest.mark.skipif(shutil.which("true") is None, reason="needs a POSIX host")
def test_a_parallel_run_puts_the_group_on_one_worker(tmp_path):
    """EFFECT: a real -n 2 session. Four sim-driving tests record the xdist
    worker they ran on; with the group they share one worker. A run with
    the group hook removed spreads them (the control), so the assertion is
    about this conftest, not about xdist's defaults."""
    pytest.importorskip("xdist")

    def session(name: str, conftest_text: str) -> set[str]:
        work = tmp_path / name
        work.mkdir()
        (work / "conftest.py").write_text(conftest_text, encoding="utf-8")
        log = work / "workers.log"
        for i in range(4):
            (work / f"test_sim_{i}.py").write_text(
                "import os, time\n"
                'CMD = ["rr", "sim", "run", "never-executed"]\n'
                "def test_it():\n"
                "    time.sleep(0.3)\n"
                f"    open({str(log)!r}, 'a').write(os.environ['PYTEST_XDIST_WORKER'] + '\\n')\n",
                encoding="utf-8")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-n", "2", str(work)],
            cwd=work, env=env, capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
        return set(log.read_text().split())

    grouped = (HERE / "conftest.py").read_text(encoding="utf-8")
    assert len(session("grouped", grouped)) == 1

    control = grouped.replace("item.add_marker(pytest.mark.xdist_group(SIM_GROUP))", "pass")
    assert control != grouped
    assert len(session("control", control)) == 2
