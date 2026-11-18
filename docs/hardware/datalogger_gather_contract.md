# `datalogger_gather` Entity Contract

**Status:** Draft v0.1.

`datalogger_gather` is the aggregator that takes the per-protocol datalogger
instances declared in `tools/datalogger-builder/config/datalogger_config.yml`
and presents the deepskopion top-level with **two unified output paths
(burst + stream)** plus per-path telemetry. The generator
(`datalogger_gather_gen.py`) emits the instance count, IDs, and pin
mapping; everything that follows is the *fixed* RTL ABI the generator
targets.

This document is the contract. The generator MUST produce a module
whose entity matches this interface, and the hand-written record types
listed below MUST exist in a package the generated module can `use`.

## Scope

In scope for the PoC (I2C/UART on Zybo Z7-20):
- One BURST output (carries I2C frames, UART characters, SPI/QSPI events).
- One STREAM output (carries continuous protocols — I2S today, future
  audio/ADC).
- Two FIFO CSR ports — one per output — so the PS telemetry exporter
  can read fill/overflow/strobe counters per path.

Explicitly out of scope for the PoC (parked for later P3 work):
- Level-injection ports (`X_BUS`, `Y_BUS`, `X_CTRL`, `Y_CTRL`).
- Per-datalogger weighted arbitration (`ARB_WEIGHTS`).
- Phase-accumulator tuning (`TUNING_WORD`, `G_PHASE_ACC_WIDTH`).
- Per-datalogger status array (`*_STATUS`).

Removing these from the PoC entity keeps the contract small. They can
land later without breaking the burst/stream/CSR shape.

## Entity

```vhdl
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library tich;
use tich.datalogger_config_pkg.all;   -- generator output: counts, IDs, pin maps
use tich.datalogger_records_pkg.all;  -- hand-written: records + constants below
use tich.datalogger_pkg.all;          -- C_DATALOGGER_MSG_WIDTH, C_TIMESTAMP_WIDTH, ...

entity datalogger_gather is
  generic (
    G_SYS_CLK : natural := 100_000_000  -- system clock in Hz
  );
  port (
    -- Clock / reset
    CLK              : in  std_logic;
    RST              : in  std_logic;

    -- Captured pin matrix (logical datalogger pins from datalogger_config.yml)
    CAPTURE_PINS       : in  std_logic_vector(C_NUM_DATALOGGER_PINS-1 downto 0);

    -- Runtime per-datalogger config word, addressed by datalogger ID via regbank
    CONFIG           : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH-1 downto 0);

    -- Global timestamp from PL free-running counter
    TIMESTAMP        : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);

    -- Aggregated outputs: AXI-stream-ish data + valid pairs, one per
    -- traffic class. No tready here — backpressure goes through
    -- REQ_BURST / REQ_STREAM (host pulls when ready).
    DOUT_BURST_DATA  : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
    DOUT_BURST_VALID : out std_logic;
    DOUT_STREAM_DATA : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
    DOUT_STREAM_VALID: out std_logic;

    -- Downstream read-ready / backpressure (asserted by DMA-feeding FIFOs)
    REQ_BURST        : in  std_logic;
    REQ_STREAM       : in  std_logic;

    -- Per-path telemetry to PS (read via regbank)
    FIFO_BURST_CSR   : out t_fifoCsr := C_INIT_FIFO_CSR;
    FIFO_STREAM_CSR  : out t_fifoCsr := C_INIT_FIFO_CSR
  );
end entity datalogger_gather;
```

### Port semantics

| Port | Direction | Meaning |
|---|---|---|
| `CAPTURE_PINS` | in | Raw pin matrix; per-datalogger pin slicing follows `C_<PROTO><idx>_PINS` constants from `datalogger_config_pkg.vhd`. |
| `CONFIG` | in | Per-datalogger config word selected via datalogger ID (regbank-driven). Layout is per-protocol; see "Config word layout" below. |
| `TIMESTAMP` | in | Free-running PL counter; every datalogger tags its emitted message with this value. |
| `DOUT_BURST_DATA` | out | 32-bit message word (`C_DATALOGGER_MSG_WIDTH`). Encoded per `docs/software/data_plane/message_formats/`. |
| `DOUT_BURST_VALID` | out | High for one cycle when a new message word is presented. |
| `DOUT_STREAM_DATA` / `DOUT_STREAM_VALID` | out | Same shape, continuous-traffic protocols. |
| `REQ_BURST` / `REQ_STREAM` | in | Downstream read-side asserts when ready to accept. Gates the per-path arbiter. |
| `FIFO_BURST_CSR` / `FIFO_STREAM_CSR` | out | Telemetry record — status flags + level + overflow counter. |

