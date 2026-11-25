# rr unknown-llm (AI-generated)
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""DS-WS1 (DS-P2.89) — bind the LIVE daemon output to the vendored
wave_stream_v1 contract, frame by frame, before a frame ever reaches the wire.

WS-C0 (DS-P1.29) proved the producer's STATIC surface — constants, the
channel table, the frame builders — agrees with the contract. It explicitly
did NOT cover a live session: nothing stood between a bad batch (a decoder
bug, a corrupted regbank read, a stale channel reference, a clock that lost
monotonicity) and the wire. A malformed frame reaching a real RouteWave is not
a crash — it renders as a *plausible but wrong* capture, which is exactly the
failure mode WS-C0's README names ("a product that looks like it is working").

``LiveConformanceGuard`` closes that gap by re-using the vendored checker's
own incremental machinery (``_Session`` / ``_check_frame`` — the same code
path ``check_session()`` drives over a whole recorded session) so a live
judgement is provably the SAME judgement the fixtures are held to, not a
hand-rolled approximation of it.

DIRECTION MATTERS (owner ruling, DS-P2.89 revision): the guard is a DETECTOR
on the golden path, not an admission-control gate, and rollback semantics
depend on WHO produced the frame.

- ``direction="p2c"`` (the default) is an OUTGOING frame this producer is
  about to send. It is judged BEFORE it reaches the wire, so "refused" means
  "never happened" — the caller MUST NOT send it, and the would-be state
  mutation is rolled back so a dropped bad batch cannot poison the judgement
  of every batch that follows it (see FINDING 1 in the revision log for why
  this is deliberately fail-closed for HELO too: a malformed channel table
  rolls back to no-HELO, and every later frame is then refused as
  out-of-order — a loud, correct response to OUR OWN defect).
- ``direction="c2p"`` is an INBOUND frame (currently only HELO_OK) the
  consumer already sent over the wire. We cannot un-receive it: rolling back
  ``helo_ok`` on a wrongly-typed OPTIONAL field (e.g. ``consumer`` sent as an
  int, or ``max_rate_hint_hz`` as a float) would leave the guard believing the
  handshake never completed, so ``_require_handshake`` would refuse every
  subsequent SAMPLES/ANNOTATIONS/EDGE_EVENTS batch on that connection and the
  daemon would go dark on a leniency the contract explicitly documents
  ("a bare [1, {}] is a valid acknowledgement"). Inbound frames are therefore
  OBSERVE-ONLY: judged and never rolled back. The returned violation is for
  the caller to LOG (at WARNING, naming the field) — never to gate on.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT_DIR = _REPO_ROOT / "contracts" / "wave_stream_v1"
if str(_CONTRACT_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTRACT_DIR))

import msgpack  # noqa: E402
import wave_stream_contract as _C  # noqa: E402  (path set above)

_TAG_HELO_OK = _C.TAG_BY_NAME["HELO_OK"]
_TAG_EDGE_EVENTS = _C.TAG_BY_NAME["EDGE_EVENTS"]
_NATIVE_EDGE_KEYS = ("seq", "clk_mhz", "anchor_ns", "words")

#: DS-P1.74: one native EDGE_EVENTS frame in this many has every word decoded
#: and checked, starting with the first of the session. The others are checked
#: on their header: tag, the key order, seq, clk_mhz, anchor_ns and the word
#: count, read from the packed bytes against what the producer declared.
NATIVE_FULL_CHECK_EVERY = 1024

#: The most bytes before the first word of a native EDGE_EVENTS frame: array,
#: tag, map, the four keys (4 + 8 + 10 + 6), three uint64 values and the
#: words array header (1 + 1 + 1 + 28 + 27 + 5 = 63).
NATIVE_HEADER_MAX = 64

#: DS-P3.86: how many event_ids the guard remembers. The contract keeps every
#: id of a session so a reused one is caught, which is right for a recorded
#: session and wrong for a daemon that runs for weeks: at UART line rate that
#: is ~10^9 ids a day. The guard keeps the newest EVENT_ID_WINDOW and forgets
#: the oldest. The producer stamps a{seq}_{i} with seq only ever growing, so an
#: id older than the window is never issued again.
EVENT_ID_WINDOW = 4096


