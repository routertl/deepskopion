# SPDX-License-Identifier: MPL-2.0
"""DS-P1.74: generated DDR ring dumps and the Python oracle for the native drain.

No captured DE25 ring dump is committed (the DS-T1.23 session files were not
kept), so the desk proof uses a generated one. ``edge_dump`` writes
EDGE_BATCH frames through the production packer, zs_msg.pack_edge_batch, at
the 160 bytes per frame the DS-T1.23 witness measured (655360 B / 4096
frames): a 20-byte header and 35 entries. ``mixed_dump`` adds the cases the
walk must agree on: a bad-magic word, a malformed EDGE_BATCH and, at the end,
a frame of another domain.

``oracle_records`` is the reference. It runs the production
DdrRingDataSource.next_batch over the dump in a FakeDdrMmap and records the
arguments of every Batch.add_edge call, little-endian <IQ> (word, ts_ns): the
same record edge_drain_bench --out writes.
"""
from __future__ import annotations

import argparse
import random
import socket
import struct
import sys
import threading
import time
from pathlib import Path

from sw.stream import data_source, server, zs_msg
from sw.stream import protocol as proto
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.regbank import FakeRegbank, Regs

ENTRIES_PER_FRAME = 35  # 20 + 35 * 4 = 160 bytes, the DS-T1.23 ring advance per frame
RECORD = struct.Struct("<IQ")


def _edge_frame(rng: random.Random, channel: int, tick: int, entries: int = ENTRIES_PER_FRAME,
                max_delta: int = 1800) -> tuple[bytes, int]:
    events = []
    t = tick
    for _ in range(entries):
        t += rng.randint(1, max_delta)  # entries * max_delta keeps ts_delta inside u16
        flags = rng.choice((0, 0, 0, 1, 1, 1, 3, 4, 5))
        events.append(zs_msg.EdgeEvent(ts_64b=t, channel=channel, s=bool(flags & 4),
                                       f=bool(flags & 2), e=bool(flags & 1)))
    frame = zs_msg.pack_edge_batch(datalogger_id=channel, channel=channel,
                                   events=events, anchor_ts=tick)
    return frame, t


def edge_dump(n_frames: int, seed: int = 1, channels: int = 6,
              start_tick: int = (1 << 48) - 5_000_000, entries: int = ENTRIES_PER_FRAME,
              max_delta: int = 1800) -> bytes:
    """EDGE_BATCH frames only, channels interleaved; the tick crosses the 48-bit wrap.

    Stress fixture: each channel has its own tick, so the channels drift apart
    in time (hundreds of ms over a long dump). Not a model of the RTL batcher;
    do not use for cost figures (DS-P1.89: use ``rtl_edge_dump``)."""
    rng = random.Random(seed)
    ticks = [start_tick + 97 * c for c in range(channels)]
    out = bytearray()
    for i in range(n_frames):
        c = i % channels
        frame, ticks[c] = _edge_frame(rng, c, ticks[c], entries, max_delta)
        assert len(frame) == zs_msg.HEADER_SIZE + 4 * entries, len(frame)
        out += frame
    return bytes(out)


EVT_EDGE_RLE = 0x09  # zs_msg_pkg.vhd C_ZS_EVT_EDGE_RLE
RLE_INSTANCE = 7


def _rle_frame(word: int, tick: int) -> bytes:
    hdr = zs_msg.make_header(datalogger_id=RLE_INSTANCE, domain=zs_msg.DOMAIN_EDGE,
                             event_id=EVT_EDGE_RLE, severity=zs_msg.SEV_INFO,
                             timestamp=tick, payload_len=4)
    return zs_msg.pack_header(hdr) + word.to_bytes(4, "little")


def _uart_frame(tick: int, byte: int) -> bytes:
    hdr = zs_msg.make_header(datalogger_id=0, domain=zs_msg.DOMAIN_UART, event_id=0,
                             severity=zs_msg.SEV_INFO, timestamp=tick, payload_len=4)
    return zs_msg.pack_header(hdr) + bytes((byte, 0, 0, 0))


