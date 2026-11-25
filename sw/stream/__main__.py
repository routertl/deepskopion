# rr unknown-llm (AI-generated)
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""CLI entrypoint: `python -m sw.stream`.

Stand-alone wave_stream_v1 daemon for RouteWave streaming.

Sources:
  --source stub     synthetic saw-tooth + protocol-shaped traffic (DS-P2.30)
  --source regbank  live PS-side regbank telemetry drain (DS-P2.31 Phase A)
                    Requires /dev/mem access (root on PetaLinux). Use
                    --mmio-base to point at a non-default M_AXI_GP0 segment.
  --source ddr      live PS-DDR ring drain of zs_msg_header_t frames
                    (DS-P3.27 Phase B). Reads ring locator (BASE/SIZE)
                    from the regbank, then mmaps PS DDR and parses
                    per-event annotations.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .data_source import (
    DdrRingDataSource, EyeSweepDataSource, RegbankDataSource, StubDataSource,
)
from .ddr_mmap import RealDdrMmap
from .regbank import MmioRegbank, Regs
from .protocol import EVENTS_DEFAULT_PORT
from .server import serve_fanout, serve_one, serve_one_events


def _parse_listen(spec: str) -> tuple[str, int]:
    """Parse 'HOST:PORT' or ':PORT' (= all-interfaces) into (host, port).

    Examples: '0.0.0.0:7475', '127.0.0.1:7475', ':7475', '[::]:7475'.
    """
    if spec.startswith("["):
        # IPv6 literal: [host]:port
        close = spec.rfind("]")
        if close < 0 or close + 1 >= len(spec) or spec[close + 1] != ":":
            raise argparse.ArgumentTypeError(
                f"--listen {spec!r}: malformed IPv6 literal (expected '[host]:port')")
        host = spec[1:close]
        port_str = spec[close + 2:]
    else:
        if ":" not in spec:
            raise argparse.ArgumentTypeError(
                f"--listen {spec!r}: expected 'HOST:PORT' or ':PORT'")
        host, _, port_str = spec.rpartition(":")
        if not host:
            host = "0.0.0.0"
    try:
        port = int(port_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--listen {spec!r}: port {port_str!r} is not an integer")
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError(
            f"--listen {spec!r}: port {port} out of 1..65535")
    return host, port


def _parse_events_listen(spec: str):
    """--events-listen: 'HOST:PORT', 'unix:PATH', or '' to disable the lane."""
    if not spec:
        return ""
    if spec.startswith("unix:"):
        return Path(spec[len("unix:"):])
    return _parse_listen(spec)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m sw.stream",
        description="deepskopion wave_stream_v1 daemon (DS-P2.30 + DS-P2.31).",
    )
    transport = p.add_mutually_exclusive_group()
    transport.add_argument("--socket", type=Path,
                           default=Path("/tmp/ds_wave_stream.sock"),
                           help="Unix-domain socket path "
                                "(default: /tmp/ds_wave_stream.sock).")
    transport.add_argument("--listen", type=_parse_listen, metavar="HOST:PORT",
                           default=None,
                           help="TCP listen endpoint for RouteWave host-side "
                                "ingest (DS-P1.11). Examples: '0.0.0.0:7475', "
                                "':7475' (= 0.0.0.0:7475), '127.0.0.1:7475', "
                                "'[::]:7475'. When given, overrides --socket.")
    p.add_argument("--events-listen", type=_parse_events_listen, default=None,
                   metavar="HOST:PORT",
                   help="DS-P2.50: endpoint for the wave_events_v1 NDJSON lane "
                        "(decoded events for agents; 'nc HOST PORT | jq'). "
                        "'HOST:PORT' or 'unix:PATH'; '' disables it. Default: "
                        f"with --listen, the same host on port {EVENTS_DEFAULT_PORT}; "
                        "with a Unix socket, off, so a local daemon opens no TCP "
                        "port. With --serve-once, naming this serves the one "
                        "consumer here instead of on wave_stream_v1: the ring "
                        "has a single owner.")
    p.add_argument("--source", choices=["stub", "regbank", "ddr", "eye"], default="stub",
                   help="Data source. 'stub' = synthetic (DS-P2.30); "
                        "'regbank' = live regbank telemetry drain "
                        "(DS-P2.31 Phase A, needs /dev/mem); "
                        "'ddr' = live PS-DDR ring drain of zs_msg frames "
                        "(DS-P3.27 Phase B, needs /dev/mem + a populated "
                        "ring buffer).")
    p.add_argument("--connect-timeout", type=float, default=60.0,
                   help="Seconds to wait for a consumer to attach. In the "
                        "default serve-forever mode this only bounds each "
                        "accept() re-arm (the daemon keeps listening); with "
                        "--serve-once it's fatal (default 60).")
    p.add_argument("--serve-once", action="store_true",
                   help="Serve a single consumer then exit (legacy serve_one). "
                        "Default is fan-out: a single ring drainer broadcasts to "
                        "many concurrent consumers, keeps listening across "
                        "disconnects/idle, and drains the ring even with zero "
                        "clients (DS-P3.62 supersedes DS-P3.61 serve-forever).")
    p.add_argument("--max-queue", type=int, default=256,
                   help="DS-P3.62: per-consumer egress queue depth (frames). A "
                        "slow consumer drops its OLDEST frames past this bound "
                        "and is disconnected on the send budget — never stalling "
                        "the drain or the other consumers (default 256).")
    p.add_argument("--backfill-bytes", type=lambda v: int(v, 0), default=8 << 20,
                   help="DS-P3.64: byte budget for the backfill snapshot ring "
                        "(the predictable memory bound — frame size varies wildly "
                        "so frame-count alone isn't enough). 0 disables the byte "
                        "bound (default 8 MiB).")
    p.add_argument("--backfill-frames", type=int, default=512,
                   help="DS-P3.63: frame-count cap for the backfill snapshot ring "
                        "(safety against a flood of tiny frames). Applied together "
                        "with --backfill-bytes, whichever binds first. The drainer "
                        "keeps the ring even with zero clients. 0 disables the "
                        "count bound (default 512).")
    p.add_argument("--backfill-seconds", type=float, default=0.0,
                   help="DS-P3.68: time bound for the backfill snapshot ring — "
                        "keep only frames from the last N seconds. Composes with "
                        "--backfill-bytes/-frames (whichever binds first). 0 "
                        "disables the time bound (default 0). The channel-STATE "
                        "keyframe still gives a joiner each lane's last value.")
    p.add_argument("--duration", type=float, default=0.0,
                   help="Stream for this many seconds then EOF. 0 = stream "
                        "until consumer disconnects (default: 0).")
    p.add_argument("--native-edge-drain", choices=("auto", "on", "off"), default="auto",
                   help="DS-P1.74: walk the ring's EDGE_BATCH frames and pack their "
                        "EDGE_EVENTS frames in C (sw/stream/native, libds_edge_drain.so "
                        "beside edge_frames.py or at $DS_NATIVE_EDGE_LIB). auto uses "
                        "the library when it is there and says which path runs; on "
                        "refuses to start without it; off keeps the Python walk.")
    p.add_argument("--capture-edges", type=Path, default=None, metavar="PATH",
                   help="DS-P2.60: tee every batch's raw EDGE_EVENT words to a "
                        "JSONL capture file (sw/stream/edge_capture.py format) for "
                        "later deterministic replay in a cocotb testbench "
                        "(EdgeReplaySource). Records the real silicon waveform.")
    p.add_argument("--stack", action="append", default=[], metavar="SOURCE:DECODER",
                   help="DS-P2.74: run a stacked PS decoder over an annotation lane, "
                        "e.g. uart_byte:nmea0183. Repeatable, in dependency order; "
                        "a later entry may consume nmea0183@uart_byte.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable INFO-level logging to stderr.")

    g_stub = p.add_argument_group("stub source")
    g_stub.add_argument("--rate", type=int, default=100_000,
                        help="Sample rate in Hz (default: 100000).")
    g_stub.add_argument("--samples-per-batch", type=int, default=1000,
                        help="Sample-batch size (default: 1000).")
    g_stub.add_argument("--annotations", action="store_true", default=True,
                        help="Emit periodic ANNOTATIONS frames (default: on).")
    g_stub.add_argument("--no-annotations", dest="annotations",
                        action="store_false",
                        help="Disable ANNOTATIONS frames.")

    g_rb = p.add_argument_group("regbank source")
    g_rb.add_argument("--mmio-base", type=lambda v: int(v, 0),
                      default=MmioRegbank.DEFAULT_BASE_ADDR,
                      help=f"MMIO base address for the BD's M_AXI_GP0 segment "
                           f"(default: {MmioRegbank.DEFAULT_BASE_ADDR:#x}).")
    g_rb.add_argument("--mmio-size", type=lambda v: int(v, 0),
                      default=MmioRegbank.DEFAULT_WINDOW_SIZE,
                      help=f"MMIO window size (default: "
                           f"{MmioRegbank.DEFAULT_WINDOW_SIZE:#x}).")
    g_rb.add_argument("--poll-period", type=float, default=0.05,
                      help="Regbank polling period in seconds (default: 0.05 → 20 Hz).")
    g_rb.add_argument("--regbank-clk-hz", type=int, default=100_000_000,
                      help="Reported sample_clk_hz for the regbank source "
                           "(default: 100000000 — matches BD fclk_clk0).")
    g_rb.add_argument("--no-snapshot-trigger", dest="snapshot_trigger",
                      action="store_false", default=True,
                      help="Skip pulsing TELEMETRY_SNAPSHOT_TRIGGER each tick "
                           "(counters then drift between reads but no PL write).")

    g_ddr = p.add_argument_group("ddr source (Phase B)")
    g_ddr.add_argument("--ddr-base", type=lambda v: int(v, 0), default=None,
                       help="Override the ring base address (default: read "
                            "from RING_BASE_LOW/HIGH regbank slots).")
    g_ddr.add_argument("--ddr-bytes", type=lambda v: int(v, 0), default=None,
                       help="Override the ring window size (default: read "
                            "from RING_BYTES regbank slot).")
    g_ddr.add_argument("--ring-word-bytes", type=int, default=None,
                       help="The PL's DDR word width in bytes (the top's G_DDR_DATA_WIDTH / 8; the "
                            "ds_ring_word_bytes kernel-cmdline token). Zero-pad flush words up to that "
                            "boundary are consumed as pad, not reported as zs_resync (DS-P2.280).")
    g_ddr.add_argument("--no-write-back-read-ptr", dest="write_back_read_ptr",
                       action="store_false", default=True,
                       help="Don't write the PS-side read pointer back to "
                            "RING_READ_PTR each tick.")
    g_ddr.add_argument("--drain-mode", choices=["auto", "irq", "poll"],
                       default="auto",
                       help="DS-P3.60 ring drain wake: 'irq' waits on the "
                            "ring-watermark UIO interrupt (wakes instantly on a "
                            "75%% crossing, periodic timeout otherwise); 'poll' "
                            "uses the poll-period timer only; 'auto' uses irq if "
                            "a /dev/uioN watermark device is present else poll "
                            "(default: auto).")

    g_eye = p.add_argument_group("eye source (DS-P2.66 Phase 5 — live bathtub)")
    g_eye.add_argument("--eye-phases", type=int, default=32,
                       help="Number of IDELAY sample-phase taps to sweep, 0..N-1 "
                            "(default: 32 — the IDELAYE2 tap range).")
    g_eye.add_argument("--eye-freq-hz", type=int, default=50_000_000,
                       help="TX bit-clock (Hz) for the base freq profile (sel=0), "
                            "carried into the eye frame for the RouteWave freq axis "
                            "(default: 50 MHz, the RMII reference rate).")
    g_eye.add_argument("--eye-bit-target", type=lambda v: int(v, 0),
                       default=1_000_000,
                       help="PRBS bit periods measured per (freq,phase) cell "
                            "(default: 1_000_000).")
    g_eye.add_argument("--eye-poll-timeout", type=float, default=0.5,
                       help="Per-cell EYE_STATUS.done timeout (s). A cell that "
                            "doesn't finish (e.g. no PMOD jumper → never locks) "
                            "is streamed as eye_closed (default: 0.5).")
    g_eye.add_argument("--eye-aggressor", action="store_true", default=False,
                       help="Drive the adjacent-pin crosstalk aggressor during "
                            "the sweep (narrows the eye).")
    g_eye.add_argument("--eye-cell-period", type=float, default=0.0,
                       help="Extra delay (s) between cells to pace a visibly-live "
                            "fill (default: 0 — as fast as the engine measures).")
    g_eye.add_argument("--eye-no-loop", dest="eye_loop", action="store_false",
                       default=True,
                       help="Stream a single sweep then EOF, instead of looping "
                            "(continuous live update is the default).")
    g_eye.add_argument("--eye-max-freq", action="store_true", default=False,
                       help="DS-P3.119 MAX-RATE mode: sweep the full-UI MMCM sample "
                            "phase per frequency, compute the max usable bit rate, and "
                            "emit the authoritative eye_summary verdict (vs the "
                            "per-tap IDELAY bathtub). Feeds RouteWave's link-margin "
                            "view (LT-P3.243).")
    g_eye.add_argument("--eye-ber-threshold", type=float, default=1e-6,
                       help="BER pass threshold for max-freq mode (default: 1e-6).")
    g_eye.add_argument("--eye-margin-ui", type=float, default=0.30,
                       help="Eye-width margin (UI) a frequency must hold to pass in "
                            "max-freq mode (default: 0.30).")
    return p


