# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MPL-2.0
"""DS-P2.398: the desk suite's guard keeps the MSS-fabric UART armer off the host's serial port."""
from __future__ import annotations

import builtins
import io

import pytest

from sw.stream import stimulus_contract
from sw.stream.testbench import de_c0_production_armer as A


class _Regbank:
    def __init__(self):
        self.writes = []

    def write_u32(self, offset, value):
        self.writes.append((offset, value))


@pytest.fixture
def no_real_device_open(monkeypatch):
    """Records every /dev/tty* path anything tries to open, and refuses it, so even without the guard no port opens."""
    seen: list[str] = []
    real_open = builtins.open

    def recording_open(file, *args, **kwargs):
        if str(file).startswith("/dev/tty"):
            seen.append(str(file))
            raise OSError(f"test refuses {file}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(io, "open", recording_open)
    return seen


def test_the_mss_fabric_uart_armer_is_refused_before_any_port_opens(monkeypatch, no_real_device_open):
    """Red without the conftest guard: the transmitter then reaches open('/dev/ttyS1')."""
    monkeypatch.setenv("DS_MSS_FABRIC", "1")
    with pytest.raises(RuntimeError, match="host serial device /dev/ttyS1 refused in a desk test"):
        A.arm(_Regbank(), "uart")
    assert no_real_device_open == []


def test_a_test_that_patches_the_opener_still_gets_the_bytes(monkeypatch):
    """The guard yields to a test's own opener, so the existing mock-based tests keep working."""
    written: list[tuple[str, bytes]] = []

    class _Fh(io.BytesIO):
        def __init__(self, path):
            super().__init__()
            self.path = path

        def close(self):
            written.append((self.path, self.getvalue()))
            super().close()

    monkeypatch.setattr(stimulus_contract, "_open_mmuart", lambda path, mode="wb": _Fh(path))
    monkeypatch.setattr(stimulus_contract, "_set_115200_8n1", lambda fh: None)
    stimulus_contract.transmit_mmuart1(b"\x5a")
    assert written == [(stimulus_contract.MMUART1_DEV, b"\x5a")]
