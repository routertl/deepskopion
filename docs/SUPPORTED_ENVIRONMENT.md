# The supported environment

> Source of truth and requirements for host, toolchain, board, network, and power.
>
> **Machine-readable source of truth:**
> `tools/preflight/supported_environment.yml`.
> Every value below is read from that file — if the two ever disagree, the file
> is right and this page is stale, and
> `tools/preflight/tests/test_supported_environment_declaration.py` fails.
>
> **Executable check:** `python3 tools/preflight/ds_preflight.py --stage all`.
> The `build` half of it runs as the first `pre_build` hook of the canonical
> target, so a missing prerequisite stops the build **before synthesis** and
> names the prerequisite.

## Why this page exists

Before it, "run it on your own hardware" meant *reconstruct this bench from
clues*. The golden path claimed only "vendor toolchain installed, board
attached", while the real target needed a specific Vivado release, a specific
part, a specific network, a console on a particular FTDI interface, and a
source of traffic that was written down nowhere. None of that was
reproducible, and — worse — nothing in the repo could tell a user they were
missing something until a build had already run for an hour.

So: **one** supported configuration, declared, checked, and small enough to be
true.

## The declaration

| What | Supported value | Severity |
|---|---|---|
| Host OS | Ubuntu 22.04 LTS (Jammy Jellyfish); Ubuntu 24.04 LTS (Noble Numbat) for the Zybo build through the bitstream | warn |
| Host arch | `x86_64` | fail |
| RAM | ≥ 24 GB as the OS sees it (WSL2: the `.wslconfig` limit) | fail |
| Free disk | ≥ 60 GB on the checkout's filesystem | fail |
| Python | ≥ 3.10, < 3.15 | fail |
| RouteRTL | ≥ 5.6.0 (`pip install routertl`) | fail |
| Vivado | 2024.1 — ML Standard (free) or ML Enterprise | fail |
| Board | Digilent Zybo Z7-20, part `xc7z020clg400-1`, platform `zybo-z7-20` | fail |
| Network | Ethernet; `dhcp-or-user-static`, with no address in the image | fail |
| Power | USB micro-B from the host (PROG/UART port), power-select jumper in the USB position | warn |
| PMOD wiring | **not required** for the golden path | warn |
| Traffic source | on-chip; no external hardware | fail |

`fail` blocks the build. `warn` means *outside the declared envelope*: it may
work, and we would like to hear about it, but a failure there is not a defect
report we accept against the golden path.

### Host

**Ubuntu 22.04 LTS or 24.04 LTS, x86_64, ≥ 24 GB RAM, ≥ 60 GB free.**

Named OS versions, not "a modern Linux", so that *"it fails on my machine"* is
a statement with a truth value. Each version has a measured run, and the claim
covers what that run covered:

| Version | Covered | Measured run |
|---|---|---|
| Ubuntu 22.04 LTS | every target, build and board | the builds and silicon runs in the Targets table below |
| Ubuntu 24.04 LTS | the Zybo Z7-20 build from a fresh clone through the bitstream, under WSL2 | a clean-machine rehearsal of the public export (2026-11-17): RouteRTL 5.5.0 from PyPI, the README's Zybo steps through the bitstream ran as written |

Other distributions, and a listed version outside what its run covered, are
warned about, not blocked. Under WSL2 the build through the bitstream is
measured; the Linux image, SD card and board steps are not.

