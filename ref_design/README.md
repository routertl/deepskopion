# Reference design manifests

One manifest per board, each published on its own:

| Board | Target | Manifest | Publish |
|---|---|---|---|
| Digilent Zybo Z7-20 | `zybo_datalogger` | [`zybo.yml`](zybo.yml) | `rr ref publish ref_design/zybo.yml` |
| Terasic DE25 | `de25_datalogger` | [`de25.yml`](de25.yml) | `rr ref publish ref_design/de25.yml` |
| Microchip PolarFire SoC Discovery Kit | `pfsoc_datalogger` | [`pfsoc.yml`](pfsoc.yml) | `rr ref publish ref_design/pfsoc.yml` |

`python3 tools/ref_design_validator.py` checks all three; it takes one or more manifest paths to check only those.
`rr ref publish` without a path reads `ref_design.yml` in the current directory; the repository root holds none, so
each board is published by its path.
