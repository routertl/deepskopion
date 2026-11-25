# rr unknown-llm (AI-generated)
"""Cheap contract guards for the expensive DS-X3 queued simulation gate."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "targets" / "zybo_datalogger.yml"  # DS-P1.103: run_xilinx_baseline.py TARGET_NAME
TEST_ROOT = ROOT / "hw" / "sim" / "cocotb" / "tests"

EXPECTED_BY_CATEGORY = {
    "protocol": {
        "test_i2c_datalogger",
        "test_uart_precommit",
        "test_qspi_capture",
        "test_jtag_datalogger",
        "test_can_datalogger",
        "test_edge_datalogger",
    },
    "gather": {
        "test_datalogger_gather",
        "test_datalogger_gather_crossproto",
        "test_datalogger_gather_i2c",
        "test_datalogger_gather_uart",
        "test_datalogger_gather_spi",
        "test_datalogger_gather_jtag",
        "test_datalogger_gather_edge",
    },
    "regbank": {
        "test_demo_axilite_bfm_conformance",
        "test_regbank_datalogger_config_seam",
    },
    "framer": {
        "test_zs_msg_framer",
        "test_framer_axistream_conformance",
    },
    "backpressure_loss": {
        "test_arbiter",
        "test_error_event_bank",
        "test_error_event_emitter",
        "test_emit_framer_integration",
    },
    "end_to_end": {"test_datalogger_to_ddr"},
    "regbank_rom": {
        "test_regbank_rom_stream_ds_p1_82",
        "test_regbank_wrapper_rom_seam",
        "test_pfsoc_fic0_regbank_read_ds_p2_305",
        "test_regbank_reset_values_ds_p2_317",
        "test_pfsoc_fic0_rom_stream_rdloop_ds_p2_305",
    },
    "fic0_capture": {
        "test_fic0_stall_trigger_ds_p2_326",
        "test_fic0_stall_capture_ds_p2_326",
    },
}


def _tag() -> list[str]:
    target = yaml.safe_load(TARGET.read_text(encoding="utf-8"))
    return target["simulation"]["tags"]["xilinx_baseline"]


def _module_paths() -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for path in TEST_ROOT.rglob("test_*.py"):
        assert path.stem not in paths, f"duplicate cocotb module {path.stem}"
        paths[path.stem] = path
    return paths


def test_baseline_tag_pins_every_required_category() -> None:
    expected = set().union(*EXPECTED_BY_CATEGORY.values())
    tag = _tag()
    assert len(tag) == len(set(tag)), "xilinx_baseline contains duplicate modules"
    assert set(tag) == expected


def test_baseline_modules_exist_and_have_no_static_skip() -> None:
    paths = _module_paths()
    for module in _tag():
        assert module in paths, f"xilinx_baseline module does not exist: {module}"
        tree = ast.parse(paths[module].read_text(encoding="utf-8"), paths[module])
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            assert name not in {"pytest.skip", "cocotb.skip"}, (
                f"{module} can skip at runtime via {name}; DS-X3 requires a "
                "real pass/fail scenario"
            )
            if name == "cocotb.test":
                skip = next((kw.value for kw in node.keywords if kw.arg == "skip"), None)
                assert skip is None or not isinstance(skip, ast.Constant) or not skip.value, (
                    f"{module} contains @cocotb.test(skip=True)"
                )


def test_runner_expects_exact_tag_cardinality_and_forces_queue_wait() -> None:
    runner_path = ROOT / "tools" / "sim" / "run_xilinx_baseline.py"
    text = runner_path.read_text(encoding="utf-8")
    assert 'env["RR_QUEUE_SIMS"] = "1"' in text
    assert '"--wait"' in text
    assert '("failed", "errors", "skipped", "undetermined", "undetermined_xpass")' in text
    assert '"session_id"' in text


def test_runner_rejects_skipped_or_partial_result_artifacts() -> None:
    from tools.sim.run_xilinx_baseline import _result_problem

    green = {
        "module": "test_demo",
        "total": 2,
        "passed": 2,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "undetermined": 0,
        "undetermined_xpass": 0,
        "session_id": "one-run",
        "tests": [{"status": "pass"}, {"status": "pass"}],
    }
    assert _result_problem(green, "test_demo") is None
    assert _result_problem({**green, "skipped": 1}, "test_demo") == "skipped=1"
    assert _result_problem({**green, "passed": 1}, "test_demo") == "passed=1, total=2"
