# rr unknown-llm (AI-generated)
"""Stacked decoders (DS-P2.74): protocols carried over another decoded stream.

A datalogger is a flat PL block: raw pins -> one decoded-event shape. This layer
runs in the PS, consumes the annotations of one lane and emits higher-level
annotations (NMEA over UART, later Modbus over UART, HCI over UART) onto the same
timeline. It lives here and not in the fabric because reassembly costs LUTs for
no latency benefit: nothing triggers on it in hardware.

Timestamps: a PDU keeps ts_start of its FIRST source event and ts_end of its LAST.
Nothing is stamped at reassembly time, since that would destroy the cross-bus
causality the timeline exists to show.

Wire: no new FrameTag. Stacked events are ordinary ANNOTATIONS on their own
declared lanes, under annotation kinds the contract already allows to be
unregistered (COMPAT["unknown_kind"]), so an old consumer renders their text.

Loss: the source stream can drop, and a stacked decoder must say so rather than
join the bytes on either side of a hole. Three cases:

  * A source error (is_error) sits at its place in the stream: the decoder is
    reset and a ``stacked_gap`` is emitted there.
  * A loss marker, ring resync or error_run has no exact place in the stream. The
    loss counters are polled, so a backlogged ring can still hold bytes from
    before AND after the drop when the marker arrives; in-band loss frames are
    coalesced in the PL, so they sit in the ring after data written behind the
    drop; resync and error_run annotations are appended after the frames they
    span. While a ring backlog is still draining (``not batch.ring_drained``),
    source events are held rather than decoded so a PDU completed between a drop
    and its coalesced in-band loss frame cannot be emitted before that frame is
    reached. When a marker arrives, the decoder is reset, a ``stacked_gap`` is
    emitted, and source events (including any held from earlier backlog batches)
    are discarded and counted until the read head has caught up with a commit
    pointer past the drop. ``stacked_resync`` then closes the window.
  * End of stream with a PDU in flight: a ``stacked_gap``, reason end_of_stream.
  * End of stream while events are still held (the last batch left the ring
    undrained, so a coalesced loss frame may still sit unread behind them): no
    held PDU is emitted. One ``stacked_gap``, reason ring_not_drained_at_eof,
    spans the held interval and counts the withheld PDUs and source events.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .data_source import ERROR_RUN_KIND, Annotation, Batch

GAP_KIND = "stacked_gap"
UNDRAINED_EOF_REASON = "ring_not_drained_at_eof"
RESYNC_KIND = "stacked_resync"
FIRST_STACKED_CHANNEL = 400   # above the system lanes and the wave_events edge ids

_ZS_RESYNC_KIND = "zs_resync"
_GAP_COLOR = "#cc8833"
_ERROR_COLOR = "#cc3333"


@dataclass
class Pdu:
    """What a decoder emits. Its time span comes from its source events."""
    kind: str
    text: str
    ts_start_ns: int
    ts_end_ns: int
    fields: Dict[str, object]
    is_error: bool = False
    color: str = "#7a5cc8"


class StackedDecoder:
    """Consumes one lane in order. Subclasses set ``name``."""
    name = ""

    def feed(self, event: Annotation) -> List[Pdu]:
        raise NotImplementedError

    def partial_events(self) -> int:
        """Source events held for a PDU that has not completed."""
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError


class Nmea0183Decoder(StackedDecoder):
    """NMEA 0183 sentences over UART: ``$...[*HH]\\r\\n``. Bytes outside a sentence
    are ignored, so attaching mid-sentence costs only that sentence."""
    name = "nmea0183"
    MAX_SENTENCE = 82      # NMEA 0183 limit, including '$' and CRLF

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._chars: List[int] = []
        self._ts_start = 0

    def partial_events(self) -> int:
        return len(self._chars)

    def _fault(self, reason: str, ts_end_ns: int) -> Pdu:
        pdu = Pdu(f"nmea0183_{reason}", f"! NMEA {reason}", self._ts_start, ts_end_ns,
                  {"reason": reason, "partial_events": len(self._chars)},
                  is_error=True, color=_ERROR_COLOR)
        self.reset()
        return pdu

    def feed(self, event: Annotation) -> List[Pdu]:
        byte = (event.fields or {}).get("byte")
        if byte is None:
            return []
        out: List[Pdu] = []
        if byte in b"$!":
            if self._chars:
                out.append(self._fault("truncated", event.ts_start_ns))
            self._ts_start = event.ts_start_ns
            self._chars = [byte]
        elif self._chars:
            self._chars.append(byte)
            if byte == 0x0A:
                out.append(self._sentence(event.ts_end_ns))
            elif len(self._chars) > self.MAX_SENTENCE:
                out.append(self._fault("overlong", event.ts_end_ns))
        return out

    def _sentence(self, ts_end_ns: int) -> Pdu:
        line = bytes(self._chars).rstrip(b"\r\n").decode("ascii", "replace")
        body, star, given = line[1:].partition("*")
        computed = 0
        for char in body.encode("ascii", "replace"):
            computed ^= char
        checksum_ok: Optional[bool] = None
        if star:
            checksum_ok = given.upper() == f"{computed:02X}"
        if checksum_ok is False:
            return self._fault("checksum_error", ts_end_ns)
        pdu = Pdu("nmea0183_sentence", line, self._ts_start, ts_end_ns,
                  {"sentence": body.split(",")[0], "line": line,
                   "checksum_ok": checksum_ok})
        self.reset()
        return pdu


DECODERS = {Nmea0183Decoder.name: Nmea0183Decoder}


class _Registration:
    def __init__(self, decoder: StackedDecoder, source_channel: int, channel_id: int,
                 layer_depth: int, name: str) -> None:
        self.decoder = decoder
        self.source_channel = source_channel
        self.channel_id = channel_id
        self.layer_depth = layer_depth
        self.name = name
        self.suspect = False
        self.discarded = 0
        self.last_ts_ns = 0
        self._pending: List[Annotation] = []

    def descriptor(self) -> Dict[str, object]:
        return {"id": self.channel_id, "name": self.name, "width": 0, "type": "Signal",
                "role": "Wire", "radix": "Hexadecimal",
                "source_channel": self.source_channel, "layer_depth": self.layer_depth}

    def stamp(self, fields: Dict[str, object]) -> Dict[str, object]:
        return {**fields, "source_channel": self.source_channel,
                "layer_depth": self.layer_depth, "decoder": self.decoder.name}

    def gap(self, reason: str, ts_start_ns: int, ts_end_ns: int) -> Annotation:
        partial = self.decoder.partial_events()
        self.decoder.reset()
        return Annotation(
            channel_id=self.channel_id, ts_start_ns=ts_start_ns, ts_end_ns=ts_end_ns,
            kind=GAP_KIND, text=f"! gap in {self.decoder.name}: {reason}, "
                                f"{partial} partial event(s) dropped",
            color=_GAP_COLOR, is_error=True,
            fields=self.stamp({"reason": reason, "partial_events": partial,
                               "discarded_events": self.discarded}))

    def resync(self) -> Annotation:
        return Annotation(
            channel_id=self.channel_id, ts_start_ns=self.last_ts_ns,
            ts_end_ns=self.last_ts_ns, kind=RESYNC_KIND,
            text=f"! {self.decoder.name} resync: {self.discarded} source event(s) discarded",
            color=_GAP_COLOR, is_error=self.discarded > 0,
            fields=self.stamp({"discarded_events": self.discarded}))

    def annotation(self, pdu: Pdu) -> Annotation:
        return Annotation(
            channel_id=self.channel_id, ts_start_ns=pdu.ts_start_ns,
            ts_end_ns=pdu.ts_end_ns, kind=pdu.kind, text=pdu.text, color=pdu.color,
            is_error=pdu.is_error, fields=self.stamp(pdu.fields))

    def in_band_loss(self, ann: Annotation) -> bool:
        """An in-band coalesced loss frame on this lane (or ring_full)."""
        return (ann.kind == "loss"
                and bool((ann.fields or {}).get("in_band"))
                and (ann.channel_id == self.source_channel
                     or (ann.fields or {}).get("loss_stage") == "ring_full"))

    def unlocated(self, ann: Annotation) -> bool:
        """An out-of-band marker of loss with no exact place in this lane's event order."""
        if ann.kind == _ZS_RESYNC_KIND:
            return True
        if ann.kind == "loss" and not (ann.fields or {}).get("in_band"):
            # A full ring stalls every datalogger, whichever lane the marker is on.
            return (ann.channel_id == self.source_channel
                    or (ann.fields or {}).get("loss_stage") == "ring_full")
        return (ann.kind == ERROR_RUN_KIND and ann.channel_id == self.source_channel
                and bool((ann.fields or {}).get("ongoing")))

    def process(self, events: Sequence[Annotation], ring_drained: bool) -> List[Annotation]:
        out: List[Annotation] = []
        markers = [a for a in events if self.unlocated(a)]
        if markers and not self.suspect:
            self.suspect = True
            self.discarded = 0
            first = markers[0]
            self.last_ts_ns = max(self.last_ts_ns, first.ts_end_ns)
            out.append(self.gap(str((first.fields or {}).get("loss_stage", first.kind)),
                                first.ts_start_ns, first.ts_end_ns))
            for pending in self._pending:
                self.last_ts_ns = max(self.last_ts_ns, pending.ts_end_ns)
                self.discarded += 1
            self._pending.clear()
        for ann in events:
            if self.in_band_loss(ann):
                self.last_ts_ns = max(self.last_ts_ns, ann.ts_end_ns)
                if not self.suspect:
                    self.discarded = 0
                    out.append(self.gap(
                        str((ann.fields or {}).get("loss_stage", ann.kind)),
                        ann.ts_start_ns, ann.ts_end_ns))
                    for pending in self._pending:
                        self.last_ts_ns = max(self.last_ts_ns, pending.ts_end_ns)
                        self.discarded += 1
                    self._pending.clear()
                    out.append(self.resync())
                    self.discarded = 0
                continue
            if ann.channel_id != self.source_channel or ann.kind in (
                    "loss", ERROR_RUN_KIND):
                continue
            if self.suspect:
                self.last_ts_ns = max(self.last_ts_ns, ann.ts_end_ns)
                self.discarded += 1
            else:
                # Hold events in _pending until the read head reaches the commit
                # pointer (ring_drained), so a PDU completed between a drop and a
                # coalesced in-band loss frame further back in the ring is
                # discarded when that loss frame is reached.
                self._pending.append(ann)
        if not self.suspect and (ring_drained or self.layer_depth > 1):
            pending = self._pending
            self._pending = []
            out.extend(self.release(pending))
        # The commit pointer of a batch with an out-of-band marker was read before
        # the counters, so only a LATER batch's pointer is known to lie past the drop.
        if self.suspect and not markers and ring_drained:
            out.append(self.resync())
            self.suspect = False
            self.discarded = 0
        return out

    def release(self, events: Sequence[Annotation]) -> List[Annotation]:
        """Decode events no loss can still reach: every completed PDU, and a gap at
        each source error."""
        out: List[Annotation] = []
        for ann in events:
            self.last_ts_ns = max(self.last_ts_ns, ann.ts_end_ns)
            if ann.is_error:
                out.append(self.gap(f"source_error:{ann.kind}",
                                    ann.ts_start_ns, ann.ts_end_ns))
            else:
                out.extend(self.annotation(p) for p in self.decoder.feed(ann))
        return out

    def withhold(self, held: Sequence[Annotation]) -> Annotation:
        """One gap in place of events still held at an undrained end of stream.

        A coalesced in-band loss frame may sit in the unread ring behind them, so
        none of the PDUs they complete can be trusted. The decoder runs over them
        only to count those PDUs, which are discarded."""
        withheld_pdus = 0
        for ann in held:
            self.last_ts_ns = max(self.last_ts_ns, ann.ts_end_ns)
            if ann.is_error:
                self.decoder.reset()
            else:
                withheld_pdus += len(self.decoder.feed(ann))
        partial = self.decoder.partial_events()
        self.decoder.reset()
        reason = UNDRAINED_EOF_REASON
        return Annotation(
            channel_id=self.channel_id, ts_start_ns=held[0].ts_start_ns,
            ts_end_ns=held[-1].ts_end_ns, kind=GAP_KIND,
            text=f"! gap in {self.decoder.name}: ring not drained at EOF, "
                 f"{withheld_pdus} PDU(s) and {len(held)} source event(s) withheld",
            color=_GAP_COLOR, is_error=True,
            fields=self.stamp({"reason": reason, "withheld_pdus": withheld_pdus,
                               "withheld_events": len(held), "partial_events": partial,
                               "discarded_events": self.discarded}))