**Why no `t_data_valid_record` wrapping `DOUT_*`?** A record carrying just
`data` + `valid` is two flat signals dressed up. It costs an extra
record def + an extra layer of `.data`/`.valid` indirection at every
consumer for zero clarity gain. Keep the entity surface flat.

## Record types (`datalogger_records_pkg.vhd`)

These are stable design types and live in a hand-written
`hw/rtl/pkg/datalogger_records_pkg.vhd`, **not** in the generated
`datalogger_config_pkg.vhd`. The generator only owns counts, IDs, and
pin mappings; the structural records are a build-time invariant.

The package is deliberately small. Earlier drafts of this contract
mirrored a 10-flag status record + 5-counter CSR from a reference
design — almost all of which was dead weight: the deepskopion
telemetry exporter (`telemetry_counters_spec.yml`) only wires
`.fifoStatus.full` today, and the in-tree `datalogger_gather` body never
references the other fields. v0.2 trims to what's actually consumed.

Likewise, the previous `t_data_valid_record { data, valid }` was just
two flat signals in a hat — eliminated in favour of flat port pairs
(see entity above). No `t_array_data_valid_record`, no `t_status_array`
in v0.2; both can come back when a real consumer asks.

```vhdl
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library tich;
use tich.datalogger_pkg.all;  -- C_DATALOGGER_MSG_WIDTH

package datalogger_records_pkg is

  -- Width of the per-datalogger runtime CONFIG word selected via regbank.
  -- Single source of truth: every X_datalogger entity wires
  -- G_CONFIG_WIDTH => C_DATALOGGER_CONFIG_WIDTH.
  constant C_DATALOGGER_CONFIG_WIDTH : integer := 16;

  constant C_FIFO_LEVEL_WIDTH    : integer := 16;  -- FIFO depth ≤ 65k
  constant C_FIFO_OVERFLOW_WIDTH : integer := 32;  -- saturating event counter

  --------------------------------------------------------------------------
  -- Per-FIFO status — only the signals that actually drive something.
  -- Anything else is diagnostic noise and lives outside this record.
  --------------------------------------------------------------------------
  type t_fifoStatus is record
    full     : std_logic;  -- arbiter cannot accept more this cycle
    empty    : std_logic;  -- nothing to drain
    overflow : std_logic;  -- one-shot pulse on lost word
  end record t_fifoStatus;

  constant C_INIT_FIFO_STATUS : t_fifoStatus := (
    full     => '0',
    empty    => '1',
    overflow => '0'
  );

  --------------------------------------------------------------------------
  -- Per-path CSR. Three fields, all consumed:
  --   - fifoStatus   : live flags (telemetry_counters_spec.yml reads .full)
  --   - level        : current fill (PS reads via regbank)
  --   - overflowCnt  : saturating count of overflow events since reset
  --------------------------------------------------------------------------
  type t_fifoCsr is record
    fifoStatus  : t_fifoStatus;
    level       : std_logic_vector(C_FIFO_LEVEL_WIDTH-1 downto 0);
    overflowCnt : std_logic_vector(C_FIFO_OVERFLOW_WIDTH-1 downto 0);
  end record t_fifoCsr;

  constant C_INIT_FIFO_CSR : t_fifoCsr := (
    fifoStatus  => C_INIT_FIFO_STATUS,
    level       => (others => '0'),
    overflowCnt => (others => '0')
  );

end package datalogger_records_pkg;

package body datalogger_records_pkg is
end package body datalogger_records_pkg;
```

## Config word layout

> **Ratified v1.1 (2026-06-04).** Anchored to the *silicon* decode
> (`hw/rtl/datalogger/infra/config_control/config_control.vhd` + each
> `<proto>_datalogger.vhd`) and the delivery path (`sw/stream/data_source.py`,
> regbank `DATALOGGER_CONFIG` @ 0x0020), and **enforced by the drift-gate**
> `tests/test_contract_alignment.py`. The v0.1 "advisory" table (retired below)
> did **not** match the RTL — `param[15:8]` is actually the device-id field and
> `error_inject[1]` is QSPI `CPHA`. v1.0 still carried one error of its own — a
> `cfg_valid[3]` "courtesy" bit that the RTL decodes as QSPI `no_cs` / UART
> `dataBits[1]`; the drift-gate caught it and v1.1 removed it. See "Conformance
> status".

