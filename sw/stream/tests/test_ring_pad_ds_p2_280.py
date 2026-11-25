# rr unknown-llm (AI-generated)
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.280: the ring daemon consumes the PL's zero-pad flush words as pad, not as lost sync.

rr_ddr_stream ddr_fifo_intf completes a partial DDR word with zero 32-bit messages after 128 idle cycles, so the
word commits. Measured on DE25 (256-bit words, run 12 S2): RING_COMMIT_PTR 0x180, RING_READ_PTR 0x178 on every
poll for 10 s, the 8 B of pad below one header and never consumed; when the next frame arrived, the pad was slid
over 4 B at a time and reported as a zs_resync error.

A pad is a zero run from a position that is not on a DDR-word boundary up to the next boundary. Zero bytes that
start on a boundary, or stop before it, are still resynced and reported.
"""
from __future__ import annotations

import pytest

from sw.stream import data_source, zs_msg
from sw.stream.ddr_mmap import FakeDdrMmap
from sw.stream.native.ring_dump import _CommitRegbank

RING = 1 << 16


def _uart_frame(byte: int) -> bytes:
    hdr = zs_msg.make_header(datalogger_id=12, domain=zs_msg.DOMAIN_UART, event_id=0,
                             severity=zs_msg.SEV_INFO, timestamp=1000 + byte, payload_len=4)
    return zs_msg.pack_header(hdr) + bytes([byte, 0, 0, 0])


FRAME = len(_uart_frame(0))          # 24 B, common_header.md


def _drain(image: bytes, word_bytes):
    """One seeding tick at commit 0, then one tick with the commit at the end of *image*."""
    ddr = FakeDdrMmap(RING)
    ddr.write_bytes(0, image)
    rb = _CommitRegbank()
    src = data_source.DdrRingDataSource(
        regbank=rb, ddr=ddr, poll_period_s=0.0, duration_s=0.0, write_back_read_ptr=True,
        clock=lambda: 0.0, ring_word_bytes=word_bytes)
    src._TICK_BUDGET_S = 1e9
    src.next_batch()
    rb.commit = len(image)
    batch = src.next_batch()
    resync = [a for a in batch.annotations if a.kind == "zs_resync"]
    frames = [a for a in batch.annotations if a.kind != "zs_resync" and a.channel_id != data_source._ZS_RESYNC_CHANNEL]
    return src, rb, batch, resync, frames


def _uart_annotations(frames):
    return [a for a in frames if "uart" in str(a.kind) or a.channel_id == 12]


def test_a_pad_tail_between_two_frames_is_consumed_silently():
    # frame (24 B), two zero messages (8 B) to the 32 B boundary, frame
    image = _uart_frame(0x41) + bytes(8) + _uart_frame(0x42)
    src, rb, batch, resync, frames = _drain(image, 32)
    assert resync == [], [a.text for a in resync]
    assert src._read_byte_ofs == len(image)
    assert src._pad_total == 8
    assert len(frames) >= 2, [(a.kind, a.text) for a in frames]


def test_an_idle_ring_drains_to_the_commit_pointer():
    # the run 12 S2 state: a frame, then a pad shorter than one header, commit on the word boundary
    image = _uart_frame(0x41) + bytes(8)
    src, rb, batch, resync, _ = _drain(image, 32)
    assert src._read_byte_ofs == 32 and batch.ring_drained
    assert rb.read_u32(data_source.Regs.RING_READ_PTR) == 32
    assert resync == []


def test_an_undeclared_width_keeps_the_old_walk_and_reports_the_pad():
    """The behaviour on 2104e288 for the same image: the pad is slid over and reported as zs_resync."""
    image = _uart_frame(0x41) + bytes(8) + _uart_frame(0x42)
    src, _, _, resync, _ = _drain(image, None)
    assert [a.text for a in resync] == ["resync +8B (total=8B)"]
    idle = _drain(_uart_frame(0x41) + bytes(8), None)[0]
    assert idle._read_byte_ofs == FRAME          # 8 B short of the commit, as measured on DE25


def test_zero_bytes_that_stop_before_the_boundary_still_resync():
    # frame, a zero message, then a non-zero word before the 32 B boundary: corruption, not pad
    image = _uart_frame(0x41) + bytes(4) + b"\xde\xad\xbe\xef" + _uart_frame(0x42)
    _, _, _, resync, _ = _drain(image, 32)
    assert [a.text for a in resync] == ["resync +8B (total=8B)"]


def test_a_zero_word_starting_on_a_boundary_still_resyncs():
    # a whole zero DDR word after a word-aligned frame pair: no pad starts on a boundary
    image = _uart_frame(0x41) + _uart_frame(0x42) + bytes(16) + bytes(32) + _uart_frame(0x43)
    _, _, _, resync, _ = _drain(image, 32)
    # 48 B of frames, 16 B of pad to 64, then the 32 zero bytes on the boundary are resynced
    assert [a.text for a in resync] == ["resync +32B (total=32B)"]


@pytest.mark.parametrize("word_bytes, pad", [(16, 0), (32, 16)])
def test_the_pad_follows_the_declared_ratio(word_bytes, pad):
    # two frames end at 48: a 16 B word boundary (ratio 4, no pad), mid-word for 32 B (ratio 8, 16 B pad)
    image = _uart_frame(0x41) + _uart_frame(0x42) + bytes(pad) + _uart_frame(0x43)
    src, _, _, resync, _ = _drain(image, word_bytes)
    assert resync == [] and src._pad_total == pad and src._read_byte_ofs == len(image)


def test_a_pad_declared_for_the_wrong_ratio_is_not_taken_as_pad():
    # ratio 4 declared, but the 16 zero bytes sit on a 16 B boundary: resynced, never silently skipped
    image = _uart_frame(0x41) + _uart_frame(0x42) + bytes(16) + _uart_frame(0x43)
    _, _, _, resync, _ = _drain(image, 16)
    assert [a.text for a in resync] == ["resync +16B (total=16B)"]


@pytest.mark.parametrize("bad", [0, 4, 12, 24, 48])
def test_an_invalid_declared_width_is_refused(bad):
    with pytest.raises(ValueError):
        data_source._check_ring_word_bytes(bad)


# ── the declared width (DS-P2.280 done-when: from declared configuration, not a literal) ──────────────────────

def _repo():
    from pathlib import Path
    return Path(__file__).resolve().parents[3]


def _all_targets():
    """Every target file, products and bench (tools/target_files.py, DS-P1.103)."""
    import sys
    sys.path.insert(0, str(_repo() / "tools"))
    from target_files import all_target_files
    return all_target_files(_repo())


def _top_ddr_width_bits(top: str) -> int:
    """The literal G_DDR_DATA_WIDTH => N the target's top passes (comments ignored); exactly one."""
    import re
    root = _repo()
    files = [p for p in (root / "hw" / "rtl").rglob("*.vhd")
             if re.search(rf"^\s*entity\s+{top}\s+is\b", p.read_text(errors="replace"), re.M | re.I)]
    assert len(files) == 1, (top, files)
    code = "\n".join(l.split("--", 1)[0] for l in files[0].read_text(errors="replace").splitlines())
    vals = re.findall(r"G_DDR_DATA_WIDTH\s*=>\s*(\d+)", code)
    if not vals:  # DS-P2.326: a thin top passes everything to one fabric entity; the literal lives there
        inner = re.findall(r"\bentity\s+work\.(\w+)", code, re.I)
        assert len(inner) == 1, (files[0], inner)
        return _top_ddr_width_bits(inner[0])
    assert len(vals) == 1, (files[0], vals)
    return int(vals[0])


