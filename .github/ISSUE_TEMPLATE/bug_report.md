---
name: Bug Report
about: Report a hardware, software, or build issue in Deepskopion
title: "[BUG] "
labels: bug
assignees: ''
---

### Target Board and Environment
- **Target Board**: [e.g. Digilent Zybo Z7-20, Terasic DE25 Agilex-5, custom]
- **Target File**: [e.g. targets/zybo_datalogger.yml, targets/de25_datalogger.yml]
- **RouteRTL SDK Version** (`rr --version`):
- **EDA Toolchain & Version** [e.g. Vivado 2024.1, Quartus Prime Pro 25.3]:
- **Simulator Version** [e.g. NVC 1.19.3]:
- **Host OS & Architecture**: [e.g. Ubuntu 22.04 LTS x86_64]
- **Python Version**:

### Problem Description
A clear and concise description of what the defect is.

### Steps to Reproduce
1. `rr target set <target>`
2. `rr pkg install`
3. `rr synth run` (or command executed)
4. Observe failure

### Expected Behavior
A clear description of what was expected to happen.

### Actual Behavior / Error Logs
Include terminal output, synthesis error messages, daemon logs (`/var/log/deepskopion-stream.log`), or console transcripts.

```text
<paste logs here>
```

### Additional Context
Add any other relevant context, such as clock settings, pin constraints, or attached bus hardware.