The RAM floor is measured, not guessed: synthesis of the canonical target
peaked at 19.5 GB and was OOM-killed under a 14 GB cap, which is why
`targets/zybo_datalogger.yml` sizes its synth cgroup at 22 000 MB. A 16 GB machine
does not fail early and clearly — it thrashes and dies an hour in. The floor
is the memory the OS reports, because that is what the build can use: 24 GB
leaves room for the 22 000 MB cgroup. Under WSL2 the OS reports the
`.wslconfig` `memory=` limit (by default about half the host's RAM), so a WSL2
host sets that limit to 24 GB or more and restarts WSL. The disk
floor covers the checkout plus one full build tree (Vivado project,
checkpoints, bitstream, Linux artifacts); it does **not** include Vivado's own
~50 GB install.

### Toolchain

**Python 3.10 to 3.14, RouteRTL ≥ 5.6.0, Vivado 2024.1.**

Vivado is pinned because the build is pinned: `build_options.tool_version` in
the target names the same release, and a different one re-times and re-fits the
design. `xc7z020clg400-1` is inside the free **ML Standard** device set, so
there is no licence purchase on the golden path; ML Enterprise works too.

Preflight finds Vivado via `$XILINX_VIVADO`, then `PATH`, then the usual
install roots — it does not require you to have sourced `settings64.sh` first.

### Board

**Digilent Zybo Z7-20 — `xc7z020clg400-1`.**

Board revision is deliberately *not* pinned, because no revision-specific
feature is used. What the design does depend on, and what a substitute board
would have to provide:

- `xc7z020clg400-1` — the Z7-**10**'s `xc7z010` does not fit 2× CTU CAN FD plus
  the datalogger, so the Z7-10 is **not** supported;
- the 33.333 MHz PS crystal and the 125 MHz fabric oscillator on K17;
- FTDI FT2232H USB-JTAG (`if00`) and USB-UART (`if01`);
- PMOD headers JA and JB;
- a microSD slot — the golden path boots Linux from SD;
- 10/100/1000 Ethernet (RTL8211E).

### JTAG rules

Interface: **FTDI FT2232H (Digilent Adept) — `if00` = JTAG, `if01` = UART at
115200.**

1. **The public target declares no cable serial.** A serial identifies one
   physical cable on one bench; in the file a stranger edits it is at best
   inert and at worst points `rr` at somebody else's board. With a single board
   attached, no selection is needed at all.
2. A host with more than one FTDI-JTAG board must name the cable **per
   invocation** (`rr program run --cable <serial>`) or in the **per-deployment**
   bench descriptor (`runtimes/hw.local.yml`) — never in the target. Preflight fails
   `jtag.unambiguous` if two boards are attached and nothing names one.
3. Vivado's part lock is the only backstop when the cable is left unpinned, and
   it cannot tell two identical boards apart.

### Network

Connect the board by Ethernet. The supported addressing mode is
`dhcp-or-user-static`: `S40network` requests DHCP by default, so a normal LAN
needs no project edit. A point-to-point link without DHCP is configured by the
owner, either with `ds_ip=<address>/<prefix>` (and optional `ds_gateway=`) on
the kernel command line, or with `/etc/deepskopion/network.conf` from the serial
console.

No address is present in any public target and there is no fallback subnet.
For the DE25's no-DHCP deployment, `tools/de25_linux_image.py build` resolves
the address and prefix from the ignored `runtimes/hw.local.yml` and injects the
`ds_ip=` token through `rr linux dtb --bootargs`; the resulting image is a
deployment artifact even though the public target remains portable. Other
images remain DHCP-first unless their owner supplies static boot state.

### Power

USB micro-B from the host, into the board's PROG/UART port, with the
power-select jumper in the USB position. A 5 V barrel supply (jumper in the
wall position) is the alternative and is required if PMOD loads exceed what USB
delivers. Jumper designators vary across board silkscreens; the positions are
labelled on the board and in the Digilent Zybo Z7 reference manual.

No smart plug, no shared outlet, no remote power control is assumed — you can
reach the board.

### PMOD wiring

**The golden path needs no probe wires.** Every bus it decodes is generated
on-chip (below). The PMOD map matters when you point the datalogger at a *real*
external bus afterwards:

| Pin | Signal |
|---|---|
| `ja[0]` / `ja[1]` | I2C #4 SCL / SDA — or PS EMIO I2C0 when `EMIO_MUX_SEL[0]=1` |
| `ja[2]` / `ja[3]` | I2C #5 SCL / SDA (always external) |
| `ja[4..7]` | SPI #8 CS / MOSI / MISO / SCLK — or PS EMIO SPI0 when `EMIO_MUX_SEL[2]=1` |
| `jb[0]` | UART #12 TX — or PS EMIO UART0 TX when `EMIO_MUX_SEL[1]=1` |
| `jb[1]` | UART #13 TX (always external) |
| `jb[2..5]` | EDGE #1 / #2 / #3 / #6 lines (always external) |

3.3 V LVCMOS, **not** 5 V tolerant. Ground the probe on pin 5 or 6 of the same
header — an unreferenced probe reads all-ones or garbage.

### Deterministic traffic source

"Connect your bus and hope it says something" is not a golden path: the first
run has to produce **known** events, or a user cannot tell success from a dead
capture. Both sources are inside the chip:

- **UART / I2C / SPI** — the PS EMIO UART0 / I2C0 / SPI0 controllers are driven
  from Linux userspace (the EMIO UART0 tty, base 0xE0000000, resolved with
  `emio_uart_dev()`; `/dev/i2c-0`; `/dev/spidev0.0`) and
  routed back into the datalogger matrix in fabric via `EMIO_MUX_SEL`. Writing
  bytes to the device node produces decoded events on UART #12, I2C #4, SPI #8.

- **CAN (opt-in)** — the Zynq's two PS CAN controllers are routed to
  EMIO and wired-AND into one real two-node CAN bus in fabric. `can1` ACKs
  `can0`, so frames actually complete: real arbitration, real ACK, no BFM. The
  passive `can_datalogger` (id 20) taps it on `capture_pin` 28 only in a build
  whose `datalogger_config.yml` enables it; the shared config ships without it.

  ```sh
  for c in can0 can1; do ip link set $c type can bitrate 250000; ip link set $c up; done
  cansend can0 123#DEADBEEF
  ```

Preflight checks that this stays true: datalogger ids 4, 8 and 12 and the SPI
controller and `spidev` device-tree nodes must still be configured, or the
traffic source is a promise the design no longer keeps. When a build enables
the CAN datalogger, both PS CAN device-tree nodes become required as well.

## Targets

The product targets in `targets/`. The declaration and the preflight above
describe the Zybo Z7-20 golden path; the other two build with their own vendor
tool, at the release the README's Requirements table names.

| Target | Board | Vendor tool | Status | Measured on silicon (2026-11-16, pre-registered gate) |
|---|---|---|---|---|
| `zybo_datalogger` | Digilent Zybo Z7-20 | Vivado 2024.1 | supported | booted from SD; `REGBANK_NUM_WORDS` = 7436, build identity `0x43B88FAA` |
| `de25_datalogger` | Terasic DE25 | Quartus Prime Pro 25.3 | supported | `REGBANK_NUM_WORDS` = 7436, identity by the regbank stamp |
| `pfsoc_datalogger` | Microchip PolarFire SoC Discovery Kit | Libero SoC 2025.2 | preview | `REGBANK_NUM_WORDS` = 7436 on 2 cold boots, build identity `0x43B88FAA`; the self-test capture is not yet verified on silicon |

## What is NOT supported

- Windows and macOS — the Linux flow builds a Linux boot chain.
- WSL2 past the bitstream — the Linux image, SD card and board steps are not
  measured under WSL2. The build through the bitstream is (see Host).
- Zybo Z7-10 (`xc7z010`) — no build of the product for this part exists.
- Any Vivado release other than 2024.1.

## Running the check

```sh
python3 tools/preflight/ds_preflight.py --stage build   # clone → bitstream
python3 tools/preflight/ds_preflight.py --stage bench   # board, link, console
python3 tools/preflight/ds_preflight.py --stage all --json
```

`build` runs automatically as the first `pre_build` hook of the canonical
target, and `pre_build` hooks are fatal, so a missing prerequisite aborts the
build in seconds rather than surfacing as an OOM kill an hour later. `bench`
never runs there: nothing about a cable or a network link is needed to produce
a bitstream, and a build must not fail on a machine with no board attached.

A failing run looks like this — the point is *required vs found*, not "check
failed":

```
  [FAIL] vivado.version
           required : Vivado 2024.1 — ML Standard (free) or ML Enterprise
           found    : 2023.2 (/tools/Xilinx/Vivado/2023.2/bin/vivado)
           fix      : Install Vivado 2024.1 and either put it on PATH or …

MISSING PREREQUISITE (1): vivado.version
```

The guard tests remove each declared prerequisite in turn and assert exactly
that:

```sh
python3 -m pytest tools/preflight/tests -q
```

## Where bench identity lives instead

Nothing that identifies a particular machine belongs in the target. The split:

| Fact | Home | Why |
|---|---|---|
| JTAG cable serial | local `runtimes/hw.local.yml`, or `rr program run --cable` | one physical cable, one bench |
| Board addresses | DHCP or owner-supplied boot/network state | per-deployment topology |
| TFTP root, leases, power switches | local `runtimes/hw.local.yml` | host services, not product config |
| Build queue opt-in | `RR_QUEUE_BUILDS=1` in the environment | a queue with no dispatcher looks exactly like a hung build |
| Board part, datalogger mix, boot chain | **the target** | this is the product, and the one file you edit |

This mirrors RouteRTL's own boundary: the board registry carries only portable
facts, and bench topology lives in the per-deployment descriptor. Preflight enforces the deepskopion end of it — a cable serial, fixed IPv4 or concrete
developer path anywhere in the target's *configuration* is a build failure.
Public hygiene gates additionally scan comments and every other artifact byte; public prose
uses role placeholders or documentation addresses when history needs an
example.

## Known limits of this declaration

- **`rr doctor` does not check any of this.** RouteRTL's doctor has no
  project-extensible check registry (`sdk/cli/checks/`, `CheckRegistry`), so a
  project cannot contribute a prerequisite to it, and its project-level checks
  are advisory (`hardware.part` empty is a WARN). Until that seam exists,
  `ds_preflight.py` is the check with teeth, and it is wired where it matters:
  `pre_build`, fatal, before synthesis. Getting these checks *into* `rr doctor`
  is a RouteRTL-side improvement, not a deepskopion one.
- **The declaration has been proven falsifiable, not yet proven portable.**
  Every prerequisite has been demonstrated to fail when removed (guard tests,
  plus a live `pre_build` abort naming `vivado.version`), and preflight passes
  on this bench. That a *documented setup succeeds on a host that is not this
  bench* is evaluated independently; it cannot be claimed from here.
- **The clone-blocking dependency pin is gone.** `ip.lock` used to pin
  `rr_rea` to a `file://` path in the developer's home plus a sibling
  `routertl` checkout — a reason why no other machine could
  build. A prior fix addressed it: the locked `rr_rea` is the vendored
  in-repo `libs/local/rr_rea` tree, and
  `tools/tests/test_no_local_path_pollution.py` guards against any tracked
  file naming a bench-local path again. The guard is armed as a leg of
  `hooks/pre-commit.local`, and that arming was demonstrated by staging a
  poisoned file and watching the real pre-commit hook refuse.
  Refresh `linux.lock.frozen` with `tools/freeze_linux_lock.py`, never a raw
  `cp` of the live lock: the kernel `.config` records
  `CONFIG_INITRAMFS_SOURCE` absolute, so a raw copy re-imports this bench and
  the guard refuses the commit.
- The bench board's silkscreen revision is unrecorded — a physical observation
  nobody has made. The declaration lists the board features the design depends
  on instead, which is what a substitute board actually has to satisfy.
