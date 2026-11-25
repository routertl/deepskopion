# rr unknown-llm (AI-generated)
"""DS-P1.91: the DE25 clean-machine stage fetches its pinned inputs from URLs that last.

tools/de25_toolchain_stage.py fetched the OpenSSL 3.0.13-0ubuntu3.15 debs from
Ubuntu's security pool. That pool is rolling: when 3.0.13-0ubuntu3.16
superseded 3.15 the old files were removed, both URLs returned 404, and every
clean-machine DE-G0 run failed at step 4 (samsung, 2026-11-06, handle
gp_r0-20261106T190443-781176) while the bench, whose stage was already
populated, never noticed. Launchpad's librarian keeps every published version
and serves the same bytes (the sha256 pins are unchanged).

Two checks:
* a unit guard, always on: no pinned URL points into a rolling archive pool;
* a network check, opt-in with DS_NETWORK_TESTS=1 (the commit gate has no
  network): every pinned URL answers 200, so link rot is found by a check
  rather than by a clean-machine run.
"""
from __future__ import annotations

import importlib.util
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TC = _load("de25_toolchain_stage")
LI = _load("de25_linux_image")


def _pinned_urls() -> dict[str, str]:
    urls = {name: spec["url"] for name, spec in TC.OPENSSL_INPUTS.items()}
    urls["toolchain"] = TC.TOOLCHAIN_URL
    urls["msgpack"] = LI.MSGPACK_URL
    urls["python"] = LI.PYTHON_URL
    return urls


# A Debian/Ubuntu archive pool keeps only the current version of a package:
# archive.ubuntu.com, security.ubuntu.com, ports.ubuntu.com, deb.debian.org.
_ROLLING_POOL = re.compile(
    r"^https?://(archive|security|ports)\.ubuntu\.com/.*?/pool/"
    r"|^https?://deb\.debian\.org/.*?/pool/"
)


def test_every_pinned_url_is_collected():
    urls = _pinned_urls()
    assert len(urls) == 5, urls
    assert all(u.startswith("https://") for u in urls.values()), urls


@pytest.mark.parametrize("name", sorted(_pinned_urls()))
def test_no_pinned_url_points_into_a_rolling_archive_pool(name):
    url = _pinned_urls()[name]
    assert not _ROLLING_POOL.search(url), (
        f"{name}: {url} is in a rolling archive pool, which drops a version "
        "once it is superseded (the OpenSSL 3.15 debs 404'd this way); pin a "
        "permanent location such as launchpad.net/ubuntu/+archive/primary/+files/")


def test_the_rolling_pool_pattern_matches_the_url_that_rotted():
    """Mutation guard for the guard: the pattern must catch the real defect."""
    assert _ROLLING_POOL.search(
        "https://security.ubuntu.com/ubuntu/pool/main/o/openssl/"
        "libssl3t64_3.0.13-0ubuntu3.15_amd64.deb")
    assert not _ROLLING_POOL.search(
        "https://launchpad.net/ubuntu/+archive/primary/+files/"
        "libssl3t64_3.0.13-0ubuntu3.15_amd64.deb")


@pytest.mark.skipif(os.environ.get("DS_NETWORK_TESTS") != "1",
                    reason="network check; opt in with DS_NETWORK_TESTS=1")
@pytest.mark.parametrize("name", sorted(_pinned_urls()))
def test_every_pinned_url_answers_200(name):
    url = _pinned_urls()[name]
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "deepskopion-link-check"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 200, f"{name}: {url} answered {status}"
