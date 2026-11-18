"""DS-P3.167: DE25 targets must fail closed on unmatched constraints."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(ROOT / "tools"))
from target_files import target_file, with_bench  # noqa: E402  (DS-P1.103: products in targets/, the rest in bench/targets/)
# DS-P1.103: the DE25 product always; the bench CTU target only where bench/ exists.
TARGETS = tuple(sorted(with_bench({"de25_datalogger"}, {"de25_canfd"}, ROOT)))
MARKER = "DE25 unmatched-constraint gates are not fail-closed"


def test_de25_targets_enable_both_unmatched_constraint_gates_through_rr() -> None:
    """Check the resolved CLI configuration consumed by queued builds."""
    for target in TARGETS:
        config = yaml.safe_load(
            target_file(target, ROOT).read_text(encoding="utf-8")
        )
        for scope in ("synthesis", "implementation"):
            assert config[scope]["fail_on_unmatched_constraints"] is True, (
                f"{MARKER}: {target} {scope} YAML value is not true"
            )
            result = subprocess.run(
                [
                    "rr", "config", "-f", target_file(target, ROOT).relative_to(ROOT).as_posix(), "get",
                    f"{scope}.fail_on_unmatched_constraints",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                # stdout only: rr's notices go to stderr (RTL-P2.2163's Python
                # 3.10 line became "the value" when the two were merged).
                stderr=subprocess.PIPE,
                check=False,
            )
            lines = result.stdout.splitlines()
            resolved_value = lines[0].strip().lower() if lines else ""
            assert result.returncode == 0 and resolved_value == "true", (
                f"{MARKER}: rr resolved {target} {scope} to {result.stdout!r} "
                f"(stderr {result.stderr!r})"
            )
