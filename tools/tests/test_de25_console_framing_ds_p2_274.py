# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.274: a console shell call returns the command's whole output when the board's 80-column
echo wrap puts a marker at the start of a line.

Measured 2026-11-12 (DS-T1.25 run 11, DE25): with the prompt `/usr/local/share/deepskopion # `
the echo of the control-profile command wrapped so that its third line began `E<tag> rc=$?`. The
DS-P2.165 witness harness Console took that echo line as the end marker and returned before the
output. The fake serial below replays the board's behaviour: it echoes the typed line wrapped at 80
columns after the prompt, then prints the framed output as the shell does.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "hw"))
from de25_console import Console  # noqa: E402

PROMPT_RUN11 = "/usr/local/share/deepskopion # "
CMD_RUN11 = ("cd /usr/local/share/deepskopion && PYTHONHOME=/ python3 -m sw.stream.ring_cpuprofile "
             "--interval 30 --top 5")
PROFILE = ("=== PS CPU profile over 30.0s (1 cores) ===\n"
           "machine busy   1.0%   idle headroom  99.0%   (idle≈0 ⇒ drain may be starved)\n"
           "deepskopion daemon  pid 111: <0.1%\n")


class BoardSerial:
    """Echo wrapped at `cols` after `prompt`, then the framed output (B line, payload, E line, prompt)."""

    def __init__(self, prompt: str, payload: str, cols: int = 80, output_delay: float = 1.0):
        self.prompt, self.payload, self.cols, self.delay = prompt, payload, cols, output_delay
        self.typed = bytearray()
        self.out = bytearray(prompt.encode())
        self.lock = threading.Lock()

    def write(self, b: bytes) -> None:
        self.typed += b
        if b == b"\r":
            line = self.typed[:-1].decode()
            self.typed.clear()
            echo = self.prompt + line
            wrapped = "\r\n".join(echo[i:i + self.cols] for i in range(0, len(echo), self.cols))
            with self.lock:
                self.out += wrapped[len(self.prompt):].encode() + b"\r\n"
            tag = re.match(r"echo B([0-9a-f]{8}); ", line).group(1)

            def later():
                # As on the board: `echo B<tag>` prints at once, the command's output comes when the
                # command has run (the run-11 profile took 30 s), then `echo E<tag> rc=$?`.
                time.sleep(0.05)
                with self.lock:
                    self.out += f"B{tag}\r\n".encode()
                time.sleep(self.delay)
                with self.lock:
                    self.out += (self.payload.replace("\n", "\r\n") + f"E{tag} rc=0\r\n{self.prompt}").encode()
            threading.Thread(target=later, daemon=True).start()

    def read(self, n: int) -> bytes:
        time.sleep(0.01)
        with self.lock:
            got, self.out[:] = bytes(self.out[:n]), self.out[n:]
        return got


def _console(tmp_path, ser) -> Console:
    return Console("fake", tmp_path / "console.log", ser=ser)


def _run(cls_console, prompt: str, cmd: str, payload: str, tmp_path, cols: int = 80) -> str | None:
    ser = BoardSerial(prompt, payload, cols)
    con = cls_console(tmp_path, ser)
    try:
        return con.shell(cmd, timeout=5)
    finally:
        con.stop = True


def test_wrapped_echo_starting_a_line_with_the_end_marker_returns_the_whole_output(tmp_path):
    """Run 11's prompt and command: the echo's third line starts with `E<tag> rc=`."""
    echo = PROMPT_RUN11 + f"echo Babcdef01; {CMD_RUN11}; echo Eabcdef01 rc=$?"
    lines = [echo[i:i + 80] for i in range(0, len(echo), 80)]
    assert any(l.startswith("Eabcdef01 rc=") for l in lines), lines   # the case is the case
    got = _run(_console, PROMPT_RUN11, CMD_RUN11, PROFILE, tmp_path)
    assert got == PROFILE.rstrip("\n"), got


def test_wrapped_echo_starting_a_line_with_the_begin_marker_returns_only_the_output(tmp_path):
    """A prompt 75 characters long puts `B<tag>;` at the start of the echo's second line."""
    prompt = "/" + "p" * 72 + "# "
    assert len(prompt + "echo ") == 80
    got = _run(_console, prompt, "cat /etc/hostname", "de25\n", tmp_path)
    assert got == "de25", got


def test_unwrapped_echo_returns_the_output(tmp_path):
    got = _run(_console, "~ # ", "echo hi", "hi\n", tmp_path)
    assert got == "hi", got


class _SilentSerial(BoardSerial):
    """Echoes like the board but never runs the command: no B or E line ever arrives."""

    def write(self, b: bytes) -> None:
        self.typed += b
        if b == b"\r":
            line = self.typed[:-1].decode()
            self.typed.clear()
            with self.lock:
                self.out += line.encode() + b"\r\n"


def test_a_timed_out_call_returns_none_and_no_exit_code(tmp_path):
    """After a completed call (rc 0), a call that times out must not report that rc as its own."""
    ser = BoardSerial("~ # ", "hi\n")
    con = _console(tmp_path, ser)
    try:
        assert con.shell("echo hi", timeout=5) == "hi"
        assert con.last_rc == 0
        con.ser = _SilentSerial("~ # ", "")
        assert con.shell("sleep 99", timeout=1) is None
        assert con.last_rc is None, f"stale rc {con.last_rc!r} from the previous command"
    finally:
        con.stop = True
