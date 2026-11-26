# sw/linux — deepskopion embedded-Linux deployment

This directory is the bridge between the deepskopion RTL and the
userspace `sw/stream/` daemon. It's the deployment side of the
PL → PS → host pipeline frozen at
[`docs/software/data_plane/message_formats/common_header.md`](../../docs/software/data_plane/message_formats/common_header.md).

Consumed by:

- `rr linux sync` — cross-checks `system_memory_map.yml` against the
  latest XSA, regenerates `.dtsi` + `_uio.h` + `_uio.py` artefacts.
- `rr linux crosscheck` — standalone YAML ↔ XSA sanity check.
- `rr linux gen` — regenerate the three artefact families without the
  crosscheck + petalinux-spec drop.
- `rr linux rootfs` — bake `rootfs_overlay/` into a busybox initramfs
  or ext4 partition image.

## Memory map

`system_memory_map.yml` declares the two PL resources the daemon
needs at runtime:

| Device       | Base addr     | Span    | Purpose |
| :----------- | :------------ | :------ | :------ |
| `system_regs`| `0x43C00000`  | 64 KB   | Register bank (telemetry counters + ring locator) |
| `ddr_ring`   | `0x10000000`  | 256 MB  | PS-DDR window for zs_msg frames from the framer |

The `system_regs` entry carries a `regbank_ref:` pointing at
`../../project_regbank.yml` so the generated UIO bindings carry
`REG_*_OFFSET` macros + field metadata for every named register.

> **Known issue:** `rr linux gen` currently multiplies the YAML `address:`
> by 4 when emitting `system_regs_uio.py`. Until that
> lands, the canonical Python view stays at
> [`sw/stream/regbank.py`](../stream/regbank.py) (`Regs` class), and
> `sw/stream/regbank.py` is **not** wired onto the generated binding.
> The C header `system_regs_uio.h` is unaffected if you read directly
> from the generated `REG_*_ADDR` macros (also wrong by the same
> factor) — pin to the hand-crafted offsets for now.

## Rootfs bundle (`rootfs_overlay/`)

> **The deployment contract is [`docs/DEPLOYMENT_CONTRACT.md`](../../docs/DEPLOYMENT_CONTRACT.md)**
> (SW-C0) — which autostart files ship, where the daemon source
> lives, how it reaches the rootfs, and how to tell a staged artefact from a
> source file. It is generated from `deployment_contract.yml` in this
> directory and enforced by `deployment_contract.py`, which `stage_daemon.sh`
> runs on every `rr linux rootfs`. This section is the operational detail
> underneath it; if the two ever disagree, that document wins and the check
> should already have failed the build.

`rr linux rootfs` rsyncs `rootfs_overlay/` over the staging tree
before packing the image. **One** boot integration is committed, because there
is exactly one init system:

```
rootfs_overlay/
├── init                                      # PID 1 — mounts /proc /sys /dev,
│                                             #   then runs /etc/init.d/S* in order
├── etc/init.d/
│   ├── S01hostname
│   ├── S40network                            # DHCP or owner-supplied static state
│   ├── S49dropbear                           # sshd (binary staged, gitignored)
│   └── S99deepskopion-stream                 # THE CAPTURE DAEMON — last, on purpose
└── usr/local/
    ├── bin/deepskopion-authorize-key         # first-boot owner-key provisioner
    └── share/deepskopion/
        └── (sw/stream/ staged at build time by ../../stage_daemon.sh, gitignored)
```

`S99deepskopion-stream` is named to run last in `/etc/init.d/S*` order. It
launches the daemon in the background, PID file at
`/var/run/deepskopion-stream.pid`, log at `/var/log/deepskopion-stream.log`.
No further enable step needed.

**There is no systemd path, and no systemd variant to reach for (SW-C0).** Every image this repo builds is a busybox initramfs —
`sbin/init -> ../bin/busybox`, no `usr/lib/systemd`, verified by unpacking a
deployed `uImage`. Two `.service` units used to sit in
`rootfs_overlay/etc/systemd/system/` and ride into images that could never run
them; they were moved to `docs/` as "examples" and are now deleted, because
their only justification was a PetaLinux profile — a flow this house does not
run. So: `journalctl -u deepskopion-stream` **does not work on any deepskopion
board** — read `/var/log/deepskopion-stream.log` instead. The reasoning, and
what to do if a systemd-based DE25 image ever lands, is in the deployment
contract.

