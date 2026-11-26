# First boot

A boot of the Zybo Z7-20 image ends with the capture daemon listening (serial console, lines left out marked `…`):

```text
Linux version 6.6.80-xilinx-gf5fffda2f301 (builder@buildhost) (arm-xilinx-linux-gnueabi-gcc.real (GCC) 12.2.0, GNU ld (GNU Binutils) 2.39.0.20220819) #1 SMP PREEMPT Tue Nov 24 11:03:34 CET 2026
…
Run /init as init process

=======================================================
  deepskopion — busybox v1.36.1
  Kernel: Linux 6.6.80-xilinx-gf5fffda2f301 #1 SMP PREEMPT Tue Nov 24 11:03:34 CET 2026
  Hardware: armv7l
=======================================================

[init] python3 smoke:
  python 3.12.10
  msgpack (1, 1, 0)
…
macb e000b000.ethernet eth0: Link is Up - 1Gbps/Full - flow control off
…
[init] /etc/init.d/S99deepskopion-stream start
Starting deepskopion-stream...
  source=ddr
…
ddr source: watermark-IRQ drain via /dev/uio0
…
  SW daemon version : 0.1.0
  bitstream GIT_HASH : 0x87d88829
…
[ds-stream] wave_events_v1 listening on 0.0.0.0:7476
…
[ds-stream] listening on 0.0.0.0:7475 (fan-out); drainer running, max_queue=256, backfill=8388608B/512 frames/0s
```

The Zybo Z7-20 and PolarFire SoC images ship with no network address and no SSH key. The board's USB serial console
(115200 8N1) is available from power-on and takes these steps, in this order:

1. Address. A static address is used first, from the first of these sources that is set:
   - `ds_ip=<address>/<prefix>` (and optionally `ds_gateway=<gateway>`) on the kernel command line;
   - `/etc/deepskopion/network.conf` with the line `address=<address>/<prefix>` (and optionally `gateway=<gateway>`),
     applied by `/etc/init.d/S40network restart`. On the Zybo Z7-20 the root filesystem is held in RAM, so a file
     written on the board lasts until the next boot; the same file placed as `etc/deepskopion/network.conf` under the
     rootfs overlay directory `sw/linux/rootfs_overlay/` before the image build is part of the image. On the PolarFire
     SoC Discovery Kit the root filesystem is the card's third partition, mounted read-write, so a file written on the
     kit persists across boots.

   Without a static address the board requests one over DHCP. Without a DHCP server it boots with no address and
   prints `network: no DHCP lease; set ds_ip=<address>/<prefix> or create /etc/deepskopion/network.conf`.

   RouteWave needs only the address: the stream on port 7475 does not use SSH.
2. SSH key. SSH stays off until an owner key exists, and password logins are disabled. `deepskopion-authorize-key
   /path/to/key.pub` on the serial console installs a public key (`deepskopion-authorize-key -` reads it from standard
   input). A key file placed as `etc/deepskopion/authorized_keys` under `sw/linux/rootfs_overlay/` (ignored by git)
   before the build is installed at every boot instead; it becomes `/etc/deepskopion/authorized_keys` in the image.
3. SSH start. `/etc/init.d/S49dropbear start` starts the SSH server.

The DE25 image differs: it ships with the static address of its deployment descriptor on its kernel command line and
no SSH server ([Build and run on the Terasic DE25](de25.md)).

[`docs/SUPPORTED_ENVIRONMENT.md`](../SUPPORTED_ENVIRONMENT.md) has the details.
