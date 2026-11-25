#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
"""Run one DE25 hardware-contract REQ over the HPS Linux ``devmem`` path.

DS-T2.88. Every DE25 REQ in ``runtimes/hw.yml`` declares the capture chain
``hps-devmem+rea-build-id``: the fabric CSRs sit behind the lwh2f bridge and
are read from the HPS with busybox ``devmem`` on the board's Linux console.
``rr hw run --probe`` offers mock / juart / issp / xsdb / vboard, and none of
them reaches that path, so no sanctioned command could drive the contract.
DS-P2.165 worked around it with a one-off session harness
(``tools/tests/data/ds_p2_165_witness/harness/session.py``) that is preserved
evidence, not a tool.

This is the tool. It supplies only the missing backend, a Probe that runs
``devmem`` on the console, and leaves step driving, assertion evaluation,
the conclusion and run-log validation to routertl's own ``run_contract``,
exactly as ``rr hw run`` does for its built-in probes. The routertl-side
``--probe hps-devmem`` backend is a separate routertl ticket; when it lands,
this tool is the reference for it and can retire.

What it refuses, before any devmem is issued:

* a REQ whose capture chain is not an ``hps-devmem`` chain;
* a run without a ``rr hw bit-sha --record-chain`` manifest (the chain
  declares ``bitstream_identity_required: true``);
* a run without a bench lease covering the project's board;
* an address outside the DE25 CSR aperture. The aperture is read from the
  ``ring-watermark@20000000`` node of ``targets/de25_datalogger.yml``, the
  same declaration the DTB carries. ``devmem`` on any other physical address
  reads or writes a different device (DS-T2.86).

A ``devmem`` that fails (non-zero exit, no console answer, unparseable output)
raises ProbeError, so the run aborts instead of recording a write that never
happened.

Preconditions are operator attestations: each ``--precondition NAME`` is
recorded as applied, every other declared precondition as unmet, and routertl
concludes accordingly.

The map-access preamble walks the map the contract names,
``runtimes/de25_regbank_map.yml``: the regbank at the CSR aperture base in
byte mode (DS-T2.86). A map register outside the aperture never reaches
devmem: its read fails the preamble, and routertl marks the run's peek
evidence map-access-unproven.

    python tools/hw_run_hps_devmem.py DS-DE25-CLK-001 \\
        --capture-chain-manifest out/capture_chain.json \\
        --sof-sha256 <sha> --git-rev <rev> \\
        --precondition sof_programmed --precondition hps_bridges_enabled --write

Exit 0 = run.json produced (its verdict is inside it), 1 = refused or the
probe failed, 2 = usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import yaml

ROOT = Path(__file__).resolve().parents[1]
BACKEND = "hps-devmem"
TARGET_YML = Path("targets") / "de25_datalogger.yml"
APERTURE_NODE = "/soc@0/ring-watermark@20000000"

#: A console shell: run one line, return ``(exit_code, output)``, or None when
#: the board did not answer within ``timeout``.
Shell = Callable[[str, float], Optional[tuple]]

_HEX = re.compile(r"^0x[0-9a-fA-F]+$")


def _probe_error(msg: str) -> Exception:
    from sdk.engine.hardware.probe import ProbeError
    return ProbeError(msg)


# ── console framing ────────────────────────────────────────────────────────


def framed_command(line: str, tag: str) -> str:
    """The text typed on the console for one framed shell command."""
    return f"echo B{tag}; {line}; echo E{tag} rc=$?\r"


def parse_framed(text: str, tag: str) -> tuple | None:
    """``(rc, body)`` of one framed command in console text, or None if incomplete.

    The console echoes the typed line, which also contains ``B<tag>`` and
    ``E<tag>``; only markers at the start of a line are the command's output.
    The exit code is the one the shell printed, so a failing ``devmem`` is
    seen as failing.
    """
    text = text.replace("\r", "")
    begin = f"\nB{tag}\n"
    if begin not in text:
        return None
    body = text.split(begin, 1)[1]
    m = re.search(rf"(?:^|\n)E{tag} rc=(\d+)", body)
    if m is None:
        return None
    return int(m.group(1)), body[:m.start()].strip("\n")


class SerialShell:
    """Framed shell commands on the DE25 Linux console (pyserial).

    The only reader of the UART while it is open: two readers steal bytes.
    """

    def __init__(self, dev: str, baud: int = 115200):
        import serial
        self.ser = serial.Serial()
        self.ser.port, self.ser.baudrate, self.ser.timeout = dev, baud, 0.2
        # Haz.107: the DE25 console reads nothing without modem lines. Asserted
        # at open; pyserial skips them on a tty without modem control (a pty).
        self.ser.dtr = True
        self.ser.rts = True
        self.ser.open()
        self.ser.reset_input_buffer()

    def __call__(self, line: str, timeout: float) -> tuple | None:
        tag = uuid.uuid4().hex[:8]
        self.ser.reset_input_buffer()
        for ch in framed_command(line, tag).encode():
            self.ser.write(bytes([ch]))
            time.sleep(0.003)
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            buf += self.ser.read(4096).replace(b"\0", b"")
            got = parse_framed(buf.decode("utf-8", "replace"), tag)
            if got is not None:
                return got
        return None

    def close(self) -> None:
        self.ser.close()


# ── the probe ──────────────────────────────────────────────────────────────


class HpsDevmemProbe:
    """routertl Probe protocol over busybox ``devmem`` on the HPS console."""

    def __init__(self, shell: Shell, *, aperture: tuple[int, int],
                 preconditions: dict[str, bool] | None = None,
                 timeout_s: float = 15.0, poll_s: float = 0.05):
        self.shell = shell
        self.base, self.span = aperture
        self.pre = dict(preconditions or {})
        self.timeout_s = timeout_s
        self.poll_s = poll_s

    def _check(self, op: str, addr: str, *values: str) -> int:
        for v in (addr, *values):
            if not _HEX.match(v):
                raise _probe_error(f"{op}: malformed hex {v!r}")
        a = int(addr, 16)
        if a % 4 or not self.base <= a < self.base + self.span:
            raise _probe_error(
                f"{op} {addr}: outside the DE25 CSR aperture "
                f"0x{self.base:08X}+0x{self.span:X} (or not word aligned); "
                "devmem there reaches a different device")
        return a

    def _devmem(self, op: str, line: str) -> str:
        got = self.shell(line, self.timeout_s)
        if got is None:
            raise _probe_error(f"{op}: no console response to {line!r}")
        rc, out = got
        if rc != 0:
            raise _probe_error(f"{op}: {line!r} exited {rc}: {out.strip()!r}")
        return out

    def precondition_status(self, name: str) -> bool:
        return bool(self.pre.get(name, False))

    def poke(self, addr: str, value: str) -> None:
        a = self._check("poke", addr, value)
        self._devmem("poke", f"devmem 0x{a:08X} 32 0x{int(value, 16):08X}")

    def peek(self, addr: str) -> str:
        a = self._check("peek", addr)
        out = self._devmem("peek", f"devmem 0x{a:08X} 32")
        words = [w for w in out.split() if _HEX.match(w)]
        if len(words) != 1:
            raise _probe_error(f"peek {addr}: unparseable devmem output {out!r}")
        return "0x%08X" % int(words[0], 16)

    def wait_for(self, addr: str, mask: str, expected: str, timeout_s: float):
        m, e = int(mask, 16), int(expected, 16)
        t0 = time.monotonic()
        while True:
            observed = self.peek(addr)
            elapsed = time.monotonic() - t0
            if int(observed, 16) & m == e:
                return observed, True, elapsed
            if elapsed >= timeout_s:
                return observed, False, elapsed
            time.sleep(self.poll_s)


# ── project resolution ─────────────────────────────────────────────────────


def csr_aperture(project_root: Path) -> tuple[int, int]:
    """``(base, span)`` of the DE25 CSR aperture as the target's DT node declares it."""
    target = yaml.safe_load((project_root / TARGET_YML).read_text(encoding="utf-8"))
    for node in _find_nodes(target):
        if node.get("path") == APERTURE_NODE:
            base, span = node["properties"]["reg"]
            return int(base), int(span)
    raise ValueError(f"{TARGET_YML}: no {APERTURE_NODE} node declares the CSR aperture")


