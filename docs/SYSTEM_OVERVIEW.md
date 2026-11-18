# System Overview

Deepskopion captures bus traffic in the FPGA fabric of an SoC board and streams it from Linux on the same board over TCP. `FDD.md` at the repository root has the full design description.

## Data flow

| Stage | Where | Function |
|---|---|---|
| Capture | Fabric | Dataloggers sample the pins, decode frames and detect errors. |
| Timestamp | Fabric | Each record takes its time from one free-running counter in the fabric, so records from different buses share a timeline. |
| Framing | Fabric | An arbiter and framer merge datalogger messages into `zs_msg` records. |
| Transport | Fabric to DDR | `rr_ddr_stream` writes records to a ring buffer in board DDR over AXI4 (the `axi` writer on Zybo and DE25, the `fic` writer on PolarFire SoC). |
| Drain | Linux | The capture daemon reads the ring and decodes the payloads. |
| Stream | Linux to host | The daemon serves the records on TCP port 7475 as `wave_stream_v1`. |

## Ring buffer

| Item | Value |
|---|---|
| Owner | Linux on the processor subsystem. Device tree reserves the region. |
| Writer | The fabric, through DMA only. |
| Reader | The capture daemon, which reports how far it has read through `RING_READ_PTR`. |
| Write position | `RING_COMMIT_PTR` gives the address the DMA has actually written. |
| Loss reporting | A full ring is counted in `DDR_BACKPRESSURE_CNT`, and the fill watermark in `RING_ALARM_CNT`. |

## Related documents

* [FPGA Design](hardware/fpga_design.md)
* [Software Architecture](software/README.md)
* [Deployment Contract](DEPLOYMENT_CONTRACT.md)
* Register map: `project_regbank.yml` at the repository root
