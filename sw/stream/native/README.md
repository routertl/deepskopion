# Native edge path

Facts verified against `2aaa50b6` and this change on 2026-11-05.

`edge_drain.c` decodes the EDGE_BATCH frames of a DDR ring span in C, cuts the
edges into wire EDGE_EVENTS frames and packs each frame as msgpack. The daemon
loads it as `libds_edge_drain.so` through `edge_frames.py` (ctypes). For a run
of EDGE_BATCH frames the Python code makes one call and receives packed wire
frames, so no Python code runs per edge on the wire path. A frame that is not
a non-error EDGE_BATCH ends the native walk and takes the Python walk. The wire
contract does not change: the bytes on the wire are the same as on the Python
path.

| File | Content |
|---|---|
| `edge_drain.h`, `edge_drain.c` | `ds_edge_drain()`: the frame walk, resync and per-edge arithmetic. `ds_frames_*`: the per-batch frame cut (`Batch.add_edge`, `edge_within_reach`) and the EDGE_EVENTS packer |
| `edge_frames.py` | ctypes loader (`load()`) and `NativeEdgeFrames`, the per-batch holder `Batch.native_edges` points at |
| `edge_drain_bench.c` | Decodes a dump file, writes `(word, ts_ns)` records, times repeated passes |
| `ring_dump.py` | Generated ring dumps, the reference records from the Python drain, the daemon wire harness, and the daemon benchmark |

## Data path

| Step | Python path | Native path |
|---|---|---|
| EDGE_BATCH walk, resync, malformed batch | `DdrRingDataSource.next_batch` per frame | `ds_frames_drain`, one call per run of frames |
| First header timestamp as the PL time anchor | `next_batch` | `ds_drain_result.first_ts_ticks`, applied by `next_batch` |
| Stream-RLE words, other domains | `next_batch` | `next_batch`; RLE edges join the same native batch through `Batch.add_edge` |
| Frame cut at `EDGE_ANCHOR_REACH_US` | `Batch.add_edge` | `frames_push` |
| Wire encoding | `proto.make_edge_events` + `proto.pack_frame` | `ds_frames_pack` |
| Stream guard | `LiveConformanceGuard.check` | `LiveConformanceGuard.check_native_edge_frame` |

`check_native_edge_frame` decodes the first native frame of a session, and then
one accepted frame in `NATIVE_FULL_CHECK_EVERY` (1024), and checks every word.
Each other frame is checked on its header: tag, key order, seq, clk_mhz,
anchor_ns and word count are read from the packed bytes and compared with the
declared values, and the word bytes must number between one and five per word.
Seq order, handshake, field types and the missing-anchor rule apply to both.
`native_full_checked` and `native_header_checked` count the two, and the
session-end log line reports them.

The daemon selects the path with `--native-edge-drain auto|on|off` (default
`auto`). `auto` uses the library when it is found and prints which path runs
(`ddr source: edge frames walked and packed in C (<path>)`, or `in Python`);
`on` refuses to start without it. The library is found at `$DS_NATIVE_EDGE_LIB`,
else beside `edge_frames.py`, else at `/usr/local/lib/deepskopion/libds_edge_drain.so`
(`edge_frames.SYSTEM_LIB`).

## DE25 image

`tools/de25_linux_image.py stage` builds `edge_drain.c` with the pinned Arm GNU
Toolchain 13.3.rel1, `aarch64-none-linux-gnu`, the archive the stage already
verifies for the glibc runtime, and installs it in the Python overlay at
`/usr/local/lib/deepskopion/libds_edge_drain.so`. The Python overlay is the
image's AArch64 tree; the script overlay holds no ELF file.
`etc/deepskopion-inputs` records:

| Key | Value |
|---|---|
| `native_edge_lib` | the install path |
| `native_edge_lib_sha256` | sha256 of the installed library |
| `native_edge_source_sha256` | sha256 over `edge_drain.c` and `edge_drain.h`, each as path, NUL, bytes, NUL |
| `native_edge_toolchain`, `native_edge_toolchain_sha256` | the toolchain archive and its sha256 |

The build is reproducible: two stages give the same library sha256. The library
needs `libc.so.6` and `libm.so.6`, both in the Python overlay. A host build
beside `edge_frames.py` is not copied into any image (`STREAM_EXCLUDE` in the
stage, `--exclude='*.so'` in `sw/linux/stage_daemon.sh`).

## Zynq-7000 and PolarFire SoC images

`tools/native_edge_lib.py install --profile zybo|pfsoc` builds `edge_drain.c`
into the board's Python overlay at the same path, with that overlay's
Buildroot gcc, and records the build in `etc/deepskopion-native-edge`
(`sw/linux/README.md`, "Python in the rootfs"). The ddr targets' stage command
runs `tools/native_edge_lib.py check`, which stops the image build when the
overlay has no library built from the checkout's sources.

