# Redistribution of third-party and vendor-generated files

Facts verified against `72e8aeee` on 2026-11-02.

Each row gives one verdict for one path pattern in the source tree.

| Verdict | Meaning |
|---|---|
| SHIP | In the public tree, under the stated licence. |
| REGENERATE | Not in the public tree. The stated `rr` command recreates the file. |
| DROP | Not in the public tree. |
| OPEN | Verdict not decided. See Items to confirm. |

In a pattern, `*` matches within one path segment and `**` matches across segments.

## Third-party and generated files in the public tree

| Path | Verdict | Basis |
|---|---|---|
| `hw/rtl/regbank/regbank_schema.bin` | SHIP | CERN-OHL-W-2.0. Output of this project's generator, from `project_regbank.yml`. |
| `libs/routertl/**` | SHIP | MIT. Declared in `REUSE.toml`. Text in `LICENSES/MIT.txt`. |
| `sw/linux/rootfs_overlay/usr/lib/python3.12/site-packages/smbus2/**` | SHIP | MIT. Copyright Karl-Petter Lindegaard, stated in the file header. |
| `sw/linux/rootfs_overlay/usr/lib/python3.12/site-packages/smbus2-0.6.1.dist-info/**` | SHIP | MIT. Package metadata with the licence text. |

## Intel and Altera files for the DE25 target

The source tree holds no Intel or Altera generated file. `ip.yml` declares the
Agilex 5 HPS, its io96b EMIF and the fabric IOPLL by parameter
(`hps_regenerate: true`, `vendor_ip_variants:`). `rr synth run` makes the IP
variants and their synthesis trees under the user's own Quartus Prime Pro, in
`build/rr_ip_regen/`, which git ignores.

## Binaries built from this tree for the DE25 image

Facts verified against `2aaa50b6` and this change on 2026-11-05.

`tools/de25_linux_image.py stage` compiles one binary from this project's own
source, with the pinned Arm GNU Toolchain 13.3.rel1.

| Image path | Source | Licence | Links against |
|---|---|---|---|
| `/usr/local/lib/deepskopion/libds_edge_drain.so` | `sw/stream/native/edge_drain.c`, `sw/stream/native/edge_drain.h` | MPL-2.0, source in this repository | `libc.so.6` and `libm.so.6` (glibc, shipped in the same image) |

## Binaries built from this tree for the Zynq-7000 and PolarFire SoC images

Facts verified against this change on 2026-11-05.

`tools/native_edge_lib.py install` compiles one binary from this project's own
source into each Buildroot Python overlay, with that overlay's toolchain. No
build of it is committed; `etc/deepskopion-native-edge` in the overlay records
its sha256 and its compiler.

| Image path | Source | Licence | Links against |
|---|---|---|---|
| `/usr/local/lib/deepskopion/libds_edge_drain.so` | `sw/stream/native/edge_drain.c`, `sw/stream/native/edge_drain.h` | MPL-2.0, source in this repository | `libc.so.6` and `libm.so.6` (glibc, shipped in the same overlay) |

## PolarFire SoC

The PolarFire SoC Discovery Kit is a supported board. Its RTL, constraints,
Libero scripts, target files and Linux build ship under this project's licence.
The rows below cover the Microchip files that ship and the files that do not.