def _find_nodes(doc) -> list[dict]:
    """Every ``{path:, properties:}`` mapping anywhere in the target yml."""
    found: list[dict] = []
    stack = [doc]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "path" in item and isinstance(item.get("properties"), dict):
                found.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def console_device(project_root: Path) -> str:
    """The board console from the gitignored deployment descriptor."""
    local = yaml.safe_load((project_root / "runtimes" / "hw.local.yml").read_text())
    for res in (local.get("bench") or {}).get("resources", {}).values():
        if isinstance(res, dict) and res.get("kind") == "uart":
            return res["dev"]
    raise ValueError("runtimes/hw.local.yml declares no uart resource")


def _require_lease(project_root: Path):
    from sdk.cli.lease_guard import require_lease
    return require_lease(action="run hw contract over HPS devmem",
                         project_root=str(project_root))


# ── CLI ────────────────────────────────────────────────────────────────────


class Refused(Exception):
    pass


def _parse(argv) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("req_id")
    ap.add_argument("--project", type=Path, default=ROOT)
    ap.add_argument("--mode", choices=("replay", "explore"), default="replay")
    ap.add_argument("--console", default="",
                    help="board console tty (default: runtimes/hw.local.yml uart)")
    ap.add_argument("--capture-chain-manifest", type=Path, default=None,
                    help="rr hw bit-sha --record-chain output for the programmed image")
    ap.add_argument("--precondition", action="append", default=[], metavar="NAME",
                    help="attest a declared precondition as applied (repeatable)")
    ap.add_argument("--git-rev", default="unknown")
    ap.add_argument("--sof-sha256", default="unknown")
    ap.add_argument("--config-delta", action="append", default=[])
    ap.add_argument("--timeout-s", type=float, default=15.0,
                    help="per-devmem console timeout")
    ap.add_argument("--write", action="store_true",
                    help="persist run.json under <project>/runs/hw/<REQ>/<run_id>/")
    return ap.parse_args(argv)


