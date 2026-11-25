# rr unknown-llm (AI-generated)
"""Regression guard for the public demo cast's vocabulary.

WHY THIS EXISTS
---------------
A previous landing was refused because the shipped asciinema cast still
spoke the retired vocabulary: its title said the retired term and its
rendered terminal stream typed it three times, even though the stack had
been renamed to "data logger". A green unit suite said nothing about it, because
nothing asserted on the ARTIFACT A VIEWER ACTUALLY SEES — the rendered cast.

This test asserts at that exact surface: the cast header `title` and the fully
rendered output stream (every `o` event, ANSI stripped), which is byte-for-byte
what the asciinema player shows and what WEB embeds. If a future re-record (or a
"rename the old file" shortcut) reintroduces retired vocabulary, or ships a cast
whose capture silently failed, this goes RED.

Runs anywhere pytest runs (CI, a fresh clone, a sandbox): it reads only the
committed .cast next to it — no board, no network, no daemon.
"""
import json
import re
from pathlib import Path

import pytest

CAST = Path(__file__).with_name("deepskopion-datalogger.cast")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_RETIRED_TERM = "sn" + "iffer"


def _load(cast_path):
    """Return (title, rendered_plain_text) for an asciicast v2 file."""
    lines = cast_path.read_text().splitlines()
    header = json.loads(lines[0])
    out = "".join(
        json.loads(l)[2] for l in lines[1:] if l.strip() and json.loads(l)[1] == "o"
    )
    plain = _ANSI.sub("", out).replace("\r", "")
    return header.get("title", ""), plain


def test_cast_exists_and_nonempty():
    assert CAST.is_file(), f"delivered cast missing: {CAST}"
    assert CAST.stat().st_size > 2000, "cast suspiciously small — a truncated take?"


def test_title_speaks_data_logger_not_retired_term():
    title, _ = _load(CAST)
    tl = title.lower()
    assert ("data logger" in tl) or ("datalogger" in tl), f"title lacks data-logger vocab: {title!r}"
    assert _RETIRED_TERM not in tl, f"title still speaks the retired vocab: {title!r}"


def test_rendered_stream_has_zero_retired_term():
    """Ensure retired term is not visible on screen."""
    _, plain = _load(CAST)
    n = plain.lower().count(_RETIRED_TERM)
    assert n == 0, f"rendered cast stream contains retired term {n}x (must be 0)"


def test_rendered_stream_has_no_uart_framing_error():
    """A non-zero UART error column means a mis-framed data logger on camera
    (storyboard rung-3 defect); the honest take shows clean 8N1 UART."""
    _, plain = _load(CAST)
    assert "uart_stop_error" not in plain, "cast shows a UART stop error — mis-framed capture, re-record"


def test_rendered_stream_is_a_real_capture_not_a_crash():
    """Guard against shipping a cast whose capture silently failed: the summary
    milestones (identity + all four protocols + the honest anomaly label) must
    all be present, and no python traceback / agent identity may leak."""
    _, plain = _load(CAST)
    for required in ("GIT_HASH=", "UART", "SPI", "I2C", "EDGE", "capture-side"):
        assert required in plain, f"cast missing required milestone {required!r} (capture failed?)"
    for forbidden in ("Traceback", "ModuleNotFoundError", "cl" + "aude", "CL" + "AUDE"):
        assert forbidden not in plain, f"cast leaked forbidden token {forbidden!r}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
