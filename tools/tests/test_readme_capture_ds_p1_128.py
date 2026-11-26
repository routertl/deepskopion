# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""The repository ships a capture a newcomer opens without building anything (DS-P1.128)."""

from __future__ import annotations

import gzip
import re
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
CAPTURE_REL = "docs/captures/i2c_write_transaction.fst"
CAPTURE = ROOT / CAPTURE_REL
SCOPE = b"tb_i2c_datalogger"
MAX_BYTES = 64 * 1024
HOST_PATH = re.compile(rb"/(?:home|tmp|srv|opt|root|Users)/[\x21-\x7e]{2,}")


FST_HEADER, FST_HIERARCHY_GZIP, FST_WRAPPER_GZIP = 0, 4, 254


def _blocks(data: bytes) -> list[tuple[int, bytes]]:
    """(type, payload) of each FST block; a section length counts its own 8 bytes."""
    blocks, i = [], 0
    while i + 9 <= len(data):
        kind, length = data[i], struct.unpack(">Q", data[i + 1:i + 9])[0]
        if length < 8 or i + 1 + length > len(data):
            raise ValueError(f"block {kind} at {i} runs past the file")
        blocks.append((kind, data[i + 9:i + 1 + length]))
        i += 1 + length
    return blocks


def _readable_blocks(data: bytes) -> list[bytes]:
    """The FST's text-bearing parts: the file, the unwrapped file, and the decompressed hierarchy (scope and signal
    names, which a host path would sit in)."""
    readable = [data]
    blocks = _blocks(data)
    if blocks and blocks[0][0] == FST_WRAPPER_GZIP:
        data = gzip.decompress(blocks[0][1][8:])  # 8-byte uncompressed length, then a gzip stream
        readable.append(data)
        blocks = _blocks(data)
    for kind, payload in blocks:
        if kind == FST_HIERARCHY_GZIP:
            readable.append(gzip.decompress(payload[8:]))
    return readable


def _capture_problems(data: bytes) -> list[str]:
    problems = []
    if not data or data[0] not in (FST_HEADER, FST_WRAPPER_GZIP):
        return ["not an FST file (an FST starts with a header block or a gzip wrapper block)"]
    if len(data) > MAX_BYTES:
        problems.append(f"{len(data)} bytes, over {MAX_BYTES}")
    try:
        blocks = _readable_blocks(data)
    except (ValueError, OSError, EOFError) as exc:
        return problems + [f"unreadable FST: {exc}"]
    if len(blocks) < 2 or not any(SCOPE in block for block in blocks[1:]):
        problems.append(f"no scope named {SCOPE.decode()}")
    paths = sorted({m for block in blocks for m in HOST_PATH.findall(block)})
    if paths:
        problems.append(f"host paths: {paths[:3]}")
    return problems


#: The README blocks a newcomer runs, byte for byte: the RouteWave download block and the capture block, per
#: platform. These are the texts executed on Linux and Windows (DS-P2.403); hypothetical edit shapes beyond them are
#: out of scope (Mission Control ruling at the T1 cut-off rv-0278716768).
PINNED_BLOCKS = {
    ('RouteWave', 'bash'): (
        'cd ~\n'
        'VER=2.7.0\n'
        'base=https://github.com/djmazure/routewave/releases/download/v${VER}\n'
        'for f in routewave-${VER}-linux-x86_64.tar.gz SHA256SUMS.txt SHA256SUMS.txt.asc PUBLIC_KEY.asc; do curl -fsSLO ${base}/$f; done\n'
        'sha256sum --check --ignore-missing SHA256SUMS.txt\n'
        'gpg --import PUBLIC_KEY.asc && gpg --verify SHA256SUMS.txt.asc SHA256SUMS.txt\n'
        'tar xzf routewave-${VER}-linux-x86_64.tar.gz && ./routewave-${VER}-linux-x86_64/routewave --version\n'
    ),
    ('RouteWave', 'powershell'): (
        'Set-Location "$HOME\\Downloads"\n'
        '$VER = "2.7.0"\n'
        '$base = "https://github.com/djmazure/routewave/releases/download/v$VER"\n'
        'curl.exe -fsSLO "$base/routewave-$VER-windows-x86_64.zip"\n'
        'curl.exe -fsSLO "$base/SHA256SUMS.txt"\n'
        '(Get-FileHash "routewave-$VER-windows-x86_64.zip").Hash\n'
        'Select-String -Path SHA256SUMS.txt -Pattern "windows-x86_64.zip"\n'
        'Expand-Archive "routewave-$VER-windows-x86_64.zip" -DestinationPath .\n'
    ),
    ('capture', 'bash'): (
        'RW=~/routewave-2.7.0-linux-x86_64/routewave\n'
        '"$RW" -c "open docs/captures/i2c_write_transaction.fst; wait 3000; show --tree tb_i2c_datalogger; zoom 190.852us 190.878us"\n'
    ),
    ('capture', 'powershell'): (
        '$RW = "$HOME\\Downloads\\routewave-2.7.0-windows-x86_64\\routewave.exe"\n'
        '& $RW -c "open docs/captures/i2c_write_transaction.fst; wait 3000; show --tree tb_i2c_datalogger; zoom 190.852us 190.878us"\n'
    ),
}


