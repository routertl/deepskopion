# Deepskopion PS-Side Telemetry Exporter

## Overview

The Deepskopion PS-Side Telemetry Exporter is a lightweight Python package
designed to collect system-level metrics from the Zynq-7020 processing system.
It provides high-frequency visibility into CPU load, memory usage, device
temperature, interrupt rates, and kernel health without requiring changes
to the FPGA fabric (PL).

This tool is a core component of the **Dashboard Phase 1** milestone.

## Design Principles

- **Zero External Dependencies**: Uses only the Python 3 standard library to
  ensure compatibility with minimal `buildroot-debian` environments.
- **Embedded Reliability**: Avoids heavy parsing or memory-intensive
  operations. Uses delta-based measurements for rate-based metrics.
- **Testability**: All collectors support file system path injection,
  allowing full verification on development workstations using synthetic
  `/proc` and `/sys` data.

## Metrics Collected

| Metric | Source | Description |
|--------|--------|-------------|
| **CPU Load** | `/proc/stat` | User, system, and idle percentages (delta-based). |
| **Memory** | `/proc/meminfo` | Total, available, and used percentages. |
| **Temperature** | IIO Sysfs | Zynq XADC temperature via `/sys/bus/iio/devices/iio:device0`. |
| **IRQ Rate** | `/proc/interrupts` | Total interrupt rate and top N active IRQs. |
| **Kernel Errors** | `dmesg` | Recent error/warning messages from the kernel ring buffer. |
| **Uptime** | `/proc/uptime` | System uptime in seconds. |

## JSON Snapshot Schema

Snapshots are exported as a single JSON object:

```json
{
  "schema_version": 1,
  "timestamp_utc": "2026-03-25T12:00:00Z",
  "hostname": "deepskopion",
  "uptime_seconds": 12345.67,
  "metrics": {
    "cpu": { "user": 12.3, "system": 5.1, "idle": 82.6, "load_pct": 17.4 },
    "memory": { "total_kb": 524288, "available_kb": 312000, "used_pct": 40.5 },
    "temperature": { "temp_celsius": 42.1, "raw": 2498 },
    "irq_rate": { "total_irqs_per_sec": 1523.4, "top_irqs": [...] },
    "kernel_errors": { "error_count": 2, "recent_errors": [...] }
  }
}
```

## Usage

### Command Line

Run as a module to output a single snapshot:

```bash
python3 -m sw.telemetry --pretty
```

Run in continuous mode (e.g., for live dashboard feed):

```bash
python3 -m sw.telemetry --interval 5 --pretty
```

### Library Integration

```python
from sw.telemetry import collect_snapshot

data = collect_snapshot()
process_metrics(data['metrics'])
```

## Running it on a board

**The exporter does not autostart, and it is not baked into any image.** That
is a deliberate position, recorded in
[the deployment contract](../DEPLOYMENT_CONTRACT.md): `stage_daemon.sh` stages
`sw/stream/` only, and the one autostart mechanism this product has —
busybox `/etc/init.d/S*` — starts the capture daemon and nothing else.

This section previously described a systemd service plus timer. That recipe
could not work: every image this repo builds is a busybox initramfs with no
systemd in it (measured, see the deployment contract), so the units would never
have been read and `systemctl enable` had nothing to talk to. It was also
redundant — the exporter already has a continuous mode.

Copy it over and run it by hand:

```sh
# host: the rootfs is tmpfs, so this lasts until the next reboot
scp -r sw/telemetry root@192.0.2.2:/usr/local/share/deepskopion/sw/

# board: continuous mode, one snapshot every 10 s, appended as JSON lines
cd /usr/local/share/deepskopion
python3 -m sw.telemetry --interval 10 --output /var/log/telemetry.jsonl &
```

`--interval` is the whole scheduler; there is no timer to install. A dashboard
agent or web server reads `/var/log/telemetry.jsonl` directly. Drop `--output`
to print snapshots to stdout instead.

If this ever needs to survive a reboot, it needs an entry in the deployment
contract and an `S*` script in the overlay — in that order, because the check
in `sw/linux/deployment_contract.py` refuses an overlay that starts something
the contract does not declare.

## Troubleshooting

- **XADC Missing**: Ensure the Xilinx XADC driver is enabled in the kernel
  and the device tree includes the XADC entry. If missing, the exporter
  gracefully sets `temp_celsius` to `null`.
- **Permission Denied (dmesg)**: On some systems, `dmesg` requires sudo.
  The exporter will return an empty error list and a descriptive error field
  in the `kernel_errors` metric if it lacks permissions. On the target
  device, run the service as root or add `CAP_SYSLOG` capability.

