# PolarFire SoC MSS configuration

The Microcontroller Subsystem (MSS) of the PolarFire SoC Discovery Kit is set by
`disco_mss.cfg`, which `targets/pfsoc_datalogger.yml` names as `hardware.mss_config`.
During the Libero flow, RouteRTL runs the PolarFire SoC MSS Configurator on it and
generates the component `disco_mss.cxz`; `../pfsoc_mss_subsys.tcl` imports that
component with `import_mss_component`.

## Files

| File | Content |
|---|---|
| `reference/MPFS_DISCOVERY_KIT_MSS.cfg` | MSS configuration of Microchip's `polarfire-soc-discovery-kit-reference-design` at commit `f09b6e0`, copied unchanged. MIT licence, in `reference/LICENSE.md`. |
| `disco_mss.cfg` | The reference configuration plus the changes listed in `mss_deltas.yml`. |
| `mss_deltas.yml` | Every key where `disco_mss.cfg` differs from the reference, with the reason. |
| `disco_mss.cxz` | Reference copy of the component generated from `disco_mss.cfg`. Not part of the published tree. Builds and the public export use only the component RouteRTL generates (below); this copy feeds three checks and nothing else: the `.cxz` content checks, the DDR-alias SEG decode, and the HSS IOMUX byte-identity check. |

## Part and memory

| Key | Value |
|---|---|
| `DIE` | `MPFS095T` |
| `PACKAGE` | `FCSG325` |
| `SPEED` | `-1` |
| `DDR_SDRAM_TYPE` | `DDR4` |
| `DDR4_WIDTH` | `16` |
| `DDR4_CLOCK_DDR` | `800` |

On `FCSG325` the configurator refuses a 32-bit DQ width, and on the `STD` speed
grade it limits DDR4 to 666.66 MHz. The kit's part is `MPFS095T-1FCSG325E`, grade
`-1`, so 800 MHz is accepted.

## Changes from the reference

| Key | Reference | `disco_mss.cfg` | Reason |
|---|---|---|---|
| `MODULE_NAME` | `MPFS_DISCOVERY_KIT_MSS` | `disco_mss` | Component name that `pfsoc_mss_subsys.tcl` instantiates as `u_mss`. |
| `MMUART_1` | `MSSIO_B2` | `FABRIC` | Self-test UART: Linux writes on `/dev/ttyS1` and the traffic reaches the logger source select. |
| `CAN_0`, `CAN_0_TX_EBL_N` | `UNUSED` | `FABRIC` | Self-test CAN: `CAN_0` reaches the CAN logger through the fabric. |
| `FIC_1_AXI4_INITIATOR_USED` | `true` | `false` | FIC1 is used only as the target of the ring DMA. An unused initiator leaves inputs undriven, which SmartDesign rejects. |

The Linux console stays on `MMUART_4` on `MSSIO_B2` (`/dev/ttyS0`), as in the
reference. `I2C_0` and `SPI_0` are routed to the fabric in the reference already.

## Generated component

RouteRTL generates the component on every Libero build of the target (Libero SoC
2025.2 provides the configurator; the batch run needs no licence). It writes the
component and `rr_mss_stamp.json` to `build/pfsoc_datalogger/mss/`:

| Stamp key | Content |
|---|---|
| `cxz` | Absolute path of the generated component. |
| `cfg` | The configuration it was generated from. |
| `cfg_sha256` | sha256 of that configuration. |

`sw/linux/pfsoc/hss/disco_iomux_config.py` reads the component the stamp names. It
refuses when the stamp is missing, unreadable, names a missing component, or carries
a `cfg_sha256` other than that of the tracked `disco_mss.cfg`.

## Checks

| Test | Checks |
|---|---|
| `tools/tests/test_pfsoc_mss_matches_reference_ds_p1_60.py` | `disco_mss.cfg` equals the reference except for the keys in `mss_deltas.yml`, and every listed delta is present. |
| `tools/tests/test_pfsoc_target_contract.py` | `hardware.mss_config` names `disco_mss.cfg`; no `hardware.mss` and no `linux.mss_handoff`. |
| `tools/tests/test_pfsoc_mss_stamp_ds_p1_113.py` | The HSS IOMUX overlay reads the component the stamp names and refuses a missing, unreadable or mismatched stamp. |
| `tools/tests/test_pfsoc_mss_builder_contract.py` | `pfsoc_mss_subsys.tcl` calls `import_mss_component` with the arguments Libero 2025.2 accepts. |