class LiveConformanceGuard:
    """Judges one wave_stream_v1 frame at a time against the SAME state
    machine ``check_session()`` runs over a whole recorded session.

    An outgoing (p2c) frame that would violate the contract has its would-be
    state mutation (seq bookkeeping, the running sample-timestamp
    high-watermark, ...) rolled back — a frame that never reaches the wire
    must not be allowed to poison the judgement of the frames that follow it,
    or a single dropped bad batch could cause every subsequent GOOD batch to
    be refused too (e.g. a rolled-back timestamp regression would otherwise
    permanently lower the monotonic floor).

    An inbound (c2p) frame is never rolled back — see the module docstring.
    """

    def __init__(self, native_full_check_every: int = NATIVE_FULL_CHECK_EVERY) -> None:
        self._st = _C._Session()
        self.refused = 0
        # DS-P1.74: native EDGE_EVENTS frames judged word by word, and by header only.
        self.native_full_every = max(1, int(native_full_check_every))
        self.native_full_checked = 0
        self.native_header_checked = 0

    def _snapshot(self):
        """Save just the fields the frame kinds routed through this guard
        (HELO, SAMPLES, ANNOTATIONS, EDGE_EVENTS) can mutate. Cheap on
        purpose: a full ``copy.deepcopy(_Session)`` re-copies the entire HELO
        channel table on EVERY frame even though ``helo``/``channel_ids`` are
        only ever replaced wholesale (never mutated in place) after HELO, so
        saving a reference to them is safe — no aliasing hazard, ~90us/frame
        cheaper than deepcopy at the default 50ms poll period."""
        st = self._st
        # event_ids is only ever APPENDED to while a frame is judged, so its
        # length is enough to roll it back (DS-P3.86); copying it per frame
        # would cost O(window) on every batch.
        return (st.helo, st.channel_ids.copy(), st.seq.copy(),
                st.last_sample_ts, st.annotation_count,
                list(st.edge_words_without_anchor), len(st.event_ids))

    def _restore(self, snapshot) -> None:
        st = self._st
        (st.helo, st.channel_ids, st.seq, st.last_sample_ts,
         st.annotation_count, st.edge_words_without_anchor, n_ids) = snapshot
        # A refused frame's ids never reached the wire: forget them, or the
        # next batch, stamped under the same seq, is refused as a reuse.
        while len(st.event_ids) > n_ids:
            st.event_ids.popitem()

    def _trim_event_ids(self) -> None:
        ids = self._st.event_ids
        while len(ids) > EVENT_ID_WINDOW:
            del ids[next(iter(ids))]   # oldest first (dicts keep insertion order)

    def check(self, tag: int, payload: dict, direction: str = "p2c") -> Optional[str]:
        """Judge one frame. Returns ``None`` if it conforms; otherwise a
        human-readable string naming every violated field.

        For ``direction="p2c"`` (default, OUTGOING) a non-None return is a
        command: the caller MUST NOT put this frame on the wire, and the
        guard's session state has already been rolled back to as if the
        frame was never judged.

        For ``direction="c2p"`` (INBOUND) a non-None return is an
        OBSERVATION only: the frame already arrived and its state mutation
        (e.g. ``helo_ok``) stands regardless — the caller should log the
        violation, not act on it as a refusal."""
        inbound = direction == "c2p"
        out: list = []
        snapshot = None if inbound else self._snapshot()
        _C._check_frame("live", tag, payload, direction, self._st, out)
        if inbound and tag == _TAG_HELO_OK:
            # The vendored `_check_frame` skips a frame's handler entirely
            # when `_check_map` finds ANY violation (including a wrongly-
            # typed OPTIONAL field) — correct for a frame WE control emitting
            # (p2c: don't apply a bad frame's effect), wrong here: the
            # consumer already sent a syntactically-tagged HELO_OK, so the
            # handshake DID happen regardless of whether every field parsed.
            # `_h_helo_ok` itself is an unconditional `helo_ok = True` with
            # no field validation inside it, so applying it directly is
            # exactly what would have run had `_check_frame` let it through.
            self._st.helo_ok = True
        if out:
            violation = "; ".join(str(f) for f in out)
            if inbound:
                return violation
            self._restore(snapshot)
            self.refused += 1
            return violation
        self._trim_event_ids()
        return None

    def check_native_edge_frame(self, frame: bytes, seq: int, clk_mhz: int,
                                anchor_ns: int, n_words: int) -> Optional[str]:
        """Judge one EDGE_EVENTS frame packed by the native edge path (DS-P1.74).

        *frame* is the wire bytes; the other arguments are what the producer
        declares it holds. Returns None if it conforms; otherwise the frame
        must not be sent, as for ``check``.

        The first native frame of the session, and then one in
        ``native_full_every``, is decoded whole and judged by ``check`` with
        every word, after its fields are compared with the declared ones. Each
        other frame is judged on its header read from the bytes: tag, key
        order, seq, clk_mhz, anchor_ns and word count must equal the declared
        values, and the word bytes must number between one and five per word
        (msgpack packs a uint32 in 1 to 5 bytes). The seq,
        handshake, field-type and missing-anchor rules then run as for a
        Python-built frame; only the per-word uint32 check is not run on it.
        ``native_full_checked`` and ``native_header_checked`` count the two."""
        declared = (int(seq), int(clk_mhz), int(anchor_ns), int(n_words))
        # Sampled on the frames accepted so far, so a refused frame's slot
        # goes to the next one and the session's first sent frame is checked whole.
        full = (self.native_full_checked + self.native_header_checked) % self.native_full_every == 0
        if full:
            try:
                obj = msgpack.unpackb(frame, raw=False, strict_map_key=False)
            except (msgpack.UnpackException, ValueError) as exc:
                return self._refuse_native(f"native EDGE_EVENTS does not decode: {exc}")
            if not (isinstance(obj, list) and len(obj) == 2 and obj[0] == _TAG_EDGE_EVENTS
                    and isinstance(obj[1], dict)):
                return self._refuse_native("native frame is not [EDGE_EVENTS, map]")
            payload = obj[1]
            words = payload.get("words")
            found = (payload.get("seq"), payload.get("clk_mhz"), payload.get("anchor_ns"),
                     len(words) if isinstance(words, list) else None)
            if tuple(payload) != _NATIVE_EDGE_KEYS or found != declared:
                return self._refuse_native(
                    f"native EDGE_EVENTS holds keys {tuple(payload)} with "
                    f"(seq, clk_mhz, anchor_ns, n_words)={found}, declared {declared}")
            violation = self.check(_TAG_EDGE_EVENTS, payload)
            if violation is None:
                self.native_full_checked += 1
            return violation
        mismatch = _native_header_mismatch(frame, declared)
        if mismatch is not None:
            return self._refuse_native(mismatch)
        violation = self.check(_TAG_EDGE_EVENTS, {"seq": declared[0], "clk_mhz": declared[1],
                                                  "anchor_ns": declared[2]})
        if violation is None:
            if declared[3] and not declared[2]:
                # what _h_edge_events records for words without an anchor
                self._st.edge_words_without_anchor.append("live[EDGE_EVENTS]")
            self.native_header_checked += 1
        return violation

    def _refuse_native(self, why: str) -> str:
        self.refused += 1
        return why


