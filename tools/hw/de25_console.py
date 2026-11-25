# rr unknown-llm (AI-generated)
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""The one reader of a board's Linux console, with framed shell commands (DS-P2.274).

Same interface as the DS-P2.165 witness harness Console (``mark``, ``since``, ``send``,
``wait_for``, ``shell``, ``stop``), which is a pinned record of what ran and stays unchanged.
The difference is the framing. The board echoes the typed line and wraps it at the terminal
width, so a wrapped echo line can start with ``E<tag> rc=`` or ``B<tag>``. The witness Console
took markers from anywhere in the buffer and, on such a wrap, returned before the command's output
(DS-T1.25 run 11, 2026-11-12). Here the output is read with ``hw_run_hps_devmem.parse_framed``:
the begin marker must be a whole line, and the end marker a line start after it.

``dev`` may be a device path or a pyserial URL (``socket://host:port``), so the rehearsal VM's
serial is driven by this same class.
"""
from __future__ import annotations

import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hw_run_hps_devmem import framed_command, parse_framed  # noqa: E402


class Console:
    """The ONE reader of the console (two readers steal bytes)."""

    def __init__(self, dev: str, logpath: Path, ser=None):
        if ser is None:
            import serial
            ser = serial.serial_for_url(dev, 115200, timeout=0.2, do_not_open=True)
            if "://" not in dev:
                ser.dtr = True
                ser.rts = True  # Haz.107: the DE25 console reads nothing without modem lines
            ser.open()
            ser.reset_input_buffer()
        self.ser = ser
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.logf = open(logpath, "ab", buffering=0)
        self.stop = False
        self.last_rc: int | None = None
        self.t = threading.Thread(target=self._reader, daemon=True)
        self.t.start()

    def _reader(self) -> None:
        while not self.stop:
            try:
                b = self.ser.read(4096)
            except Exception as exc:  # noqa: BLE001 — log and keep the reader alive
                self.logf.write(f"\n[console: reader error {exc!r}]\n".encode())
                time.sleep(0.5)
                continue
            if b:
                b = b.replace(b"\0", b"")
                with self.lock:
                    self.buf += b
                self.logf.write(b)

    def mark(self) -> int:
        with self.lock:
            return len(self.buf)

    def since(self, m: int) -> bytes:
        with self.lock:
            return bytes(self.buf[m:])

    def send(self, s: str, pace: float = 0.003) -> None:
        for ch in s.encode():
            self.ser.write(bytes([ch]))
            time.sleep(pace)

    def wait_for(self, m: int, pat: bytes, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if pat in self.since(m):
                return True
            time.sleep(0.05)
        return False

    def shell(self, line: str, timeout: float = 30.0) -> str | None:
        """Run one framed command; its output text, or None if it did not complete in time."""
        tag = uuid.uuid4().hex[:8]
        # A call that times out has no exit code. Cleared first, so a caller never reads the previous
        # command's rc for this one (DE25 UART rehearsal, 2026-11-12: a timed-out call read a stale 0).
        self.last_rc = None
        m = self.mark()
        self.send(framed_command(line, tag))
        end = time.time() + timeout
        while time.time() < end:
            got = parse_framed(self.since(m).decode("utf-8", "replace"), tag)
            if got is not None:
                self.last_rc, body = got
                return body
            time.sleep(0.02)
        return None