class _CapturingSource:
    """Transparent DataSource proxy that tees each batch's raw EDGE_EVENT words
    to an EdgeCaptureWriter (DS-P2.60 --capture-edges). Delegates every other
    method to the wrapped source via __getattr__, so server.py is untouched and
    it works for both serve_one and serve_fanout."""

    def __init__(self, inner, writer):
        self._inner = inner
        self._writer = writer

    def next_batch(self):
        batch = self._inner.next_batch()
        if batch is not None and batch.has_edges():
            self._writer.append(batch.all_edge_words())
        return batch

    def __getattr__(self, name):
        return getattr(self._inner, name)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="[ds-stream] %(message)s",
    )

    regbank = None  # set by the regbank/ddr branches; used for the boot banner
    if args.source == "stub":
        source = StubDataSource(
            sample_clk_hz=args.rate,
            samples_per_batch=args.samples_per_batch,
            emit_annotations=args.annotations,
            duration_s=args.duration,
        )
    elif args.source == "regbank":
        try:
            regbank = MmioRegbank(base_addr=args.mmio_base, size=args.mmio_size)
        except OSError as exc:
            print(f"regbank source: could not open /dev/mem: {exc}", file=sys.stderr)
            print("  (root or CAP_SYS_RAWIO is required on PetaLinux)",
                  file=sys.stderr)
            return 1
        source = RegbankDataSource(
            regbank=regbank,
            sample_clk_hz=args.regbank_clk_hz,
            poll_period_s=args.poll_period,
            duration_s=args.duration,
            snapshot_trigger=args.snapshot_trigger,
        )
    elif args.source == "ddr":
        try:
            regbank = MmioRegbank(base_addr=args.mmio_base, size=args.mmio_size)
        except OSError as exc:
            print(f"ddr source: could not open regbank via /dev/mem: {exc}",
                  file=sys.stderr)
            return 1
        # Discover ring locator unless overridden on the CLI.
        ring_base_lo = (args.ddr_base & 0xFFFFFFFF) if args.ddr_base is not None \
                       else regbank.read_u32(Regs.RING_BASE_LOW)
        ring_base_hi = ((args.ddr_base or 0) >> 32) & 0xFFFFFFFF if args.ddr_base is not None \
                       else regbank.read_u32(Regs.RING_BASE_HIGH)
        ring_base = (ring_base_hi << 32) | ring_base_lo
        ring_bytes = args.ddr_bytes if args.ddr_bytes is not None \
                     else regbank.read_u32(Regs.RING_BYTES)
        if ring_base == 0 and ring_bytes == 0:
            print("ddr source: RING_BASE_LOW/HIGH + RING_BYTES are all zero — "
                  "is the bitstream loaded? Pass --ddr-base/--ddr-bytes to "
                  "override.", file=sys.stderr)
            return 1
        try:
            ddr = RealDdrMmap(base_addr=ring_base, size=ring_bytes)
        except OSError as exc:
            print(f"ddr source: could not mmap PS DDR at {ring_base:#x}+{ring_bytes:#x}: "
                  f"{exc}", file=sys.stderr)
            return 1
        # DS-P3.60: optionally drive the drain from the ring-watermark IRQ.
        drain_irq = None
        if args.drain_mode in ("auto", "irq"):
            from .ring_irq import RingIrq
            try:
                drain_irq = RingIrq()
                print(f"ddr source: watermark-IRQ drain via {drain_irq.path}",
                      file=sys.stderr)
            except FileNotFoundError as exc:
                if args.drain_mode == "irq":
                    print(f"ddr source: --drain-mode irq but no watermark UIO "
                          f"device: {exc}", file=sys.stderr)
                    return 1
                print(f"ddr source: no watermark UIO device ({exc}); "
                      "falling back to poll-period drain", file=sys.stderr)
        native_edge_drain = None
        if args.native_edge_drain != "off":
            from .native import edge_frames
            try:
                native_edge_drain = edge_frames.load(required=args.native_edge_drain == "on")
            except OSError as exc:
                print(f"ddr source: --native-edge-drain {args.native_edge_drain}: {exc}",
                      file=sys.stderr)
                return 1
        print("ddr source: edge frames walked and packed "
              + (f"in C ({native_edge_drain.path})" if native_edge_drain is not None
                 else "in Python"), file=sys.stderr)
        source = DdrRingDataSource(
            regbank=regbank,
            ddr=ddr,
            sample_clk_hz=args.regbank_clk_hz,
            poll_period_s=args.poll_period,
            duration_s=args.duration,
            write_back_read_ptr=args.write_back_read_ptr,
            drain_irq=drain_irq,
            native_edge_drain=native_edge_drain,
            ring_word_bytes=args.ring_word_bytes,
        )
    elif args.source == "eye":
        from .eye_sweep import FreqProfile
        try:
            regbank = MmioRegbank(base_addr=args.mmio_base, size=args.mmio_size)
        except OSError as exc:
            print(f"eye source: could not open regbank via /dev/mem: {exc}",
                  file=sys.stderr)
            print("  (root or CAP_SYS_RAWIO is required on PetaLinux)",
                  file=sys.stderr)
            return 1
        source = EyeSweepDataSource(
            regbank=regbank,
            # FREQUENCY axis of the eye. Each (sel, hz) MUST match the clk_drp DRP
            # profile ROM in hw/rtl/eye/clk_drp.vhd (C_CLKREG1): VCO is fixed at
            # 1000 MHz and CLKOUT0_DIVIDE = D gives eye_clk = 1000/D MHz. sel 0-3 are
            # PINNED (the baked-in silicon image); sel 4-9 are the DS-P3.119 dense
            # grid that resolves the max-rate breakpoint finely.
            #   sel0 D=10→100  sel1 D=8→125  sel2 D=6→166.7  sel3 D=5→200
            #   sel4 D=9→111   sel5 D=7→143  sel6 D=12→83.3  sel7 D=16→62.5
            #   sel8 D=20→50   sel9 D=40→25 MHz.
            freqs=[
                FreqProfile(sel=0, hz=100_000_000),
                FreqProfile(sel=1, hz=125_000_000),
                FreqProfile(sel=2, hz=166_666_667),
                FreqProfile(sel=3, hz=200_000_000),
                FreqProfile(sel=4, hz=111_111_111),
                FreqProfile(sel=5, hz=142_857_143),
                FreqProfile(sel=6, hz=83_333_333),
                FreqProfile(sel=7, hz=62_500_000),
                FreqProfile(sel=8, hz=50_000_000),
                FreqProfile(sel=9, hz=25_000_000),
            ],
            phases=list(range(args.eye_phases)),
            aggressor=args.eye_aggressor,
            bit_target=args.eye_bit_target,
            poll_timeout_s=args.eye_poll_timeout,
            sample_clk_hz=args.regbank_clk_hz,
            cell_period_s=args.eye_cell_period,
            loop=args.eye_loop,
            max_freq=args.eye_max_freq,
            ber_threshold=args.eye_ber_threshold,
            eye_margin_ui=args.eye_margin_ui,
        )
    else:  # argparse choices guard makes this unreachable
        print(f"unknown source: {args.source}", file=sys.stderr)
        return 2

    # DS-P3.85: boot-time SW + bitstream identity banner (stale-deploy guard).
    from .protocol import daemon_version
    from .boot_banner import log_boot_banner
    log_boot_banner(lambda m: print(m, file=sys.stderr), daemon_version(), regbank=regbank)

    if args.stack:
        from . import channels
        from .stacked import StackedDataSource, build_chain
        try:
            source = StackedDataSource(source, build_chain(args.stack, channels.CHANNELS))
        except ValueError as exc:
            print(f"[ds-stream] {exc}", file=sys.stderr)
            return 2

    capture_writer = None
    if args.capture_edges is not None:
        from .edge_capture import EdgeCaptureWriter
        clk_mhz = max(1, source.sample_clk_hz() // 1_000_000)
        capture_writer = EdgeCaptureWriter(
            str(args.capture_edges), clk_mhz=clk_mhz,
            meta={"source": args.source, "sample_clk_hz": source.sample_clk_hz()})
        source = _CapturingSource(source, capture_writer)
        print(f"[ds-stream] capturing EDGE_EVENT words → {args.capture_edges} "
              f"(clk={clk_mhz} MHz)", file=sys.stderr)

    endpoint = args.listen if args.listen is not None else args.socket
    try:
        if args.serve_once:
            if args.events_listen:
                return serve_one_events(args.events_listen, source,
                                        connect_timeout_s=args.connect_timeout)
            return serve_one(endpoint, source, connect_timeout_s=args.connect_timeout)
        events_endpoint = args.events_listen or None
        if args.events_listen is None and args.listen is not None:
            events_endpoint = (args.listen[0], EVENTS_DEFAULT_PORT)
        return serve_fanout(endpoint, source, connect_timeout_s=args.connect_timeout,
                        max_queue=args.max_queue,
                        backfill_bytes=args.backfill_bytes,
                        backfill_frames=args.backfill_frames,
                        backfill_seconds=args.backfill_seconds,
                        events_endpoint=events_endpoint)
    finally:
        if capture_writer is not None:
            print(f"[ds-stream] capture closed: {capture_writer.total_words} "
                  f"edge words → {args.capture_edges}", file=sys.stderr)
            capture_writer.close()


if __name__ == "__main__":
    sys.exit(main())
