# Build and run on the Zybo Z7-20

Preconditions: [Host setup](host_setup.md), and RouteRTL installed with the clone as the current directory, as in [Install RouteRTL and clone the repository](host_setup.md#install-routertl-and-clone-the-repository).

Built from an unpacked archive (no git history), the bitstream carries no build stamp in USR_ACCESS, so `rr hw bit-sha` cannot confirm on the board that it runs this build; a build from the clone carries the stamp.

The build runs Vivado 2024.1, so put it on `PATH` and set `XILINX_VIVADO` first, in the shell that runs the steps below. After a standard install, the installer's script does both:

```bash
INSTALL_DIR=/path/to/xilinx-install   # the directory Vivado was installed into
source "$INSTALL_DIR/Vivado/2024.1/settings64.sh"
```

An install copied to another directory, rather than installed there, keeps the original directory inside `settings64.sh`, so that script points at the wrong place. Set the two variables directly instead; no vendor file needs editing:

```bash
INSTALL_DIR=/path/to/vivado-copy   # the directory the install was copied to
export XILINX_VIVADO="$INSTALL_DIR/Vivado/2024.1"
export PATH="$XILINX_VIVADO/bin:$PATH"
```

`vivado -version` then prints `vivado v2024.1`, and step 2 checks the same version. RouteRTL finds the pinned 2024.1 release through `XILINX_VIVADO`.

```bash
# 1. Select the board, then fetch the packages its committed lock names
rr target set zybo_datalogger
rr pkg install
pip install -r requirements.txt

# 2. Check the host against the supported environment
python3 tools/preflight/ds_preflight.py --stage build

# 3. Build the bitstream (step 4 needs it)
rr synth run
rr implementation run
rr bitstream run

# 4. Build the Linux image, which embeds the bitstream, and write it to a microSD card.
#    It needs step 3's implementation: without it, the Linux build stops with
#    "Implementation checkpoint not found".
#    The Python runtime of the image is built once from upstream Buildroot (1-3 h).
#    CARD is the card's boot (FAT) partition in the host reader, for example /dev/sdb1.
#    rr linux tools install installs the host tools of this step first (it uses sudo apt-get).
rr linux tools install
sw/linux/build_buildroot.sh && sw/linux/extract_python_overlay.sh
rr linux build
CARD=/dev/sdX1
rr linux flash-sd --device "$CARD"

# 5. Boot the board from the card with Ethernet connected, then attach RouteWave.
#    BOARD_IP is the address the board takes at first boot (see First boot).
BOARD_IP=board-ip
routewave -c "stream attach $BOARD_IP:7475"
```

Host requirements of step 4, on Ubuntu and Debian:

- `rr linux tools install` installs the programs Buildroot's dependency check requires and the tools `rr linux build` needs (it uses `sudo apt-get`). Without them, `sw/linux/build_buildroot.sh` stops at its pre-check with exit 2 and names the missing programs.
- Buildroot's own dependency check (`support/dependencies/dependencies.sh` of the pinned release) requires these programs on the host: `make`, `gcc`, `g++`, `bash`, `git`, `patch`, `perl`, `tar`, `wget`, `cpio`, `unzip`, `rsync`, `bc`, `cmp`, `find`, `xargs`, `which`, `sed` and `file`. A stock Ubuntu 24.04 install lacks some of them (`cpio` was missing on a WSL host). On a host where `rr` cannot run `apt-get` (no `sudo` for it), one line installs all of them:

  ```bash
  sudo apt-get install -y build-essential cpio unzip rsync bc wget file patch perl git diffutils findutils
  ```

  Buildroot builds the other host tools it needs (`bison`, `flex`, `dtc`, `pkg-config`, OpenSSL) itself, so none of them is a host requirement of `build_buildroot.sh`.
- `rr linux tools status` lists the tools `rr linux build` needs on the host: the ARM cross-compiler (`arm-linux-gnueabihf-gcc`), `mkimage`, `dtc` and the kernel build packages. Without them, `rr linux build` stops while compiling BusyBox with `arm-linux-gnueabihf-gcc: command not found`.
- WSL: a default WSL install appends the Windows `PATH` to the Linux one, and entries such as `/mnt/c/Program Files/...` contain a space. Buildroot refuses a `PATH` with a space and stops within seconds with `Your PATH contains spaces, TABs, and/or newline characters`. This line removes the entries with whitespace from the `PATH` of the current shell; a new shell restores them:

  ```bash
  export PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '[[:space:]]' | paste -sd:)"
  ```

  It runs before `sw/linux/build_buildroot.sh`. Vivado, put on the `PATH` earlier for step 3, is not needed by step 4 itself; if its install directory contains a space, the line removes it from the `PATH` and the `XILINX_VIVADO` variable still locates it.

RouteWave shows edge records in its Edge Scope. `stream status --json` in the RouteWave console reports the connection state.

In the RouteWave console, `export <file.vcd>` saves the session, and `open <file>` opens a saved capture (VCD, FST or ltrace).

The bitstream reaches the fabric from the card at boot, so a new bitstream goes to the board in a new card image.

The first boot, the network address and SSH: [First boot](first_boot.md).