| Profile | Compiler | ELF | `size_t` |
|---|---|---|---|
| `zybo` | `arm-buildroot-linux-gnueabi-gcc` 13.3.0, `-mcpu=cortex-a9` | ELF32 ARM, EABI5, soft-float ABI (`e_flags` 0x5000200) | 32 bits |
| `pfsoc` | `riscv64-buildroot-linux-gnu-gcc` 13.3.0, `-march=rv64gc -mabi=lp64d` | ELF64 RISC-V, RVC, double-float ABI (`e_flags` 0x5) | 64 bits |

Both compilers are Buildroot 2024.11.4 (`c9e65c2c`) builds; the two build
scripts pin that tag as `BUILDROOT_REF`. From the
current `edge_drain.c` and `edge_drain.h` (`native_edge_source_sha256`
`5c37c4c2…`), each build gives the same library on every run:

| Profile | `native_edge_lib_sha256` |
|---|---|
| `zybo` | `0e64dbbe30bf5d465c3674ca104cbdfd4f8b0bbef09e21945638e76319642f11` |
| `pfsoc` | `fc201ad3fa467eb4df8ea5d4045d89487a2aebbdc654d043df98f9f2cd30c28e` |

A source change changes both values, and so does a change to the build flags in `tools/native_edge_lib.py`.

The riscv64 toolchain is built by `sw/linux/pfsoc/build_python_overlay.sh` (Buildroot 2024.11.4,
`buildroot_pfsoc_defconfig`) with `LD_LIBRARY_PATH` unset: Buildroot refuses an `LD_LIBRARY_PATH` that holds an
empty element. Measured 2026-11-15 on the bench host (24 jobs): `host-gcc-final` was installed 24 min after the
start, and `tools/native_edge_lib.py install --profile pfsoc` then runs with `--cc` set to the
`riscv64-buildroot-linux-gnu-gcc` that build produces.

The Zynq image's floating point is software: `edge_word` runs one double
division and one `nearbyint` per edge in libgcc and glibc soft-float code.

`tools/native_edge_cross_check.py` loads a cross-built library into the
board's own CPython under qemu-user and compares the daemon's wire bytes with
the host's Python path, for the three dumps and four clocks of
`test_native_edge_frames_ds_p1_74.py`:

```bash
python3 tools/native_edge_cross_check.py --lib <overlay>/usr/local/lib/deepskopion/libds_edge_drain.so \
    --root <overlay> --qemu qemu-arm-static        # or qemu-riscv64-static
```

A 32-bit guest needs the overlay on a filesystem with 32-bit inode numbers
(a tmpfs); on ext4 its directory reads fail and CPython finds no `encodings`.

Readers of a batch's edges use `Batch.has_edges()`, `Batch.all_edge_words()`
and `Batch.edge_wire_frames()`. On a native batch, `edge_words` and
`edge_frames` stay empty. `all_edge_words()`, `wire_edge_frames()` and the
wave_events_v1 lane build Python lists, one entry per edge.

## Build

```bash
cc -O2 -std=c11 -D_POSIX_C_SOURCE=199309L -fPIC -shared -o libds_edge_drain.so edge_drain.c -lm
cc -O2 -std=c11 -D_POSIX_C_SOURCE=199309L -o edge_drain_bench \
    edge_drain.c edge_drain_bench.c -lm
```

For the Agilex 5 HPS, the same sources with an aarch64 GNU toolchain. The bench
is statically linked; the library needs `libc.so.6` and `libm.so.6`, symbol
version GLIBC_2.17.

```bash
aarch64-linux-gnu-gcc -O2 -std=c11 -D_POSIX_C_SOURCE=199309L -mcpu=cortex-a55 \
    -fPIC -shared -o libds_edge_drain.so edge_drain.c -lm
aarch64-linux-gnu-gcc -O2 -std=c11 -D_POSIX_C_SOURCE=199309L -mcpu=cortex-a55 \
    -static -o edge_drain_bench.aarch64 edge_drain.c edge_drain_bench.c -lm
```

## Run

```bash
edge_drain_bench <dump> <sample_clk_hz> --out native.rec   # records, little-endian <IQ>
edge_drain_bench <dump> <sample_clk_hz> --repeat 200       # ns per edge, C walk only
python3 -m sw.stream.native.ring_dump <dump> <sample_clk_hz> \
    --native libds_edge_drain.so --repeat 20 --out wire.bin  # ns per edge, daemon
```

The last command runs `server._drain` over a socketpair, with the consumer in a
thread of the same process, and times the whole stream. Without `--native` it
times the Python path.

Dumps for cost figures come from `ring_dump.write_rtl_dumps(<dir>)`, which writes
`rtl_edge.bin` and `rtl_interleaved.bin`. They follow the RTL edge pipeline
(`rtl_batches`): one tick for all channels, and each channel's batch flushes at
32 entries, before an event more than 0xFFFF ticks after its anchor, or 10 000
ticks after its last event. In `rtl_interleaved.bin` the Stream-RLE words of
instance 7 carry all elapsed time (IDLE, then the edge as PACK or LONG), so the
decoded RLE edge times track the same tick, as `edge_datalogger.vhd` writes them.
`edge_dump` and `interleaved_dump` give each channel
its own tick, so the channels drift apart; they are stress fixtures for the
native-vs-Python equality tests and are not used for cost figures.

