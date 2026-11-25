# rr unknown-llm (AI-generated)
"""DS-P2.311: no edge word is lost when PL zero-pad words sit between EDGE_BATCH frames.

DE25 C3 run 1 (2026-11-15): one edge word reached the wire where the stimulus
drives four, on a daemon that ran without ds_ring_word_bytes, so every PL
zero-pad word was walked as resync. This guard holds the drain side: RTL-shaped
EDGE_BATCH frames (ring_dump.rtl_batches), each followed by PL zero-pad up to the
32-byte DE25 DDR word, go through the production drain (server._drain via
ring_dump.daemon_wire) on the Python walk and on the native C walk, with
ring_word_bytes undeclared and 32. Every case must put every input edge on the
wire, and the same words as the frames give with no pad between them.

Hard-coded oracle: rtl_batches(200, seed=5) holds 200 events (asserted below);
the single-batch case holds 4.
"""
from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import msgpack
import pytest

from sw.stream import data_source
from sw.stream import protocol as proto
from sw.stream.native import edge_frames, ring_dump

NATIVE = Path(__file__).resolve().parents[1] / "native"
DE25_WORD = 32
N_EVENTS = 200


@pytest.fixture(scope="module")
def lib(tmp_path_factory) -> edge_frames.EdgeDrainLib:
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.fail("no C compiler (cc/gcc) on PATH; the native edge path cannot be built")
    so = tmp_path_factory.mktemp("native") / edge_frames.LIB_NAME
    subprocess.run([cc, "-O2", "-Wall", "-Wextra", "-Werror", "-std=c11", "-fPIC", "-shared",
                    "-o", str(so), str(NATIVE / "edge_drain.c"), "-lm"],
                   check=True, capture_output=True, text=True)
    return edge_frames.load(so, required=True)


def _padded(frames, word=DE25_WORD) -> bytes:
    out = bytearray()
    for f in frames:
        out += f
        if len(out) % word:
            out += bytes(word - len(out) % word)
    return bytes(out)


def _batches():
    batches = ring_dump.rtl_batches(N_EVENTS, seed=5)
    assert sum(len(ev) for *_, ev in batches) == N_EVENTS
    return batches


def _wire_words(dump: bytes, ring_word_bytes, native, monkeypatch) -> list:
    orig = data_source.DdrRingDataSource

    class _Declared(orig):
        def __init__(self, *a, **k):
            k.setdefault("ring_word_bytes", ring_word_bytes)
            super().__init__(*a, **k)

    monkeypatch.setattr(data_source, "DdrRingDataSource", _Declared)
    wire = ring_dump.daemon_wire(dump, 100_000_000, native)
    monkeypatch.setattr(data_source, "DdrRingDataSource", orig)
    return [w for tag, p in msgpack.Unpacker(io.BytesIO(wire), raw=False, strict_map_key=False)
            if tag == proto.TAG_EDGE_EVENTS for w in p.get("words", [])]


@pytest.mark.parametrize("walk", ["python", "native"])
@pytest.mark.parametrize("ring_word_bytes", [None, DE25_WORD], ids=["undeclared", "word32"])
def test_every_edge_crosses_the_pads(walk, ring_word_bytes, lib, monkeypatch):
    native = lib if walk == "native" else None
    frames = [ring_dump._rtl_frame(ch, a, ev) for _, ch, a, ev in _batches()]
    reference = _wire_words(b"".join(frames), ring_word_bytes, native, monkeypatch)
    assert len(reference) == N_EVENTS
    words = _wire_words(_padded(frames), ring_word_bytes, native, monkeypatch)
    assert len(words) == N_EVENTS, f"{N_EVENTS - len(words)} edge word(s) lost across the pads"
    assert words == reference


@pytest.mark.parametrize("walk", ["python", "native"])
@pytest.mark.parametrize("ring_word_bytes", [None, DE25_WORD], ids=["undeclared", "word32"])
def test_a_single_four_edge_batch_crosses_its_pad(walk, ring_word_bytes, lib, monkeypatch):
    """The C4 edge leg's shape: one 4-edge batch, then the pad to the word."""
    native = lib if walk == "native" else None
    _, ch, anchor, events = _batches()[0]
    frame = ring_dump._rtl_frame(1, anchor, events[:4])
    words = _wire_words(_padded([frame]), ring_word_bytes, native, monkeypatch)
    assert len(words) == 4, words
