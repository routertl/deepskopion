"""Shared harness for the DS-P2.137 ``rr sim run --ip`` closure gates.

``rr sim run <test> --ip <ip-dir>`` does NOT run the cocotb file's ``main()``.
It compiles exactly the closure the selected ``ip.yml`` declares: its own
sources plus whatever its ``dependencies:`` reach. The tests' ``main()``
``custom_libraries={"tich": [...]}`` (what a project-mode run compiles) is
never consulted, and neither are the generics ``main()`` passes. Those come
from the manifest's ``build.simulation.tests.<module>.generics``.

Every sim gate runs the real ``rr sim run --ip`` CLI with RR_QUEUE_SIMS=0 so
it runs in-process instead of via the bench queue, and judges the
result_collector's latest.json. It deletes ``sim/work/nvc`` (a gitignored,
regenerable build cache) first, so the ``tich`` library has to be compiled by
this run and is not inherited from an earlier one.

The gates are split by what a clean checkout must do before the sim, because
the test-power runner gives each pytest run 120 s:

* test_ip_standalone_closure_ds_p2_137.py: config_control, lifecycle and the
  per-test generics. No ``rr workspace sync`` needed.
* test_ip_standalone_closure_olo_ds_p2_137.py: fifo_sync and uart_datalogger.
  They need the open-logic FIFO in libs/ (gitignored), so each runs
  ``rr workspace sync`` first (about 65 s on a clean checkout).
* test_ip_standalone_closure_long_ds_p2_137.py: the UART smoke test exactly as
  the ticket names it (about 110-290 s of simulation) and the negative
  controls. Too long for one 120 s run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "sim"

CONFIG_CONTROL_IP = "hw/rtl/datalogger/infra/config_control"
LIFECYCLE_IP = "hw/rtl/datalogger/infra/datalogger_lifecycle"
UART_IP = "hw/rtl/datalogger/uart"
FIFO_IP = "hw/rtl/fifo"


def rr() -> str:
    path = shutil.which("rr")
    assert path, "rr CLI not on PATH — this gate cannot run the sanctioned sim"
    return path


def run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout,
        env={**os.environ, "RR_QUEUE_SIMS": "0", "RR_QUEUE_BUILDS": "0"},
    )


def tail(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout[-4000:] + proc.stderr[-2000:]


def sync_workspace() -> None:
    # A clean checkout has no libs/open-logic (gitignored); the fifo_sync
    # closure reads the ip.lock packages from there. Idempotent, cache-backed.
    sync = run([rr(), "workspace", "sync"], ROOT, timeout=600)
    assert sync.returncode == 0, "rr workspace sync failed:\n" + tail(sync)


def standalone_sim(
    ip_rel: str, module: str, *, generics: dict[str, int] | None = None,
    coverage: bool = False, timeout_s: int = 300,
) -> tuple[subprocess.CompletedProcess, dict]:
    """Run ``rr sim run <module> --ip <ip_rel>`` from a cold nvc work dir.

    ``generics`` become ``-g KEY=VALUE`` on the command line. A gate passes
    them only when it judges the dependency closure, not the manifest's
    per-test generics (those have their own gate).
    """
    for stale in (SIM / "work" / "nvc", SIM / "results" / module, SIM / "waves" / module):
        shutil.rmtree(stale, ignore_errors=True)
    cmd = [rr(), "sim", "run", module, "--ip", ip_rel, "--timeout", str(timeout_s)]
    for name, value in (generics or {}).items():
        cmd += ["-g", f"{name}={value}"]
    if coverage:
        cmd.append("--coverage")
    proc = run(cmd, ROOT, timeout=timeout_s + 600)
    latest = SIM / "results" / module / "latest.json"
    verdict = json.loads(latest.read_text()) if latest.is_file() else {}
    return proc, verdict


def green_failure(label: str, proc, verdict: dict, expected: int) -> str:
    """Why a standalone sim is not green, or "" when it is.

    Returns the message instead of asserting so the ``assert`` runs in the
    test body, where pytest's assertion rewriting counts it as executed.
    Two distinct failures. A closure that does not compile writes no
    result at all. A closure that compiles but runs the DUT with the wrong
    generics writes a result whose cocotb assertions failed.
    """
    if not verdict:
        return (
            f"DS-P2.137 {label} closure never reached cocotb: rc={proc.returncode}, "
            "no latest.json written; output tail:\n" + tail(proc))
    if (proc.returncode == 0
            and verdict.get("passed") == expected
            and verdict.get("total") == expected
            and verdict.get("failed") == 0
            and verdict.get("errors") == 0
            and verdict.get("skipped") == 0):
        return ""
    return (
        f"DS-P2.137 {label} cocotb tests failed under --ip: rc={proc.returncode} "
        f"passed={verdict.get('passed')} failed={verdict.get('failed')} "
        f"errors={verdict.get('errors')} skipped={verdict.get('skipped')} "
        f"total={verdict.get('total')} (expected {expected}); output tail:\n"
        + tail(proc))


def ip_declared_closure(ip_rel: str) -> tuple[dict[str, set[str]], set[str], str]:
    """What ``rr sim run --ip <ip_rel>`` is told to compile besides the IP itself.

    Computed by the SDK steps ``build_sim_plan`` runs, up to the point where
    it opens the installed libs/ packages (which needs ``rr workspace sync``,
    about 65-150 s on a clean checkout on this bench):

    * ``libraries``: library -> repo-relative VHDL files that the declared
      local manifests (``dependencies: {path: ...}``) compose, depth-first;
    * ``locked``: the declared registry refs that ip.lock pins.

    Returns ``(libraries, locked, error)``; a manifest the SDK refuses to load
    declares nothing, and ``error`` says why. The sim gates in
    test_ip_standalone_closure_olo_ds_p2_137.py compile the same closure.
    """
    from routertl_core.package_resolver import load_lock
    from sdk.cli.ip_standalone import (  # the installed routertl SDK
        _declared_package_refs,
        _resolve_local_dependency_payload,
        _standalone_package_root,
        load_ip_manifest,
    )

    ip_dir = (ROOT / ip_rel).resolve()
    try:
        manifest = load_ip_manifest(ip_dir)
        payload, _verilog = _resolve_local_dependency_payload(
            manifest, _standalone_package_root(ip_dir))
        refs = _declared_package_refs(manifest.dependencies, ip_dir)
        lock = load_lock(ROOT) or {}
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return {}, set(), f"{type(exc).__name__}: {exc}"
    libraries = {
        lib: {Path(f).resolve().relative_to(ROOT).as_posix() for f in files}
        for lib, files in payload.items()
    }
    return libraries, {ref for ref in refs if ref in lock}, ""


def ip_test_generics(ip_rel: str, module: str) -> tuple[dict, str]:
    """The generics ``rr sim run <module> --ip <ip_rel>`` applies to the DUT.

    Read through the SDK's own selection (``build_sim_plan`` calls
    ``_selected_test_simulation_config`` for its ``test_generic_overrides``),
    not by re-parsing the YAML here. ``build_sim_plan`` itself also resolves
    the ip.lock packages, and the SDK has no public call that returns the
    per-test generics without doing that, so this stops before package
    resolution: the answer does not depend on libs/ or on the dependency
    closure. Returns ``(generics, error)``; a manifest the SDK refuses to load
    applies no generics, and ``error`` says why.
    """
    from sdk.cli.ip_standalone import (  # the installed routertl SDK
        _selected_test_simulation_config,
        load_ip_manifest,
    )

    ip_dir = (ROOT / ip_rel).resolve()
    try:
        manifest = load_ip_manifest(ip_dir)
        selected = _selected_test_simulation_config(
            manifest.simulation_config or {}, module, ip_dir)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return {}, f"{type(exc).__name__}: {exc}"
    return dict(selected.get("generics") or {}), ""