**Which `--source` the daemon runs** comes from `ds_source=` on the
kernel cmdline, declared per target in `targets/<name>.yml → linux.bootargs`
(`ddr` for canfd_demo, `eye` for classic_eye). It travels with the image, so it
survives a reboot; a file written on
the board would not, because the rootfs is tmpfs. `cat /proc/cmdline` on the
board shows what it was built with. `/etc/deepskopion-source` still overrides
it for interactive poking, and is deliberately gone after a power cycle.

## SSH access — dropbear (`stage_dropbear_overlay.sh`)

The rootfs ships an sshd so we are never locked to the serial console. The
package is already enabled in the Buildroot defconfig
(`BR2_PACKAGE_DROPBEAR=y`), but `extract_python_overlay.sh` is python-only,
so the binary needs a staging step. The Zynq targets' `linux.rootfs.stage_command`
runs `stage_dropbear_overlay.sh` after `stage_daemon.sh`, so `rr linux rootfs`
stages it and stops with "the rootfs would have no sshd" when Buildroot has not
built it. `DS_BUILDROOT_OUTPUT` points a linked worktree at another checkout's
`sw/linux/buildroot/output`.

```bash
rr linux build                           # stage_command stages dropbear, rebuilds the initramfs INTO uImage, restages the SD
# then redeploy uImage (TFTP / SD) — the initramfs is baked into uImage,
# NOT into BOOT.bin, so a BOOT.bin-only reflash changes nothing here.
```

Tracked access-control pieces:

```
rootfs_overlay/
├── etc/init.d/S49dropbear               # stays closed until an owner key exists
└── usr/local/bin/deepskopion-authorize-key
```

- **NEEDED libs** (`libc.so.6`, `ld-linux.so.3`, `libcrypt.so.2`) already
  ride in `python_overlay`, so only the 220 KB binary is staged.
- **No public key ships in the image.** On first boot, use the serial console:
  `deepskopion-authorize-key /path/to/owner.pub`, then
  `/etc/init.d/S49dropbear start`. An image builder may privately provide
  `/etc/deepskopion/authorized_keys`; the init script accepts it on first boot.
- **Password login is disabled** (`dropbear -s`). With no owner key, the init
  script creates an empty root-owned key file and does not start sshd.
- **Host keys** are generated by `dropbear -R` only after access is provisioned.
- **The DE25 image leaves both files out.** It stages no AArch64 dropbear, so it has no SSH
  server. The built card is checked after every layer is merged: `tools/de25_linux_image.py
  build` reads `sdcard.img` partition 2, and `tools/de25_linux_image.py check-rootfs <image>
  [--offset N]` reads any ext4 image. Both refuse an image that holds a dropbear file or
  whose init scripts run a program the image does not hold, and print each construct the
  scan does not model as a `WARN` line.