def run(argv=None, *, open_shell: Callable[[str], Shell] | None = None,
        acquire_lease: Callable[[Path], object] | None = None) -> int:
    a = _parse(argv)
    project_root = a.project.resolve()
    try:
        return _run(a, project_root, open_shell or SerialShell,
                    acquire_lease or _require_lease)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


def _run(a, project_root: Path, open_shell, acquire_lease) -> int:
    from sdk.engine.hardware.contract import load_contract
    from sdk.engine.hardware.executor import _make_run_id, find_requirement, run_contract
    from sdk.engine.hardware.probe import ProbeError
    from sdk.engine.hardware.run_log import save_run_log

    contract = load_contract(project_root / "runtimes" / "hw.yml")
    try:
        req = find_requirement(contract, a.req_id)
    except KeyError as exc:
        raise Refused(str(exc)) from exc
    chain_id = str((req.get("capture_chain") or {}).get("id", ""))
    if BACKEND not in chain_id.split("+"):
        raise Refused(f"{a.req_id} declares capture chain {chain_id!r}, not an "
                      f"{BACKEND} chain; use rr hw run")
    if a.capture_chain_manifest is None:
        raise Refused(f"{a.req_id}: no --capture-chain-manifest; the {chain_id} chain "
                      "requires the image identity rr hw bit-sha --record-chain records")
    try:
        chain = json.loads(a.capture_chain_manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"cannot load capture-chain manifest: {exc}") from exc
    if not isinstance(chain, dict):
        raise Refused("capture-chain manifest must contain an object")
    declared = {p["name"] for p in req.get("preconditions", [])}
    unknown = sorted(set(a.precondition) - declared)
    if unknown:
        raise Refused(f"{a.req_id} declares no precondition(s) {unknown}; "
                      f"declared: {sorted(declared)}")
    base, span = csr_aperture(project_root)

    acquire_lease(project_root)
    shell = open_shell(a.console or console_device(project_root))
    probe = HpsDevmemProbe(shell, aperture=(base, span),
                           preconditions={n: True for n in a.precondition},
                           timeout_s=a.timeout_s)
    run_id = _make_run_id(a.mode)
    run_dir = None
    if a.write:
        run_dir = project_root / "runs" / "hw" / a.req_id / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
    try:
        run_log = run_contract(
            contract, a.req_id, probe, mode=a.mode, project=project_root.name,
            sof_sha256=a.sof_sha256, git_rev=a.git_rev,
            config_delta_from_last_run=list(a.config_delta),
            run_id=run_id, evidence_run_dir=run_dir, command_cwd=project_root,
            capture_chain=chain, execution_backend=BACKEND, hardware_access=True,
        )
    except ProbeError as exc:
        if run_dir is not None and not any(run_dir.iterdir()):
            run_dir.rmdir()
        raise Refused(f"probe failed: {exc}") from exc
    finally:
        close = getattr(shell, "close", None)
        if callable(close):
            close()
    if run_dir is not None:
        out = run_dir / "run.json"
        save_run_log(run_log, out)
        print(f"wrote {out.relative_to(project_root)}")
    else:
        print(json.dumps(run_log, indent=2))
    c = run_log["conclusion"]
    print(f"{a.req_id}: {c['req_status']} ({c['status']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(run())