| Path | Verdict | Basis |
|---|---|---|
| `hw/libero/pfsoc/mss/reference/MPFS_DISCOVERY_KIT_MSS.cfg` | SHIP | MIT. Copyright 2023 Microchip Corporation, text in `hw/libero/pfsoc/mss/reference/LICENSE.md`. Copied unchanged from Microchip's `polarfire-soc-discovery-kit-reference-design` at `f09b6e0`. |
| `hw/libero/pfsoc/mss/disco_mss.cfg` | SHIP | MIT. The reference configuration with the changes listed in `hw/libero/pfsoc/mss/mss_deltas.yml`. |
| `hw/libero/pfsoc/mss/disco_mss.cxz` | DROP | Output of the Libero MSS Configurator. Generated from `disco_mss.cfg` with `pfsoc_mss -GENERATE`, as `hw/libero/pfsoc/mss/README.md` states. |
| `hw/libero/pfsoc/mss/BRINGUP_HISTORY.md` | DROP | Measurement record. |
| `out/**` | DROP | Libero and FlashPro output, and its digests. |
| `sw/linux/pfsoc/python_overlay.tar.gz` | DROP | Git LFS object. A public repository carries no LFS pointer. Not published (item 1): `sw/linux/pfsoc/build_python_overlay.sh` builds it from upstream sources. |
| `debug/pfsoc_*.yml` | DROP | Bench debug configuration. |
| `bench/**` | DROP | Bench targets, fixtures and their contract sidecar. Not part of the distribution. |
| `targets/canfd_demo.ip.lock` | DROP | Package lock of a bench target (rr keeps per-target locks in targets/). |
| `targets/de25_canfd.ip.lock` | DROP | Package lock of a bench target (rr keeps per-target locks in targets/). |
| `tools/tests/test_de25_shell_contract_bench.py` | DROP | Test of a bench-only testbench (bench/sim/tb_lwh2f_ctucanfd_probe.vhd). |
| `targets/canfd_demo.routertl.lock` | DROP | Build lock of a bench target (rr keeps per-target locks in targets/). |
| `targets/classic_eye.routertl.lock` | DROP | Build lock of a bench target (rr keeps per-target locks in targets/). |
| `targets/de25_canfd.routertl.lock` | DROP | Build lock of a bench target (rr keeps per-target locks in targets/). |
| `targets/de25_core_baseline.routertl.lock` | DROP | Build lock of a bench target (rr keeps per-target locks in targets/). |
| `targets/de25_p2writer.routertl.lock` | DROP | Build lock of a bench target (rr keeps per-target locks in targets/). |
| `targets/eye_loopback.routertl.lock` | DROP | Build lock of a bench target (rr keeps per-target locks in targets/). |
| `targets/pfsoc_fic0_rea.routertl.lock` | DROP | Build lock of a bench target (rr keeps per-target locks in targets/). |
| `targets/pfsoc_ka_a.routertl.lock` | DROP | Build lock of a bench target (rr keeps per-target locks in targets/). |
| `targets/pfsoc_ka_b.routertl.lock` | DROP | Build lock of a bench target (rr keeps per-target locks in targets/). |
| `targets/pfsoc_ka_rea.routertl.lock` | DROP | Build lock of a bench target (rr keeps per-target locks in targets/). |
| `sw/linux/kernel_ctucanfd.config-fragment` | DROP | Kernel configuration for the CTU CAN FD driver. Used only by a bench target. |
| `sw/linux/kernel_patches/ctucanfd_ntxbufs.sh` | DROP | Kernel patch for the CTU CAN FD driver. Used only by a bench target. |
| `sw/linux/tests/test_ctucanfd_ntxbufs_patch_ds_p2_67.py` | DROP | Test of the CTU CAN FD kernel patch, which is not published. |
| `hw/sim/cocotb/can_ctu_oracle.py` | DROP | Python port of CTU CAN FD core code. |
| `hw/sim/cocotb/tests/datalogger/can/ds_t2_97_ctu_two_node_wire.json` | DROP | Wire record produced by the CTU CAN FD testbench, which is not published. |
| `hw/sim/cocotb/tests/datalogger/can/test_ctu_two_node_wire_ds_t2_97.py` | DROP | Test that runs a CTU CAN FD testbench, which is not published. |
| `hw/sim/cocotb/tests/datalogger/can/test_ctu_two_txtb_ds_p2_67.py` | DROP | Test that runs a CTU CAN FD testbench, which is not published. |
| `hw/sim/cocotb/tests/datalogger/can/test_can_datalogger_ctu_wire_ds_t2_97.py` | DROP | Test that reads the CTU CAN FD wire record, which is not published. |
| `hw/sim/cocotb/tests/datalogger/can/test_ctu_stuff_oracle.py` | DROP | Test of the CTU CAN FD Python port, which is not published. |
| `hw/xdc/pblock_ctu.xdc` | DROP | Floorplan for the CTU CAN FD cores. Used only by a bench target. |
| `sw/can/ctu_canfd.py` | DROP | Driver for the CTU CAN FD core. Used only with bench targets. |
| `sw/can/tests/test_ctu_canfd.py` | DROP | Test of the CTU CAN FD driver, which is not published. |
| `sw/stream/tests/test_arm_datalogger_defaults_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `sw/stream/tests/test_overlay_in_sync_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `tools/tests/test_baseline_scripts_name_their_target_ds_p2_273_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `tools/tests/test_ctu_package_lock_scope_ds_p1_103_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `tools/tests/test_de25_core_baseline_contract_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `tools/tests/test_pfsoc_ka_silicon_p2_132_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `tools/tests/test_pkg_install_clean_clone_p3_150_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `tools/tests/test_redistribution_verdicts_ds_p2_196_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `tools/tests/test_ref_design_manifest_p2_119_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `tools/tests/test_sim_sources_clean_checkout_ds_p2_204_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `tools/tests/test_target_files_ds_p1_103_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `tools/tests/test_target_xdc_rea_contract_ds_p3_168_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `sw/linux/de25/test_de25_linux_image_bench.py` | DROP | Bench-target tests split from the public test file of the same name. Not part of the distribution. |
| `tools/tests/test_canfd_demo_can_datalogger_ds_p2_218.py` | DROP | Tests of the CTU CAN FD bench target canfd_demo, which is not published. |
| `tools/tests/test_de25_canfd_target_ds_p2_135.py` | DROP | Tests of the CTU CAN FD bench target de25_canfd, which is not published. |
| `tools/tests/test_can_opt_in_targets_carry_can_datalogger_ds_p3_211.py` | DROP | Tests of the CTU CAN FD bench targets, which are not published. |
| `tools/tests/test_eye_loopback_buildability_ds_p3_172.py` | DROP | Tests of the eye_loopback bench target, which is not published. |
| `tools/tests/test_pfsoc_mss_wrapper_isolation_ds_p3_174.py` | DROP | Test of the pfsoc_mss bench target, which is not published. |
| `docs/design/pfsoc_port_plan.md` | DROP | Bring-up planning document. |
| `docs/hardware/pf-l0*/**` | DROP | Measurement record. |
| `tools/pfsoc_stall_recover.py` | DROP | Bench tool. Its defaults name one bench's console port and power control. |
| `tools/tests/test_pfsoc_frozen_mss_cxz_ds_p1_81.py` | DROP | Test of `disco_mss.cxz`, which is not published. |
| `tools/tests/test_pfsoc_ring_ddr_alias_ds_p1_62.py` | DROP | Test of `disco_mss.cxz`, which is not published. |
| `tools/tests/test_pfsoc_port_plan.py` | DROP | Test of the bring-up planning document. |
| `tools/tests/test_pfsoc_stall_recover_ds_p1_63.py` | DROP | Test of a bench tool that is not published. |
| `tools/tests/test_pf_l0b_*.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_cdc_evidence_ds_t1_22.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_constraint_evidence_ds_t1_18.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_ddr_progress_ds_p2_145.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_fabric_program_p2_144.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_hss_envm_job_ds_p2_145.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_iomux_console_mux_ds_p2_145.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_ioscb_snapshot_ds_p2_145.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_l2lim_backing_p2_143.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_mmio_routewave_p2_128.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_mss_stage_p2_142.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_rea_mmio_rr_capture_ds_p2_128.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_scratchpad_firmware_composed_p2_143.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_hss_l2lim_evidence_ds_p1_81.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_ka_silicon_evidence_ds_p1_81.py` | DROP | Test of a measurement record. |
| `tools/tests/test_pfsoc_overlay_asset_ds_p1_81.py` | DROP | Test of `sw/linux/pfsoc/python_overlay.tar.gz`, which is not published. |
| `tools/tests/test_pfsoc_rootfs_overlay_ds_p2_146.py` | DROP | Test of `sw/linux/pfsoc/python_overlay.tar.gz`, which is not published. |
| `tools/tests/test_redistribution_python_tarballs.py` | DROP | Test of the Zynq-7000 overlay built by `sw/linux/extract_python_overlay.sh` and of `sw/linux/pfsoc/python_overlay.tar.gz`, which are not published. |
| `tools/tests/test_pfsoc_golden_path_link_ds_p1_81.py` | DROP | Test of `docs/GOLDEN_PATH.md`, which is not published. |