def interleaved_dump(n_frames: int = 9000, seed: int = 3) -> bytes:
    """More frames than one tick takes (4096), starting with a UART frame: runs
    of EDGE_BATCH frames broken by Stream-RLE words of instance 7 (edges the
    Python walk adds to the same batch) and by UART frames (annotations).

    Stress fixture: the six channels have independent ticks and drift up to
    about 425 ms apart, which cuts the wire into many small EDGE_EVENTS frames.
    Not a model of the RTL batcher; do not use for cost figures (DS-P1.89: use
    ``rtl_interleaved_dump``)."""
    rng = random.Random(seed)
    tick = 1 << 40
    ticks = [tick + 97 * c for c in range(6)]
    out = bytearray(_uart_frame(tick, 0x41))
    rle_tick = tick
    out += _rle_frame(0x01 << 24 | (rle_tick & 0xFFFFFF), rle_tick)  # SYNC_HI
    for i in range(n_frames):
        r = rng.random()
        if r < 0.08:
            rle_tick += rng.randint(3000, 9000)
            d = [rng.randint(1, 255) for _ in range(3)]
            out += _rle_frame(0x05 << 24 | d[2] << 16 | d[1] << 8 | d[0], rle_tick)  # PACK
        elif r < 0.10:
            out += _uart_frame(max(ticks), rng.randint(0x20, 0x7E))
        else:
            c = i % 6
            frame, ticks[c] = _edge_frame(rng, c, ticks[c])
            out += frame
    return bytes(out)


#: DS-P1.89: the edge pipeline as instantiated in deepskopion_core.vhd
#: (u_edge_agg: G_N_MAX => C_PAYLOAD_WORDS_MAX = 32, G_T_FLUSH_CYCLES => 10_000,
#: G_CHANNEL_IDS = the edge datalogger ids) and the flush rules of
#: edge_batcher.vhd: delta overflow (next event more than 0xFFFF ticks after
#: the batch anchor), capacity (G_N_MAX entries), timer (G_T_FLUSH_CYCLES idle
#: cycles). One batcher per channel, all on one tick.
RTL_CHANNELS = (1, 2, 3, 6)
RTL_N_MAX = 32
RTL_SPAN_MAX = 0xFFFF
RTL_T_FLUSH = 10_000
#: How often each channel emits, relative to the others: two busy channels
#: (batches end at capacity), one medium (events about 4800 ticks apart, so
#: batches end on the 0xFFFF span) and one sparse (batches end on the timer).
RTL_WEIGHTS = (1.0, 1.0, 0.1, 0.02)


def rtl_batches(n_events: int, seed: int = 1, start_tick: int = 1 << 40,
                channels=RTL_CHANNELS, weights=RTL_WEIGHTS, max_gap: int = 450):
    """The EDGE_BATCH frames the RTL edge pipeline emits for *n_events* edges.

    One tick for all channels: each event is a random 1..*max_gap* ticks after
    the previous one, on a channel drawn by *weights*. Each channel's batch
    flushes as edge_batcher.vhd flushes it: before an event more than
    RTL_SPAN_MAX ticks after the batch anchor (that event opens the next
    batch), at RTL_N_MAX entries, or RTL_T_FLUSH ticks after its last event.

    Returns (flush_tick, channel, anchor, events) in flush order, the order
    the aggregator hands batches to the framer."""
    rng = random.Random(seed)
    t = start_tick
    open_batches: dict = {}  # channel -> [anchor, events, last_tick]
    out = []

    def flush(ch: int, at: int) -> None:
        anchor, events, _ = open_batches.pop(ch)
        out.append((at, len(out), ch, anchor, events))

    for _ in range(n_events):
        t += rng.randint(1, max_gap)
        for ch in sorted(open_batches):
            if t - open_batches[ch][2] >= RTL_T_FLUSH:
                flush(ch, open_batches[ch][2] + RTL_T_FLUSH)
        c = rng.choices(channels, weights)[0]
        if c in open_batches and t - open_batches[c][0] > RTL_SPAN_MAX:
            flush(c, t)
        flags = rng.choice((0, 0, 0, 1, 1, 1, 3, 4, 5))
        ev = zs_msg.EdgeEvent(ts_64b=t, channel=c, s=bool(flags & 4),
                              f=bool(flags & 2), e=bool(flags & 1))
        batch = open_batches.setdefault(c, [t, [], t])
        batch[1].append(ev)
        batch[2] = t
        if len(batch[1]) == RTL_N_MAX:
            flush(c, t)
    for ch in sorted(open_batches):
        flush(ch, open_batches[ch][2] + RTL_T_FLUSH)
    out.sort(key=lambda r: (r[0], r[1]))
    return [(at, ch, anchor, events) for at, _, ch, anchor, events in out]