def _native_header_mismatch(frame: bytes, declared) -> Optional[str]:
    """None when *frame* starts [EDGE_EVENTS, {seq, clk_mhz, anchor_ns, words}]
    with the declared values and holds bytes for that many words, else why not."""
    unp = msgpack.Unpacker(raw=False)
    # The header ends within NATIVE_HEADER_MAX bytes; feeding the whole frame
    # would copy every word byte into the unpacker for a check that reads none.
    unp.feed(memoryview(frame)[:NATIVE_HEADER_MAX])
    try:
        if unp.read_array_header() != 2 or unp.unpack() != _TAG_EDGE_EVENTS:
            return "native frame is not [EDGE_EVENTS, payload]"
        if unp.read_map_header() != len(_NATIVE_EDGE_KEYS):
            return "native EDGE_EVENTS payload does not hold four keys"
        found = []
        for key in _NATIVE_EDGE_KEYS[:3]:
            if unp.unpack() != key:
                return f"native EDGE_EVENTS key order differs at {key!r}"
            found.append(unp.unpack())
        if unp.unpack() != "words":
            return "native EDGE_EVENTS key order differs at 'words'"
        found.append(unp.read_array_header())
    except (msgpack.UnpackException, msgpack.OutOfData, ValueError) as exc:
        return f"native EDGE_EVENTS header does not decode: {exc}"
    if tuple(found) != declared:
        return (f"native EDGE_EVENTS header (seq, clk_mhz, anchor_ns, n_words)="
                f"{tuple(found)}, declared {declared}")
    rest = len(frame) - unp.tell()
    n = declared[3]
    if not n <= rest <= 5 * n:
        return f"native EDGE_EVENTS carries {rest} word bytes for {n} words"
    return None