Tests:

| Test file | Compares |
|---|---|
| `sw/stream/tests/test_native_edge_drain_ds_p1_74.py` | C records with the Python drain's `Batch.add_edge` arguments |
| `sw/stream/tests/test_native_edge_frames_ds_p1_74.py` | The whole wire stream of `server._drain`, native and Python, at 100, 125, 80 and 33 MHz, on the edge, mixed and interleaved dumps; the contract `check_stream` over the native bytes; the packer at every msgpack size boundary; the reach cut at its boundary; the guard's native-frame check; Python calls per edge |

## Measured

x86-64 dev host: Ryzen 9 5900X, load average 30 to 50, one pinned core. These
are stand-in figures. The figures that decide the design are measured on the
DE25 HPS.

| Path | Dump | ns per edge, best |
|---|---|---|
| C walk only (`edge_drain_bench`) | 4096 frames, 143360 edges | 6.8 |
| Native: source walk, in `next_batch` | same | 11.5 |
| Native: packing | same | 4 |
| Native: guard header checks | same | 2.5 |
| Native: whole daemon stream, consumer thread in process | same | 61 |
| Python: whole daemon stream | same | 2930 |
| Native: whole daemon stream | interleaved, 9000 frames, 285459 edges | 187 |
| Python: whole daemon stream | same | 3185 |

| Quantity | Value |
|---|---|
| Guard full check, per word | 213 to 419 ns |
| Guard header check, per frame, steady state | 6 to 8 µs at 6000 and 100000 words |
| Python calls per added edge, native path | -0.007 (per-frame calls only) |
| Python calls per added edge, Python path | 14.0 |

The 61 ns daemon figure includes the full check of the session's first frame,
about a twenty-fourth of the dump's edges, and the consumer thread. The
interleaved dump puts 10 % of its frames through the Python walk and is cut into
1132 frames.

### Zybo-Z7-20 (DS-ZYBO-TPUT-001)

Measured 2026-11-05 on the Zybo-Z7-20: Zynq-7020, two Cortex-A9 cores
(BogoMIPS 333.33), Linux 6.6.80-xilinx. The image is the `canfd_demo` uImage
`7aa8bacc…` built from `5d496202`. It was booted over TFTP with the PL that the
`canfd_demo` slot's `BOOT.bin` (`6ebc1e1b…`) loads; the daemon reports bitstream
GIT_HASH `0x5b3aad42`. `etc/deepskopion-native-edge` on the board shows
`native_edge_source_sha256` `58ebff51…` and `native_edge_lib_sha256`
`c3000f86…`; the installed library has that sha256. The daemon ran with
`--source ddr` and used 0 % CPU during the runs; load average 0.60. The dumps and
the host reference files are the DE25 run's.

| Path | Dump | ns per edge, best | ns per edge, mean | Wire bytes equal to the host's |
|---|---|---|---|---|
| C walk only, soft-float division (`edge_drain_bench.armv7`) | 4096 frames, 143360 edges | 1170.0 | 1183.4 | records equal |
| C walk only, VFP division (`edge_drain_bench.armv7-vfp`) | same | 1008.4 | 1018.4 | records equal |
| Native: whole daemon stream, image library | same | 2555.4 | 2578.8 | yes |
| Python: whole daemon stream | same | 129816.4 | 129976.1 | not compared |
| Native: whole daemon stream, image library | interleaved, 9000 frames, 285459 edges | 7498.3 | 7514.5 | yes |

| Quantity | Value |
|---|---|
| Soft-float division cost per edge (soft-float best minus VFP best) | 161.5 ns |
| Native daemon path against the 347 ns per-edge budget | 7.4 times the budget |
| C walk alone with VFP division against the same budget | 2.9 times the budget |
| Guard full check, per word | 5668 ns (100000 words: 566.8 ms best) |
| Guard header check, per frame | 590.5 µs |
| Daemon start line | `ddr source: edge frames walked and packed in C (/usr/local/lib/deepskopion/libds_edge_drain.so)` |

`nearbyint` runs in soft-float libm in both builds, so its cost is not in the
161.5 ns. At the floor rate a wire frame spans up to 32.768 ms of edges; one
100000-word full check takes 566.8 ms on this board.

## Items to confirm

1. ns per edge on the DE25 HPS: `edge_drain_bench.aarch64`, and the daemon
   benchmark with the aarch64 `libds_edge_drain.so`, against the 347 ns per-edge
   budget at the 14.4 MB/s floor.
2. The stall of one sampled full check on the HPS. At the floor rate a wire
   frame holds up to 32.768 ms of edges, about 100000 words; on x86 its full
   check takes 21 to 42 ms.
3. The start line `edge frames walked and packed in C` on the DE25, from an
   image built by the stage above.
