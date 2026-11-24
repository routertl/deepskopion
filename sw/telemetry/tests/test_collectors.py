# SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT

import shutil
import tempfile
import unittest
from pathlib import Path

from sw.telemetry.collectors import (
    collect_cpu_load_from_snapshots,
    collect_kernel_errors,
    collect_memory,
    collect_uptime,
    collect_xadc_temperature,
    compute_irq_rate_from_snapshots,
)


class TestCpuLoad(unittest.TestCase):
    def test_basic_delta(self):
        snap1 = {
            "user": 100, "nice": 0, "system": 50, "idle": 800,
            "iowait": 0, "irq": 10, "softirq": 10, "steal": 0,
        }
        snap2 = {
            "user": 200, "nice": 0, "system": 100, "idle": 1600,
            "iowait": 0, "irq": 20, "softirq": 20, "steal": 0,
        }
        load = collect_cpu_load_from_snapshots(snap1, snap2)
        self.assertEqual(load["user"], 10.3)
        self.assertEqual(load["system"], 7.2)
        self.assertEqual(load["idle"], 82.5)
        self.assertEqual(load["load_pct"], 17.5)

    def test_zero_delta_returns_idle(self):
        snap = {
            "user": 100, "nice": 0, "system": 50, "idle": 800,
            "iowait": 0, "irq": 10, "softirq": 10, "steal": 0,
        }
        load = collect_cpu_load_from_snapshots(snap, snap)
        self.assertEqual(load["idle"], 100.0)
        self.assertEqual(load["load_pct"], 0.0)


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.proc_dir = Path(tempfile.mkdtemp())
        (self.proc_dir / "meminfo").write_text(
            "MemTotal:         512000 kB\n"
            "MemFree:          100000 kB\n"
            "MemAvailable:     250000 kB\n"
            "Buffers:           20000 kB\n"
            "Cached:           130000 kB\n"
            "SwapTotal:             0 kB\n"
            "SwapFree:              0 kB\n"
        )

    def tearDown(self):
        shutil.rmtree(self.proc_dir)

    def test_parsing(self):
        mem = collect_memory(procfs_root=str(self.proc_dir))
        self.assertEqual(mem["total_kb"], 512000)
        self.assertEqual(mem["available_kb"], 250000)
        self.assertEqual(mem["free_kb"], 100000)
        self.assertEqual(mem["used_pct"], 51.2)
        self.assertEqual(mem["buffers_kb"], 20000)
        self.assertEqual(mem["cached_kb"], 130000)


class TestXadc(unittest.TestCase):
    def setUp(self):
        self.sys_dir = Path(tempfile.mkdtemp())
        self.iio_dir = self.sys_dir / "bus" / "iio" / "devices" / "iio:device0"
        self.iio_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.sys_dir)

    def test_temperature_calculation(self):
        # Xilinx XADC: temp_celsius = (raw + offset) * scale / 1000
        # Example: (2498 + (-1777.807693)) * 123.040771 / 1000 = 88.59°C
        (self.iio_dir / "in_temp0_raw").write_text("2498\n")
        (self.iio_dir / "in_temp0_scale").write_text("123.040771\n")
        (self.iio_dir / "in_temp0_offset").write_text("-1777.807693\n")

        result = collect_xadc_temperature(sysfs_root=str(self.sys_dir))
        self.assertIsNotNone(result["temp_celsius"])
        self.assertEqual(result["raw"], 2498)
        expected = (2498 + (-1777.807693)) * 123.040771 / 1000.0
        self.assertAlmostEqual(result["temp_celsius"], round(expected, 2), places=2)

    def test_missing_device_returns_null(self):
        empty_sys = Path(tempfile.mkdtemp())
        result = collect_xadc_temperature(sysfs_root=str(empty_sys))
        self.assertIsNone(result["temp_celsius"])
        self.assertIsNone(result["raw"])
        self.assertIn("error", result)
        shutil.rmtree(empty_sys)


class TestIrqRate(unittest.TestCase):
    def test_basic_rate(self):
        snap1 = {"0:timer": 100, "1:keyboard": 200}
        snap2 = {"0:timer": 200, "1:keyboard": 400}
        rate = compute_irq_rate_from_snapshots(snap1, snap2, interval_s=1.0)
        self.assertEqual(rate["total_irqs_per_sec"], 300.0)
        self.assertEqual(len(rate["top_irqs"]), 2)
        self.assertEqual(rate["top_irqs"][0]["rate"], 200.0)

    def test_new_irq_in_snap2(self):
        snap1 = {"0:timer": 100}
        snap2 = {"0:timer": 200, "1:new_irq": 50}
        rate = compute_irq_rate_from_snapshots(snap1, snap2, interval_s=1.0)
        self.assertEqual(rate["total_irqs_per_sec"], 150.0)

    def test_top_n_limiting(self):
        snap1 = {f"{i}:irq": 0 for i in range(10)}
        snap2 = {f"{i}:irq": (i + 1) * 100 for i in range(10)}
        rate = compute_irq_rate_from_snapshots(snap1, snap2, interval_s=1.0, top_n=3)
        self.assertEqual(len(rate["top_irqs"]), 3)


class TestKernelErrors(unittest.TestCase):
    # DS-P3.335: the collector reads /dev/kmsg (BusyBox dmesg refuses --level/-T); these
    # cases were the dmesg-command cases, now driven through a kmsg-format file.
    def test_successful_kmsg(self):
        with tempfile.TemporaryDirectory() as tmp:
            kmsg = Path(tmp) / "kmsg"
            kmsg.write_text("3,1,1000000,-;test error message\n", encoding="utf-8")
            result = collect_kernel_errors(kmsg_path=str(kmsg))
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(len(result["recent_errors"]), 1)
        self.assertIn("test error message", result["recent_errors"][0]["message"])

    def test_unreadable_kmsg(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = collect_kernel_errors(kmsg_path=tmp)   # a directory: read fails
        self.assertEqual(result["error_count"], 0)
        self.assertIn("error", result)

    def test_missing_kmsg(self):
        result = collect_kernel_errors(
            kmsg_path="/nonexistent/kmsg",
        )
        self.assertEqual(result["error_count"], 0)
        self.assertIn("error", result)


class TestUptime(unittest.TestCase):
    def test_parsing(self):
        proc_dir = Path(tempfile.mkdtemp())
        (proc_dir / "uptime").write_text("1234.56 7890.12\n")
        self.assertEqual(collect_uptime(procfs_root=str(proc_dir)), 1234.56)
        shutil.rmtree(proc_dir)

    def test_missing_file(self):
        self.assertEqual(collect_uptime(procfs_root="/nonexistent"), 0.0)


if __name__ == "__main__":
    unittest.main()