- **Init-script scan scope.** Parsed: each script is parsed by the ash of BusyBox 1.36.1
  (tools/ash_dump.py), built from the same source and with the same ash options as the
  image's busybox, not the same binary, in a sandbox with no network; nothing in a script is
  executed. Judged: every simple command the parse yields, wherever it sits: lists,
  pipelines and && / || chains, if, while, until, for and case (patterns included),
  subshells ( ), brace groups { }, redirected compound commands, function bodies, and the $(
  ) and backtick substitutions (compound commands inside them included) in any word,
  redirection target, for list, case pattern or here-document body that the shell expands;
  the literal command words, quotes, escapes and line continuations as the shell resolves
  them; literal NAME=value assignments, prefix assignments included; a literal PATH
  assignment at the top level of the file, unconditional only; function definitions, visible
  in their own subshell scope, to calls after the definition, and inside function bodies
  (command NAME bypasses a function); the -x operand of [ and test; the #! interpreter; and
  a command word that is exactly $NAME or ${NAME} for a variable the script assigns, judged
  by each literal value assigned. At each judged program position a path under a bin
  directory, or any other absolute command word whose every segment is a plain name of
  letters, digits and . _ + - (and not . or ..), except the runtime and computed paths named
  below, must be an executable file in the image, and a bare name must resolve through the
  PATH in effect. Not judged, and reported as one WARN line naming script:line and the
  construct, and refused by the image gate unless it is on the reviewed accept list:
  command -p; a PATH assignment that is conditional or inside
  a compound command, a subshell, a function body, a pipeline or the background (it is not
  applied); a PATH entry that is not a literal absolute directory; an absolute command path
  under tmp, run, var, dev, proc, sys, mnt or media (the file is created at boot), and an
  absolute command path that is computed (a variable, substitution, arithmetic expansion,
  unquoted glob or tilde, or a character outside the plain-name set in it) or has a . or ..
  segment; a command word that is not a literal name or absolute path, such as a relative
  path; a command word that joins a variable with other text; eval; alias; a here-document
  fed to a shell; process substitution; sh -c strings; . and source; a command word held in
  a variable the script does not assign or assigns a value that is not a literal; a command
  word produced by a substitution; and a program run through a wrapper (env, nice, nohup,
  setsid, timeout, xargs, chroot, su, flock, or start-stop-daemon with --exec or --startas),
  whose own name is judged. A script that ash refuses is an error, never skipped. What the
  scan cannot know is what a variable, a substitution or a sourced file holds when the
  script runs. An accepted WARN passes with its reason; a clean run is not a proof.

## Daemon staging — `stage_daemon.sh`

The daemon's canonical Python source is at `<repo>/sw/stream/`, not under
`rootfs_overlay/`. `stage_daemon.sh` copies it into
`rootfs_overlay/usr/local/share/deepskopion/sw/stream/`.

**You should not have to run it by hand.** Each target declares
`linux.rootfs.stage_command: ./sw/linux/stage_daemon.sh`, so `rr linux rootfs`
runs it first and aborts if it fails. The Zynq-7000 and PolarFire SoC
ddr targets then run `tools/native_edge_lib.py check` in the same
`stage_command` ("Python in the rootfs"), so an overlay without the native
edge library also stops the image build. Running `stage_daemon.sh` directly
is still fine and idempotent.

```bash
rr linux build          # stage_daemon.sh → rootfs → kernel → sdcard
```

The staged tree **is** gitignored now. It was not for months, while this
README, `stage_daemon.sh` and a second staging script all said it was — a
committed second copy of `sw/stream/` that nothing ever compared against the
first. If you find a `refresh_daemon_overlay.sh`, it is from before
that cleanup: it is deleted, because it staged a subtly different tree (no
top-level `sw/__init__.py`, so `python -m sw.stream` would not resolve).

The only on-disk truth is `sw/stream/`.

## "Is the board running what I built?" — ask this FIRST

A stale rootfs presents as a hardware fault: bring-up debug can waste hours
if nobody checks whether the file under discussion was
even ON the board — it was in the overlay and not in the image. Do not start a
bring-up debug by reasoning about silicon. Ask this first:

```bash
# 1. copy the board's stamp off (over ssh, or paste it from the console)
#    it lives at /etc/rr-sw-provenance, written by `rr linux rootfs`
rr linux verify --board-file /tmp/board-provenance     # one-line verdict
rr linux verify --fetch                                # same, over ssh
```

It compares the **payload hash** (`RR_SW_PAYLOAD_SHA`, the field in
`/etc/rr-sw-provenance` that identifies the deployed rootfs software content)
and the source git revision (`RR_SW_GIT_HASH` / `RR_SW_GIT_REV`) of the
last-baked rootfs against what the running board reports, so it answers the
question directly instead of by inference. A mismatch in `RR_SW_PAYLOAD_SHA`
means the board is running different software content than the rootfs in
`sw/linux/out/<target>/rootfs/` — redeploy before debugging anything else.

Two things that make this a real trap rather than a theoretical one:

- **The rootfs is an initramfs baked into `uImage`.** Editing `sw/stream/`,
  or even re-staging the overlay, changes nothing on a running board — and
  nothing on the SD either until a new `uImage` is written. A `BOOT.bin`-only
  reflash also changes nothing here.
- **The rootfs is a tmpfs.** Anything you copy onto a booted board (see the
  serial/ssh push paths above) is gone at the next reboot, so a fix that
  "works on the board" can still be absent from every image you ship.

If the board predates the provenance stamp and has no `/etc/rr-sw-provenance`,
that absence is itself the answer: it predates provenance stamping support.

## Python in the rootfs — open prerequisite

`rr linux rootfs --mode initramfs` is busybox-only today. The
deepskopion daemon needs **Python 3 + msgpack + the `sw` package**
inside the rootfs (extend `rr linux rootfs` to support an optional Python target, or
provide a hook for declarative Python recipes / static binary
embedding).

What we do today: the **python overlay** — a build-once CPython 3.12 + msgpack
tree, **per-arch** because a riscv64 kernel cannot exec an armhf interpreter.
The overlay tarballs are not published: the project does not redistribute
their components (`docs/REDISTRIBUTION.md`). A clone builds each overlay from
upstream sources; both scripts clone the Buildroot 2024.11.4 tag
(`BUILDROOT_REF`) and build it on the host, 1-3 h per architecture:

| Board | Build | Overlay directory |
|---|---|---|
| Zynq-7000 (ARMv7, EABI5 soft-float, `ld-linux.so.3`) | `sw/linux/build_buildroot.sh && sw/linux/extract_python_overlay.sh` | `sw/linux/python_overlay/` |
| PolarFire SoC (riscv64) | `sw/linux/pfsoc/build_python_overlay.sh` | `sw/linux/pfsoc/python_overlay/` |

The Zynq overlay is always built this way: no Zynq tarball is kept in the tree.
A PolarFire checkout that holds a `python_overlay.tar.gz` unpacks it instead
with `pfsoc/fetch_python_overlay.sh`; without a tarball that script exits 1 and
names the build above.

Each overlay also holds the daemon's native edge library
(`usr/local/lib/deepskopion/libds_edge_drain.so`), built from
`sw/stream/native/edge_drain.c` by `tools/native_edge_lib.py install`. The two
build scripts above run it with their Buildroot gcc. The PolarFire tarball does
not carry the library, so after `pfsoc/fetch_python_overlay.sh` it is built with:

| Board | Command |
|---|---|
| PolarFire SoC | `python3 tools/native_edge_lib.py install --profile pfsoc` (compiler: `sw/linux/pfsoc/buildroot/output/host/bin/riscv64-buildroot-linux-gnu-gcc`; interim: `--cc $(command -v riscv64-linux-gnu-gcc)`) |

The `linux.rootfs.stage_command` of every ddr target on these overlays runs
`tools/native_edge_lib.py check`. The image build stops when the library is
missing, its ELF flags differ from the overlay's `libpython` (a hard-float ARM
build on the soft-float Zynq image, a RISC-V build without RVC or the
double-float ABI, or with an ISA extension the image lacks), or it was built
from other sources than the checkout's. `etc/deepskopion-native-edge` in the
overlay records:

