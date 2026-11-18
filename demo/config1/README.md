# Deepskopion — Demo Config #1 (UART + I2C + Edge on Zybo Z7-20)

The first curated **v0 catalog** entry. It captures a real, mixed multi-protocol
bus session on a live FPGA and turns it into a **cross-protocol Session Summary**
— the artefact a closed-architecture logic analyzer cannot produce, because the
value is *captured intent, not signals*: every transaction across every protocol
is stamped on **one shared microsecond timebase** and reported with counts,
durations, and honestly-classified anomalies.

- **Board:** Zybo Z7-20 (`xc7z020`), dataloggers on the PMOD/EMIO seam.
- **Config:** 2× I2C, 2× UART, 4× EDGE (+ SPI) dataloggers — the `canfd_demo`
  bitstream (full datalogger set); this demo exercises the UART/I2C/SPI/edge subset.
- **Result (this capture):** 3863 decoded events over 25 s — UART 518 (0 err),
  I2C 300, SPI 600, EDGE 2322 — see [`SESSION_SUMMARY.md`](SESSION_SUMMARY.md).

## The pipeline: capture → log → summary

```
 board (PL dataloggers)         host
 ┌────────────────┐   TCP    ┌───────────────────────┐   JSON-lines   ┌──────────────────┐
 │ wave_stream     │  :7475  │ ws_capture.py         │──────────────▶ │ session_summary  │
 │ daemon (decodes │────────▶│ (HELO_OK + record     │  transaction   │ .py → markdown   │
 │ the DDR ring)   │         │  ANNOTATIONS+EDGE)    │      log       │ Session Summary  │
 └────────────────┘         └───────────────────────┘                └──────────────────┘
        ▲
        │ EMIO stimulus (regbank writes only — no ring contention)
 ┌────────────────┐
 │ demo_config1_   │  on the board
 │ drive.py        │
 └────────────────┘
```

### 1. Bring the board up (if needed)
See the `deepskopion-board-console` skill. The board boots Linux + the
`wave_stream` daemon (listening on `:7475`) from SD/TFTP; reach it at
`root@192.0.2.2`. If it's wedged, reboot over JTAG (SLCR soft-reset).

### 2. Drive traffic + capture (two halves, run concurrently)
```sh
# host — record the decoded stream to a JSON-lines transaction log
python3 tools/capture/ws_capture.py 32 capture.jsonl

# board — sustained per-protocol EMIO injection (in another shell / over ssh)
ssh root@192.0.2.2 'cd /usr/local/share/deepskopion && \
    python3 -m sw.stream.testbench.demo_config1_drive mixed'
```
The daemon owns the DDR ring; the driver only *writes* the regbank/peripherals,
so the two never contend. (Running the ring-reading `capture_session.py` while the
daemon runs would hang — the two-reader race.)

### 3. Generate the Session Summary
```sh
python3 tools/capture/session_summary.py capture.jsonl SESSION_SUMMARY.md
```

### 4. Watch it live in RouteWave (optional)
```sh
routewave -c "stream attach 192.0.2.2:7475"
```
RouteWave attaches to the *same* live stream (it sends the HELO_OK handshake and
renders the decoded protocols + edge lanes on the shared timebase).

## Files in this package
| File | What |
|---|---|
| [`SESSION_SUMMARY.md`](SESSION_SUMMARY.md) | the generated cross-protocol summary (the headline artefact) |
| [`transaction_log.sample.jsonl`](transaction_log.sample.jsonl) | representative slice of the JSON-lines log (48 events; full run = 3863) |
| [`deepskopion-datalogger.cast`](deepskopion-datalogger.cast) | the recorded asciinema cast of a live capture on a Zybo Z7-20 (renamed data-logger stack) |

## Honesty note
The summary reports **observed facts only**. Two flags are labelled as what they
are, not as bus faults: `zs_resync` = a capture-side stream resync when the EMIO
mux switches during self-injection (an artefact of driving one board's pins
sequentially — absent with real simultaneous external signals); an I2C terminal
NACK = the normal end-of-read. No inferred-cause section — credibility over an
impressive-looking summary.

## Reproducing on your own board
The tooling is board-generic: point `ws_capture.py` at any deepskopion daemon,
drive whatever traffic your DUT produces, and the summary generator turns the log
into the same cross-protocol report. `demo_config1_drive.py` is the self-injection
stimulus for a bench with no external traffic source.
