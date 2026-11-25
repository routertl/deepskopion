# rr unknown-llm (AI-generated)
"""PS-side AXI-Lite regbank access — abstract interface + /dev/mem + fake backends.

Used by RegbankDataSource (data_source.py) to drain telemetry counters from the
deepskopion regbank at runtime. The abstract Regbank Protocol lets the same
data-source code run against a live Zybo Z7-20 via /dev/mem and against a
Python-only fake under pytest, with no PetaLinux dependency at test time.

Layout-of-record: project_regbank.yml. The MMIO base for the deepskopion BD's
M_AXI_GP0 segment is 0x4000_0000 (4 KB window) — see hw/bd/deepskopion_bd.tcl
assign_bd_address call.
"""

from __future__ import annotations

import logging
import mmap
import os
import ctypes
from pathlib import Path
import platform
import struct
from typing import Callable, Dict, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

# DS-T2.82: machines whose CPython lowers every Python-level 32-bit MMIO
# access to BYTE accesses. On the PolarFire kit's riscv64 CPython (Buildroot
# 2024.11, rv64gc, strict-align tuning) ctypes' integer setter is
# `memcpy(ptr, &x, 4)` on an unknown-alignment pointer, compiled to four `sb`
# stores (no `sw` exists in any integer setter of its _ctypes module); struct,
# memoryview and mmap slicing all go through the same kind of memcpy. A
# write-strobe slot then fires on the FIRST byte lane: the DE-C0 I2C
# self-test sent 0xAB,0x00,0x00 instead of 0xAB,0xCD,0x3E. aarch64/armv7
# (Zynq, DE25) allow unaligned access, so the same memcpy is one `str` there.
_BYTE_SPLITTING_MACHINES = ("riscv",)

# The kit's libatomic __atomic_store_4 is `fence rw,w; sw a1,0(a0);
# fence rw,rw` and __atomic_load_4 is one `lw` — a single naturally-aligned
# word access with writel/readl-style ordering. Loaded by soname: Buildroot
# ships no ldconfig cache, so ctypes.util.find_library() finds nothing there.
_LIBATOMIC_SONAME = "libatomic.so.1"
_ATOMIC_SEQ_CST = 5

WordAccess = Tuple[Callable[[int, int], None], Callable[[int], int]]


def _libatomic_word_access(base_addr: int) -> WordAccess:
    """(store32, load32) over the mapping at `base_addr`, each a single
    32-bit bus access via libatomic. Raises OSError when libatomic is absent —
    a byte-splitting machine must refuse rather than silently fall back."""
    lib = ctypes.CDLL(_LIBATOMIC_SONAME)
    store = lib.__atomic_store_4
    store.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int)
    store.restype = None
    load = lib.__atomic_load_4
    load.argtypes = (ctypes.c_void_p, ctypes.c_int)
    load.restype = ctypes.c_uint32

    def store32(offset: int, value: int) -> None:
        store(base_addr + offset, value, _ATOMIC_SEQ_CST)

    def load32(offset: int) -> int:
        return load(base_addr + offset, _ATOMIC_SEQ_CST)

    return store32, load32


