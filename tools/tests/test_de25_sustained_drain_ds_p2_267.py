# rr unknown-llm (AI-generated)
"""DS-P2.267 — DS-DE25-DRAIN-001: the DE25 ring fill does not grow over >= 60 s.

DS-DE25-TPUT-001 judges a 2 s RING_READ_PTR rate against a 14.4e6 B/s floor.
DS-P1.74 run 3 measured the drain at 13.1e6 B/s against a 20.0e6 B/s ring
write: a drain between the floor and the write rate passes that test while the
ring fills. DS-DE25-DRAIN-001 judges the fill itself, through an instrument
(tools/hw/de25_probe_instr.py) and an oracle (tools/hw/de25_fill_growth_oracle.py),
because the contract grammar cannot bound a difference of two registers read
together (RTL-P3.2480).

POWER
-----
* Run-3 replay: the three judged run-3 windows (committed transcript) are RED
  on fill growth (F = 17 922 880 / 19 234 400 / 18 604 800 B > 655 360 B), and
  the full oracle does not pass them (their windows are 2.6-2.8 s, < 60 s).
* Synthetic GREEN: a model board whose drain keeps pace passes every layer,
  through routertl's own run_contract on the hw.yml requirement.
* Fail-closed cases: a dead stimulus (F = 0), a write rate outside the band,
  a window under 60 s and a missing instrument never pass.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "hw"))
sys.path.insert(0, str(ROOT))

import de25_fill_growth_oracle as oracle  # noqa: E402
import de25_probe_instr as instr  # noqa: E402

CONTRACT = ROOT / "runtimes" / "hw.yml"
RUN3 = ROOT / "docs" / "hardware" / "evidence" / "ds_p1_74" / "run3_probe_transcript.jsonl"
MANIFEST = ROOT / "tools" / "tests" / "data" / "ds_p2_165_witness" / "session_C2" / \
    "capture_chain.json"
REQ = "DS-DE25-DRAIN-001"
RED = "DS-DE25-DRAIN-001-RED"
BASE = 0x20000000
STIM = ("0x20000244", "0x00000191")
RING = 1 << 28


def _status(out: dict) -> dict:
    return {layer["name"]: layer["status"] for layer in out["layers"]}


def _sample(commit: int, read: int, uptime: float, bp: int = 0) -> dict:
    return {"op": "peek_instr", "addr": "0x20000154",
            "observed": "0x%08X" % (read % RING), "commit": "0x%08X" % (commit % RING),
            "write_msgs": "0x00000000", "backpressure": "0x%08X" % (bp % (1 << 32)),
            "fifo_overflow": "0x00000000", "board_uptime": f"{uptime:.2f}"}


def _ring(w: float, r: float, f0: int, dt: float) -> tuple[int, int, int]:
    """(commit, read, backpressure) of a ring written at w and drained at r for
    dt seconds from fill f0. At full the commit pointer equals the read pointer
    (rr-ddr-stream ptr_mgr) and DDR_BACKPRESSURE_CNT counts 100e6 cycles/s."""
    read = int(r * dt)
    if w > r and f0 + (w - r) * dt >= RING:
        t_full = (RING - f0) / (w - r)
        return read + RING, read, int((dt - t_full) * 100e6)
    return int(f0 + w * dt), read, 0


def _three(w=20.0e6, r=20.0e6, f0=1_000_000, t=(100.0, 102.2, 162.6)) -> list[dict]:
    """S0, S1, S2 of a board written at w and drained at r (B/s)."""
    return [_sample(*_ring(w, r, f0, u - t[0])[:2], u, bp=_ring(w, r, f0, u - t[0])[2])
            for u in t]


# ── run-3 replay ────────────────────────────────────────────────────────────


def _run3_windows() -> list[list[dict]]:
    """The two instrumented samples after each stimulus-on poke, in order:
    W1, W2, W3 (daemon running) and the RED window (daemon stopped)."""
    recs = oracle.load_records(RUN3)
    windows, cur = [], None
    for r in recs:
        if r.get("op") == "poke" and r.get("addr") == STIM[0]:
            cur = [] if r.get("value") == STIM[1] else None
            if cur is not None:
                windows.append(cur)
        elif r.get("op") == "peek_instr" and cur is not None:
            cur.append(r)
    return windows


def test_run3_windows_are_red_on_fill_growth():
    """The fill grew by many ticks in every run-3 window: the drain was capped
    at 13.1e6 B/s against a 20.0e6 B/s write (DS-P1.74 run 3)."""
    windows = _run3_windows()
    assert len(windows) == 4, f"run-3 transcript: {len(windows)} stimulus windows"
    growth = []
    for w in windows[:3]:
        assert len(w) == 2, w
        s0, s1 = w
        out = oracle.judge([s0, s1, s1], min_window_s=0.0)
        assert _status(out) == {"instrument": "pass", "stimulus": "pass",
                                "window": "pass", "fill_growth": "fail"}, out
        growth.append(out["layers"][3]["metrics"]["F_B"])
    assert growth == [17_922_880, 19_234_400, 18_604_800]
    assert all(g > oracle.TICK_BYTES for g in growth)


def test_run3_windows_never_pass_the_full_oracle():
    """With the 60 s window rule as frozen, a run-3 window (2.6-2.8 s) is not
    judged: the window layer and fill_growth do not pass."""
    for w in _run3_windows()[:3]:
        out = oracle.judge([w[0], w[1], w[1]])
        st = _status(out)
        assert st["window"] != "pass" and st["fill_growth"] != "pass", out


# ── the oracle's judgement ──────────────────────────────────────────────────


def test_drain_keeping_pace_passes():
    out = oracle.judge(_three())
    assert _status(out) == dict.fromkeys(
        ("instrument", "stimulus", "window", "fill_growth"), "pass"), out


@pytest.mark.parametrize("extra,status", [(oracle.TICK_BYTES, "pass"),
                                          (oracle.TICK_BYTES + 1, "fail")])
def test_one_tick_is_the_limit(extra, status):
    s = _three()
    s[2] = _sample(int(s[2]["commit"], 16) + extra, int(s[2]["observed"], 16),
                   float(s[2]["board_uptime"]))
    out = oracle.judge(s)
    assert _status(out)["fill_growth"] == status
    assert out["layers"][3]["metrics"]["F_B"] == extra


def test_run3_drain_rate_is_red_over_a_full_window():
    """A model of the run-3 board (w 20.0e6, r 13.1072e6 B/s) over 62.6 s: the
    ring is full after about 39 s, so f2 reads 0 and only the backpressure
    clause can see it."""
    out = oracle.judge(_three(r=13_107_200))
    m = out["layers"][3]["metrics"]
    assert _status(out)["fill_growth"] == "fail"
    assert m["f2_B"] == 0 and m["F_B"] < 0 and m["backpressure_delta"] > 0, m


def test_full_ring_reading_fill_zero_is_not_green():
    """A full ring reads f = 0 (commit == read). Without the backpressure clause
    F would be negative and pass."""
    s = _three()
    c2, r2, u2 = int(s[2]["observed"], 16), int(s[2]["observed"], 16), float(s[2]["board_uptime"])
    s[2] = _sample(c2, r2, u2, bp=12345)
    out = oracle.judge(s)
    assert out["layers"][3]["metrics"]["F_B"] < 0
    assert _status(out)["fill_growth"] == "fail"


def test_run3_windows_did_not_reach_full():
    """The run-3 replay is red on fill growth itself: backpressure did not move."""
    for w in _run3_windows()[:3]:
        out = oracle.judge([w[0], w[1], w[1]], min_window_s=0.0)
        assert out["layers"][3]["metrics"]["backpressure_delta"] == 0


def test_dead_stimulus_is_not_green():
    """No ring write: F = 0, which must not read as a sustained drain."""
    out = oracle.judge(_three(w=0.0, r=0.0))
    st = _status(out)
    assert st["stimulus"] != "pass" and st["fill_growth"] != "pass", out


@pytest.mark.parametrize("w", [17.9e6, 22.1e6])
def test_write_rate_outside_the_band_is_not_judged(w):
    st = _status(oracle.judge(_three(w=w, r=w)))
    assert st["stimulus"] != "pass" and st["fill_growth"] != "pass"


def test_write_rate_span_long_enough_to_wrap_is_not_judged():
    """S0 -> S1 of 12.5 s: a 22.0e6 B/s commit could wrap the ring unseen."""
    st = _status(oracle.judge(_three(t=(100.0, 112.5, 172.0))))
    assert st["stimulus"] != "pass" and st["fill_growth"] != "pass"


def test_commit_wrap_inside_the_write_rate_span_is_unwrapped():
    s = _three(f0=RING - 1_000_000)
    out = oracle.judge(s)
    assert _status(out)["stimulus"] == "pass"
    assert abs(out["layers"][1]["metrics"]["w_Bps"] - 20.0e6) < 0.01e6


def test_window_under_60_s_is_not_judged():
    st = _status(oracle.judge(_three(t=(100.0, 102.2, 159.99))))
    assert st["window"] != "pass" and st["fill_growth"] != "pass"
    assert _status(oracle.judge(_three(t=(100.0, 102.2, 160.0))))["window"] == "pass"


@pytest.mark.parametrize("n", [0, 2, 4])
def test_instrument_must_give_exactly_three_samples(n):
    s = (_three() + _three())[:n]
    st = _status(oracle.judge(s))
    assert st["instrument"] != "pass" and st["fill_growth"] != "pass"


def test_samples_are_taken_after_the_last_stimulus_poke(tmp_path):
    t = tmp_path / "t.jsonl"
    old = _three(r=0.0)
    recs = ([{"op": "poke", "addr": STIM[0], "value": STIM[1]}] + old
            + [{"op": "poke", "addr": STIM[0], "value": "0x00000000"},
               {"op": "poke", "addr": STIM[0], "value": STIM[1]}] + _three())
    t.write_text("".join(json.dumps(r) + "\n" for r in recs))
    out = io.StringIO()
    with redirect_stdout(out):
        rc = oracle.main(["--transcript", str(t), "--stim-addr", STIM[0],
                          "--stim-value", STIM[1]])
    assert rc == 0
    assert _status(json.loads(out.getvalue()))["fill_growth"] == "pass"


def test_missing_transcript_is_not_green(tmp_path):
    out = io.StringIO()
    with redirect_stdout(out):
        oracle.main(["--transcript", str(tmp_path / "absent.jsonl"),
                     "--stim-addr", STIM[0], "--stim-value", STIM[1]])
    st = _status(json.loads(out.getvalue()))
    assert set(st.values()) == {"skipped"}


def test_oracle_output_is_valid_routertl_layered_json():
    from sdk.engine.hardware.run_log import oracle_validation_errors
    for out in (oracle.judge(_three()), oracle.judge([]), oracle.judge(_three(w=0, r=0))):
        assert oracle_validation_errors(out) == [], out


# ── the instrument ──────────────────────────────────────────────────────────


def test_instrument_reads_the_read_pointer_first_in_one_line():
    cmd = instr.instr_command(BASE)
    reads = re.findall(r"devmem (0x[0-9A-F]+) 32", cmd)
    assert reads == ["0x20000154", "0x20000164", "0x20000150", "0x20000168", "0x200000C4"]
    assert cmd.endswith("cut -d' ' -f1 /proc/uptime") and "\n" not in cmd


def test_instrument_parses_a_run3_line():
    out = "0x00868B54\n0x00DE4E40\n0x0033F6A2\n0x00000000\n0x00000000\n87.62\n"
    rec = instr.parse_instr(out, BASE)
    assert rec == {"op": "peek_instr", "addr": "0x20000154", "observed": "0x00868B54",
                   "commit": "0x00DE4E40", "write_msgs": "0x0033F6A2",
                   "backpressure": "0x00000000", "fifo_overflow": "0x00000000",
                   "board_uptime": "87.62"}


@pytest.mark.parametrize("out", ["0x1\n0x2\n0x3\n0x4\n87.62\n",
                                 "0x1\n0x2\n0x3\n0x4\n0x5\n", ""])
def test_incomplete_line_is_a_probe_error(out):
    from sdk.engine.hardware.probe import ProbeError

    class Base:
        def peek(self, addr):
            raise AssertionError("instrumented peek reached the plain probe")

    p = instr.InstrumentedProbe(Base(), base=BASE, run_line=lambda line: out,
                                record=lambda r: None)
    with pytest.raises(ProbeError):
        p.peek("0x20000154")


def test_other_peeks_and_pokes_pass_through_untouched():
    calls = []

    class Base:
        def peek(self, addr):
            calls.append(("peek", addr))
            return "0x00000007"

        def poke(self, addr, value):
            calls.append(("poke", addr, value))

    p = instr.InstrumentedProbe(Base(), base=BASE, run_line=lambda line: None,
                                record=lambda r: None)
    assert p.peek("0x20000168") == "0x00000007"
    p.poke("0x20000244", "0x00000191")
    assert calls == [("peek", "0x20000168"), ("poke", "0x20000244", "0x00000191")]


# ── the requirement, end to end through routertl's executor ────────────────


class ModelBoard:
    """A DE25 console on a time model: the ring is written at w and drained at
    r (B/s) from a starting fill; every console line costs 0.2 s; registers
    other than the ring pointers read the regbank constants or 0."""

    CONSTANTS = {0x2000000C: 0x00030000, 0x20000028: 0x00020000, 0x20000160: 0x10000000}

    def __init__(self, w: float, r: float, f0: int = 1_000_000):
        self.w, self.r, self.f0, self.t = w, r, f0, 0.0
        self.regs = dict(self.CONSTANTS)

    def _read(self, addr: int) -> int:
        commit, read, bp = _ring(self.w, self.r, self.f0, self.t)
        if addr == BASE + 0x164:
            return commit % RING
        if addr == BASE + 0x154:
            return read % RING
        if addr == BASE + 0x168:
            return bp % (1 << 32)
        return self.regs.get(addr, 0)

    def line(self, line: str) -> str:
        self.t += 0.2
        out = []
        for part in line.split("; "):
            m = re.fullmatch(r"devmem (0x[0-9A-Fa-f]+) 32(?: (0x[0-9A-Fa-f]+))?", part)
            if m and m.group(2) is None:
                out.append("0x%08X" % self._read(int(m.group(1), 16)))
            elif m:
                self.regs[int(m.group(1), 16)] = int(m.group(2), 16)
            else:
                assert part == "cut -d' ' -f1 /proc/uptime", part
                out.append(f"{100.0 + self.t:.2f}")
        return "\n".join(out) + "\n"

    def shell(self, line: str, timeout: float):
        return 0, self.line(line)


def _run(req_id: str, board: ModelBoard, tmp_path: Path, monkeypatch) -> dict:
    import importlib.util

    from sdk.engine.hardware.contract import load_contract
    from sdk.engine.hardware.executor import run_contract

    spec = importlib.util.spec_from_file_location("hw_run_hps_devmem",
                                                  ROOT / "tools" / "hw_run_hps_devmem.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    transcript = tmp_path / "probe_transcript.jsonl"

    def record(entry):
        with transcript.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    class Plain(tool.HpsDevmemProbe):
        def poke(self, addr, value):
            super().poke(addr, value)
            record({"op": "poke", "addr": addr, "value": value})

    class Timed(instr.InstrumentedProbe):
        def sleep(self, s):
            board.t += s
            return s

    pre = {p["name"]: True for p in next(
        r for r in load_contract(CONTRACT)["requirements"] if r["id"] == req_id)["preconditions"]}
    probe = Timed(Plain(board.shell, aperture=tool.csr_aperture(ROOT), preconditions=pre),
                  base=BASE, run_line=board.line, record=record)
    monkeypatch.setenv("DS_PROBE_TRANSCRIPT", str(transcript))
    chain = json.loads(MANIFEST.read_text())
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return run_contract(load_contract(CONTRACT), req_id, probe, mode="replay",
                        project="deepskopion", sof_sha256="0" * 64, git_rev="abc1234",
                        evidence_run_dir=run_dir, command_cwd=ROOT, capture_chain=chain,
                        execution_backend="hps-devmem", hardware_access=True)


def _oracle_step(run: dict) -> dict:
    return next(s for s in run["steps"] if s.get("kind") == "oracle")


def _assertions(run: dict) -> dict:
    return {a["id"]: a["result"] == "pass" for a in run["assertions"]}


def test_requirement_passes_on_a_board_whose_drain_keeps_pace(tmp_path, monkeypatch):
    run = _run(REQ, ModelBoard(w=20.0e6, r=20.0e6), tmp_path, monkeypatch)
    layers = _status(_oracle_step(run)["oracle"])
    assert layers == dict.fromkeys(("instrument", "stimulus", "window", "fill_growth"),
                                   "pass"), _oracle_step(run)
    assert all(_assertions(run).values()), run["assertions"]


def test_requirement_is_red_on_a_board_draining_at_the_run3_rate(tmp_path, monkeypatch):
    """As on silicon with the 4096-frame cap: D1 (backpressure) and D2 (fill) red."""
    run = _run(REQ, ModelBoard(w=20.0e6, r=13_107_200), tmp_path, monkeypatch)
    a = _assertions(run)
    assert a["P1"] and a["P2"] and a["P3"], run["assertions"]
    assert a["D2"] is False and a["D1"] is False, run["assertions"]
    assert run["conclusion"]["req_status"] != "pass", run["conclusion"]


def test_requirement_is_red_on_fill_growth_before_the_ring_fills(tmp_path, monkeypatch):
    """A drain short of the write by 1e6 B/s: the fill grows 62e6 B, the ring
    never fills, D1 stays green and D2 alone is red."""
    run = _run(REQ, ModelBoard(w=20.0e6, r=19.0e6), tmp_path, monkeypatch)
    a = _assertions(run)
    assert a["D1"] is True and a["D2"] is False, run["assertions"]


def test_falsifier_is_red_on_fill_growth_with_the_drain_stopped(tmp_path, monkeypatch):
    run = _run(RED, ModelBoard(w=20.0e6, r=0.0), tmp_path, monkeypatch)
    layers = _status(_oracle_step(run)["oracle"])
    assert layers["fill_growth"] == "fail" and layers["stimulus"] == "pass", layers


# ── the declaration ─────────────────────────────────────────────────────────


def _req(req_id: str) -> dict:
    doc = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return next(r for r in doc["requirements"] if r["id"] == req_id)


def test_requirement_declares_window_instrument_and_gap():
    req = _req(REQ)
    steps = {s["idx"]: s for s in req["steps"]}
    holds = [s for s in req["steps"] if s["kind"] == "rate"]
    s0, s2 = [s["idx"] for s in req["steps"] if s["kind"] == "peek"
              and s["addr"] == "0x20000154"][::2]
    held = sum(s["interval_s"] for s in holds if s0 < s["idx"] < s2)
    assert held >= 60.0, f"window holds only {held} s between S0 and S2"
    assert all(s["read"] == "0x20000168" and s["max_per_s"] == 0 for s in holds)
    oracle_steps = [s for s in steps.values() if s["kind"] == "oracle"]
    assert len(oracle_steps) == 1
    assert "tools/hw/de25_fill_growth_oracle.py" in oracle_steps[0]["cmd"]
    for path in ("tools/hw/de25_probe_instr.py", "tools/hw/de25_fill_growth_oracle.py"):
        assert path in req["description"] or path in json.dumps(req), path
        assert (ROOT / path).is_file(), path
    assert "RTL-P3.2480" in req["description"]
    assert "unmeasured" in req["description"].lower()
    ids = {a["id"]: a["expr"] for a in req["assertions"]}
    assert ids["D2"] == "step[10].oracle.layers[fill_growth].status == pass"
    assert req["falsifier"] == RED and _req(RED)["expected_red_layer"] == "fill_growth"


def test_tput_001_is_unchanged():
    """The new claim leaves DS-DE25-TPUT-001 (steps, floor, window) as it was."""
    req = _req("DS-DE25-TPUT-001")
    rate = next(s for s in req["steps"] if s["kind"] == "rate")
    assert (rate["read"], rate["interval_s"], rate["min_per_s"], rate["max_per_s"]) == \
        ("0x20000154", 2.0, 14400000, 105600000)
    assert [a["expr"] for a in req["assertions"]] == [
        "step[5].satisfied == true", "step[6].observed == step[6].expected",
        "step[7].satisfied == true"]


def test_oracle_runs_as_a_command_from_the_project_root(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("".join(json.dumps(r) + "\n" for r in
                         [{"op": "poke", "addr": STIM[0], "value": STIM[1]}] + _three()))
    proc = subprocess.run([sys.executable, "tools/hw/de25_fill_growth_oracle.py",
                           "--stim-addr", STIM[0], "--stim-value", STIM[1]],
                          cwd=ROOT, capture_output=True, text=True, timeout=60,
                          env={"DS_PROBE_TRANSCRIPT": str(t), "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stderr
    assert _status(json.loads(proc.stdout))["fill_growth"] == "pass"