def _executed_blocks(text: str) -> dict[tuple[str, str], str | None]:
    sections = {
        "RouteWave": text.split("### RouteWave\n", 1)[1].split("\n### ", 1)[0] if "### RouteWave\n" in text else "",
        "capture": text.split("### View a capture without building anything", 1)[-1].split("\n### ", 1)[0],
    }
    found = {}
    for section, lang in PINNED_BLOCKS:
        blocks = re.findall(rf"```{lang}\n(.*?)```", sections[section], re.S)
        found[(section, lang)] = blocks[0] if blocks else None
    return found


def _snapshot_problems(text: str) -> list[str]:
    found = _executed_blocks(text)
    return [
        f"the {lang} block of the {section} section differs from the text run on both platforms: re-run the README "
        "blocks on Linux and Windows before updating PINNED_BLOCKS"
        for (section, lang), pinned in PINNED_BLOCKS.items() if found[(section, lang)] != pinned
    ]


def _readme_problems(text: str) -> list[str]:
    problems = []
    section = text.split("### View a capture without building anything", 1)
    if len(section) < 2:
        return ["the README has no 'View a capture without building anything' section"]
    body = section[1].split("\n### ", 1)[0]
    command = f'-c "open {CAPTURE_REL}; wait 3000; show --tree {SCOPE.decode()}; zoom 190.852us 190.878us"'
    if f'"$RW" {command}' not in body:
        problems.append("the section does not give the command that opens the shipped capture")
    if f"& $RW {command}" not in body:
        problems.append("the section does not give the Windows command that opens the shipped capture")
    problems += _platform_problems(text, body, command)
    if "](docs/images/capture_i2c_write_transaction.png)" not in body:
        problems.append("the section has no screenshot of the result")
    return problems


def _fenced(text: str, lang: str) -> list[list[str]]:
    return [block.splitlines() for block in re.findall(rf"```{lang}\n(.*?)```", text, re.S)]


def _platform_problems(text: str, body: str, command: str) -> list[str]:
    """Per platform: the capture block assigns RW to the program the RouteWave download block unpacks, and launches it
    on an uncommented line (T1 rv-7bd1acf1a8)."""
    problems = []
    rw_section = text.split("### RouteWave\n", 1)[1].split("\n### ", 1)[0] if "### RouteWave\n" in text else ""
    platforms = (
        ("bash", r"cd (\S+)", r"VER=(\S+)", "{dir}/routewave-{ver}-linux-x86_64/routewave",
         r"RW=(\S+)", f'"$RW" {command}'),
        ("powershell", r'Set-Location "(.+)"', r'\$VER = "(.+)"', "{dir}\\routewave-{ver}-windows-x86_64\\routewave.exe",
         r'\$RW = "(.+)"', f"& $RW {command}"),
    )
    for lang, cd_re, ver_re, layout, rw_re, launch in platforms:
        download = next((b for b in _fenced(rw_section, lang) if any(re.fullmatch(ver_re, ln) for ln in b)), [])
        folder = next((m.group(1) for ln in download[:1] for m in [re.fullmatch(cd_re, ln)] if m), None)
        ver = next((m.group(1) for ln in download for m in [re.fullmatch(ver_re, ln)] if m), None)
        capture = [ln for b in _fenced(body, lang) for ln in b]
        rw = next((m.group(1) for ln in capture for m in [re.fullmatch(rw_re, ln)] if m), None)
        if folder is None or ver is None:
            problems.append(f"the {lang} RouteWave download block does not start in a named folder")
        elif rw != layout.format(dir=folder, ver=ver):
            problems.append(f"the {lang} capture block's RW is not the program its download block unpacks: {rw}")
        if launch not in capture:
            problems.append(f"the {lang} capture block does not launch RW on an uncommented line")
    return problems


