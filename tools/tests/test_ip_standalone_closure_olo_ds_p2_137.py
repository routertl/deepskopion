# rr unknown-llm (AI-generated)
"""DS-P2.137 — the ``rr sim run --ip`` closures that reach the open-logic FIFO.

These two need libs/open-logic (gitignored), so each runs ``rr workspace
sync`` first, and one run takes about 90-150 s on a clean checkout on this
bench. That is over the test-power runner's 120 s, so the receipt parts for
these two manifests are the declared-closure gates in
test_ip_standalone_closure_ds_p2_137.py. These gates compile and run the
same closures. See ip_standalone_closure.py for the split.

* hw/rtl/fifo/ip.yml: the manifest loads by itself and declares the
  open-logic FIFO that fifo_sync instantiates under ``dependencies:``.
  Without that dependency the standalone compile cannot find library olo.
* uart_datalogger: the package pair, uart_pkg, the local config_control /
  lifecycle / fifo_sync manifests, and the ip.lock packages behind them. rr
  does not follow a local child's registry ``dependencies:``, so the UART
  manifest names the olo FIFO itself and does not depend on fifo's entry.
  The gate runs test_uart_lifecycle (one short test on the uart_datalogger
  top that checks the dev-id in its ENABLED/DISABLED frames) with the dev-id
  on the command line, so it judges the closure alone. The ticket's UART
  smoke flow is in test_ip_standalone_closure_long_ds_p2_137.py.

No skip paths: a gate that cannot run the simulator fails loudly.
"""
from __future__ import annotations

from ip_standalone_closure import (
    FIFO_IP,
    UART_IP,
    green_failure,
    standalone_sim,
    sync_workspace,
)

# uart_datalogger_test_utils.GENERICS["G_UART_DEV_ID"] (RTL default 28).
UART_DEV_ID = 23


def test_fifo_sync_standalone_closure_loads_and_runs_green() -> None:
    sync_workspace()
    proc, verdict = standalone_sim(FIFO_IP, "test_fifo_sync")
    failure = green_failure("fifo_sync", proc, verdict, expected=1)
    assert not failure, failure


def test_uart_datalogger_standalone_closure_compiles_and_runs_green() -> None:
    sync_workspace()
    proc, verdict = standalone_sim(
        UART_IP, "test_uart_lifecycle", generics={"G_UART_DEV_ID": UART_DEV_ID})
    failure = green_failure("uart_datalogger", proc, verdict, expected=1)
    assert not failure, failure
