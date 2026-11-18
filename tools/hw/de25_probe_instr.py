"""DE25 ring-pointer instrument: RING_READ_PTR and RING_COMMIT_PTR read in ONE console line.

The instrument of DS-DE25-DRAIN-001 (runtimes/hw.yml). The ring fill is
f = (RING_COMMIT_PTR - RING_READ_PTR) mod 2^28; both pointers move at about
20e6 B/s under the TPUT-001 stimulus, so two separate console peeks about 0.2 s
apart would skew f by about 4e6 B. This wrapper turns every peek of
RING_READ_PTR into one shell line of busybox ``devmem`` reads, in this order:

    RING_READ_PTR, RING_COMMIT_PTR, RING_WRITE_PTR, DDR_BACKPRESSURE_CNT,
    FIFO_OVERFLOW_CNT, then the board uptime (first field of /proc/uptime)

The routertl contract executor receives the RING_READ_PTR value unchanged.
The full sample goes to the probe transcript as one record with op
``peek_instr``; tools/hw/de25_fill_growth_oracle.py reads those records.
Every other peek, poke and wait is passed to the wrapped probe untouched.

Inside one line the reads are sequential ``devmem`` processes, so
RING_COMMIT_PTR is read a few process start-ups after RING_READ_PTR. That
skew is not measured (the line carries one uptime at 0.01 s resolution); it
is stated as an unmeasured term in DS-DE25-DRAIN-001.

The register offsets come from sw/stream/regbank.py ``Regs``; the CSR base is
the caller's (the lwh2f aperture the probe already enforces).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from sw.stream.regbank import Regs

#: Offsets read by one instrumented line, in read order. The first is the
#: register whose peek is instrumented.
INSTR_OFFSETS = (
    ("observed", Regs.RING_READ_PTR),
    ("commit", Regs.RING_COMMIT_PTR),
    ("write_msgs", Regs.RING_WRITE_PTR),
    ("backpressure", Regs.DDR_BACKPRESSURE_CNT),
    ("fifo_overflow", Regs.FIFO_OVERFLOW_CNT),
)

_HEX = re.compile(r"^0x[0-9a-fA-F]+$")
_UPTIME = re.compile(r"^\d+(\.\d+)?$")


class InstrError(Exception):
    """An instrumented line without all five devmem words and an uptime."""


def instr_addrs(base: int) -> list[str]:
    return ["0x%08X" % (base + off) for _, off in INSTR_OFFSETS]


def instr_command(base: int) -> str:
    """The one console line for an instrumented sample."""
    reads = "; ".join(f"devmem {a} 32" for a in instr_addrs(base))
    return f"{reads}; cut -d' ' -f1 /proc/uptime"


def parse_instr(out: str, base: int) -> dict:
    """The ``peek_instr`` record of one line's output; InstrError if incomplete."""
    lines = [ln.strip() for ln in out.replace("\r", "").splitlines() if ln.strip()]
    words = [ln for ln in lines if _HEX.match(ln)]
    if len(words) != len(INSTR_OFFSETS):
        raise InstrError(f"expected {len(INSTR_OFFSETS)} devmem words, got {out!r}")
    uptimes = [ln for ln in lines if _UPTIME.match(ln)]
    if len(uptimes) != 1:
        raise InstrError(f"expected one /proc/uptime field, got {out!r}")
    rec = {"op": "peek_instr", "addr": instr_addrs(base)[0]}
    for (name, _), w in zip(INSTR_OFFSETS, words):
        rec[name] = "0x%08X" % int(w, 16)
    rec["board_uptime"] = uptimes[0]
    return rec


class InstrumentedProbe:
    """Wrap a routertl Probe so a peek of RING_READ_PTR is an instrumented line.

    ``run_line(line)`` runs one shell line on the console and returns its
    output text, or None when the board did not answer. ``record(entry)``
    appends one transcript record.
    """

    def __init__(self, probe, *, base: int,
                 run_line: Callable[[str], Optional[str]],
                 record: Callable[[dict], None]):
        self._probe = probe
        self._base = base
        self._run_line = run_line
        self._record = record
        self._addr = int(instr_addrs(base)[0], 16)

    def peek(self, addr: str) -> str:
        if int(addr, 16) != self._addr:
            return self._probe.peek(addr)
        from sdk.engine.hardware.probe import ProbeError
        out = self._run_line(instr_command(self._base))
        if out is None:
            raise ProbeError(f"peek {addr}: no console response")
        try:
            rec = parse_instr(out, self._base)
        except InstrError as exc:
            raise ProbeError(f"peek {addr}: {exc}") from exc
        self._record(rec)
        return rec["observed"]

    def __getattr__(self, name):
        return getattr(self._probe, name)