class StackedChain:
    """The registered decoders. A decoder declares its source lane by id or name;
    the source must already exist, so registration order is dependency order and
    a cycle cannot be expressed."""

    def __init__(self, base_channels: Sequence[Dict[str, object]]) -> None:
        self._lanes: Dict[object, Tuple[int, int]] = {}   # id or name -> (id, depth)
        for channel in base_channels:
            if not channel.get("width"):    # width>0 lanes carry SAMPLES, not events
                self._lanes[int(channel["id"])] = (int(channel["id"]), 0)
                self._lanes[str(channel["name"])] = (int(channel["id"]), 0)
        self._regs: List[_Registration] = []

    def register(self, decoder: StackedDecoder, source: object) -> _Registration:
        if source not in self._lanes:
            raise ValueError(f"stacked decoder {decoder.name!r}: no annotation lane "
                             f"{source!r} to consume")
        source_channel, depth = self._lanes[source]
        if any(r.source_channel == source_channel and r.decoder.name == decoder.name
               for r in self._regs):
            raise ValueError(f"stacked decoder {decoder.name!r} is already on {source!r}")
        channel_id = FIRST_STACKED_CHANNEL + len(self._regs)
        name = f"{decoder.name}@{source}"
        reg = _Registration(decoder, source_channel, channel_id, depth + 1, name)
        self._regs.append(reg)
        self._lanes[channel_id] = self._lanes[name] = (channel_id, depth + 1)
        return reg

    def channels(self) -> List[Dict[str, object]]:
        return [r.descriptor() for r in self._regs]

    def process(self, batch: Batch) -> Batch:
        # In registration order, so a layer sees its source layer's output (a gap
        # below arrives as is_error) in the same batch.
        events = list(batch.annotations)
        for reg in self._regs:
            events.extend(reg.process(events, batch.ring_drained))
        batch.annotations = events
        return batch

    def finish(self) -> List[Annotation]:
        out: List[Annotation] = []
        for reg in self._regs:
            # A drained batch releases every held event in process(), so events
            # are still held at EOF only when the last batch left the ring
            # undrained. Committed data then remains unread and a coalesced loss
            # frame may be in it, so their PDUs are replaced by one gap: never
            # emitted, never dropped silently.
            held = reg._pending
            reg._pending = []
            if held:
                out.append(reg.withhold(held))
            # What a lower layer emitted at EOF feeds the layers stacked on it.
            if reg.layer_depth > 1:
                out.extend(reg.release(
                    [a for a in out if a.channel_id == reg.source_channel]))
            if reg.decoder.partial_events() or reg.suspect:
                ts = reg.last_ts_ns
                out.append(reg.gap("end_of_stream", ts, ts))
                reg.suspect = False
        return out


class StackedDataSource:
    """DataSource proxy that runs a chain over each batch (same shape as
    __main__._CapturingSource, so the server and both lanes see one stream)."""

    def __init__(self, inner, chain: StackedChain) -> None:
        self._inner = inner
        self._chain = chain
        self._finished = False

    def next_batch(self) -> Optional[Batch]:
        batch = self._inner.next_batch()
        if batch is not None:
            return self._chain.process(batch)
        if self._finished:
            return None
        self._finished = True
        tail = self._chain.finish()
        return Batch(annotations=tail) if tail else None

    def extra_channels(self) -> List[Dict[str, object]]:
        return self._chain.channels()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def build_chain(specs: Sequence[str], base_channels: Sequence[Dict[str, object]]) -> StackedChain:
    """``SOURCE:DECODER`` specs, in order. SOURCE is a lane name or id."""
    chain = StackedChain(base_channels)
    for spec in specs:
        source, sep, decoder = spec.rpartition(":")
        if not sep or decoder not in DECODERS:
            raise ValueError(f"--stack {spec!r}: expected SOURCE:DECODER, decoder one "
                             f"of {', '.join(sorted(DECODERS))}")
        chain.register(DECODERS[decoder](), int(source) if source.isdigit() else source)
    return chain