def disassemble_riscv64_elf_symbol(
    elf_path: os.PathLike[str] | str, symbol: str
) -> list[str]:
    """Decode a symbol's RV64GC (RVC + RV64I) machine instructions from a
    64-bit little-endian RISC-V ELF into objdump-style ``mnemonic\\toperands``
    lines in-process, without requiring ``riscv64-linux-gnu-objdump`` (DS-T3.35)."""
    lib = Path(elf_path)
    data = lib.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01", f"{lib}: expected 64-bit little-endian ELF"
    hdr = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    e_machine, e_shoff, e_shentsize, e_shnum = hdr[1], hdr[5], hdr[10], hdr[11]
    assert e_machine == 243, f"{lib}: expected EM_RISCV (243), got {e_machine}"
    sections = [
        struct.unpack_from("<IIQQQQIIQQ", data, e_shoff + i * e_shentsize)
        for i in range(e_shnum)
    ]
    code = b""
    for sh in sections:
        sh_type, sh_offset, sh_size, sh_link, sh_entsize = (
            sh[1], sh[4], sh[5], sh[6], sh[9]
        )
        if sh_type not in (2, 11) or sh_entsize == 0:
            continue
        str_sh = sections[sh_link]
        strtab = data[str_sh[4] : str_sh[4] + str_sh[5]]
        for pos in range(sh_offset, sh_offset + sh_size, sh_entsize):
            st_name, _info, _other, st_shndx, st_value, st_size = struct.unpack_from(
                "<IBBHQQ", data, pos
            )
            end = strtab.find(b"\x00", st_name)
            name = (
                strtab[st_name:end].decode("ascii", errors="replace").split("@", 1)[0]
            )
            if name == symbol and 0 < st_shndx < len(sections) and st_size > 0:
                sec = sections[st_shndx]
                off = sec[4] + (st_value - sec[3])
                code = data[off : off + st_size]
                break
        if code:
            break
    assert code, f"symbol {symbol!r} not found in {lib}"
    regs = (
        "zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2",
        "s0", "s1", "a0", "a1", "a2", "a3", "a4", "a5",
        "a6", "a7", "s2", "s3", "s4", "s5", "s6", "s7",
        "s8", "s9", "s10", "s11", "t3", "t4", "t5", "t6",
    )
    load_ops = {0: "lb", 1: "lh", 2: "lw", 3: "ld", 4: "lbu", 5: "lhu", 6: "lwu"}
    store_ops = {0: "sb", 1: "sh", 2: "sw", 3: "sd"}
    c_mem_ops = {2: "lw", 3: "ld", 6: "sw", 7: "sd"}
    body: list[str] = []
    pc = 0
    while pc < len(code):
        hw = struct.unpack_from("<H", code, pc)[0]
        if (hw & 0x3) != 0x3:
            op = hw & 0x3
            f3 = (hw >> 13) & 0x7
            if op == 0b00 and f3 in c_mem_ops:
                rs1_p = regs[8 + ((hw >> 7) & 0x7)]
                r2_p = regs[8 + ((hw >> 2) & 0x7)]
                if f3 in (2, 6):
                    uimm = (
                        (((hw >> 6) & 0x1) << 2)
                        | (((hw >> 10) & 0x7) << 3)
                        | (((hw >> 5) & 0x1) << 6)
                    )
                else:
                    uimm = (((hw >> 10) & 0x7) << 3) | (((hw >> 5) & 0x3) << 6)
                body.append(f"{c_mem_ops[f3]}\t{r2_p},{uimm}({rs1_p})")
            elif op == 0b01 and f3 == 0b000:
                rd = regs[(hw >> 7) & 0x1f]
                imm = ((hw >> 2) & 0x1f) - (32 if ((hw >> 12) & 0x1) else 0)
                body.append("nop" if rd == "zero" and imm == 0 else f"addi\t{rd},{rd},{imm}")
            elif op == 0b01 and f3 == 0b001:
                rd = regs[(hw >> 7) & 0x1f]
                imm = ((hw >> 2) & 0x1f) - (32 if ((hw >> 12) & 0x1) else 0)
                body.append(
                    f"sext.w\t{rd},{rd}" if imm == 0 else f"addiw\t{rd},{rd},{imm}"
                )
            elif (
                op == 0b10
                and f3 == 0b100
                and ((hw >> 12) & 0x1) == 0
                and ((hw >> 2) & 0x1f) == 0
            ):
                rs1 = (hw >> 7) & 0x1f
                body.append("ret" if rs1 == 1 else f"jr\t{regs[rs1]}")
            else:
                raise AssertionError(f"unsupported RVC insn {hw:#06x} at +{pc:#x}")
            pc += 2
        else:
            word = struct.unpack_from("<I", code, pc)[0]
            opcode = word & 0x7f
            rd = (word >> 7) & 0x1f
            f3 = (word >> 12) & 0x7
            rs1 = (word >> 15) & 0x1f
            rs2 = (word >> 20) & 0x1f
            if opcode == 0x0f and f3 == 0:
                pred = "".join(
                    c
                    for b, c in ((8, "i"), (4, "o"), (2, "r"), (1, "w"))
                    if ((word >> 24) & 0xf) & b
                )
                succ = "".join(
                    c
                    for b, c in ((8, "i"), (4, "o"), (2, "r"), (1, "w"))
                    if ((word >> 20) & 0xf) & b
                )
                body.append(f"fence\t{pred},{succ}")
            elif opcode == 0x03 and f3 in load_ops:
                imm = (word >> 20) - (0x1000 if (word & 0x80000000) else 0)
                body.append(f"{load_ops[f3]}\t{regs[rd]},{imm}({regs[rs1]})")
            elif opcode == 0x23 and f3 in store_ops:
                imm_u = ((word >> 7) & 0x1f) | (((word >> 25) & 0x7f) << 5)
                imm = imm_u - (0x1000 if (imm_u & 0x800) else 0)
                body.append(f"{store_ops[f3]}\t{regs[rs2]},{imm}({regs[rs1]})")
            elif opcode == 0x1b and f3 == 0:
                imm = (word >> 20) - (0x1000 if (word & 0x80000000) else 0)
                body.append(
                    f"sext.w\t{regs[rd]},{regs[rs1]}"
                    if imm == 0
                    else f"addiw\t{regs[rd]},{regs[rs1]},{imm}"
                )
            elif opcode == 0x67 and f3 == 0 and rd == 0 and rs1 == 1 and (word >> 20) == 0:
                body.append("ret")
            else:
                raise AssertionError(f"unsupported RV64 insn {word:#010x} at +{pc:#x}")
            pc += 4
    return body


