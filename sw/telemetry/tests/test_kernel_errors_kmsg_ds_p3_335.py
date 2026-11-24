# SPDX-License-Identifier: MPL-2.0
"""DS-P3.335: kernel_errors works with what the board images ship.

The images' dmesg is BusyBox (rr linux rootfs builds 1.36.1), which refuses util-linux's
--level/-T, so the collector used to report an error on every board. It now reads /dev/kmsg.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from sw.telemetry import collectors
from sw.telemetry.collectors import collect_kernel_errors

BUSYBOX = shutil.which("busybox")

# One record per syslog level 0..7, a continuation line, and a facility-1 (user) warning.
KMSG = (
    "0,1,1000000,-;emergency record\n"
    "1,2,2000000,-;alert record\n"
    "2,3,3000000,-;critical record\n"
    "3,4,4000000,-;error record\n"
    " SUBSYSTEM=mmc\n"
    "4,5,5000000,-;warning record\n"
    "5,6,6000000,-;notice record\n"
    "6,7,7000000,-;info record\n"
    "7,8,8000000,-;debug record\n"
    "12,9,9500000,-;user-facility warning\n"
)


@pytest.mark.skipif(BUSYBOX is None, reason="DS-P3.338: needs a host busybox binary")
def test_busybox_is_the_only_dmesg_and_the_collector_still_counts(tmp_path, monkeypatch):
    """The public call on a board image: BusyBox is the only dmesg, and the kernel log holds warnings.

    The kernel log is supplied at the I/O boundary (the module's KMSG_PATH), so the same call runs on
    the dmesg-based collector too, which fails here: BusyBox refuses its --level/-T options.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "dmesg").symlink_to(BUSYBOX)
    refused = subprocess.run([str(bindir / "dmesg"), "--level=err,warn", "-T"],
                             capture_output=True, text=True, check=False)
    assert refused.returncode != 0 and "unrecognized option" in refused.stderr
    monkeypatch.setenv("PATH", str(bindir))
    kmsg = tmp_path / "kmsg"
    kmsg.write_text(KMSG, encoding="utf-8")
    monkeypatch.setattr(collectors, "KMSG_PATH", str(kmsg), raising=False)
    result = collectors.collect_kernel_errors()
    assert "error" not in result, result
    assert result["error_count"] == 6


def test_levels_up_to_warning_are_counted_and_the_rest_ignored(tmp_path):
    kmsg = tmp_path / "kmsg"
    kmsg.write_text(KMSG, encoding="utf-8")
    result = collect_kernel_errors(max_lines=3, kmsg_path=str(kmsg))
    assert result["error_count"] == 6
    assert [r["message"] for r in result["recent_errors"]] == [
        "error record", "warning record", "user-facility warning"]
    assert result["recent_errors"][0]["timestamp"] == "4.000000"


def test_a_device_like_source_is_read_without_blocking(tmp_path):
    """/dev/kmsg returns EAGAIN when no further record exists; a FIFO with its writer open does too."""
    fifo = tmp_path / "kmsg_fifo"
    os.mkfifo(fifo)
    writer = os.open(fifo, os.O_RDWR)
    try:
        os.write(writer, b"3,1,1500000,-;mmc0: error -110\n6,2,1600000,-;info\n")
        result = collect_kernel_errors(kmsg_path=str(fifo))
    finally:
        os.close(writer)
    assert result == {"error_count": 1,
                      "recent_errors": [{"timestamp": "1.500000", "message": "mmc0: error -110"}]}


def test_an_unreadable_log_is_an_error_never_zero_errors(tmp_path):
    result = collect_kernel_errors(kmsg_path=str(tmp_path / "absent"))
    assert result["error_count"] == 0 and result["recent_errors"] == []
    assert "absent" in result["error"]


def test_text_that_is_not_the_kernel_log_format_is_an_error(tmp_path):
    kmsg = tmp_path / "kmsg"
    kmsg.write_text("[Mon Mar 25 12:00:00 2026] dmesg -T output\n", encoding="utf-8")
    result = collect_kernel_errors(kmsg_path=str(kmsg))
    assert result["error_count"] == 0
    assert "no record in the kernel log format" in result["error"]


def test_an_empty_log_is_zero_errors(tmp_path):
    kmsg = tmp_path / "kmsg"
    kmsg.write_text("", encoding="utf-8")
    assert collect_kernel_errors(kmsg_path=str(kmsg)) == {"error_count": 0, "recent_errors": []}
