"""DS-P2.137 — datalogger IP simulations are self-contained under ``rr sim run --ip``.

The gates that need no ``rr workspace sync`` (see ip_standalone_closure.py for
the split). One gate per production change, so reverting any one of them
turns exactly its own gate red:

* config_control and datalogger_lifecycle declare the datalogger package pair
  (hw/rtl/pkg_ip/datalogger). Without it analysis dies on "library TICH not
  found", the queued-worker failure the ticket recorded. The lifecycle closure
  gate passes the dev-id on the command line so it judges the closure alone.
* hw/rtl/fifo/ip.yml loads as a manifest and declares the open-logic FIFO
  that fifo_sync instantiates under ``dependencies:``.
* uart_datalogger declares its whole closure: the package pair, uart_pkg, the
  local config_control / lifecycle / fifo_sync manifests, and the ip.lock
  packages (olo FIFO, edge_detector, ram_dual_port).
* The per-test generics the tests' ``main()`` used to supply live in the
  manifests (lifecycle dev-id 12, uart smoke dev-id 23). Their gates ask the
  SDK which generics ``--ip`` applies; that answer does not need the closure.
* The exact ticket flow for lifecycle (no ``-g``) shows the two together.

The fifo and UART gates here read the closure through the SDK's own ``--ip``
resolution and stop before the installed libs/ packages, because compiling
those closures needs ``rr workspace sync`` first and a single run then takes
longer than the test-power runner's 120 s. The olo file compiles and runs
both closures; the long file runs the UART smoke test as the ticket names it.

No skip paths: a gate that cannot run the simulator fails loudly.
"""
from __future__ import annotations

import re

from ip_standalone_closure import (
    CONFIG_CONTROL_IP,
    LIFECYCLE_IP,
    SIM,
    FIFO_IP,
    UART_IP,
    green_failure,
    ip_declared_closure,
    ip_test_generics,
    standalone_sim,
)

CONFIG_CONTROL_TESTS = {
    "test_reset_determinism",
    "test_enable_latches_regardless_of_config_timing",
    "test_enable_latches_when_config_steps_on_clock_edge",
}

# Hard-coded from the cocotb tests, not read from them at runtime:
# test_datalogger_lifecycle.py DEV_ID = 12 (RTL default G_DEV_ID := 0) and
# uart_datalogger_test_utils.GENERICS["G_UART_DEV_ID"] = 23 (RTL default 28).
LIFECYCLE_DEV_ID = 12
UART_SMOKE_DEV_ID = 23

# Every unit uart_datalogger.vhd and its sub-blocks `use` or instantiate,
# outside the uart directory itself.
UART_CLOSURE_TICH = {
    "hw/rtl/pkg/datalogger_config_pkg.vhd",
    "hw/rtl/pkg/datalogger_pkg.vhd",
    "hw/rtl/pkg/uart_pkg.vhd",
    "hw/rtl/datalogger/infra/config_control/config_control.vhd",
    "hw/rtl/datalogger/infra/datalogger_lifecycle/datalogger_lifecycle.vhd",
    "hw/rtl/fifo_sync.vhd",
}
UART_CLOSURE_LOCKED = {
    "open-logic/olo_base_fifo_sync",
    "routertl/edge_detector",
    "routertl/ram_dual_port",
}


def test_config_control_standalone_closure_compiles_runs_covers_and_dumps_waves() -> None:
    proc, verdict = standalone_sim(
        CONFIG_CONTROL_IP, "test_config_control", coverage=True, timeout_s=120)
    failure = green_failure("config_control", proc, verdict, expected=3)
    assert not failure, failure
    assert {t["name"] for t in verdict["tests"]} == CONFIG_CONTROL_TESTS, verdict["tests"]

    # The packages and the DUT were analysed into a tich library by THIS run.
    assert (SIM / "work" / "nvc" / "tich").is_dir(), "no tich library compiled"

    # Measured coverage: nvc's report for the DUT, with real hit counts.
    cover = SIM / "work" / "nvc" / "config_control.cover.xml"
    assert cover.is_file(), "--coverage produced no config_control.cover.xml"
    text = cover.read_text()
    assert "TICH.CONFIG_CONTROL" in text, "coverage report is not for tich.config_control"
    hits = [int(n) for n in re.findall(r'<statement [^>]*data="(\d+)"', text)]
    assert hits and max(hits) > 0, f"coverage report has no executed statements: {hits}"

    # One non-empty FST per isolated cocotb test.
    waves = sorted((SIM / "waves" / "test_config_control").glob("*.fst"))
    assert len(waves) == 3 and all(w.stat().st_size > 0 for w in waves), waves


def test_datalogger_lifecycle_standalone_closure_compiles_and_runs_green() -> None:
    proc, verdict = standalone_sim(
        LIFECYCLE_IP, "test_datalogger_lifecycle",
        generics={"G_DEV_ID": LIFECYCLE_DEV_ID})
    failure = green_failure("datalogger_lifecycle", proc, verdict, expected=1)
    assert not failure, failure


def test_fifo_manifest_loads_and_declares_the_open_logic_fifo() -> None:
    _libraries, locked, error = ip_declared_closure(FIFO_IP)
    assert "open-logic/olo_base_fifo_sync" in locked, (
        f"DS-P2.137 fifo_sync --ip closure does not reach the open-logic FIFO: "
        f"locked refs {sorted(locked)} {error}".rstrip())


def test_uart_manifest_declares_its_whole_closure() -> None:
    libraries, locked, error = ip_declared_closure(UART_IP)
    missing = sorted((UART_CLOSURE_TICH - libraries.get("tich", set()))
                     | (UART_CLOSURE_LOCKED - locked))
    assert not missing, (
        f"DS-P2.137 uart_datalogger --ip closure is missing {missing} {error}".rstrip())


def test_ip_flow_gives_lifecycle_test_its_dev_id() -> None:
    generics, error = ip_test_generics(LIFECYCLE_IP, "test_datalogger_lifecycle")
    assert generics.get("G_DEV_ID") == LIFECYCLE_DEV_ID, (
        f"DS-P2.137 --ip does not give test_datalogger_lifecycle "
        f"G_DEV_ID={LIFECYCLE_DEV_ID}: it applies {generics!r} {error}".rstrip())


def test_ip_flow_gives_uart_smoke_test_its_dev_id() -> None:
    generics, error = ip_test_generics(UART_IP, "test_uart_datalogger_smoke")
    assert generics.get("G_UART_DEV_ID") == UART_SMOKE_DEV_ID, (
        f"DS-P2.137 --ip does not give test_uart_datalogger_smoke "
        f"G_UART_DEV_ID={UART_SMOKE_DEV_ID}: it applies {generics!r} {error}".rstrip())


def test_datalogger_lifecycle_exact_ip_flow_runs_green() -> None:
    # The ticket's flow as written: no -g, the manifest supplies everything.
    proc, verdict = standalone_sim(LIFECYCLE_IP, "test_datalogger_lifecycle")
    failure = green_failure("datalogger_lifecycle (exact --ip flow)", proc, verdict, expected=1)
    assert not failure, failure