### Framing — the word is `[device_id | payload]`, not 16 flat bits

The 16-bit `DATALOGGER_CONFIG` regbank word is **addressed**, not flat:

```
 bit  15 14 13 12 11 | 10  9  8  7  6  5  4  3  2  1  0
      └─ device_id ─┘  └─────────── payload (11 bits) ───────────┘
         (5 bits)               = CONFIG_OUT to the matched datalogger
```

`config_control` (`G_DEV_ID_WIDTH = 5`) compares `CONFIG(15 downto 11)` to its
`G_DEV_ID`; on a match it latches `CONFIG(10 downto 0)` as that datalogger's
config payload and presents it as `CONFIG_OUT`. **Only bits 10:0 ever reach a
datalogger** — the top 5 bits are the routing ID.
`data_source.apply_datalogger_config` builds exactly this word —
`(id & 0x1F) << 11 | (cfg & 0x7FF)` — and rejects any `cfg` with bits set
above 10 (`_DATALOGGER_CFG_DELIVERABLE = 0x07FF`).

**Consequence:** the per-datalogger config space is **11 bits, not 16**. There is
no `param[15:8]` field — those bits are the device_id. A host/panel that packs
config into bits ≥ 11 is overwriting the routing ID.

### Commit semantics

`config_control` commits the payload on **any change** to the word for the
matched device_id (`CONFIG /= config_r` gated by `deviceIdMatch`). There is
**no `cfg_valid` strobe and no global bit beyond `enable[0]`** — every other
payload bit is per-protocol. (A "courtesy `cfg_valid@3`" that the panel/daemon
used to set was a v1.0 mistake: bit 3 is QSPI `no_cs` / UART `dataBits[1]`, so
setting it silently forced QSPI 2-wire mode — removed in v1.1.) Partial-
word hazards are avoided by the PS writing the whole 16-bit word atomically.

### Per-protocol payload bits (the canonical decode)

`enable` is payload bit 0 in **every** datalogger (`configEnable <= deviceLoadConfig(0)`).
Beyond that, each datalogger reads its own bits — parsed straight from the
`<sig> <= deviceLoadConfig(n)` assignments in `<proto>_datalogger.vhd`:

**QSPI / SPI** (`qspi_datalogger.vhd` — all live):
| bit | signal | meaning |
|---|---|---|
| 1 | `clockPhase` | CPHA (force clock phase) |
| 2 | `halfDuplexMode` | 3-wire SPI — shared bidir SI/SO, CS-framed (one payload) |
| 3 | `noChipSelectMode` | 2-wire SPI — clock+data, no CS (one payload, no timeout) |
| — | *(CPOL)* | **auto-detected** from the idle level (`pr_detect_cpol`), not config |

**UART** (`uart_datalogger.vhd` — **wired**):
| bit | signal | meaning |
|---|---|---|
| 1 | `autoSync` | 1 = auto-calibrate baud+format (default); 0 = use the explicit format below and skip the stop-finder replay |
| 4:2 | `dataBits` | data word length − 5 (so 0→5 bits … 4→9 bits); used when `autoSync=0` |
| 5 | `parityEn` | parity enable; used when `autoSync=0` |
| 6 | `parityTypeDef` | even/odd; used when `autoSync=0` |
| 10:7 | `stopBitsDef` | stop bits — bit 0: 0 = 1 stop, 1 = 1+ stop; used when `autoSync=0` |

> ✅ **Wired** (cap UART.1, D.18). `autoSync=1` (the default) keeps
> the auto-calibration behaviour — baud and format are auto-detected. `autoSync=0`
> takes the byte FORMAT (data/parity/stop) from the bits above and **skips the
> stop-finder calibration replay**, so a short burst (≈6 chars) decodes
> immediately instead of waiting for the stop-finder to lock on enough frames.
> **Baud is always auto-measured** (`baudRateMin_r`) — explicit config sets the
> format only, never the baud rate. Regression: `test_uart_explicit_config.py`
> (discriminator `stopFinderEverEnabled=False`).

**I2C** (`i2c_datalogger.vhd`): `enable[0]` only; addr width / R/W / 7-vs-10-bit are
recovered from the bus. Bits 10:1 unused.

**Edge / Level / JTAG**: `enable[0]` only.

