<!-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0 -->
# DE25 ring capture slice for the native pad drain test

Facts verified against the b2 capture on 2026-11-23.

| Item | Value |
|---|---|
| File | `de25_ring_head64k.bin` |
| Content | The first 65 536 B of a DE25 DDR ring capture |
| sha256 of the slice | `613a0f20c8242d3964419165a936cc7f37f3b5e81298f10c59b0849549d811d0` |
| Full capture | 8 388 608 B, read from ring offset 0x140 (a 32 B word boundary) |
| sha256 of the full capture | `c2e40e923a399f82e054c77b20f79fe88218a72679c9b8c30a2dc66e400b3a3c` |
| Where the full capture is kept | Bench-private, not committed |
| Board and image | DE25, deepskopion image at 1e14348e, TPUT-001 stimulus, ring word 32 B |

## What the slice holds

| Bytes | Content |
|---|---|
| 0 to 191 | Four edge-domain frames that are not EDGE_BATCH (event 0x8 at 0, event 0x2 at 64, 128 and 156), two 1-entry EDGE_BATCH frames, zero pad to each 32 B word boundary |
| 192 to 65 471 | 408 EDGE_BATCH frames of 32 entries (148 B), each followed by 12 B of zero pad to the 32 B word boundary |
| 65 472 to 65 535 | The head of a frame cut by the slice end |

Read by `sw/stream/tests/test_native_edge_pad_drain_ds_p1_117.py`.
