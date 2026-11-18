"""DS-P3.82: the wave_stream STATS telemetry heartbeat (producer side).

The fan-out daemon broadcasts a periodic STATS frame (tag 11) carrying the
regbank telemetry the RTL already maintains — stream drops, DDR ring fill +
backpressure, the running gateware's rom_info identity, and the daemon's own
version. This spins up serve_fanout against a regbank-backed source (FakeRegbank
seeded with known counters), connects one consumer, and asserts a STATS frame
arrives with the seeded values — independent of any sample data (the source only
ever returns empty batches here).
"""
import socket
import threading
import time
from pathlib import Path

import msgpack

from sw.stream import protocol as proto
from sw.stream.data_source import Batch, read_regbank_stats
from sw.stream.regbank import FakeRegbank, Regs
from sw.stream.server import serve_fanout


class _StatsOnlySource:
    """Minimal DataSource: empty batches (keep the drainer alive without
    emitting data) + a real regbank-backed read_stats()."""

    def __init__(self, regbank):
        self._regbank = regbank

    def sample_clk_hz(self) -> int:
        return 100_000

    def next_batch(self):
        time.sleep(0.02)      # don't busy-spin the drainer
        return Batch()        # empty → only the heartbeat emits frames

    def apply_config(self, *a, **k):
        return None

    def read_stats(self):
        return read_regbank_stats(self._regbank)


def _connect_with_retry(sock_path: Path, attempts: int = 40, delay: float = 0.05):
    for _ in range(attempts):
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock_path))
            return c
        except OSError:
            time.sleep(delay)
    raise RuntimeError(f"could not connect to {sock_path}")


def _read_frame(unp, conn, timeout_s: float = 5.0):
    conn.settimeout(timeout_s)
    while True:
        for obj in unp:
            if isinstance(obj, list) and len(obj) == 2:
                return obj[0], obj[1]
        data = conn.recv(8192)
        if not data:
            raise RuntimeError("connection closed before frame")
        unp.feed(data)


