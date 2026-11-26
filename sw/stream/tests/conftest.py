# rr unknown-llm (AI-generated)
"""Shared fixtures for the sw/stream tests."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def sock_dir():
    """A short directory for AF_UNIX sockets (DS-P3.199).

    A socket path is limited to 107 bytes (sun_path), so a socket under
    pytest's tmp_path fails to bind when TMPDIR is deep. The directory is
    made under /tmp, independent of TMPDIR, and removed afterwards."""
    d = Path(tempfile.mkdtemp(prefix="dsws-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class HostSerialRefused(RuntimeError):
    """A desk test reached the host serial port the MMUART1 transmitter writes (DS-P2.398)."""


@pytest.fixture(autouse=True)
def _no_host_serial_port(monkeypatch):
    """DS-P2.398: a desk test never opens a host serial port.

    In the MSS-fabric arming (DS_MSS_FABRIC=1) the UART armer writes its payload to /dev/ttyS1, which on a desk or
    bench host is the host's own port. The refusal sits at the one opener the transmitter uses, so a test that wants
    the bytes patches ``_open_mmuart`` itself and overrides this."""
    from sw.stream import stimulus_contract

    def _refuse(path, mode="wb"):
        raise HostSerialRefused(f"host serial device {path} refused in a desk test (mode {mode})")

    monkeypatch.setattr(stimulus_contract, "_open_mmuart", _refuse)
