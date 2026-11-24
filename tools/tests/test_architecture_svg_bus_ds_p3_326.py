# SPDX-License-Identifier: MPL-2.0
"""DS-P3.326: the architecture diagram draws ONE arrow from the datalogger bus into
"Arbiter and framer".

docs/images/architecture.svg drew two separate bus paths into the arbiter, each with
its own arrowhead, so readers saw a double arrow. The five datalogger outputs now join
one vertical trunk, and one arrow leaves the trunk into the arbiter.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SVG = ROOT / "docs" / "images" / "architecture.svg"
NS = "{http://www.w3.org/2000/svg}"
ARBITER = (232, 168, 238)  # arrow tip x, and the y range of the "Arbiter and framer" box (rect y=168 h=70)
ARBITER_RIGHT = 340        # the box's right edge (rect x=236 w=104): a tip anywhere from x=232 to here enters it
ARROW_SEGMENT = (214.0, 203.0, 232.0, 203.0)
TRUNK_X = 214
STUB_YS = (137, 181, 225, 269, 313)
BUS = (174, 130, 232, 320)  # x0, y0, x1, y1 of the bus region between the dataloggers and the arbiter
EXPECTED_BUS = {(174.0, float(y), float(TRUNK_X), float(y)) for y in STUB_YS} | {(214.0, 137.0, 214.0, 313.0)}


def _segments(d: str) -> list[tuple[float, float, float, float]]:
    """The straight segments of a path drawn with M, L, H, V and Z (absolute or
    relative). Any other command raises: the guard never skips what it cannot read."""
    tokens = re.findall(r"[A-Za-z]|-?(?:\d+(?:\.\d*)?|\.\d+)", d)
    segs, x, y, sx, sy, cmd, i = [], 0.0, 0.0, 0.0, 0.0, None, 0
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                segs.append((x, y, sx, sy))
                x, y = sx, sy
                continue
        if cmd is None or cmd not in "MmLlHhVv":
            raise ValueError(f"unsupported path command {cmd!r} in {d!r}")
        rel = cmd.islower()
        if cmd in "MmLl":
            nx, ny = float(tokens[i]), float(tokens[i + 1])
            i += 2
            nx, ny = (x + nx, y + ny) if rel else (nx, ny)
            if cmd in "Mm":
                sx, sy = nx, ny
                cmd = "l" if rel else "L"  # further pairs after M are line-tos
            else:
                segs.append((x, y, nx, ny))
            x, y = nx, ny
        else:
            v = float(tokens[i])
            i += 1
            if cmd in "Hh":
                nx = x + v if rel else v
                segs.append((x, y, nx, y))
                x = nx
            else:
                ny = y + v if rel else v
                segs.append((x, y, x, ny))
                y = ny
    return segs


def _norm(seg):
    x0, y0, x1, y1 = seg
    return (x0, y0, x1, y1) if (x0, y0) <= (x1, y1) else (x1, y1, x0, y0)


def _bus_problems(text: str) -> list[str]:
    root = ET.fromstring(text)
    problems = []
    for tag in ("line", "polyline", "polygon"):
        if any(True for _ in root.iter(f"{NS}{tag}")):
            problems.append(f"the diagram uses <{tag}>, which this check does not read")
    markers = {m.get("id") for m in root.iter(f"{NS}marker")}
    paths = []
    for el in root.iter(f"{NS}path"):
        try:
            paths.append((el, _segments(el.get("d", ""))))
        except (ValueError, IndexError) as exc:
            problems.append(str(exc))
    arrows = []
    for el, segs in paths:
        ref = el.get("marker-end")
        if not ref:
            continue
        m = re.fullmatch(r"url\(#([^)]+)\)", ref.strip())
        if not m or m.group(1) not in markers:
            problems.append(f"marker-end {ref!r} names no <marker>")
        if segs and ARBITER[0] <= segs[-1][2] <= ARBITER_RIGHT and segs[-1][0] < ARBITER[0] \
                and ARBITER[1] <= segs[-1][3] <= ARBITER[2]:
            arrows.append((el.get("d"), segs[-1]))
    if len(arrows) != 1:
        problems.append(f"{len(arrows)} arrowheads enter the arbiter at x={ARBITER[0]}: {[d for d, _ in arrows]}")
    elif arrows[0][1] != ARROW_SEGMENT:
        problems.append(f"the arbiter arrow is not the trunk segment {ARROW_SEGMENT}: {arrows[0][0]}")
    inside = {_norm(s) for _, segs in paths for s in segs
              if all(BUS[0] <= v <= BUS[2] for v in (s[0], s[2])) and all(BUS[1] <= v <= BUS[3] for v in (s[1], s[3]))
              and _norm(s) != _norm(ARROW_SEGMENT)}
    if not any(s[0] == s[2] == TRUNK_X and s[1] <= min(STUB_YS) and s[3] >= max(STUB_YS) for s in inside):
        problems.append(f"no trunk: no single segment at x={TRUNK_X} spans y={min(STUB_YS)}..{max(STUB_YS)}")
    for y in STUB_YS:
        if (174.0, float(y), float(TRUNK_X), float(y)) not in inside:
            problems.append(f"no stub from x=174 to the trunk at y={y}")
    extra = sorted(inside - EXPECTED_BUS)
    if extra:
        problems.append(f"lines in the bus region other than the five stubs and the trunk: {extra}")
    return problems


#: The reviewed diagram. Any change to the file fails this pin until the diagram is
#: rendered (a browser or cairosvg), checked by eye for the bus and the single arrow,
#: and the pin is updated in the same commit. A structural reader alone cannot close
#: every SVG drawing trick (styles, markers, number forms), so the pin is the guard
#: and the structural checks below say what the review looks for.
REVIEWED_SHA256 = "35f3c051181a1a2bdefed0f119ff86b6b8f394bb687d8dff8eb9ab28c4e66a80"
BUS_BLOCK = """\
  <path class="data" d="M174,137 H214"/>
  <path class="data" d="M174,181 H214"/>
  <path class="data" d="M174,225 H214"/>
  <path class="data" d="M174,269 H214"/>
  <path class="data" d="M174,313 H214"/>
  <path class="data" d="M214,137 V313"/>
  <path class="data" d="M214,203 H232" marker-end="url(#ad)"/>