def _rtl_frame(ch: int, anchor: int, events) -> bytes:
    return zs_msg.pack_edge_batch(datalogger_id=ch, channel=ch, events=events, anchor_ts=anchor)


def rtl_edge_dump(n_events: int = 143360, seed: int = 1, **kw) -> bytes:
    """EDGE_BATCH frames only, as the RTL edge pipeline emits them (rtl_batches).
    The dump for cost figures (DS-P1.89)."""
    return b"".join(_rtl_frame(ch, anchor, ev) for _, ch, anchor, ev in rtl_batches(n_events, seed, **kw))


RLE_PACK_WORD, RLE_LONG_WORD, RLE_IDLE_WORD = 0x05, 0x06, 0x07  # edge_datalogger.vhd C_RLE_*
RLE_MAX = (1 << 24) - 1


def _rle_gap_words(gap: int, tick: int) -> bytes:
    """Stream-RLE words that move instance 7's time base forward by *gap* ticks
    and put one edge at the end, as edge_datalogger.vhd encodes a gap: IDLE
    words for each full 2^24-1 ticks without an edge, then the edge as a PACK
    delta (1..255) or a LONG delta (256..2^24-1)."""
    out = bytearray()
    while gap > RLE_MAX:
        out += _rle_frame(RLE_IDLE_WORD << 24 | RLE_MAX, tick)
        gap -= RLE_MAX
    if gap <= 255:
        out += _rle_frame(RLE_PACK_WORD << 24 | max(gap, 1), tick)
    else:
        out += _rle_frame(RLE_LONG_WORD << 24 | gap, tick)
    return bytes(out)