**Live host knobs today:** `enable` (all); QSPI `CPHA[1]` / `halfDuplexMode[2]`
/ `noChipSelectMode[3]`; and UART `autoSync[1]` + format (`dataBits[4:2]` /
`parityEn[5]` / `parityTypeDef[6]` / `stopBitsDef[10:7]`, effective when
`autoSync=0`). Auto-detected regardless: QSPI CPOL, UART baud (always), I2C addr.
Unused payload bits SHOULD be written 0.

### Enable arbitration — host CONFIG vs trigger enable-mask

Two sources can set a datalogger's `enable[0]`: the host, through the wave_stream
CONTROL frame, and a trigger action that reconfigures the enable-mask
adaptively (trigger feedback, see `trigger_architecture.md`). The rule is **host baseline AND trigger allow**:

| # | Rule |
|---|---|
| 1 | The host word is the baseline and the ceiling. A host `enable=0` is absolute. A trigger never enables a datalogger the host disabled or never configured (the `config_control` power-on latch is 0 = disabled). |
| 2 | A trigger supplies a transient 32-bit ALLOW mask over device ids (bit `i` = id `i`). Effective `enable(id) = host_enable(id) AND allow(id)`. A trigger can only gate a host-enabled datalogger off, and restore it. |
| 3 | A host write while a mask is active updates the baseline at once and is gated by the active mask. A host disable takes effect immediately. A host enable on a gated id stays off until the mask is released. |
| 4 | Releasing the mask (trigger revert / `revert_ms` expiry) restores the latest host word, not a snapshot taken when the trigger fired. |
| 5 | The trigger touches payload bit 0 only. All other payload bits come from the host word. |
| 6 | A CAN data-bank word (id 20, payload bit 10 = 1) is not an enable word: its bit 0 is `data_cyc[0]` (payload bit 9 = 0) or bit 0 of a sample point (payload bit 9 = 1). It passes through unchanged and is not an enable baseline. |

"Detail-up" adaptive capture therefore works by the host enabling the full set
the user permits and the trigger's quiet-state mask narrowing it; the trigger
widens back up to, never beyond, the host set.

Implementation: `sw/stream/enable_arbiter.py` (`EnableArbiter`) is the single
writer of `DATALOGGER_CONFIG` for the regbank-backed daemon sources. Host
CONTROL enters through `apply_config` (`RegbankDataSource`,
`DdrRingDataSource`); the trigger enable-mask path enters through
`apply_trigger_enable_mask` / `release_trigger_enable_mask` on the same source.
A PL-side trigger reconfig (the Phase 2 trigger dispatcher) MUST compose the same
way: it may AND-gate `enable[0]` of the host-latched word, never drive it high.
The `DATALOGGER_CONFIG` regbank slot and `config_control` RTL (`tb_regbank_datalogger_config_seam`)
are unchanged: arbitration happens before the word is written.
Regression: `sw/stream/tests/test_enable_arbitration_ds_p3_74.py`.

### Conformance status (2026-06-04)

| Consumer | Conforms? | Note |
|---|---|---|
| `sw/stream/data_source.py` (`apply_datalogger_config`) | ✅ | Frames `[id<<11 \| cfg&0x7FF]`; rejects bits ≥ 11. |
| `config_control.vhd` + `qspi/i2c/uart_datalogger.vhd` | ✅ (the SoT) | `enable@[0]`; QSPI `CPHA@[1]`, `halfDuplexMode@[2]`, `noChipSelectMode@[3]`. |
| RouteWave Datalogger Control panel (`composeCfg`) | ✅ | Packs only `enable[0]` + QSPI `CPHA[1]` / `halfDuplexMode[2]`. Previously packed SPI `CPOL@4`/`CPHA@5`, UART `parity@5:4`, I2C `10bit@4` (RTL reads none of those → silent no-ops on silicon) and a `cfg_valid@3` that forced QSPI 2-wire — all removed. |
| **drift-gate** `tests/test_contract_alignment.py` | ✅ | Parses the RTL `deviceLoadConfig(n)` decode, the panel `kCfg*` bits, and the deliverable mask; asserts enable@0, panel⊆RTL, no unsanctioned bit, mask==payload-width. This is what caught the `cfg_valid` bug. |
| v0.1 "advisory" table (retired, below) | ❌ | `error_inject@[1]` and `param[15:8]` contradicted the RTL. |

<details><summary>Retired v0.1 advisory table (history only — do NOT implement)</summary>

