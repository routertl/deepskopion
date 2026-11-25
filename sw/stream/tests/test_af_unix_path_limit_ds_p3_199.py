# rr unknown-llm (AI-generated)
"""DS-P3.199: an AF_UNIX socket path over the sun_path limit is refused by name.

Linux sun_path holds 107 bytes plus the terminating NUL. bind() past that fails
with a bare "AF_UNIX path too long"; server._bind_unix says which limit, which
path and the way out instead.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from sw.stream import server


@pytest.fixture
def short_dir():
    d = Path(tempfile.mkdtemp(prefix="ds199-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_path_at_the_limit_binds(short_dir):
    path = short_dir / ("s" * (server.AF_UNIX_PATH_MAX - len(str(short_dir)) - 1))
    assert len(str(path)) == server.AF_UNIX_PATH_MAX == 107
    srv = server._bind_unix(path)
    try:
        assert path.exists()
    finally:
        srv.close()


def test_a_path_over_the_limit_is_refused_by_name(short_dir):
    path = short_dir / ("s" * (server.AF_UNIX_PATH_MAX - len(str(short_dir))))
    assert len(str(path)) == 108
    with pytest.raises(OSError, match=r"108 bytes, over the 107-byte sun_path limit"):
        server._bind_unix(path)
    assert not path.exists()
