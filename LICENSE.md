# Licensing

Deepskopion is an open hardware + software reference design. It is released
under two licenses, split by the nature of each component. The exact,
machine-readable mapping lives in [`REUSE.toml`](REUSE.toml); this file is the
human summary.

| Component | Path(s) | License | Full text |
|---|---|---|---|
| **Gateware / RTL** + testbenches + design docs | `hw/`, `sim/`, `docs/` | **CERN-OHL-W-2.0** (Weakly Reciprocal) | [`LICENSES/CERN-OHL-W-2.0.txt`](LICENSES/CERN-OHL-W-2.0.txt) |
| **Software** — capture daemon, decoders, telemetry, build tooling, wire contract | `sw/`, `tools/`, `contracts/` | **MPL-2.0** | [`LICENSES/MPL-2.0.txt`](LICENSES/MPL-2.0.txt) |
| **Vendored third-party cores** | `libs/` | each core's own upstream license (table below) | per core |

Copyright © 2026 Daniel J. Mazure.

## What this means

Both licenses are **weakly reciprocal**, at the level of the files you took.
You may use, study, modify and redistribute Deepskopion, including inside a
closed commercial product, and you owe back only the changes you make to
*these* files, under the same license. The rest of your design or program stays
yours.

- **CERN-OHL-W-2.0**: put a data logger into your own FPGA design and ship the
  product; you must make available the source of the Deepskopion parts you
  used, with your modifications, and nothing else of your design.
- **MPL-2.0**: link the daemon, decoders or contract into your own software;
  modified Deepskopion files must be made available under MPL-2.0, files you
  add do not.

This is deliberate. Deepskopion exists so that engineers meet the RouteRTL
flow on a real design; a license that kept it out of products would defeat
that purpose. Contributions are accepted under the Developer Certificate of
Origin (see CONTRIBUTING), no contributor license agreement.

## Vendored cores

`libs/` contains third-party IP fetched by `rr pkg install` / `rr workspace
sync` (CAN FD, fixed-point utilities, portable primitives) plus RouteRTL's own
packaged units. Each stays under its upstream license and copyright; nothing
in this file relicenses them. The per-core `[[annotations]]` tables in
[`REUSE.toml`](REUSE.toml) are the machine-readable form of this table;
the pinned upstream commit for each is in `ip.lock`, except the CTU core, which a bench target pins in its own lock.

| Core | Path | Upstream copyright holder | License | Full text |
|---|---|---|---|---|
| CTU CAN FD IP core | `libs/.targets/<target>/ctu-fee/ctucanfd_ip_core/` | Ondrej Ille (2021-present); Ondrej Ille, Martin Jerabek, CTU FEE (2015-2020) | **MIT AND LicenseRef-CTU-CAN-FD-NonCommercial** — the 2021+ notice forbids commercial use without the holder's agreement, and silicon needs a Bosch CAN protocol licence | [`LICENSES/LicenseRef-CTU-CAN-FD-NonCommercial.txt`](LICENSES/LicenseRef-CTU-CAN-FD-NonCommercial.txt), [`LICENSES/MIT.txt`](LICENSES/MIT.txt) |
| CTU register-map generated files | `libs/.targets/<target>/ctu-fee/ctucanfd_ip_core/src/memory_registers/generated/` (8 files named in `REUSE.toml`) | Ondrej Ille (2018) | **MIT** | [`LICENSES/MIT.txt`](LICENSES/MIT.txt) |
| Open Logic `olo_base_*` | `libs/open-logic/` (gitignored, synced) | Paul Scherrer Institute (2018); Oliver Bruendler (2024-2025) | **PSI HDL Library License 1.0** = LGPL-2.1-or-later with a binary/bitstream exception (no SPDX id) | upstream `License.txt` in the synced tree |
| Enclustra `en_cl_fix` | `libs/enclustra/` (gitignored, synced) | Enclustra GmbH (2024-2025) | **MIT** | [`LICENSES/MIT.txt`](LICENSES/MIT.txt) |
| RouteRTL SDK units, `rr_ddr_stream` + `rr_rea` | `libs/routertl/` | Daniel J. Mazure (2024-2026) | **MIT** (as their own headers / upstream LICENSE declare, not this design's CERN-OHL-W) | [`LICENSES/MIT.txt`](LICENSES/MIT.txt) |

The two CTU rows apply to bench targets only. `rr pkg` fetches the CTU CAN FD
core into `libs/.targets/<target>/` for a bench target; no shipped target uses
it, and the distribution does not contain it. The rest of Deepskopion is
weakly-reciprocal open hardware; the CTU CAN FD core is not: its current
licence permits educational, research, evaluation and self-interest use only.

## Build dependency

Building Deepskopion requires the **RouteRTL SDK** (`rr`), which is distributed
separately under its own terms. RouteRTL is a generally-available build tool;
its own license is unaffected by, and does not alter, the licenses above.