class Regbank(Protocol):
    """Minimal byte-oriented MMIO interface — 32-bit aligned reads + writes only."""

    def read_u32(self, offset: int) -> int: ...
    def write_u32(self, offset: int, value: int) -> None: ...
    def close(self) -> None: ...


class MmioRegbank:
    """/dev/mem-backed regbank for a PS PetaLinux runtime.

    Maps a 4 KB window at `base_addr` (default 0x4000_0000, matching the
    BD's M_AXI_GP0 segment). Word-aligned reads/writes only. Caller is
    responsible for close(); used as a context manager preferred.

    Requires the process to have read+write access to /dev/mem (root or
    CAP_SYS_RAWIO under PetaLinux). On most distros this is root-only.
    """

    DEFAULT_BASE_ADDR = 0x40000000
    DEFAULT_WINDOW_SIZE = 0x1000

    def __init__(self,
                 base_addr: int = DEFAULT_BASE_ADDR,
                 size: int = DEFAULT_WINDOW_SIZE,
                 devmem_path: str = "/dev/mem") -> None:
        if size % mmap.PAGESIZE != 0:
            raise ValueError(f"mmap window size {size} must be multiple of {mmap.PAGESIZE}")
        if base_addr % mmap.PAGESIZE != 0:
            raise ValueError(f"base_addr {base_addr:#x} must be page-aligned")

        self._base_addr = base_addr
        self._size = size
        self._fd: Optional[int] = None
        self._mmap: Optional[mmap.mmap] = None

        fd = os.open(devmem_path, os.O_RDWR | os.O_SYNC)
        try:
            self._mmap = mmap.mmap(
                fd, size,
                flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
                offset=base_addr,
            )
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        self._word_access: Optional[WordAccess] = None
        if platform.machine().lower().startswith(_BYTE_SPLITTING_MACHINES):
            view = ctypes.c_char.from_buffer(self._mmap)
            mapped_addr = ctypes.addressof(view)
            del view  # release the export so close() can unmap
            try:
                self._word_access = _libatomic_word_access(mapped_addr)
            except (OSError, AttributeError):
                self.close()
                raise
        logger.info("MmioRegbank mapped %#x..%#x via %s", base_addr,
                    base_addr + size - 1, devmem_path)

    def _check_offset(self, offset: int) -> None:
        if offset < 0 or offset + 4 > self._size:
            raise ValueError(f"offset {offset:#x} out of range [0,{self._size:#x})")
        if offset % 4 != 0:
            raise ValueError(f"offset {offset:#x} must be 4-byte aligned")

    def read_u32(self, offset: int) -> int:
        self._check_offset(offset)
        assert self._mmap is not None
        if self._word_access is not None:
            return self._word_access[1](offset)
        return struct.unpack_from("<I", self._mmap, offset)[0]

    def write_u32(self, offset: int, value: int) -> None:
        self._check_offset(offset)
        assert self._mmap is not None
        # DS-T2.17: MUST be a single 32-bit bus store. struct.pack_into is an
        # mmap memcpy that can emit sub-word/byte stores — harmless for normal
        # registers (the value settles) but it CORRUPTS write-strobe slots like
        # *_INJ_DATAIN, where every AXI write is one FIFO push. ctypes c_uint32
        # assignment is a single aligned word store on ARM (verified on HW:
        # the SPI/I2C injector preload round-trips only with this form) but
        # NOT on riscv64 (DS-T2.82) — there it goes through libatomic.
        if self._word_access is not None:
            self._word_access[0](offset, value & 0xFFFFFFFF)
            return
        ctypes.c_uint32.from_buffer(self._mmap, offset).value = value & 0xFFFFFFFF

    def close(self) -> None:
        self._word_access = None  # its raw address dies with the mapping
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "MmioRegbank":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class FakeRegbank:
    """In-memory regbank for tests + dev — dict offset → 32-bit word.

    `write_u32` can be observed by inspecting `.writes` (ordered list of
    (offset, value) tuples). Useful to assert that the data source poked
    TELEMETRY_SNAPSHOT_TRIGGER each tick.
    """

    def __init__(self, initial: Optional[Dict[int, int]] = None) -> None:
        self._words: Dict[int, int] = dict(initial or {})
        self.writes: list[tuple[int, int]] = []
        self.closed = False

    def read_u32(self, offset: int) -> int:
        if offset % 4 != 0:
            raise ValueError(f"offset {offset:#x} must be 4-byte aligned")
        return self._words.get(offset, 0) & 0xFFFFFFFF

    def write_u32(self, offset: int, value: int) -> None:
        if offset % 4 != 0:
            raise ValueError(f"offset {offset:#x} must be 4-byte aligned")
        self._words[offset] = value & 0xFFFFFFFF
        self.writes.append((offset, value & 0xFFFFFFFF))

    def close(self) -> None:
        self.closed = True

    def set(self, offset: int, value: int) -> None:
        """Test helper — set a slot without going through the writes log."""
        self._words[offset] = value & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Symbolic offsets (mirror project_regbank.yml — keep in sync).
