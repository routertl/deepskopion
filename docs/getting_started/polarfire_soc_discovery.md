# Build and run on the PolarFire SoC Discovery Kit

RouteRTL installed in a Python 3.11 virtual environment, that environment active, and the clone as the current
directory, as in [Install RouteRTL and clone the repository](host_setup.md#install-routertl-and-clone-the-repository).

Preconditions:

| Item | Required value |
|---|---|
| Libero SoC | 2025.2, with a licence that covers the MPFS095T |
| Kit eNVM | the Hart Software Services (HSS) of Microchip's `polarfire-soc-discovery-kit-reference-design`, programmed once with that release's FlashPro Express job. The DeepSkopion build does not write the eNVM. |
| microSD card | at least 1 GB, in a card reader on the build host |
| RISC-V compiler (step 6) | a bare-metal RISC-V GCC with a C library (newlib) on `PATH`, for the Hart Software Services. Ubuntu's `gcc-riscv64-unknown-elf` package has no C library. Such a compiler comes for example with Microchip SoftConsole or AMD Vitis. |
| Host libraries (step 3) | on Ubuntu 24.04: `sudo apt-get install -y libglib2.0-0t64 libxslt1.1` (the Libero build's PS configuration step needs them) |

The card image alone (no kit attached) needs steps 1-2, then `rr project create` in place of step 3 (it generates
the MSS component the boot chain is built from), then steps 5-7. Step 4 needs the kit and a lease on it.

The kit's MSS component is generated locally from the committed configuration ([`hw/libero/pfsoc/mss/README.md`](https://github.com/routertl/deepskopion/blob/main/hw/libero/pfsoc/mss/README.md)).

```bash
# 1-2. Select the board, fetch its packages, and check the host against the supported environment
rr target set pfsoc_datalogger
unset RR_TARGET    # an exported RR_TARGET would override the target set above
rr pkg install
pip install -r requirements.txt
python3 tools/preflight/ds_preflight.py --stage build

# 3. Build the FPGA design (Libero SoC); the result is a FlashPro Express job
rr synth run
rr implementation run
rr bitstream run

# 4. Program the fabric and its design-initialisation data over the kit's embedded FlashPro5,
#    under a lease on the kit (RouteRTL refuses a hardware command without one)
rr lease run polarfire-soc-discovery -- rr program run

# 5-7. Build the riscv64 Python runtime of the image once from upstream Buildroot (1-3 h), then the boot chain,
#    kernel, root file system and card image, then write the whole card image. The commands are chained, so a failed
#    step stops the ones after it and an older image is never written.
#    rr linux tools install installs the host tools of steps 5 and 6 (it uses sudo apt-get).
#    Known issues on RouteRTL 5.7.0: step 6 stops with "Could not allocate block in ext2 filesystem" (rr linux build
#    makes a 32 MiB root file system for about 53 MiB of files); without a RISC-V compiler that has a C library, the
#    Hart Software Services build stops on a missing "assert.h". No workaround is verified on 5.7.0.
#    CARD is the whole card (`lsblk` lists it as TYPE disk), with none of its partitions mounted. The last command
#    shows the card, asks for its serial (or its kernel name when it reports none) before it erases anything,
#    refuses a mounted or non-removable disk, and reads the card back.
CARD=/dev/sdX
rr linux tools install &&
  sw/linux/pfsoc/build_python_overlay.sh &&
  rr linux build &&
  rr linux flash-sd --image --device "$CARD"

# 8. Put the card in the kit (J17), power it on, then attach RouteWave.
#    BOARD_IP is the address the kit takes at first boot (see First boot).
#    RW is RouteWave as `rr ws update-viewer` installs it (README, RouteWave).
BOARD_IP=board-ip
RW=~/.routertl/bin/routewave/routewave
"$RW" -c "stream attach $BOARD_IP:7475"
```

The fabric stays programmed across power cycles; step 4 is repeated only for a new FPGA build. The card holds the
kernel, the root file system and the capture daemon. The first boot follows [First boot](first_boot.md).
