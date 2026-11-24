# Deepskopion PS-Side Telemetry Exporter

## Overview

The Deepskopion PS-Side Telemetry Exporter is a lightweight Python package
that collects system-level metrics from the Linux system on the board's processor subsystem.
It reports CPU load, memory usage, interrupt rates and uptime without requiring changes
to the FPGA fabric (PL). Its temperature and kernel-error readings depend on the board (see [Per board](#per-board)).

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
| **Temperature** | IIO Sysfs | `in_temp0_*` of `/sys/bus/iio/devices/iio:device0`, converted with the Zynq XADC formula; the device's identity is not checked (see [Items to confirm](#items-to-confirm)). |
| **IRQ Rate** | `/proc/interrupts` | Total interrupt rate and top N active IRQs. |
| **Kernel Errors** | `/dev/kmsg` | Kernel log records at warning level or more severe (syslog levels 0-4), timestamps in seconds since boot. Needs read access to `/dev/kmsg` (root). Measured on the Zybo Z7-20, where a warning-level record written to `/dev/kmsg` is counted; not measured on the DE25 or the PolarFire SoC Discovery Kit (see [Per board](#per-board)). |
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

The exporter does not autostart and is in no image, as recorded in
[the deployment contract](../DEPLOYMENT_CONTRACT.md): the image builds stage `sw/stream/` and not `sw/telemetry/`,
and the busybox `/etc/init.d/S*` scripts start nothing for it. The images use BusyBox init (`BR2_INIT_BUSYBOX=y` in
`sw/linux/buildroot_deepskopion_defconfig` and `sw/linux/pfsoc/buildroot_pfsoc_defconfig`, and the BusyBox root file
system that `rr linux rootfs` builds); no systemd was found on one deployed Zybo uImage (measured, see the deployment
contract). There is no service unit; continuous mode (`--interval`) is the scheduler.

### Per board

The package goes to `/usr/local/share/deepskopion/sw/telemetry`, next to the staged capture daemon
(`/usr/local/share/deepskopion/sw/stream`). How it gets there depends on the board:

| Board | Temperature | Root file system (from the image build) | Copy route |
|---|---|---|---|
| Digilent Zybo Z7-20 | not measured (see Items to confirm) | initramfs in RAM: a copied package lasts until the next boot | `ssh` with `cat`, after the SSH key and SSH start steps of the README's First boot |
| PolarFire SoC Discovery Kit | not measured (see Items to confirm) | the card's third partition, read-write: the package persists | `ssh` with `cat`, as on the Zybo Z7-20 (not run on the kit, see Items to confirm) |
| Terasic DE25 | not measured (see Items to confirm) | the card's second partition (ext4), read-write: the package persists | the image has no SSH server: the card, in a host card reader |

Kernel errors on all three boards: the collector reads the kernel's record interface `/dev/kmsg` and does not call
`dmesg`, because the images' `dmesg` is the BusyBox applet (`rr linux rootfs` builds BusyBox 1.36.1, RouteRTL
`sdk/cli/commands/linux_steps/rootfs.py`), which accepts only `[-cr] [-n LEVEL] [-s SIZE]` (`util-linux/dmesg.c`, tag
`1_36_1`). On the Zybo Z7-20 (image `87d88829`, 2026-11-24) a warning-level record written to `/dev/kmsg` is counted;
on the DE25 and the PolarFire SoC Discovery Kit a count has not been measured yet (see Items to confirm). When `/dev/kmsg` cannot be
read, `kernel_errors` carries an `error` field and a zero count.

Zybo Z7-20 and PolarFire SoC Discovery Kit, from the host (`BOARD_IP` is the board's address). The images' SSH server
(dropbear) has no SFTP server, and the default Zybo build has no `scp` binary (`sw/linux/stage_openssh_scp_overlay.sh`
adds one when it is run before the image build), so each file is written with `cat`. This route
is recorded for the Zybo development workflow in `sw/linux/stage_openssh_scp_overlay.sh`; it has not been run on the
PolarFire kit (see Items to confirm).

```sh
( set -e
  ssh "root@$BOARD_IP" "mkdir -p /usr/local/share/deepskopion/sw/telemetry"
  for f in sw/telemetry/*.py; do
      ssh "root@$BOARD_IP" "cat > /usr/local/share/deepskopion/$f" < "$f"
  done )
```

Terasic DE25, with the board powered off and its card in the host reader (`CARD_ROOT` is the card's second
partition, for example `/dev/sdb2`). The card is unmounted on exit, also when the copy fails. Afterwards the card goes
back into the board, and the board starts Linux as in the README's DE25 steps 9 and 10 (the FPGA and its first-stage
boot loader are programmed over JTAG at every power-on):

```sh
( set -e
  MNT=$(mktemp -d)
  sudo mount "$CARD_ROOT" "$MNT"
  trap 'sudo umount "$MNT"; rmdir "$MNT"' EXIT
  test -e "$MNT/usr/local/share/deepskopion/sw/stream/__main__.py"   # the root partition, not the boot partition
  sudo mkdir -p "$MNT/usr/local/share/deepskopion/sw/telemetry"
  sudo cp sw/telemetry/*.py "$MNT/usr/local/share/deepskopion/sw/telemetry/" )
```

On the board's shell (serial console on every board, or SSH on the Zybo Z7-20 and the PolarFire kit), with the
same `PYTHONHOME` choice as the capture daemon's start script `etc/init.d/S99deepskopion-stream` (`/usr/` where the
Python standard library is under `/usr/lib`, `/` otherwise):

```sh
cd /usr/local/share/deepskopion
if ls -d /usr/lib/python3*/encodings >/dev/null 2>&1 && ! ls -d /lib/python3*/encodings >/dev/null 2>&1; then
    export PYTHONHOME=/usr/
else
    export PYTHONHOME=/
fi
python3 -m sw.telemetry --interval 10 --output /var/log/telemetry.jsonl &
```

A dashboard agent or web server reads `/var/log/telemetry.jsonl` directly. Drop `--output`
to print snapshots to stdout instead.

To start at boot, it needs an entry in the deployment
contract and an `S*` script in the overlay — in that order, because the check
in `sw/linux/deployment_contract.py` refuses an overlay that starts something
the contract does not declare.

## Items to confirm

- Kernel errors on the DE25 and the PolarFire SoC Discovery Kit: a `kernel_errors` count read from `/dev/kmsg` on an
  identified image, for example after a known warning.

- The copy and run steps above on each board. The Zybo `ssh` with `cat` route is the recorded development workflow;
  the PolarFire route, the DE25 card route and a snapshot from the documented command have not been run on an
  identified image of each board.

- Temperature on all three boards. The collector reads `in_temp0_raw`, `in_temp0_scale` and `in_temp0_offset` of
  `iio:device0` and applies the Zynq XADC formula. Where no such device exists, the snapshot carries
  `temp_celsius: null` and an `error` field. No repository source enables the XADC in the Zybo Z7-20 device tree, and
  whether any board's image exposes an `iio:device0`, and what it measures there, has not been measured. A value read
  from a device other than the XADC is not a valid temperature.

## Troubleshooting

- **XADC Missing**: Ensure the Xilinx XADC driver is enabled in the kernel
  and the device tree includes the XADC entry. If missing, the exporter
  gracefully sets `temp_celsius` to `null`.
- **Kernel errors carry an `error` field**: `/dev/kmsg` could not be read; it needs root.
