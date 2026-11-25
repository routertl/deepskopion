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
