# rr unknown-llm (AI-generated)
"""DS-P3.60 companion — PS CPU profiler for the ring drain.

Answers "is another task stealing the cycles the DDR-ring drain needs?" using
only /proc (busybox + python3, no psutil). It samples twice over an interval
and reports, for that window:

  * per-machine CPU busy% and idle headroom (low idle = the drain may starve),
  * the top CPU-consuming processes (the drain's competition),
  * the deepskopion daemon's own CPU% (found by cmdline), and
  * (optional) the ring counters' movement over the SAME window —
    RING_ALARM_CNT (75% watermark crossings) and DDR_BACKPRESSURE_CNT
    (ring-full cycles = loss). The smoking gun: idle≈0 AND backpressure
    climbing AND the top consumer is NOT the daemon → that task is starving
    the drain. The fix is then scheduling (nice/affinity/RT), not more RTL.

CPU% is normalised to one core (the familiar `top` convention): 100% = one
core saturated, ncpu*100% = the whole machine.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional


def _read_aggregate_cpu() -> tuple[int, int]:
    """Return (busy_jiffies, total_jiffies) from /proc/stat aggregate line."""
    fields = Path("/proc/stat").read_text(errors="replace").splitlines()[0].split()[1:]
    vals = [int(x) for x in fields]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
    total = sum(vals)
    return total - idle, total


def _ncpu() -> int:
    n = 0
    for line in Path("/proc/stat").read_text(errors="replace").splitlines():
        if line.startswith("cpu") and line[3:4].isdigit():
            n += 1
    return max(n, 1)


def _proc_ticks() -> dict[int, tuple[str, int]]:
    """pid → (comm, utime+stime ticks) for every live process."""
    out: dict[int, tuple[str, int]] = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            raw = (d / "stat").read_text(errors="replace")
            # comm is field 2 in parens and may contain spaces — split on the
            # last ')' so utime/stime (fields 14/15) line up regardless.
            rparen = raw.rfind(")")
            comm = raw[raw.find("(") + 1:rparen]
            rest = raw[rparen + 2:].split()
            utime, stime = int(rest[11]), int(rest[12])  # 14,15 → 0-based 11,12
            out[int(d.name)] = (comm, utime + stime)
        except (OSError, ValueError, IndexError):
            continue
    return out


def _find_daemon_pid() -> Optional[int]:
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if "sw.stream" in cmd or "wave_stream" in cmd:
            return int(d.name)
    return None


def profile(interval: float, top: int, ring: bool) -> int:
    ncpu = _ncpu()
    rb = regs = None
    if ring:
        try:
            from .regbank import MmioRegbank, Regs
            rb = MmioRegbank().__enter__()
            regs = Regs
            r0_alarm = rb.read_u32(regs.RING_ALARM_CNT)
            r0_bp = rb.read_u32(regs.DDR_BACKPRESSURE_CNT)
        except Exception as e:   # noqa: BLE001 - profiler must never crash the box
            print(f"(ring counters unavailable: {e})")
            rb = None

    b0, t0 = _read_aggregate_cpu()
    p0 = _proc_ticks()
    daemon = _find_daemon_pid()
    time.sleep(interval)
    b1, t1 = _read_aggregate_cpu()
    p1 = _proc_ticks()

    d_total = max(t1 - t0, 1)
    busy_pct = 100.0 * (b1 - b0) / d_total
    idle_pct = 100.0 - busy_pct

    # per-process %CPU of one core
    rows = []
    for pid, (comm, t) in p1.items():
        if pid in p0:
            d = t - p0[pid][1]
            if d > 0:
                rows.append((100.0 * d / d_total * ncpu, pid, comm))
    rows.sort(reverse=True)

    print(f"=== PS CPU profile over {interval:.1f}s ({ncpu} cores) ===")
    print(f"machine busy {busy_pct:5.1f}%   idle headroom {idle_pct:5.1f}%"
          f"   (idle≈0 ⇒ drain may be starved)")
    if daemon is not None:
        drow = next((r for r in rows if r[1] == daemon), None)
        share = f"{drow[0]:.1f}% of one core" if drow else "<0.1%"
        print(f"deepskopion daemon  pid {daemon}: {share}")
    else:
        print("deepskopion daemon: NOT RUNNING (no sw.stream process)")
    print(f"--- top {top} CPU consumers (% of one core) ---")
    for pct, pid, comm in rows[:top]:
        tag = "  <-- DRAIN" if pid == daemon else ""
        print(f"  {pct:6.1f}%  pid {pid:>6}  {comm}{tag}")

    if rb is not None:
        d_alarm = (rb.read_u32(regs.RING_ALARM_CNT) & 0x7FFFFFFF) - (r0_alarm & 0x7FFFFFFF)
        d_bp = rb.read_u32(regs.DDR_BACKPRESSURE_CNT) - r0_bp
        live = (rb.read_u32(regs.RING_ALARM_CNT) >> 31) & 1
        print("--- ring health over the same window ---")
        print(f"  watermark crossings: +{d_alarm}   live_alarm={live}")
        print(f"  backpressure cycles: +{d_bp}   "
              f"{'(LOSS happening!)' if d_bp > 0 else '(no loss)'}")
        if d_bp > 0 and idle_pct < 5.0 and daemon is not None:
            top_pid = rows[0][1] if rows else None
            if top_pid is not None and top_pid != daemon:
                print(f"  VERDICT: CPU saturated by pid {top_pid} ({rows[0][2]}) while the "
                      f"drain loses data → starvation. Fix scheduling, not RTL.")
        rb.__exit__(None, None, None)
    return 0


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="DS-P3.60 PS CPU profiler for the ring drain")
    ap.add_argument("--interval", type=float, default=2.0, help="sample window (s)")
    ap.add_argument("--top", type=int, default=8, help="top-N processes to list")
    ap.add_argument("--ring", action="store_true",
                    help="also sample RING_ALARM_CNT / DDR_BACKPRESSURE_CNT")
    args = ap.parse_args(argv)
    return profile(args.interval, args.top, args.ring)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
