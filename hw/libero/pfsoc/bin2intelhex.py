#!/usr/bin/env python3
# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
"""DS-P2.145 (PF-L0e) — raw binary -> Intel HEX for the fpgenprog eNVM client.

fpgenprog's ``envm_client --content_file_format`` accepts
{intel-hex, microsemi-binary-8-16-32, motorola-s, simple-hex}. The
``microsemi-binary-8-16-32`` format is an ASCII 0/1 *word* format (each line must
be 0/1 characters), NOT raw bytes — feeding it a raw binary fails with
"Each line (word) must consist of '0' and '1' characters". So the HSS raw binary
(``hss.bin``, the E51 first-stage reset-trampoline image) is converted to Intel
HEX here (base address 0; the client is placed by the tool). Deterministic:
16 data bytes per record, 4-byte extended-linear-address records as needed.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _rec(addr: int, rectype: int, payload: list[int]) -> str:
    body = [len(payload), (addr >> 8) & 0xFF, addr & 0xFF, rectype] + list(payload)
    checksum = (-sum(body)) & 0xFF
    return ":" + "".join("%02X" % b for b in body) + "%02X" % checksum


def bin_to_intel_hex(data: bytes) -> str:
    lines: list[str] = []
    upper: int | None = None
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hi = (i >> 16) & 0xFFFF
        if hi != upper:
            lines.append(_rec(0, 0x04, [(hi >> 8) & 0xFF, hi & 0xFF]))
            upper = hi
        lines.append(_rec(i & 0xFFFF, 0x00, list(chunk)))
    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: bin2intelhex.py <in.bin> <out.hex>\n")
        return 2
    src, dst = Path(argv[1]), Path(argv[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    dst.write_text(bin_to_intel_hex(data), encoding="ascii")
    print(f"bin2intelhex: {len(data)} bytes -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