def test_stats_frame_streamed(sock_dir):
    rb = FakeRegbank({
        Regs.GIT_HASH: 0x7D5E9058,
        Regs.BUILD_DATE: 0x00012301,
        Regs.RING_BYTES: 268435456,
        Regs.RING_COMMIT_PTR: 0x300000,
        Regs.RING_READ_PTR: 0x200000,
        Regs.DDR_BACKPRESSURE_CNT: 5,
        Regs.RING_ALARM_CNT: (1 << 31) | 9,        # active alarm + 9 excursions
        Regs.PKT_CNT_I2C: 100, Regs.PKT_CNT_SPI: 200, Regs.PKT_CNT_UART: 300,
        Regs.PKT_CNT_QSPI: 400, Regs.PKT_CNT_EDGE: 500, Regs.PKT_CNT_I2S: 600,
        Regs.ERR_CNT_I2C: 11, Regs.ERR_CNT_SPI: 12, Regs.ERR_CNT_UART: 13,
        Regs.ERR_CNT_QSPI: 14, Regs.ERR_CNT_EDGE: 7,
        # DS-T2.56 exposed CAN/JTAG PKT+ERR; seeded non-zero so a real read is
        # distinguishable from FakeRegbank's unseeded-slot 0.
        Regs.PKT_CNT_CAN: 700, Regs.ERR_CNT_CAN: 17,
        Regs.PKT_CNT_JTAG: 800, Regs.ERR_CNT_JTAG: 18,
        Regs.DROP_CNT_I2C: 1, Regs.DROP_CNT_SPI: 2, Regs.DROP_CNT_UART: 3,
        Regs.FIFO_OVERFLOW_CNT: 21, Regs.FIFO_OVERFLOW_CNT_RSVD: 22,
        Regs.FIFO_HIGH_WATERMARK: 31, Regs.FIFO_HIGH_WATERMARK_RSVD: 32,
        Regs.ARB_GRANT_CNT: 41, Regs.ARB_GRANT_CNT_RSVD: 42,
        Regs.HEARTBEAT_CNT: 51, Regs.CLK_DDR_TICKS: 52, Regs.DDR_RESET_STATUS: 0x6,
    })
    source = _StatsOnlySource(rb)
    # DS-P4.10: sock_dir, not tmp_path: a socket path under a deep TMPDIR
    # plus xdist's popen-gwN passes the 107-byte sun_path limit and bind fails.
    sock_path = sock_dir / "ds.sock"

    def run():
        serve_fanout(sock_path, source, connect_timeout_s=2.0,
                     max_connections=1, stats_period_s=0.05)

    threading.Thread(target=run, daemon=True).start()

    conn = _connect_with_retry(sock_path)
    try:
        unp = msgpack.Unpacker(raw=False)
        tag, _helo = _read_frame(unp, conn)
        assert tag == proto.TAG_HELO
        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"consumer": "test"}))

        stats = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            tag, payload = _read_frame(unp, conn)
            if tag == proto.TAG_STATS:
                stats = payload
                break
        assert stats is not None, "no STATS frame (tag 11) received"

        assert stats["rtl_git_hash"] == "7d5e9058"
        assert stats["rtl_build_date"] == "0x00012301"
        assert stats["ring_bytes"] == 268435456
        assert stats["ring_used_bytes"] == 0x300000 - 0x200000
        assert stats["ddr_backpressure_cnt"] == 5
        assert stats["ring_alarm"] == {"excursions": 9, "active": True}
        # LT-P3.250 / DS-P3.120: self-describing per-protocol map keyed by the
        # canonical ACTIVE datalogger set (alphabetical, single-sourced from
        # datalogger_config.yml via datalogger_registry). No stale i2s/qspi rows. The
        # order is the registry order. DS-T2.56 gave CAN + JTAG real PKT/ERR
        # slots (fanned into telemetry_in in deepskopion_top.vhd), so they carry
        # the seeded values; CAN/JTAG/EDGE still have no DROP slot → null (n/a),
        # never a fabricated 0. See EXPECTED_SLOTS in test_proto_stats_registry.py.
        assert list(stats["proto_stats"].keys()) == ["can", "edge", "i2c", "spi", "uart"]
        assert stats["proto_stats"]["i2c"] == {"pkts": 100, "errs": 11, "drops": 1}
        assert stats["proto_stats"]["spi"] == {"pkts": 200, "errs": 12, "drops": 2}
        assert stats["proto_stats"]["uart"] == {"pkts": 300, "errs": 13, "drops": 3}
        assert stats["proto_stats"]["edge"] == {"pkts": 500, "errs": 7, "drops": None}
        assert stats["proto_stats"]["can"] == {"pkts": 700, "errs": 17, "drops": None}
        # No stale i2s row, and the old flat maps are gone (superseded).
        assert "proto_pkts" not in stats
        assert "proto_errs" not in stats
        assert "proto_drops" not in stats
        # DS-P3.94 ph3: fifo/arb flattened {burst,stream}->scalar (Stream tier
        # dead, D.4). The scalar is the single real counter; the *_RSVD seed (22/
        # 32) is no longer emitted.
        assert stats["fifo_overflows"] == 21
        assert stats["fifo_high_watermark"] == 31
        # DS-T2.33: ARB_GRANT_CNT is RTL-undriven (always 0 on silicon); the data
        # source deliberately reports it as null (n/a) regardless of the slot
        # contents, so the consumer doesn't render a misleading "0 grants = no
        # traffic". The FakeRegbank seeds 41 above precisely to prove the SW no
        # longer passes the raw slot through.
        assert stats["arb_grants"] is None
        assert stats["heartbeat"] == 51
        assert stats["clk_ddr_ticks"] == 52
        assert stats["ddr_reset_status"] == 0x6
        assert "edge_drops" not in stats   # folded into proto_stats.edge.errs (v2)
        assert isinstance(stats["daemon_ver"], str) and stats["daemon_ver"]
    finally:
        conn.close()


def test_stub_source_emits_no_stats(sock_dir):
    """A source without a regbank (read_stats()→None) must emit no STATS — the
    stub daemon is unaffected by the heartbeat."""
    from sw.stream.data_source import StubDataSource

    source = StubDataSource(sample_clk_hz=1000, duration_s=0.3)
    sock_path = sock_dir / "stub.sock"    # DS-P4.10, as above

    def run():
        serve_fanout(sock_path, source, connect_timeout_s=2.0,
                     max_connections=1, stats_period_s=0.02)

    threading.Thread(target=run, daemon=True).start()
    conn = _connect_with_retry(sock_path)
    try:
        unp = msgpack.Unpacker(raw=False)
        tag, _ = _read_frame(unp, conn)
        assert tag == proto.TAG_HELO
        conn.sendall(proto.pack_frame(proto.TAG_HELO_OK, {"consumer": "t"}))
        saw_stats = False
        try:
            for _ in range(50):
                tag, _payload = _read_frame(unp, conn, timeout_s=0.5)
                if tag == proto.TAG_STATS:
                    saw_stats = True
                    break
                if tag == proto.TAG_EOF:
                    break
        except (RuntimeError, socket.timeout):
            pass
        assert not saw_stats, "stub source must not emit STATS (no regbank)"
    finally:
        conn.close()
