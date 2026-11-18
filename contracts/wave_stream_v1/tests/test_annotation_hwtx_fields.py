"""Contract 2.5.0 — the two HWTX fields of an annotation: event_id and text_res.

The HWTX transcript reads a decoded annotation as a T1 event. A T1v verdict
points back at the event it judges by ``event_id``, so the id must name ONE
event for the whole session. ``text_res`` carries the label at two
resolutions. These are the checker's rules for both; the fixtures that pin
them (``reject_event_id_names_two_events``, ``reject_text_res_*``) are judged
by ``test_wave_stream_contract.py`` with every other fixture.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTRACT_DIR = HERE.parent
sys.path.insert(0, str(CONTRACT_DIR))

import wave_stream_contract as C  # noqa: E402  (path set above)

GOLDEN = C.load_fixture(CONTRACT_DIR / "fixtures" / "session_golden.json")


def _annotated_session(*annotation_frames):
    """The golden handshake (HELO, HELO_OK), then one ANNOTATIONS frame per
    list given, seq 0, 1, ... in order."""
    head = [(f["dir"], f["tag"], f["payload"]) for f in GOLDEN["frames"][:2]]
    assert [tag for _, tag, _ in head] == [0, 1]
    return head + [("p2c", 6, {"seq": seq, "annotations": anns})
                   for seq, anns in enumerate(annotation_frames)]


def _uart(ts, byte, **extra):
    ann = {"ts_start": ts, "ts_end": ts + 10, "channel_id": 100,
           "kind": "uart_byte", "text": f"0x{byte:02X}",
           "payload": {"byte": byte}}
    ann.update(extra)
    return ann


def test_an_event_id_names_one_event_for_the_whole_session():
    """The id is what an HWTX verdict points at. The same event may come again
    under its id (a late joiner's keyframe resends it), but a second event
    under that id makes the verdict's reference ambiguous."""
    resent = C.check_session(_annotated_session(
        [_uart(10, 0x55, event_id="a0_0")],
        [_uart(10, 0x55, event_id="a0_0"), _uart(40, 0x41, event_id="a1_1")]))
    assert resent.accepted, str(resent)

    reused = C.check_session(_annotated_session(
        [_uart(10, 0x55, event_id="a0_0")],
        [_uart(40, 0x41, event_id="a0_0")]))
    assert reused.fields == ["frame[3][ANNOTATIONS].annotations[0].event_id"], \
        str(reused)

    # Within one frame too, and on any of the four identifying fields.
    for moved in ({"ts_start": 11}, {"ts_end": 21}, {"channel_id": 101},
                  {"kind": "uart_config", "payload": {"config": 1}}):
        twin = _uart(10, 0x55, event_id="e")
        twin.update(moved)
        same_frame = C.check_session(_annotated_session(
            [_uart(10, 0x55, event_id="e"), twin]))
        assert same_frame.fields == \
            ["frame[2][ANNOTATIONS].annotations[1].event_id"], (moved, str(same_frame))

    empty = C.check_session(_annotated_session([_uart(10, 0x55, event_id="")]))
    assert empty.fields == ["frame[2][ANNOTATIONS].annotations[0].event_id"], \
        str(empty)


def test_text_res_carries_both_resolutions_and_a_short_inline_label():
    def judge(text_res):
        return C.check_session(_annotated_session(
            [_uart(10, 0x55, text_res=text_res)]))

    path = "frame[2][ANNOTATIONS].annotations[0].text_res"
    assert judge({"long": "uart_byte: 0x55 'U'", "short": "0x55 'U'"}).accepted
    assert judge({"long": "x" * 200, "short": "s" * 32}).accepted   # the bound itself

    assert judge({"long": "x" * 200, "short": "s" * 33}).fields == [f"{path}.short"]
    assert judge({"short": "0x55 'U'"}).fields == [f"{path}.long"]
    assert judge({"long": "uart_byte: 0x55 'U'"}).fields == [f"{path}.short"]
    assert judge({"long": "", "short": "0x55"}).fields == [f"{path}.long"]
    assert judge({"long": "uart_byte: 0x55", "short": ""}).fields == [f"{path}.short"]
    assert judge({"long": "uart_byte: 0x55", "short": 85}).fields == [f"{path}.short"]
    assert judge("0x55 'U'").fields == [path]   # a bare string is not the map
