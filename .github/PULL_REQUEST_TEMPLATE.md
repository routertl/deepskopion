## Summary of Changes

A clear, concise explanation of **WHY** this change is needed and what it accomplishes.

---

## Subsystem(s) Affected

- [ ] Hardware / RTL Gateware (`hw/rtl/`)
- [ ] Block Design & Board Presets (`hw/bd/`, `hw/xdc/`, `hw/sdc/`)
- [ ] Capture Daemon & Linux Software (`sw/stream/`, `sw/linux/`)
- [ ] Telemetry & Tooling (`sw/telemetry/`, `tools/`)
- [ ] Wire Contract & Protocols (`contracts/`)
- [ ] Target YAML & Build Configuration (`targets/`, `project.yml`)
- [ ] Documentation (`docs/`, `README.md`)

---

## Hardware Validation Checklist

Per [`CONTRIBUTING.md`](../CONTRIBUTING.md), any PR modifying RTL, daemons, device trees, or targets requires physical hardware validation:

- [ ] **Hardware-validated on physical silicon**
- [ ] **Target board named**: [e.g. Digilent Zybo Z7-20, Terasic DE25 Agilex-5]
- [ ] **Attached `rr hw run <REQ_ID> --probe <xsdb|juart> --write` execution log and artifact (`run.json`)**
- [ ] **Simulation regression suite passed**:
  - `rr sim run` exited 0
  - `python3 -m pytest tools/tests/` passed cleanly
- [ ] **Regression guard power verified**:
  - Baseline: test fails against pre-change code
  - Mutation: test fails when guarded mechanism is deleted or altered
- [ ] **Developer Certificate of Origin (DCO) sign-off**:
  - All commits carry `Signed-off-by: Full Name <email>`
- [ ] **License compliance**:
  - Gateware/docs under CERN-OHL-W-2.0
  - Software under MPL-2.0
  - No unlicensed dependencies added
