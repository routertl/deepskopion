# Vendored reference MSS configuration

`MPFS_DISCOVERY_KIT_MSS.cfg` is copied verbatim from Microchip's
`polarfire-soc-discovery-kit-reference-design` at commit `f09b6e0`, file
`MPFS_DISCOVERY/component/work/MPFS_DISCOVERY_KIT_MSS/MPFS_DISCOVERY_KIT_MSS.cfg`
(the configuration Libero 2025.2 generated for the reference build). MIT
licence — see `LICENSE.md` beside it.

It is the board fact this kit's eNVM HSS was built against: the reference
design trained DDR and booted Linux to a shell on this kit.
`../disco_mss.cfg` must equal it except for the deltas declared in
`../mss_deltas.yml`; `tools/tests/test_pfsoc_mss_matches_reference_ds_p1_60.py`
enforces that.