## Measurement records

These records stay in the private repository. They include captures from hardware and reports from vendor tools.

| Path | Verdict | Basis |
|---|---|---|
| `docs/evidence/**` | DROP | Measurement record. Includes a dump of Microchip eNVM contents. |
| `docs/hardware/evidence/**` | DROP | Measurement record. |
| `docs/DS-*.md` | DROP | Measurement record. |
| `tools/datalogger-builder/tests/fixtures/*.rpt` | DROP | Vendor tool report. It carries a vendor copyright header and no licence grant. |
| `tools/datalogger-builder/tests/fixtures/pfsoc_compile_netlist_hier_resources.csv` | DROP | Vendor tool report (Libero SoC compile netlist resources). No licence grant. |
| `tools/datalogger-builder/tests/test_cli_configurator_pfsoc.py` | DROP | Test of the Libero fixtures above, which are not published. |
| `tools/tests/fixtures/ds_t1_20/*.rpt` | DROP | Vendor tool report. It carries a vendor copyright header and no licence grant. |
| `tools/tests/data/**` | DROP | Measurement record. |
| `sw/stream/tests/test_ds_p1_61_proto_stats_build_selection.py` | DROP | Test of a measurement record. |
| `sw/stream/tests/test_edge4_live_evidence_ds_p1_71.py` | DROP | Test of a measurement record. |
| `sw/stream/tests/test_edge_wire_identity_live_ds_t2_84.py` | DROP | Test of a measurement record. |
| `sw/stream/tests/test_regbank_word_store_pfsoc_ds_t2_82.py` | DROP | Test of a measurement record. |
| `tools/tests/test_de25_hw_contract_silicon_p2_165.py` | DROP | Test of a measurement record. |
| `tools/tests/test_de25_drain_silicon_ds_p2_267.py` | DROP | Test of a measurement record. |
| `tools/tests/test_ds_p1_79_de25_silicon_witness.py` | DROP | Test of a measurement record. |
| `tools/tests/test_ds_p2_190_de25_silicon_witness.py` | DROP | Test of a measurement record. |
| `tools/tests/test_witness_pin_gate_ds_p3_184.py` | DROP | Test of a measurement record. |
| `tools/tests/test_cross_link_rollout_ds_p2_44.py` | DROP | Test of a measurement record. |
| `tools/tests/test_datalogger_mix_synthesis_ds_p2_163.py` | DROP | Test of a measurement record. |
| `tools/tests/test_de25_generated_sdc_waivers_ds_t1_20.py` | DROP | Test of a measurement record. |
| `tools/tests/test_ds_p2_178_zybo_cdc_evidence.py` | DROP | Test of a measurement record. |
| `tools/tests/test_ds_p2_191_zybo_timing_evidence.py` | DROP | Test of a measurement record. |
| `tools/tests/test_ds_t1_12_repin_silicon_witness.py` | DROP | Test of a measurement record. |
| `tools/tests/test_hosting_decision_ds_p2_42.py` | DROP | Test of a measurement record. |
| `tools/tests/test_hw_run_hps_devmem_ds_t2_88.py` | DROP | Test of a measurement record. |
| `tools/tests/test_regbank_preamble_ds_t2_87.py` | DROP | Test of a measurement record. |
| `tools/tests/test_timing_constraint_audit_ds_p1_72.py` | DROP | Test of a measurement record. |
| `tools/tests/test_vendor_cdc_evidence_ds_t1_22.py` | DROP | Test of a measurement record. |
| `tools/datalogger-builder/tests/test_cli_configurator_fit_reports.py` | DROP | Test of a measurement record. |
| `tools/tests/test_clean_clone_rehearsal_record_p2_131.py` | DROP | Test of a measurement record. |
| `tools/tests/test_de25_constraint_evidence_doc_ds_t1_19.py` | DROP | Test of a measurement record. |
| `tools/tests/test_public_artifact_hygiene_evidence_ds_p1_45.py` | DROP | Test of a measurement record. |
| `context.json` | DROP | Private project state. No third-party content. |