"""


def _guard_problems(text: str) -> list[str]:
    """The guard on a diagram's text: the reviewed-file pin, the verbatim bus block, and
    the structural bus check."""
    import hashlib

    problems = []
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != REVIEWED_SHA256:
        problems.append("architecture.svg changed: render it, check the bus and the single arrow into the "
                        "arbiter, then update REVIEWED_SHA256")
    if text.count(BUS_BLOCK) != 1:
        problems.append("the bus block is not drawn verbatim once")
    return problems + _bus_problems(text)


def test_the_diagram_passes_the_guard():
    assert _guard_problems(SVG.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("old,new", [
    ('<path class="data" d="M214,203 H232" marker-end="url(#ad)"/>',
     '<path class="data" d="M214,203 H232" marker-end="url(#ad)"/>\n  <path class="data" d="M214,214 H236" marker-end="url(#ad)"/>'),
    ('d="M174,269 H214"', 'd="M174,269 H.214"'),
    ('marker-end="url(#ad)"/>\n\n  <rect class="blk" x="362"', 'marker-end="url(#at)"/>\n\n  <rect class="blk" x="362"'),
    ('<path class="data" d="M214,203 H232" marker-end="url(#ad)"/>',
     '<path class="data" d="M214,203 H232" marker-end="url(#ad)" style="marker-end:none"/>'),
    ('d="M174,137 H214"/>', 'd="M174,137 H214" marker-end="url(#ad)"/>'),
])
def test_the_pin_refuses_every_reviewed_evasion(old, new):
    """rv-62a5f5a3a1's mutations, through the same guard the checked-in file passes."""
    text = SVG.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    assert _guard_problems(text.replace(old, new, 1))


def test_an_arrow_past_the_arbiter_edge_is_caught_without_the_pin():
    """rv-9a166bb140: re-pinning a file with a second arrow into the box must still fail."""
    text = SVG.read_text(encoding="utf-8")
    arrow = '<path class="data" d="M214,203 H232" marker-end="url(#ad)"/>'
    bad = text.replace(arrow, arrow + '\n  <path class="data" d="M214,214 H236" marker-end="url(#ad)"/>', 1)
    assert bad != text
    assert any("2 arrowheads" in p for p in _bus_problems(bad)), _bus_problems(bad)


def test_one_arrow_leaves_the_trunk_into_the_arbiter():
    assert _bus_problems(SVG.read_text(encoding="utf-8")) == []


ARROW = '<path class="data" d="M214,203 H232" marker-end="url(#ad)"/>'
TRUNK = '<path class="data" d="M214,137 V313"/>'


@pytest.mark.parametrize("mutate,expected", [
    (lambda t: t.replace(ARROW, ARROW + "\n  " + ARROW.replace("203", "214"), 1), "2 arrowheads"),
    (lambda t: t.replace(ARROW, ARROW + '\n  <path class="data" d="M214,214 L232,214" marker-end="url(#ad)"/>', 1),
     "2 arrowheads"),
    (lambda t: t.replace(ARROW, ARROW.replace(' marker-end="url(#ad)"', ""), 1), "0 arrowheads"),
    (lambda t: t.replace(ARROW, ARROW.replace("url(#ad)", "url(#nope)"), 1), "names no <marker>"),
    (lambda t: t.replace(ARROW, ARROW.replace("M214,203 H232", "M214,230 H232"), 1), "is not the trunk segment"),
    (lambda t: t.replace(TRUNK, '<path class="data" d="M214,137 V225"/>', 1), "no trunk"),
    (lambda t: t.replace('<path class="data" d="M174,269 H214"/>', "", 1), "no stub from x=174 to the trunk at y=269"),
    (lambda t: t.replace(TRUNK, TRUNK + '\n  <path class="data" d="M210,137 V313"/>', 1), "lines in the bus region"),
    (lambda t: t.replace(TRUNK, TRUNK + '\n  <path class="data" d="M210,137 C210,200 210,250 210,313"/>', 1),
     "unsupported path command"),
    (lambda t: t.replace(TRUNK, TRUNK + '\n  <line x1="210" y1="137" x2="210" y2="313"/>', 1), "uses <line>"),
])
def test_the_bus_check_refuses_a_broken_diagram(mutate, expected):
    text = SVG.read_text(encoding="utf-8")
    bad = mutate(text)
    assert bad != text
    assert any(expected in p for p in _bus_problems(bad)), _bus_problems(bad)