def _fst(hierarchy: bytes) -> bytes:
    """A minimal FST: a header block and a gzip hierarchy block holding ``hierarchy``."""
    header = bytes([FST_HEADER]) + struct.pack(">Q", 8 + 4) + b"head"
    packed = gzip.compress(hierarchy)
    return header + bytes([FST_HIERARCHY_GZIP]) + struct.pack(">QQ", 16 + len(packed), len(hierarchy)) + packed


def test_the_capture_is_shipped_small_clean_and_carries_the_scope():
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", CAPTURE_REL], cwd=ROOT,
                             capture_output=True, text=True, check=False)
    assert tracked.returncode == 0, f"{CAPTURE_REL} is not tracked: {tracked.stderr}"
    assert _capture_problems(CAPTURE.read_bytes()) == []


def test_the_readme_opens_the_shipped_capture():
    assert _readme_problems(README.read_text(encoding="utf-8")) == []
    assert (ROOT / "docs/images/capture_i2c_write_transaction.png").is_file()


def test_a_broken_capture_or_section_is_reported():
    data = CAPTURE.read_bytes()
    assert _capture_problems(data) == []
    assert _capture_problems(_fst(b"tb_i2c_datalogger clk rst")) == []
    assert "no scope named tb_i2c_datalogger" in _capture_problems(_fst(b"tb_other_bench clk rst"))
    with_path = _fst(b"tb_i2c_datalogger /" + b"home" + b"/someone/work/tb.vhd")
    assert any("host paths" in p for p in _capture_problems(with_path))
    assert _capture_problems(b"\x99" + data[1:])[0].startswith("not an FST file")
    assert any("bytes, over" in p for p in _capture_problems(data + b"\x00" * MAX_BYTES))
    text = README.read_text(encoding="utf-8")
    for lang, old, new, problem in (
        ("bash", "RW=~/routewave-2.7.0-linux-x86_64/routewave\n", "", "RW is not the program"),
        ("bash", "RW=~/routewave-2.7.0-linux-x86_64/routewave", "RW=~/routewave/routewave", "RW is not the program"),
        ("bash", '\n"$RW" -c', '\n# "$RW" -c', "does not launch RW"),
        ("bash", "```bash\ncd ~\n", "```bash\ncd /tmp\n", "RW is not the program"),
        ("powershell", '$RW = "$HOME\\Downloads\\routewave-2.7.0-windows-x86_64\\routewave.exe"\n', "",
         "RW is not the program"),
        ("powershell", "Downloads\\routewave-2.7.0-windows-x86_64\\routewave.exe", "Desktop\\routewave.exe",
         "RW is not the program"),
        ("powershell", "\n& $RW -c", "\n# & $RW -c", "does not launch RW"),
        ("powershell", 'Set-Location "$HOME\\Downloads"', 'Set-Location "$HOME\\Desktop"', "RW is not the program"),
    ):
        assert old in text, (lang, old)
        mutated = text.replace(old, new, 1)
        assert any(lang in p and problem in p for p in _readme_problems(mutated)), (lang, old, _readme_problems(mutated))
    wrong_scope = text.replace("show --tree tb_i2c_datalogger", "show --tree tb_top", 1)
    assert "the section does not give the command that opens the shipped capture" in _readme_problems(wrong_scope)


def test_the_executed_readme_blocks_are_the_pinned_ones():
    text = README.read_text(encoding="utf-8")
    assert _snapshot_problems(text) == []
    for (section, lang), pinned in PINNED_BLOCKS.items():
        assert pinned in text, (section, lang)
        changed = text.replace(pinned, pinned.replace("\n", " \n", 1), 1)  # one byte added to that block
        problems = _snapshot_problems(changed)
        assert len(problems) == 1 and f"the {lang} block of the {section} section" in problems[0], problems