| Key | Value |
|---|---|
| `native_edge_lib`, `native_edge_lib_sha256` | install path and sha256 of the library |
| `native_edge_source_sha256` | sha256 over `edge_drain.c` and `edge_drain.h`, each as path, NUL, bytes, NUL |
| `native_edge_compiler`, `native_edge_compiler_version`, `native_edge_compiler_sha256` | the compiler's file name (no path), its `--version` line and the sha256 of its binary |
| `native_edge_toolchain` | `buildroot`, or `interim, not the Buildroot toolchain` |
| `native_edge_cflags` | the compile flags |
| `native_edge_ldflags` | the link flags (`-Wl,--strip-debug`: no debug sections, so no toolchain build paths) |

The Zynq overlay carries no build-host data. `extract_python_overlay.sh` runs
`sw/linux/clean_python_overlay.py --fix` with the Buildroot host `python3`: it removes
`usr/lib/python3*/config-3*/Makefile` and `usr/lib/*-gdb.py`, and rewrites
`_sysconfigdata*.pyc` without the build-host part of each path. The `stage_command` of
every target on the Zynq overlay runs `clean_python_overlay.py --check`, which refuses
those files and any file holding a home-directory or `/root/` path, and prints the
overlay directory followed by `: no build-host data` when the overlay is clean. A `canfd_demo`
image built this way (commit `11ad1d06`) was booted on a Zybo-Z7-20 from the SD card:
`sysconfig.get_config_var('prefix')` returned `/usr`, and the daemon loaded the native
library with the sha256 recorded in `etc/deepskopion-native-edge`.

