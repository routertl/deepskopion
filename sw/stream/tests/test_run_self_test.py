"""Regression tests for the PS self-test orchestrator's process verdict."""

from __future__ import annotations

import json
from types import SimpleNamespace

from sw.stream.testbench import run_self_test
from sw.stream.testbench.scenarios import (
    Scenario,
    Verdict,
    all_scenarios,
    spi_emio_round_trip,
)


class _FakeRegbank:
    def identity(self) -> dict[str, int]:
        return {
            "git_hash": 0x2C87E213,
            "system_version": 0x02050000,
            "build_date": 0x20260813,
            "regbank_version": 0x00010000,
        }

    def contract_abi_ver(self) -> int:
        return run_self_test.abi_verdict(0)["hw"]


class _FakeBench:
    def __init__(self) -> None:
        self.regbank = _FakeRegbank()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_every_declared_ps_loop_requirement_has_a_scenario():
    assert {scenario.req_id for scenario in all_scenarios()} == {
        f"PS-LOOP-REQ-{number}" for number in range(1, 10)
    }


def test_unavailable_ring_oracle_cannot_false_green(monkeypatch, tmp_path):
    """The DS-X5 defect: an all-skipped capture layer used to exit zero."""
    bench = _FakeBench()
    scenarios = [
        Scenario("PS-LOOP-REQ-1", "layer_zero", lambda _b, _o: Verdict.passed(), 0),
        Scenario("PS-LOOP-REQ-5", "capture_one", lambda _b, _o: Verdict.passed(), 1),
        Scenario("PS-LOOP-REQ-6", "capture_two", lambda _b, _o: Verdict.passed(), 1),
    ]
    monkeypatch.setattr(run_self_test, "SelfTestBench", lambda: bench)
    monkeypatch.setattr(run_self_test, "all_scenarios", lambda: scenarios)
    monkeypatch.setattr(
        run_self_test,
        "make_oracle",
        lambda _bench: (_ for _ in ()).throw(PermissionError("/dev/mem denied")),
    )
    monkeypatch.setattr(run_self_test, "clock_is_trustworthy", lambda: True)
    monkeypatch.setattr(run_self_test, "read_deploy_stamp", lambda: None)
    monkeypatch.setattr(run_self_test, "testbench_git_rev", lambda: "test-rev")

    assert run_self_test.run_all(tmp_path) == 1
    assert bench.closed is True

    verdict = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert verdict["summary"] == {"PASS": 1, "FAIL": 0, "SKIP": 2}
    assert [item["status"] for item in verdict["scenarios"]] == [
        "PASS", "SKIP", "SKIP",
    ]


def test_wrong_ddr_payload_makes_spi_scenario_fail(monkeypatch):
    """Negative control: master success cannot mask wrong captured bytes."""
    expected = bytes([0x12, 0x34, 0x56, 0x78])

    class _Regbank:
        load_spi_response = lambda self, _payload: None
        set_emio_mux = lambda self, **_kwargs: None

    bench = SimpleNamespace(
        regbank=_Regbank(),
        spi=SimpleNamespace(xfer=lambda _mosi: expected),
    )
    wrong_frames = [
        SimpleNamespace(
            datalogger_id=8,
            payload=bytes([byte]),
            header=SimpleNamespace(event_id=0b010),
        )
        for byte in bytes([0x12, 0x34, 0x56, 0x79])
    ]
    oracle = SimpleNamespace(
        snapshot=lambda: 0,
        collect_new_frames=lambda **_kwargs: wrong_frames,
    )
    monkeypatch.setattr("sw.stream.testbench.scenarios.time.sleep", lambda _s: None)

    verdict = spi_emio_round_trip(bench, oracle)

    assert verdict.status == "FAIL"
    assert verdict.expected == {"captured_miso": expected.hex()}
    assert verdict.got["captured_miso"] == "12345679"


def test_uart_scenario_passes_with_appended_console_bytes_and_isolates_window(monkeypatch):
    """DS-P3.165: MMUART1 is shared with HSS E51 monitor console on PolarFire SoC.
    The self-test verdict must be unaffected by extra appended console text after
    the stimulus payload, and must isolate its stimulus window (set_emio_mux(uart=False))."""
    from sw.stream.testbench.scenarios import uart_emio_round_trip

    payload = b"DEEPSKOPION-DS-T9-UART-READBACK!"
    # Realistic HSS monitor loop ticks captured on PolarFire silicon
    console_bytes = b"\x1b[32m[698.176744]\x1b[1;32m loop 85000000 took 4907 ticks (max 606530678 ticks)\r\n\x1b[0m"

    mux_history: list[dict[str, bool]] = []

    class _Regbank:
        def enable_datalogger(self, _id, **_kwargs):
            pass

        def set_emio_mux(self, **kwargs):
            mux_history.append(kwargs)

    written: list[bytes] = []

    class _Uart:
        def write(self, data: bytes):
            written.append(data)

    bench = SimpleNamespace(
        regbank=_Regbank(),
        uart=_Uart(),
    )

    captured_stream = payload + console_bytes
    frames = [
        SimpleNamespace(
            datalogger_id=12,
            payload=bytes([byte, 0, 0, 0]),
        )
        for byte in captured_stream
    ]

    oracle = SimpleNamespace(
        snapshot=lambda: 0,
        collect_new_frames=lambda **_kwargs: frames,
    )
    monkeypatch.setattr("sw.stream.testbench.scenarios.time.sleep", lambda _s: None)

    verdict = uart_emio_round_trip(bench, oracle)

    # 1. Payload check: verdict must pass despite appended console text
    assert verdict.status == "PASS", (
        f"self-test UART verdict affected by appended console bytes: {verdict.detail}"
    )

    # 2. Stimulus window isolation: set_emio_mux(uart=False) must be called in finally
    assert len(mux_history) >= 2, (
        f"uart_emio_round_trip failed to isolate stimulus window: "
        f"expected set_emio_mux calls for open and close, got {mux_history}"
    )
    assert mux_history[-1].get("uart") is False, (
        "uart_emio_round_trip failed to isolate stimulus window: "
        f"final set_emio_mux call was {mux_history[-1]} instead of uart=False"
    )
