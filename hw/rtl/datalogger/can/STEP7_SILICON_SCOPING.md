# CAN-FD datalogger — step 7 silicon validation (scoping)

The final step of the CAN-FD datalogger. Steps 1–6 are sim-complete and
landed (see `CAN_FD_SCOPING.md`); this scopes the on-silicon validation —
**checkpoint C on FD frames** — and the board decision behind it.

> **Why silicon at all:** the Zynq-7 PS CAN controllers are **classic-only** — they
> cannot emit FD. So unlike the classic CAN validation (PS `can0↔can1` fabric
> loopback), FD stimulus needs a **soft FD source** in the PL. And two sim-deferred
> items can *only* close here: the **BRS=1 dual-rate** drive, and the **FD CRC
> coverage verdict** (a real spec-correct FD frame is the external golden frame).

---

## 1. Board decision — Z7-20 (already the target)

deepskopion targets **`xc7z020clg400-1` (Zybo Z7-20)** today; the classic CAN
datalogger (`v0.3.0-can-emio-silicon`) was validated on it. The Z7-10 question is
settled hard by the numbers:

| Resource | Current build | Z7-20 cap | % | Z7-10 cap |
|---|---|---|---|---|
| LUTs | **22,504** | 53,200 | 42 % | **17,600 ← base design alone overflows by 1.28×** |
| FFs | 33,691 | 106,400 | 32 % | 35,200 |
| BRAM tiles | 33 | 140 | 24 % | 60 |
| DSP | 0 | 220 | 0 % | 80 |
| Timing | **WNS +0.026 ns** (closed, tight) | | | |

**The classic Z7-10 cannot host even the base deepskopion design (22.5k LUT >
17.6k), let alone + 2× CTU CAN FD.** The Z7-20 has comfortable headroom — 2× CTU
(~5–7k LUT + a few BRAM) projects to ~55–60 % LUT, ~30 % BRAM. So: **stay on
Z7-20, no migration.** KV260 (UltraScale+, aarch64) and DE25 (Intel/Quartus) are
strategic platforms whose bring-up tax isn't justified just to host 2× CAN-FD.

The real risk on Z7-20 is **not capacity** but **timing**: WNS is only +26 ps
today (`NoTimingRelaxation`), so two cores + AXI/APB bridges + interconnect will
need a timing/floorplan pass.

---

## 2. FPGA architecture

```
            ┌── ctucanfd #A (TX) ──txA──┐
 M_AXI_GP0 ─┤   (can_top_apb)           AND ─── bus ─┬─► ctucanfd #A.rx
   │  ┌─AXI→APB─┘                        ▲           ├─► ctucanfd #B.rx
   │  │                                  │           └─► can_datalogger.RX (id 20, EMIO tap)
   └──┼─AXI→APB── ctucanfd #B (RX+ACK) ─txB──────────┘
      │            (can_top_apb)
   IRQ_F2P ◄── #A.irq, #B.irq
```

- **2× `ctu-fee/ctucanfd_ip_core`** (MIT, VHDL-2008, lib `ctu_can_fd_rtl`), declared
  in **`ip.yml`** (`rr pkg add` + 2 instances; dep resolver wires them — the routertl
  way, NOT hand-instantiation).
- **Wired-AND fabric bus** `bus = txA AND txB` — the open-drain CAN bus emulated in
  PL, fed to both cores' RX and to the datalogger tap. Node A transmits, **node B must
  be up to ACK** (else A error-frames and retransmits forever).
- Register access: each `can_top_apb` ← **AXI4-Lite→APB bridge** ← `M_AXI_GP0`. One
  IRQ per core → `IRQ_F2P`.
- **Datalogger tap**: reuse the existing EMIO `can_datalogger` RX (id 20) — no new datalogger
  wiring; it just watches the shared bus.
- **Clocking / rate**: 100 MHz fabric. CAN 500 k nominal / 2 M data → the datalogger's
  `G_DATA_CYC_PER_BIT = 50` (2 M @ 100 MHz) already matches by default; SW sets it
  live to track the CTU `dbitrate`.

---

## 3. Linux (pure `rr linux`, no PetaLinux)

- Mainline **`ctucanfd`** driver (`drivers/net/can/ctucanfd/`) via declarative
  `linux.dt_nodes` — `compatible = "ctu,ctucanfd"`, `reg` = each core's GP0 address,
  `interrupts` = its `IRQ_F2P` line. Same DT-injection pattern as the DE25 EMAC and
  the Zybo emaclite bring-ups.
- Confirm **can-utils** in the rootfs supports FD `cansend ##` syntax (the CAN
  overlay already added `BR2_PACKAGE_CAN_UTILS`); add `iproute2 ip` if not present.

---

## 4. Validation — checkpoint C, on FD

- **A — enumerate**: `ip link set canN up type can bitrate 500000 dbitrate 2000000 fd on`
  for both cores → both come up.
- **B — bus proven**: `cansend can0 123##1DEADBEEF` → `candump can1` shows the FD
  frame (wired-AND + ACK works).
- **C — the datalogger (the payoff)**: `devmem 0x40000020 32 0xA033` to enable, run the
  FD `can_decode` over the DDR ring. Assert **byte-exact** decode of std / ext / BRS
  frames across DLCs **including 64 B**, **stuff-count parity OK**, and — the
  keystone — **`crc_error` stays LOW on spec-correct frames**. That real CTU frame
  *is* the external golden frame. A deliberately corrupted frame must raise it.
- **D — dual rate**: confirm BRS=1 frames decode → validates the `RATE_SEL` /
  dual-rate sampler path on silicon (the part sim couldn't drive).

Per the bug-finding rule: a real FD frame that the datalogger mis-decodes, or a
`crc_error` that fires on a spec-correct frame, is a **real DUT defect** → file
T-tier, don't paper over. The datalogger is unproven on FD silicon until C/D pass.

---

## 5. Work breakdown + dependencies

| Component | Scope |
|---|---|
| **Data prescaler** | `CONFIG.data_prescaler` field — SW sets the datalogger FD data rate to the CTU `dbitrate`. **Prerequisite** for C/D. Cross-contract (config word + codegen + SW Regs + daemon). |
| **Bring-up** | The bring-up: 2× CTU in `ip.yml`, wired-AND bus, AXI→APB bridges, DT/driver, checkpoint C/D. |
| **CRC coverage** | FD CRC coverage verdict — closed by step C. |
| **CTU IP** | CTU IP integration (uses the IP as a *traffic source* — our own can_datalogger is the decoder). |

## 6. Watch items

- ⚠ **Timing** — WNS only +26 ps today; re-check after the cores land, budget a
  timing/floorplan pass.
- ⚠ CTU core is **`⚠ Unlinted`** in the registry — run `rr lint`; expect possible
  VHDL-2008-strict nits.
- ⚠ Both nodes must share **identical bit/data timing** (same bitrate/dbitrate) or
  arbitration + ACK fail.
- ⚠ The FD CRC coverage in the datalogger is **best-effort today** (engines start
  init=0, not the leading-1 preset; covered-bit boundary unverified). If C shows a
  systematic `crc_error` on clean frames, that's the coverage bug to fix — expected,
  and exactly what the golden frame is for.