The DE25 image takes its Python from pinned upstream downloads instead
(`tools/de25_linux_image.py stage`). The overlay is applied by `rr linux rootfs` via
`linux.rootfs.python_overlay` in the target yml — and `rr linux rootfs` aborts
(FileNotFoundError → exit 1) if that declared overlay directory is absent, so a
missing overlay fails the bake loudly rather than shipping a python-less rootfs.
That is the sanctioned answer to this gap; a PetaLinux profile
is **not** (house rule: no `petalinux-build`, no `bitbake`, no raw `dtc` — and
it was the only justification the deleted systemd unit ever had, SW-C0).

Other workarounds:

- Develop without a baked-in daemon: scp `sw/stream/` to the board's
  filesystem after boot, run by hand under `screen`.
- (Future) PyOxidizer / Nuitka / staticx the daemon into a single
  static binary, drop it directly into `rootfs_overlay/usr/local/bin/`,
  use the busybox init script unchanged.

## Workflow recap

1. Edit `system_memory_map.yml` if an AXI device is added or moved.
2. `rr linux sync --skip-crosscheck`. Passing `--xsa <path>` alone still runs
   the cross-check, and on canfd_demo it fails: `system_regs` and `ddr_ring`
   sit in the PL top level, outside the block design, so the XSA has no module
   for them. `--skip-crosscheck --xsa <path>` regenerates from a built XSA.
3. Verify the generated `gen/dtbo/deepskopion_overlay.dts` + headers.
4. **`rr linux build`** — one command. It stages the daemon
   (`linux.rootfs.stage_command`), rebuilds the initramfs, bakes it into
   `uImage`, and restages `out/sdcard/`. Editing anything under
   `rootfs_overlay/` or `sw/stream/` correctly invalidates all of it.
   Two inputs are taken from the environment:
   - Kernel config fragments: `rr linux kernel` reads `$KERNEL_CONFIG_FRAGMENTS`,
     not the target's `linux.kernel_config`. For zybo_datalogger it names
     `sw/linux/kernel_emio.config-fragment` (absolute path); without it the
     kernel has no `spidev`. Expected in the built `.config`:
     `CONFIG_SPI_SPIDEV=y`. A bench target with the CTU CAN FD cores adds its
     own fragment for the `ctucanfd` driver.
   - Buildroot output: a linked worktree with no Buildroot build of its own
     sets `DS_BUILDROOT_OUTPUT` to another checkout's `sw/linux/buildroot/output`
     (dropbear for the rootfs, and the compiler for
     `tools/native_edge_lib.py install --profile zybo`).
5. `rr linux flash-sd` (or `rr linux tftp-sync`). Both refuse to run when
   `out/linux.lock` no longer matches the artefacts on disk, or when the
   staged payload was built for a different target.

   **`out/` is target-scoped:** with 2+ targets declared, every
   `rr linux` step reads and writes `out/<target>/` (e.g.
   `out/canfd_demo/sdcard/`), so the canfd and classic_eye payloads can no
   longer collide under one filename. Legacy flat `out/` artefacts are
   ignored — a loud migration hint prints once when the scoped dir is first
   created. The lock target stamp stays as defence in depth.
6. Boot the board; `tail -f /var/log/deepskopion-stream.log` for daemon logs.
   (Not `journalctl` — there is no systemd in the image. See above.)
7. On the host: launch RouteWave, ingest from `tcp://<board-ip>:7475`
   via `WaveStreamCaptureEngine::AttachTcp`.

### Why it is one command now

It used to be seven hops and four copies of the daemon — `sw/stream/` →
`rootfs_overlay/` → `out/rootfs/` → `uImage` → `out/sdcard/uImage` → SD —
every hop a separate command with nothing enforcing the order. `rr linux
build` was `(kernel, sdcard)`: it did not include the rootfs step at all, and
`run_rootfs` was reachable from nothing but its own CLI command. The result,
measured on 2026-08-02, was a board running a five-week-old daemon with three
landed fix-commits on no image, while every command in the build reported
success.
