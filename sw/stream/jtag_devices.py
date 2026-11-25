# rr unknown-llm (AI-generated)
"""JTAG device profiles — chain-aware decode config (DS-P3.56 P3).

The generic reassembler (:mod:`jtag_reassembler`) knows the TAP FSM and the
IR/DR bitstreams but not what an IR *value* means — that is device-specific
(IR length + instruction map). A :class:`JtagDevice` profile supplies it, so a
Shift-DR after ``IR=USER1`` is labelled as a BSCAN/fabric scan (e.g. our REA
IP's ILA payload) rather than an anonymous DR, and an explicit ``IR=IDCODE``
read decodes as an IDCODE even when it is not the post-reset scan.

This is the SW side of "chain-aware" in the DS-P3.56 phase plan; per-device
BSDL ingestion and multi-device chains (each device's IR in series) build on
this same profile shape.

Instruction codes are the stable public values from the vendor user guides /
BSDL:
  * Xilinx 7-series / Zynq-7000 PL TAP — UG470 Table 6-3 (IR length 6). USER1/2
    drive the BSCANE2 primitives (JTAG_CHAIN 1/2) that the REA reaches fabric
    through; USER3/4 are the second BSCANE2 pair.
  * ARM CoreSight DAP — ADIv5 (IR length 4): DPACC/APACC register access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class JtagDevice:
    """A single TAP's decode profile."""
    name: str
    ir_length: int
    instructions: Dict[int, str]          # IR value -> instruction mnemonic
    idcode_instr: Optional[int] = None    # IR value that selects the IDCODE DR
    short: str = ""                       # terse label for chain output (PL/DAP)

    def instr_name(self, ir_val: int) -> Optional[str]:
        return self.instructions.get(ir_val)

    def instr_label(self, ir_val: int) -> str:
        """Mnemonic if known, else BYPASS for all-ones, else raw hex."""
        name = self.instr_name(ir_val)
        if name is not None:
            return name
        if ir_val == (1 << self.ir_length) - 1:
            return "BYPASS"
        return f"0x{ir_val:X}"

    def is_bypass(self, ir_val: int) -> bool:
        return ir_val == (1 << self.ir_length) - 1

    def is_idcode_instr(self, ir_val: int) -> bool:
        return self.idcode_instr is not None and ir_val == self.idcode_instr

    def is_bscan_instr(self, ir_val: int) -> bool:
        """USER1..USER4 route the TAP to fabric (BSCANE2) — this is where an
        embedded analyzer (our REA) lives. Recognised by mnemonic so a BSDL
        import or a custom profile naming them USERn composes for free."""
        name = self.instr_name(ir_val)
        return name is not None and name.startswith("USER")


# ── Xilinx 7-series / Zynq-7000 PL TAP (the Zybo Z7-20 fabric TAP) ─────
XILINX_7SERIES = JtagDevice(
    name="Xilinx 7-series",
    ir_length=6,
    short="PL",
    idcode_instr=0x09,
    instructions={
        0x01: "SAMPLE",
        0x02: "USER1",      # BSCANE2 JTAG_CHAIN=1 — REA fabric path
        0x03: "USER2",      # BSCANE2 JTAG_CHAIN=2
        0x04: "CFG_OUT",
        0x05: "CFG_IN",
        0x09: "IDCODE",
        0x0A: "HIGHZ",
        0x0B: "JPROGRAM",
        0x0C: "JSTART",
        0x0D: "JSHUTDOWN",
        0x22: "USER3",      # BSCANE2 JTAG_CHAIN=3
        0x23: "USER4",      # BSCANE2 JTAG_CHAIN=4
        0x26: "EXTEST",
        0x3F: "BYPASS",
    },
)

# ── ARM CoreSight DAP (Zynq-7000 PS debug, ADIv5) ─────────────────────
ARM_DAP = JtagDevice(
    name="ARM CoreSight DAP",
    ir_length=4,
    short="DAP",
    idcode_instr=0xE,
    instructions={
        0x8: "ABORT",
        0xA: "DPACC",
        0xB: "APACC",
        0xE: "IDCODE",
        0xF: "BYPASS",
    },
)

# Lookup by short key (for CLI / config selection).
PROFILES: Dict[str, JtagDevice] = {
    "xc7": XILINX_7SERIES,
    "arm_dap": ARM_DAP,
}

# Default for deepskopion's own board — the Zybo Z7-20 PL TAP is 7-series.
DEFAULT_DEVICE = XILINX_7SERIES


# ── Multi-device scan chains (DS-P3.58) ───────────────────────────────
#
# A real board's TDI..TDO often holds several TAPs in series, so an IR (or DR)
# shift is the CONCATENATION of every device's register. A single JtagDevice
# profile can't split that; a JtagChain can.

@dataclass(frozen=True)
class JtagChain:
    """An ordered JTAG scan chain. `devices[0]` is nearest TDI, `devices[-1]`
    nearest TDO.

    During Shift-IR/Shift-DR the whole chain is one long shift register, and the
    datalogger captures bits LSB-first (first bit shifted = LSB). The first bits
    shifted travel furthest — into the device nearest TDO — so a reassembled
    value splits from the LSB up, assigning to devices in REVERSE chain order
    (the TDO-nearest device takes the low bits).
    """
    name: str
    devices: Tuple[JtagDevice, ...]

    @property
    def ir_width(self) -> int:
        return sum(d.ir_length for d in self.devices)

    def split_ir(self, ir_value: int) -> List[Tuple[JtagDevice, int]]:
        """Split a reassembled combined IR into [(device, ir_field), ...] in
        chain order (TDI..TDO)."""
        rev: List[Tuple[JtagDevice, int]] = []
        pos = 0
        for d in reversed(self.devices):
            rev.append((d, (ir_value >> pos) & ((1 << d.ir_length) - 1)))
            pos += d.ir_length
        return list(reversed(rev))


# Zynq-7000 default chain as Vivado/xsdb enumerates it: the PL (Artix-7) TAP
# and the ARM CoreSight DAP in series. Order TDI -> PL TAP (IR=6) -> ARM DAP
# (IR=4) -> TDO, i.e. the DAP sits at the LSB (TDO) end. Validated on silicon
# (DS-P3.58): a combined IR=0x21 splits to PL=0x02 (USER1) | DAP=0x1, and a
# USER1 REA read shows PL=USER1 with the DAP in BYPASS.
ZYNQ7_CHAIN = JtagChain(name="Zynq-7000 (PL TAP + ARM DAP)",
                        devices=(XILINX_7SERIES, ARM_DAP))