## Python runtime for the Zynq-7000 image

The Zynq-7000 overlay is not in the tree. `sw/linux/build_buildroot.sh` and
`sw/linux/extract_python_overlay.sh` build it from upstream sources into the
ignored directory `sw/linux/python_overlay/` (item 1). No tarball of it is kept.

## Contents of the Python runtime tarballs

Facts verified against `c53b9bd5` on 2026-11-16 (armhf, the overlay built by the
scripts below) and `45f1ca2f` on 2026-11-04 (riscv64).

Two runtimes carry the board-side runtime of the capture daemon. Both are
Buildroot 2024.11.4 builds: the armhf overlay directory that
`sw/linux/build_buildroot.sh` and `sw/linux/extract_python_overlay.sh` build,
and the riscv64 tarball of `sw/linux/pfsoc/build_python_overlay.sh`. Neither is
published (item 1); the table states what an overlay built by those scripts
contains.

| Tarball | Architecture | sha256 | Entries |
|---|---|---|---|
| `sw/linux/python_overlay` | armhf (Zynq-7000) | derived (a directory built on the user's host; no sha256) | 725: 651 files, 16 links, 58 directories |
| `sw/linux/pfsoc/python_overlay.tar.gz` | riscv64 (PolarFire SoC) | `f11405bcb31094e020b4c327a5a04c61c91e11d313c9f86b98feee43ef9b9221` | 671: 606 files, 17 links, 48 directories |

Every file of the armhf overlay but three is byte-identical to the Buildroot
2024.11.4 target tree (`c9e65c2c`) it was extracted from. The three:
`usr/lib/python3.12/_sysconfigdata__linux_arm-linux-gnueabi.pyc`, rewritten by
`sw/linux/clean_python_overlay.py --fix` to drop build-host paths, and the two
project files of the native edge library row. Each other file maps to one Buildroot
package through that tree's `packages-file-list.txt`. The 537 `.pyc` files
outside the list are compiled from the `python3`, `python-serial` and
`python-msgpack` sources. The riscv64 build tree is not kept. Its versions
below are read from the binaries and match Buildroot 2024.11.4.

Each Files cell lists globs: `*` within one path segment, `**` across segments,
and a leading `!` excludes; the last matching glob in a cell decides.
`tools/tests/test_redistribution_python_tarballs.py` holds every file of each
runtime to exactly one row whose Tarball column includes it, and every glob to
a file. A checkout that has not built the armhf overlay checks the riscv64 rows
only and reports the armhf half as skipped.

| Component | Version | Files | Tarball | Licence | Source obligation |
|---|---|---|---|---|---|
| glibc | 2.40-18-g5641780762 | `lib/ld-linux*.so*`, `lib/libc.so.6`, `lib/libm.so.6`, `lib/libdl.so.2`, `lib/libpthread.so.0`, `lib/librt.so.1`, `lib/libresolv.so.2`, `lib/libanl.so.1`, `lib/libutil.so.1`, `lib/libnss_dns.so.2`, `lib/libnss_files.so.2` | both | LGPL-2.1-or-later (library), with BSD-3-Clause and MIT parts | Corresponding source with the binaries, or a written offer (LGPL-2.1 section 6) |
| GCC runtime | 13.3.0 | `lib/libgcc_s.so*`, `lib/libatomic.so*`, `usr/lib/libstdc++.so*` | both | GPL-3.0-or-later WITH GCC-exception-3.1 | Corresponding source with the binaries, or a written offer (GPL-3.0 section 6) |
| libxcrypt | 4.4.36 | `usr/lib/libcrypt.so*` | both | LGPL-2.1-or-later | Corresponding source with the binaries, or a written offer |
| iproute2 | 6.11.0 | `sbin/ip` | armhf | GPL-2.0-or-later | Corresponding source with the binaries, or a written offer valid for three years (GPL-2.0 section 3) |
| can-utils | 2023.03 | `usr/bin/can*` (7 programs) | armhf | BSD-3-Clause OR GPL-2.0-only | None under BSD-3-Clause. Notice and licence text |
| CPython | 3.12.10 | `usr/bin/python3*`, `usr/lib/libpython3*.so*`, `usr/lib/python3.12/**`, `!usr/lib/python3.12/site-packages/**`, `usr/lib/python3.12/site-packages/README.txt` | both | PSF-2.0, with the notices of incorporated software in the Python licence | None. Notice and licence text |
| OpenSSL | 3.4.1 | `usr/lib/libssl.so*`, `usr/lib/libcrypto.so*` | both | Apache-2.0 | None. Licence text |
| zlib | 1.3.1 | `usr/lib/libz.so*` | both | Zlib | None |
| bzip2 | 1.0.8 | `usr/lib/libbz2.so*` | both | bzip2-1.0.6 | None. Notice in the documentation |
| libffi | 3.4.6 | `usr/lib/libffi.so*` | both | MIT | None. Notice and licence text |
| msgpack | 1.1.0 | `usr/lib/python3.12/site-packages/msgpack/**`, `usr/lib/python3.12/site-packages/msgpack-1.1.0.dist-info/**` | both | Apache-2.0 | None. Licence text |
| pyserial | 3.5 | `usr/lib/python3.12/site-packages/serial/**`, `usr/lib/python3.12/site-packages/pyserial-3.5.dist-info/**` | armhf | BSD-3-Clause | None. Notice and licence text |
| spidev | 3.6 | `usr/lib/python3.12/site-packages/spidev.cpython-*.so`, `usr/lib/python3.12/site-packages/spidev-3.6.dist-info/**` | armhf | MIT | None. Notice and licence text |
| DeepSkopion native edge library | this repository | `usr/local/lib/deepskopion/libds_edge_drain.so`, `etc/deepskopion-native-edge` | armhf | MPL-2.0 (`sw/stream/native/edge_drain.c`); the stamp is a generated record | Source in this repository |
| Dropbear | 2024.86 | `usr/sbin/dropbear`, `usr/bin/dbclient` | riscv64 | MIT, BSD-2-Clause, Unlicense, WTFPL, public domain | None. Notice and licence text |
| OpenSSH | 9.9p2 | `usr/bin/ssh`, `usr/bin/scp` | riscv64 | BSD-3-Clause, BSD-2-Clause, public domain | None. Notice and licence text |

Buildroot `make legal-info` at 2024.11.4 writes the licence text and the source
archive of every package above to `legal-info/licenses/` and
`legal-info/sources/`, with `manifest.csv` giving each package's version and
licence. Measured 2026-11-04: 308 MB of sources for the armhf configuration and
310 MB for `sw/linux/pfsoc/buildroot_pfsoc_defconfig`. Both manifests list the
versions in the table above.

## Contents of the DE25 image's Python runtime

Facts verified against `b8e1de04` on 2026-11-05, read from an overlay staged by
`tools/de25_linux_image.py stage` and from the pinned inputs it verifies.

The SocketCAN tool rows (iproute2, can-utils) are verified against `b37512e5`
and this change on 2026-11-07, read from the upstream sources and the staged
binaries.

The DE25 image is not built from a committed tarball. `stage` downloads and
checks five pinned archives and writes two overlays: the Python overlay
(`sw/linux/de25/python_overlay`, laid over the image root) and the script
overlay (`sw/linux/de25/rootfs_overlay`). The script overlay holds no ELF file.

| Input | sha256 |
|---|---|
| `cpython-3.13.1+20241219-aarch64-unknown-linux-gnu-install_only.tar.gz` (python-build-standalone 20241219) | `c991fe7da0fdcdc35738169c02b966d23e69dbf3e33a6dd73d157fc9d14325ad` |
| `arm-gnu-toolchain-13.3.rel1-x86_64-aarch64-none-linux-gnu.tar.xz` (glibc runtime, and the compiler of `libds_edge_drain.so`) | `322f0b4482fc0d9fa0bb468134841f08d8c554c54ff5aa29a13a7a24bf7e1eb5` |
| `msgpack-1.1.1.tar.gz` | `77b79ce34a2bdab2594f490c8e80dd62a02d650b91a75159a63ec413b8d104cd` |
| `iproute2-6.18.0.tar.xz` (from `mirrors.edge.kernel.org/pub/linux/utils/net/iproute2/`, digest as in that directory's signed `sha256sums.asc`) | `6ba520e1975e4c50dc931eeae91ea37c198b8a173744885f8895b84325f9d456` |
| `can-utils-v2025.01.tar.gz` (GitHub archive of tag `v2025.01` of `linux-can/can-utils`) | `88b30feace372c4a61e6adb2534791c0cfba6123ebcca7f59bbb76580d4a6915` |

ELF files in the Python overlay, by image path. `tools/tests/test_redistribution_de25_overlay.py`
holds this list equal to the ELF files of a staged overlay.

| Image path | Component |
|---|---|
| `/bin/python3.13` | CPython |
| `/lib/libpython3.13.so.1.0` | CPython, with the libraries linked into it below |
| `/lib/libpython3.so` | CPython |
| `/lib/python3.13/config-3.13-aarch64-linux-gnu/python.o` | CPython (object file for embedding, not loaded at run time) |
| `/lib/ld-linux-aarch64.so.1` | glibc |
| `/lib/libc.so.6` | glibc |
| `/lib/libm.so.6` | glibc |
| `/lib/libpthread.so.0` | glibc |
| `/lib/librt.so.1` | glibc |
| `/lib/libdl.so.2` | glibc |
| `/lib/libutil.so.1` | glibc |
| `/usr/local/lib/deepskopion/libds_edge_drain.so` | this project, see "Binaries built from this tree for the DE25 image" |
| `/usr/sbin/ip` | iproute2 |
| `/usr/bin/cansend` | can-utils |
| `/usr/bin/candump` | can-utils |
| `/usr/bin/cangen` | can-utils |

No GCC runtime library is shipped: no ELF file above lists `libgcc_s.so.1` as
needed.

Components. The versions and licences of the libraries linked into
`libpython3.13.so.1.0` are those of python-build-standalone's download manifest
at tag 20241219 (`pythonbuild/downloads.py`, sha256
`2f6f99c8412af89de53e386e03f4c8095ac7b35474402adc80eb399686ea52d6`). The version
strings in the staged library agree, except where noted.

| Component | Version | Files | Licence | Source obligation |
|---|---|---|---|---|
| glibc | 2.38 (Arm build) | `/lib/ld-linux-aarch64.so.1`, `libc`, `libm`, `libpthread`, `librt`, `libdl`, `libutil` | LGPL-2.1-or-later (library), with BSD-3-Clause and MIT parts | Corresponding source with the binaries, or a written offer (LGPL-2.1 section 6) |
| CPython | 3.13.1 | `/bin/python3.13`, `/lib/libpython3*`, `/lib/python3.13/**` | Python-2.0, CNRI-Python, with the notices of incorporated software in `lib/python3.13/LICENSE.txt` | None. Notice and licence text |
| OpenSSL | 3.0.15 | in `libpython3.13.so.1.0` | Apache-2.0 | None. Licence text |
| SQLite | 3.47.1 | in `libpython3.13.so.1.0` | Public domain (manifest gives no SPDX id) | None |
| zlib | 1.2.13 | in `libpython3.13.so.1.0` | Zlib | None |
| bzip2 | 1.0.8 | in `libpython3.13.so.1.0` | bzip2-1.0.6 | None. Notice in the documentation |
| liblzma (XZ Utils) | 5.2.12 | in `libpython3.13.so.1.0` | Public domain (manifest gives no SPDX id) | None |
| libffi | 3.4.6 | in `libpython3.13.so.1.0` | MIT | None. Notice and licence text |
| Expat | 2.6.3 | in `libpython3.13.so.1.0` | MIT | None. Notice and licence text |
| mpdecimal | 4.0.0 | in `libpython3.13.so.1.0` | BSD-2-Clause | None. Notice and licence text |
| ncurses | 6.5 (20240427) | in `libpython3.13.so.1.0` | X11 | None. Notice and licence text |
| libedit | 20210910-3.1 | in `libpython3.13.so.1.0`, behind the `readline` module | BSD-3-Clause | None. Notice and licence text |
| libuuid | 1.0.3 | in `libpython3.13.so.1.0` | BSD-3-Clause | None. Notice and licence text |
| Berkeley DB | 6.0.19 | in `libpython3.13.so.1.0` | Sleepycat, per the manifest (item 3) | Under Sleepycat: information on how to obtain the complete source of Berkeley DB and of the software that uses it |
| Tcl | 8.6.12 | in `libpython3.13.so.1.0`, behind `_tkinter` | TCL | None. Notice and licence text |
| Tk | 8.6.12 | in `libpython3.13.so.1.0`, behind `_tkinter` | TCL | None. Notice and licence text |
| libX11 | 1.6.12 | in `libpython3.13.so.1.0`, behind `_tkinter` | MIT, X11 | None. Notice and licence text |
| libxcb | 1.14 | in `libpython3.13.so.1.0`, behind `_tkinter` | MIT | None. Notice and licence text |
| libXau | 1.0.11 | in `libpython3.13.so.1.0`, behind `_tkinter` | MIT | None. Notice and licence text |
| msgpack | 1.1.1, pure Python | `/lib/python3.13/site-packages/msgpack/**` (script overlay) | Apache-2.0 (`COPYING`) | None. Licence text |
| iproute2 | 6.18.0, built by `stage` with the Arm GNU Toolchain 13.3.rel1, linked against `libc.so.6` | `/usr/sbin/ip` | GPL-2.0 (`COPYING`); the `ip/` and `lib/` sources carry GPL-2.0-only and GPL-2.0-or-later SPDX identifiers | Corresponding source with the binaries, or a written offer valid for three years (GPL-2.0 section 3) (item 2) |
| can-utils | 2025.01, built by `stage` with the same toolchain, linked against `libc.so.6` | `/usr/bin/cansend`, `/usr/bin/candump`, `/usr/bin/cangen` | GPL-2.0-only OR BSD-3-Clause (`cansend.c`, `candump.c`, `cangen.c`, `lib.c`); texts in `LICENSES/` | None under BSD-3-Clause. Notice and licence text |

GNU readline is in the manifest but not in this build: the `readline` module is
built on libedit (its docstring names libedit, and no GNU Readline version
string is present). Tix is in the archive but not in the image: `stage` drops
`lib/Tix*`, `lib/tcl` and `lib/tk`, and `libpython3.13.so.1.0` has no Tix code.

## CTU CAN FD

The core is not in the public tree, and no target in the public tree uses it. `rr pkg` fetches it only for bench targets, which are not part of the distribution.

| Fact | Value |
|---|---|
| Licence | MIT AND LicenseRef-CTU-CAN-FD-NonCommercial |
| Copyright | Ondrej Ille, 2021-present |
| Permitted use | Educational, research, evaluation and self-interest purposes, free of charge |
| Commercial use | Requires prior agreement with the copyright holder |
| Licence text | `LICENSES/LicenseRef-CTU-CAN-FD-NonCommercial.txt` |
| Patent notice | Robert Bosch GmbH holds patents on the CAN protocol. Implementing the core on silicon requires a CAN protocol licence from Bosch. |

## Bench CI

The bench Forgejo runs the desk checks on its own runner. Public CI is
`.github/workflows/ci.yml`.

| Path | Verdict | Basis |
|---|---|---|
| `.forgejo/**` | DROP | Bench CI configuration. It names the bench runner's labels. |
| `tools/tests/test_forge_ci_floor_leg_ds_p2_209.py` | DROP | Test of the bench CI configuration, which is not published. |

## Items to confirm

1. Resolved 2026-11-04 by the owner: `sw/linux/python_overlay.tar.gz` and `sw/linux/pfsoc/python_overlay.tar.gz` are not published, as release assets or otherwise. The public tree carries the scripts that build each overlay from upstream sources on the user's side (`sw/linux/build_buildroot.sh` with `sw/linux/extract_python_overlay.sh`, and `sw/linux/pfsoc/build_python_overlay.sh`; steps in `sw/linux/README.md`), so the project redistributes none of the components in "Contents of the Python runtime tarballs" and no source offer is owed for them.
2. Publication terms for a DE25 image (`sdcard.img`) that carries the files in "Contents of the DE25 image's Python runtime". glibc requires the corresponding source with the binaries, or a written offer. Not found yet: where Arm publishes the glibc source of the 13.3.rel1 toolchain. The same terms apply to `/usr/sbin/ip` (iproute2, GPL-2.0): its corresponding source is the pinned `iproute2-6.18.0.tar.xz` in the input table, built without patches by `tools/de25_linux_image.py stage`.
3. The licence of Berkeley DB 6.0.19 in this build. The python-build-standalone manifest states Sleepycat; to be read from the licence file of the 20241219 release (`LICENSE.bdb.txt` in its full archive).
4. The licence of HACL*, the hash code vendored in CPython's `Modules/_hacl`, which is in `libpython3.13.so.1.0`.
5. The origin of a second zlib banner, `deflate 1.2.11`, in `libpython3.13.so.1.0` beside the zlib 1.2.13 version string.