def _ddr_targets():
    import yaml
    out = []
    for t in _all_targets():  # DS-P1.103: products and bench
        c = yaml.safe_load(t.read_text())
        ba = ((c.get("linux") or {}).get("bootargs")) or ""
        if "ds_source=ddr" in ba.split():
            out.append((t.stem, c["project"]["top_module"], ba))
    return out


def test_the_scan_sees_the_ddr_targets():
    names = {n for n, _, _ in _ddr_targets()}
    from target_files import with_bench  # products always; the bench target only where bench/ exists
    assert with_bench({"zybo_datalogger", "de25_datalogger", "pfsoc_datalogger"}, {"canfd_demo"}, _repo()) <= names, names


@pytest.mark.parametrize("name, top, bootargs", _ddr_targets(), ids=[n for n, _, _ in _ddr_targets()])
def test_each_ddr_target_declares_its_tops_word_width(name, top, bootargs):
    toks = [t for t in bootargs.split() if t.startswith("ds_ring_word_bytes=")]
    assert len(toks) == 1, f"{name}: ds_ring_word_bytes must appear once in linux.bootargs ({toks})"
    assert int(toks[0].split("=", 1)[1]) == _top_ddr_width_bits(top) // 8, (name, top, toks)
    data_source._check_ring_word_bytes(int(toks[0].split("=", 1)[1]))


def test_the_init_script_passes_the_token_to_the_daemon():
    s = (_repo() / "sw/linux/rootfs_overlay/etc/init.d/S99deepskopion-stream").read_text()
    assert "s/^ds_ring_word_bytes=" in s and '--ring-word-bytes ${RING_WORD_BYTES}' in s
    launch = [l for l in s.splitlines() if '--source "${SOURCE}"' in l]
    assert len(launch) == 1 and "${RING_ARGS}" in launch[0], launch


def test_the_cli_flag_reaches_the_source():
    import inspect
    from sw.stream import __main__ as cli
    src = inspect.getsource(cli)
    assert '"--ring-word-bytes"' in src and "ring_word_bytes=args.ring_word_bytes" in src


def test_a_pad_cut_by_the_span_end_is_held_for_the_next_tick():
    # the tick's span ends 4 B into the 8 B pad: hold (head stays after the frame), then take it whole
    image = _uart_frame(0x41) + bytes(8) + _uart_frame(0x42)
    ddr = FakeDdrMmap(RING)
    ddr.write_bytes(0, image)
    rb = _CommitRegbank()
    src = data_source.DdrRingDataSource(
        regbank=rb, ddr=ddr, poll_period_s=0.0, duration_s=0.0, clock=lambda: 0.0, ring_word_bytes=32)
    src._TICK_BUDGET_S = 1e9
    src.next_batch()
    rb.commit = len(image)
    src._MAX_SPAN_BYTES_PER_TICK = FRAME + 4
    b1 = src.next_batch()
    assert src._read_byte_ofs == FRAME and src._pad_total == 0
    src._MAX_SPAN_BYTES_PER_TICK = 1 << 20
    b2 = src.next_batch()
    assert src._read_byte_ofs == len(image) and src._pad_total == 8
    assert [a for a in b1.annotations + b2.annotations if a.kind == "zs_resync"] == []