def rtl_interleaved_dump(n_events: int = 285_000, seed: int = 3, **kw) -> bytes:
    """rtl_batches broken, as interleaved_dump breaks its runs, by Stream-RLE
    words of instance 7 (before 8 % of the batches) and UART frames (2 %), on
    the same tick. The RLE stream starts with a SYNC_HI; before a batch it
    carries the edge datalogger's words for the time since its last edge (one
    edge after the gap, then a PACK of three short deltas), so the decoded
    edge times track the tick, as edge_datalogger.vhd's do (edge time = SYNC +
    sum of the deltas). Starts with a UART frame. The dump for cost figures
    (DS-P1.89)."""
    rng = random.Random(seed + 1000)
    batches = rtl_batches(n_events, seed, **kw)
    first = batches[0][2]
    out = bytearray(_uart_frame(first, 0x41))
    out += _rle_frame(0x01 << 24 | (first & 0xFFFFFF), first)  # SYNC_HI
    rle_tick = first
    for at, ch, anchor, events in batches:
        r = rng.random()
        room = at - rle_tick  # the RLE word follows its edges: none after `at`
        if r < 0.08 and room >= 3:
            d = [rng.randint(1, min(255, room // 3)) for _ in range(3)]
            gap = room - sum(d)
            if gap > 0:
                out += _rle_gap_words(gap, at)
                rle_tick += gap
            out += _rle_frame(RLE_PACK_WORD << 24 | d[2] << 16 | d[1] << 8 | d[0], at)
            rle_tick += sum(d)
        elif r < 0.10:
            out += _uart_frame(at, rng.randint(0x20, 0x7E))
        out += _rtl_frame(ch, anchor, events)
    return bytes(out)


def write_rtl_dumps(directory: Path) -> list:
    """Write rtl_edge.bin and rtl_interleaved.bin into *directory* (the dumps
    for cost figures) and return their paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return [write(directory / "rtl_edge.bin", rtl_edge_dump()),
            write(directory / "rtl_interleaved.bin", rtl_interleaved_dump())]


def mixed_dump(seed: int = 2) -> bytes:
    """Edge frames plus a bad-magic word, a malformed batch, and a trailing UART frame."""
    rng = random.Random(seed)
    out = bytearray(edge_dump(40, seed=seed))
    out += b"\xde\xad\xbe\xef"  # bad magic: both walks slide 4 bytes
    good, _ = _edge_frame(rng, 2, 10_000)
    bad = bytearray(good)
    bad[20 + 2] ^= 0x01  # entry 0 channel != datalogger_id: a malformed batch
    out += bad
    out += edge_dump(20, seed=seed + 1)
    hdr = zs_msg.make_header(datalogger_id=0, domain=zs_msg.DOMAIN_UART, event_id=0,
                             severity=zs_msg.SEV_INFO, timestamp=0, payload_len=4)
    out += zs_msg.pack_header(hdr) + b"\x00\x00\x00\x00"
    return bytes(out)


class _CommitRegbank(FakeRegbank):
    def __init__(self) -> None:
        super().__init__()
        self.commit = 0

    def read_u32(self, offset: int) -> int:
        if offset == Regs.RING_COMMIT_PTR:
            return self.commit
        return super().read_u32(offset)


def oracle_records(dump: bytes, sample_clk_hz: int = 100_000_000) -> bytes:
    """Every (word, ts_ns) the production drain hands Batch.add_edge for *dump*."""
    ring = len(dump) + 4096
    ddr = FakeDdrMmap(ring)
    ddr.write_bytes(0, dump)
    regbank = _CommitRegbank()
    src = data_source.DdrRingDataSource(
        regbank=regbank, ddr=ddr, sample_clk_hz=sample_clk_hz, poll_period_s=0.0,
        duration_s=0.0, write_back_read_ptr=True, clock=lambda: 0.0)
    src._TICK_BUDGET_S = 1e9  # the reference decodes everything; time is not its job
    src.next_batch()  # seeds the read pointer at commit 0
    regbank.commit = len(dump)
    seen: list[tuple[int, int]] = []
    original = data_source.Batch.add_edge

    def record(self, word, ts_ns):
        seen.append((int(word), int(ts_ns)))
        return original(self, word, ts_ns)

    data_source.Batch.add_edge = record
    try:
        for _ in range(1 + len(dump) // 4096 * 4):
            before = src._read_byte_ofs
            src.next_batch()
            if src._read_byte_ofs == before:
                break
    finally:
        data_source.Batch.add_edge = original
    return b"".join(RECORD.pack(w, t) for w, t in seen)


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def daemon_source(dump: bytes, sample_clk_hz: int, native=None):
    """A DdrRingDataSource over *dump*, seeded, ending the stream once drained."""
    ddr = FakeDdrMmap(len(dump) + 4096)
    ddr.write_bytes(0, dump)
    regbank = _CommitRegbank()
    src = data_source.DdrRingDataSource(
        regbank=regbank, ddr=ddr, sample_clk_hz=sample_clk_hz, poll_period_s=0.0,
        duration_s=0.0, write_back_read_ptr=True, clock=lambda: 0.0,
        native_edge_drain=native)
    src._TICK_BUDGET_S = 1e9  # both walks decode every frame; time is not compared
    src.next_batch()          # seeds the read pointer at commit 0
    regbank.commit = len(dump)
    return _UntilDrained(src)


class _UntilDrained:
    def __init__(self, src):
        self._src = src

    def next_batch(self):
        before = self._src._read_byte_ofs
        batch = self._src.next_batch()
        return None if self._src._read_byte_ofs == before else batch

    def __getattr__(self, name):
        return getattr(self._src, name)


def daemon_wire(dump: bytes, sample_clk_hz: int, native=None) -> bytes:
    """Every byte server._drain sends a consumer over a socketpair for *dump*."""
    a, b = socket.socketpair()
    out = bytearray()

    def consumer():
        a.sendall(proto.pack_frame(proto.TAG_HELO_OK, {}))
        while True:
            data = a.recv(1 << 16)
            if not data:
                return
            out.extend(data)

    t = threading.Thread(target=consumer)
    t.start()
    try:
        if server._drain(b, daemon_source(dump, sample_clk_hz, native)) != 0:
            raise RuntimeError("server._drain did not end cleanly")
    finally:
        b.close()
        t.join(timeout=60)
        a.close()
    return bytes(out)


def stage_times(dump: bytes, sample_clk_hz: int, native=None, repeat: int = 5) -> dict:
    """Best seconds of the daemon's edge path, one cumulative stage at a time
    (DS-P1.89). Each stage repeats the one before and adds a step, so the
    difference between two rows is that step's cost:

      walk   DdrRingDataSource.next_batch over the dump (the native walk and
             frame cut, or the Python walk)
      pack   + each wire frame packed (ds_frames_pack, or proto.pack_frame)
      guard  + each frame judged as server._edge_frame_bytes judges it, after
             the server's own HELO handshake primed the guard
      full   the whole daemon stream of daemon_wire: server._drain, socket and
             consumer thread
    """
    from sw.stream.wave_stream_guard import LiveConformanceGuard
    clk_mhz = max(1, sample_clk_hz // 1_000_000)

    def run(step: str) -> None:
        src = daemon_source(dump, sample_clk_hz, native)
        guard = None
        if step == "guard":
            a, b = socket.socketpair()
            try:
                a.sendall(proto.pack_frame(proto.TAG_HELO_OK, {}))
                guard = LiveConformanceGuard()
                if server._handshake(b, src, guard=guard, timeout_s=5.0) is None:
                    raise RuntimeError("handshake failed")
            finally:
                a.close()
                b.close()
        seq = 0
        while True:
            batch = src.next_batch()
            if batch is None:
                return
            if step == "walk":
                continue
            for frame in batch.edge_wire_frames():
                if step == "pack":
                    if frame.native:
                        frame.pack(seq, clk_mhz)
                    else:
                        proto.pack_frame(proto.TAG_EDGE_EVENTS, proto.make_edge_events(
                            seq, frame.words(), clk_mhz=clk_mhz, anchor_ns=frame.anchor_ns))
                else:
                    violation, _ = server._edge_frame_bytes(guard, frame, seq, clk_mhz)
                    if violation is not None:
                        raise RuntimeError(f"guard refused a frame: {violation}")
                seq += 1

    best = {}
    for step in ("walk", "pack", "guard", "full"):
        times = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            if step == "full":
                daemon_wire(dump, sample_clk_hz, native)
            else:
                run(step)
            times.append(time.perf_counter() - t0)
        best[step] = min(times)
    return best


def guard_split(dump: bytes, sample_clk_hz: int, native=None, repeat: int = 5) -> dict:
    """The guard stage of stage_times split by frame (DS-P1.89): the session's
    first frame, which the guard checks word by word, and frames 1..N, which
    it checks by header (one in NATIVE_FULL_CHECK_EVERY whole). Each part is
    the time server._edge_frame_bytes takes on those frames (pack + guard),
    after the server's own HELO handshake. A short dump is mostly session
    start; a long session is steady state.

    Returns the best repeat by steady-state time: seconds and words of each
    part, the frame count, and the whole run's seconds (walk included)."""
    from sw.stream.wave_stream_guard import LiveConformanceGuard
    clk_mhz = max(1, sample_clk_hz // 1_000_000)
    best = None
    for _ in range(repeat):
        t_run = time.perf_counter()
        src = daemon_source(dump, sample_clk_hz, native)
        a, b = socket.socketpair()
        try:
            a.sendall(proto.pack_frame(proto.TAG_HELO_OK, {}))
            guard = LiveConformanceGuard()
            if server._handshake(b, src, guard=guard, timeout_s=5.0) is None:
                raise RuntimeError("handshake failed")
        finally:
            a.close()
            b.close()
        part = {"start_s": 0.0, "start_words": 0, "steady_s": 0.0, "steady_words": 0}
        seq = 0
        while True:
            batch = src.next_batch()
            if batch is None:
                break
            for frame in batch.edge_wire_frames():
                words = frame.n_words if frame.native else len(frame.words())
                t0 = time.perf_counter()
                violation, _ = server._edge_frame_bytes(guard, frame, seq, clk_mhz)
                dt = time.perf_counter() - t0
                if violation is not None:
                    raise RuntimeError(f"guard refused a frame: {violation}")
                key = "start" if seq == 0 else "steady"
                part[f"{key}_s"] += dt
                part[f"{key}_words"] += words
                seq += 1
        part["frames"] = seq
        part["run_s"] = time.perf_counter() - t_run
        if best is None or part["steady_s"] < best["steady_s"]:
            best = part
    return best


def _bench(argv=None) -> int:
    """ns per edge of the daemon (source walk, guard, packing, socket send)."""
    p = argparse.ArgumentParser(prog="python -m sw.stream.native.ring_dump")
    p.add_argument("dump", type=Path)
    p.add_argument("sample_clk_hz", type=int)
    p.add_argument("--native", type=Path, default=None, help="libds_edge_drain.so; Python walk without")
    p.add_argument("--repeat", type=int, default=5)
    p.add_argument("--out", type=Path, default=None, help="write the wire bytes of the first run")
    p.add_argument("--stages", action="store_true",
                   help="also time walk / +pack / +guard / full and print ns per edge "
                        "for each, and the step differences (DS-P1.89)")
    args = p.parse_args(argv)
    from sw.stream.native import edge_frames
    native = edge_frames.load(args.native, required=True) if args.native else None
    dump = args.dump.read_bytes()
    edges = 0
    times = []
    for r in range(args.repeat):
        t0 = time.perf_counter()
        wire = daemon_wire(dump, args.sample_clk_hz, native)
        times.append(time.perf_counter() - t0)
        if r == 0:
            import msgpack
            edges = sum(len(pl["words"]) for tag, pl in msgpack.Unpacker(
                __import__("io").BytesIO(wire), raw=False) if tag == proto.TAG_EDGE_EVENTS)
            if args.out:
                args.out.write_bytes(wire)
    best, mean = min(times), sum(times) / len(times)
    print(f"path={'native' if native else 'python'} bytes={len(dump)} edges={edges} "
          f"wire_bytes={len(wire)} repeat={args.repeat} "
          f"ns_per_edge_best={best / edges * 1e9:.1f} ns_per_edge_mean={mean / edges * 1e9:.1f} "
          f"MB_per_s_best={len(dump) / best / 1e6:.1f}")
    if args.stages:
        st = stage_times(dump, args.sample_clk_hz, native, args.repeat)
        ns = {k: v / edges * 1e9 for k, v in st.items()}
        print("stages ns_per_edge_best: " + " ".join(f"{k}={v:.1f}" for k, v in ns.items()))
        print(f"steps ns_per_edge: walk={ns['walk']:.1f} pack={ns['pack'] - ns['walk']:.1f} "
              f"guard={ns['guard'] - ns['pack']:.1f} server_socket={ns['full'] - ns['guard']:.1f}")
        sp = guard_split(dump, args.sample_clk_hz, native, args.repeat)
        steady = (sp["steady_s"] / sp["steady_words"] * 1e9) if sp["steady_words"] else 0.0
        print(f"guard split (pack + guard): session_start frame 0 words={sp['start_words']} "
              f"ns_per_dump_edge={sp['start_s'] / edges * 1e9:.1f} | steady_state "
              f"frames={sp['frames'] - 1} words={sp['steady_words']} ns_per_edge={steady:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(_bench())