# ---------------------------------------------------------------------------


class Regs:
    """Symbolic offsets into the deepskopion regbank.

    Kept in sync with project_regbank.yml by convention; a future
    `tools/regen_regbank.sh` extension could codegen this class directly
    from the YAML to remove the drift risk (DS-P3.<next>).
    """

    # ── Identity ──────────────────────────────────────────────────────────
    GIT_HASH                   = 0x0000
    SYSTEM_VERSION             = 0x0004
    BUILD_DATE                 = 0x0008
    REGBANK_VERSION            = 0x000C
    REGBANK_NUM_WORDS          = 0x0010
    REGBANK_DATA               = 0x0014
    CONFIG_NUM_WORDS           = 0x0018
    CONFIG_DATA                = 0x001C
    # RTL-P2.654: shared RTL↔PS-software ABI contract version. Reads 0 on
    # bitstreams that predate the baked slot — the PS harness treats 0 as
    # "unversioned" (warn, don't error). Mirror of provenance.CONTRACT_ABI_VER;
    # both are bumped together. The baked value is the RTL-side remainder
    # (declare in project_regbank.yml + emit via gen_build_metadata + rebuild).
    CONTRACT_ABI_VER           = 0x0028

    # ── Runtime control ───────────────────────────────────────────────────
    DATALOGGER_CONFIG             = 0x0020

    # ── DS-P2.51: EMIO loop + injector responders ────────────────────────
    EMIO_MUX_SEL               = 0x0024   # bit0=I2C, bit1=UART, bit2=SPI, bit3=EDGE×4←full SPI set (DS-P3.71), bit4=EDGE0/1←I2C SCL/SDA (DS-P3.69), bits[11:8]=MSS fabric vs stimulus (DS-P2.126)
    # DS-T9 / DS-T2.11: live EMIO-seam probe snapshot (RO). bit[31]=REA-triggered
    # sticky, bits[30:0]=the same 31-bit probe_word fed to the rr_rea REA core.
    # Bit map (LSB-first) is in debug/emio_seam.yml. Read for a static "does the
    # EMIO drive reach the injector/datalogger, does the injector drive back" check.
    EMIO_PROBE                 = 0x002C
    I2C_INJ_CONTROL            = 0x0030   # bit0=en, bits[7:1]=addr, bit9=fifo_rst
    I2C_INJ_DATAIN             = 0x0034   # write-strobe → push TX FIFO
    I2C_INJ_STATUS             = 0x0038   # bit0=empty, bit1=prog_full, bit2=err
    SPI_INJ_CONTROL            = 0x0040
    SPI_INJ_DATAIN             = 0x0044
    SPI_INJ_STATUS             = 0x0048

    # ── DS-T2.18: DDR-commit observers ───────────────────────────────────
    CLK_DDR_TICKS              = 0x004C   # clk_ddr liveness edge-count (RO)
    DDR_RESET_STATUS           = 0x0050   # bit0=rst_ddr live, bit1=ever-deasserted

    # ── Telemetry counters ───────────────────────────────────────────────
    HEARTBEAT_CNT              = 0x00C0
    FIFO_OVERFLOW_CNT    = 0x00C4
    FIFO_OVERFLOW_CNT_RSVD   = 0x00C8

    PKT_CNT_I2C                = 0x00D0
    PKT_CNT_SPI                = 0x00D4
    PKT_CNT_UART               = 0x00D8
    PKT_CNT_QSPI               = 0x00DC
    PKT_CNT_EDGE               = 0x00E0
    PKT_CNT_I2S                = 0x00E4
    PKT_CNT_CAN                = 0x00E8
    PKT_CNT_JTAG               = 0x00EC

    ERR_CNT_I2C                = 0x00F0
    ERR_CNT_SPI                = 0x00F4
    ERR_CNT_UART               = 0x00F8
    ERR_CNT_QSPI               = 0x00FC
    ERR_CNT_EDGE               = 0x0100
    ERR_CNT_CAN                = 0x0104
    ERR_CNT_JTAG               = 0x0108

    ARB_GRANT_CNT        = 0x0110
    ARB_GRANT_CNT_RSVD       = 0x0114

    DROP_CNT_I2C               = 0x0120
    DROP_CNT_SPI               = 0x0124
    DROP_CNT_UART              = 0x0128

    FIFO_HIGH_WATERMARK  = 0x0130
    FIFO_HIGH_WATERMARK_RSVD = 0x0134

    TELEMETRY_CTRL             = 0x0140
    TELEMETRY_SNAPSHOT_TRIGGER = 0x0144

    # ── DDR ring locator (DS-P3.27 Phase B) ──────────────────────────────
    RING_WRITE_PTR             = 0x0150
    RING_READ_PTR              = 0x0154
    RING_BASE_LOW              = 0x0158
    RING_BASE_HIGH             = 0x015C
    RING_BYTES                 = 0x0160
    RING_COMMIT_PTR            = 0x0164   # DS-T2.20: DMA commit addr (read up to THIS, not WRITE_PTR)
    DDR_BACKPRESSURE_CNT       = 0x0168   # DS-T2.22 mech B: ring-full backpressure cycles
    RING_ALARM_CNT             = 0x016C   # DS-P3.60: watermark alarm — bits[30:0]=#excursions≥75%, bit31=live alarm

    # ── DS-T2.26 probe: where is the in-band loss frame lost? ─────────────
    EMIT_VALID_CNT             = 0x0170   # cycles emitter holds a pending loss frame (err_fr_valid)
    EMIT_DONE_CNT              = 0x0174   # emitter→framer handshakes (valid & ready)

    # DS-P3.117: the per-edge max-frequency datum is surfaced IN-BAND on the
    # edge stream (EVT_EDGE_MAXFREQ, DOMAIN_EDGE|0x02 — see zs_msg.py /
    # decoders.py), NOT via a regbank CSR. The earlier MAXFREQ_IVAL_EDGE0..3
    # slots (0x0180..0x018C) were the wrong out-of-band channel and removed.

    # ── DS-P2.66 / DS-P3.118: eye-scan / BERT instrument (DOMAIN_EYE) ──────
    # eye_engine point-measure. EYE_CONTROL bit layout:
    #   bit0     start (0→1 launches one measurement; CSR-pulse)
    #   bit1     aggressor_en (drive adjacent pin with uncorrelated PRBS)
    #   bits[9:4]   phase_sel  (idelay_scan tap 0..31 — fine sub-step axis)
    #   bits[15:12] freq_sel   (clk_drp frequency-profile index)
    #   bit16    ps_step   (0→1 advances the CLKOUT1 sample_clk phase one MMCM step)
    #   bit17    ps_incdec (1=advance, 0=retard)
    #   bit18    ps_rst    (0→1 zeroes the fabric phase-step counter)
    # 48-bit err/bit counters are split lo(32)/hi(16) across two slots each.
    EYE_CONTROL                = 0x0200
    EYE_BIT_TARGET             = 0x0204   # PRBS bit periods to drive per point
    EYE_STATUS                 = 0x0208   # bit0=busy, bit1=done, bit2=locked
    EYE_ERR_COUNT_LO           = 0x020C
    EYE_ERR_COUNT_HI           = 0x0210   # bits[15:0]
    EYE_BIT_COUNT_LO           = 0x0214
    EYE_BIT_COUNT_HI           = 0x0218   # bits[15:0]
    EYE_PHASE_STATUS           = 0x021C   # bits[15:0]=ps_count (signed), bit16=ps_busy

    # ── Fabric-side stimulus generator (DS-P2.100, DS-T2.78) ──────────────
    # Master stimulus generators: UART TX, I2C master, SPI master, edge/pulse.
    # CONTROL = enable (+ per-protocol config); DATA = write-strobe trigger.
    STIM_UART_CONTROL          = 0x0220   # bit0=enable, bit1=break pattern, [15:8]=low bit-times
    STIM_UART_BAUD_DIV         = 0x0224   # bit period in clk_sys cycles
    STIM_UART_DATA             = 0x0228   # write-strobe: one 8N1 frame, or DATA[15:0] breaks when CONTROL[1]=1
    STIM_I2C_CONTROL           = 0x022C   # bit0=enable, bits[7:1]=target 7-bit addr
    STIM_I2C_CLK_DIV           = 0x0230   # SCL quarter-period in clk_sys cycles
    STIM_I2C_DATA              = 0x0234   # write-strobe: START+addr+data+STOP
    STIM_SPI_CONTROL           = 0x0238   # bit0=enable
    STIM_SPI_CLK_DIV           = 0x023C   # SCLK half-period in clk_sys cycles
    STIM_SPI_DATA              = 0x0240   # write-strobe: byte out MOSI framed by SS
    STIM_EDGE_CONTROL          = 0x0244   # bit0=enable, bits[19:4]=half-period