| Bit(s) | (v0.1, wrong) Meaning |
|---|---|
| 0 | `enable` |
| 1 | `error_inject` — debug *(actually QSPI `CPHA`)* |
| 2 | reserved *(actually QSPI `three_wire`)* |
| 3 | `cfg_valid` *(not consumed by the RTL)* |
| 7:4 | per-protocol mode *(unused; dataloggers auto-detect)* |
| 15:8 | per-protocol param *(actually overlaps device_id `[15:11]`)* |

</details>

## What the generator MUST emit

`datalogger_gather_gen.py` produces, in `datalogger_config_pkg.vhd`:

- `C_NUM_DATALOGGER_PINS` — input pin matrix width
- `C_<PROTO>_DATALOGGERS` — instance count per protocol (drives generate
  loop bounds in the gather body)
- `C_<PROTO><idx>_ID` — instance IDs (used as the regbank index)
- `C_<PROTO>_PINS`, `C_<PROTO><idx>_PINS`, `C_<PROTO>_PIN_ARRAY` —
  pin slicing
- `C_NUM_STREAMS`, `C_NUM_BURSTS` — drives arbiter input array bounds
- `C_STREAM_<PROTO><idx>_INDEX`, `C_BURST_<PROTO>_INDEX` — arbiter
  slot assignments
- `C_<PROTO>_IDS` per-type, `C_STREAM_<PROTO>_INDEXES`,
  `C_BURST_INDEXES`

And in `datalogger_gather.vhd`:

- The entity exactly as specified above.
- A `library tich; use tich.datalogger_records_pkg.all;` clause in the
  generated body.
- One arbiter per traffic class (`tich.arbiter`), wired per
  `C_NUM_STREAMS` / `C_NUM_BURSTS`.
- One per-protocol-instance `entity tich.<proto>_datalogger` instantiation
  whose generics map from `protocols.yml`.

## Verification plan

Once `datalogger_records_pkg.vhd` lands and the generator re-runs:

1. `rr lint` — `datalogger_gather` should drop out of `exclude.lint` and
   pass per-hierarchy compilation. The exclude entries
   in `project.yml` get removed.
2. `rr sim run test_datalogger_gather` — promote the existing
   `hw/sim/cocotb/tests/datalogger/infra/test_datalogger_gather.py` from
   skeleton to a real connectivity smoke (drive `CAPTURE_PINS` with a
   short UART byte, expect one `DOUT_BURST.valid` pulse, check
   `FIFO_BURST_CSR.fifoCnt` increments).
3. `rr synth run deepskopion_top` — first end-to-end build, contingent
   on the new `deepskopion_top.vhd` wrapper (tracked separately).

## Open questions parked as future work

- **Per-datalogger status bus.** The PoC drops `*_STATUS`. When we want
  per-datalogger diagnostics surfaced to PS (overflow source, decode
  errors), reintroduce as a `t_status_array(0 to N-1)(C_REGBANK_WIDTH-1 downto 0)`
  port. Tracked for future work.
- **Weighted arbitration.** Today the arbiter is round-robin. Real
  traffic asymmetry (one fast UART vs four slow I2Cs) may want
  weights. Track separately.
- **Level/edge injection.** The legacy file's `X_BUS`/`Y_BUS`/`X_CTRL`
  ports were for software-driven pin override. Useful for self-test,
  not the PoC. Track separately.
- **`C_SSI_CONFIG_WIDTH` alias.** The Jinja template (and
  `protocols.yml`) currently uses `C_SSI_CONFIG_WIDTH` for the same
  width that this contract calls `C_DATALOGGER_CONFIG_WIDTH`. Either:
  (a) rename in the template + `protocols.yml`, or (b) declare an alias
  in `datalogger_records_pkg.vhd`. Recommendation: (a) — drop the legacy
  name in the same patch as the regen.

## Change log

- v1.2 (2026-11-02) — §"Enable arbitration" — host CONFIG
  `enable` is the baseline and ceiling, a trigger enable-mask is a transient
  AND-gate; implemented in `sw/stream/enable_arbiter.py`.
- v0.2 (2026-05-15) — slim refactor: drop `t_data_valid_record` (flat
  ports instead), trim `t_fifoStatus` to 3 flags (was 10), trim
  `t_fifoCsr` to 3 fields (was 6). Anchored on what
  `telemetry_counters_spec.yml` and the datalogger_gather body actually
  consume today. Anything we cut comes back individually when a real
  consumer arrives.
- v0.1 (2026-05-15) — initial draft. Based on an existing working datalogger-aggregator design that
  uses the same record-based interface; trimmed to the PoC scope
  (I2C + UART on Zybo).
