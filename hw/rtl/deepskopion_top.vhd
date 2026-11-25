-- rr unknown-llm (AI-generated)
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-------------------------------------------------------------------------------
-- File       : deepskopion_top.vhd
-- Status     : DRAFT — user-authored Zybo Z7-20 (xc7z020clg400-1) top.
--              PoC scope: I2C + UART + SPI + Edge capturing, burst path only,
--              AXI4-DMA into PS DDR via rr-ddr-stream.
--
-- Architecture (BD-as-component pattern, mirrors linux_zybo's
-- stereoVision_top → StereoVisionBD):
--
--   sysclk pin is OWNED by the PS — the only fabric clock source is the
--   FCLK_CLK0 output from the BD. K17 (board 125 MHz osc) is left
--   unbound; if a separate fabric clock is needed later, add it as an
--   entity port and a clk_wiz instance.
--
--   PS DDR + FIXED_IO pins enter as inout ports on this entity and
--   propagate straight through to the deepskopion_bd component.
--
--                          fclk_clk0 (100 MHz, from BD)
--                                │
--                                ▼
--                          ┌──────────┐         capture_pin (64 from PMODs)
--                          │ rst sync │                  │
--                          │ + clkbuf │                  ▼
--                          └────┬─────┘         ┌────────────────┐
--                               │               │ datalogger_gather │
--          ┌────────────────────┴───────┐       │ (I2C/UART/SPI/ │
--          │                            │       │      Edge)     │
--          ▼                            │       └──────┬─────────┘
--    ┌──────────┐    deepskopion_bd     │              │ DOUT_{DATA,VALID}
--    │   PS7    │    M_AXI_GP0 (out)    │              ▼
--    │  +SCs    │ ───────────────────┐  │       ┌────────────────┐
--    │  +rst    │                    │  │       │ zs_msg_framer  │ ← 24-byte ZS frame
--    └──────────┘    S_AXI_HP0 (in)  │  │       └──────┬─────────┘   (5 hdr + 1 payload)
--          ▲             ◀───────────┼──┼──────────────┘
--          │                         │  │       ┌──────────────────────┐
--          │                         │  │       │   ddr_stream_axi     │
--          │                         │  │       │  (rr-ddr-stream IP)  │
--          │                         ▼  ▼ ◀─────┤ m_axi → S_AXI_HP0    │
--          │            ┌─────────────────────────────┐
--          │            │ system_regbank_wrapper      │
--          │            │  - axi-lite slave           │
--          │            │  - DATALOGGER_CONFIG out       │
--          │            │  - TELEMETRY_IN three-layer │
--          │            └─────────────────────────────┘
--          │
--    DDR_* / FIXED_IO_* pins  (PS dedicated balls, passthrough)
--
-- ============================================================================
--  DATALOGGER → DDR DATAPATH (what the SW parser sees in the ring)
-- ============================================================================
--
-- 1. CAPTURE  (PMOD pins OR PS EMIO → logical capture_pin matrix)
--    A per-protocol 2:1 mux sits between the physical source and each
--    "first instance" of a protocol's capture_pin slot. The mux selector
--    is the EMIO_MUX_SEL regbank slot (DS-P2.51 Phase 1):
--      EMIO_MUX_SEL bit[0] = 1 → I2C #4  sees PS EMIO I2C0     (else ja[1:0])
--      EMIO_MUX_SEL bit[1] = 1 → UART #12 sees PS EMIO UART0   (else jb[0])
--      EMIO_MUX_SEL bit[2] = 1 → SPI #8  sees PS EMIO SPI0     (else ja[7:4])
--    The "second instance" of I2C/UART (#5, #13) stays on its PMOD pads
--    unconditionally — handy when an external-vs-internal comparison is
--    useful, and zero-cost when it isn't (the regbank bit stays 0).
--
--    Linux userspace drives the PS EMIO controllers as /dev/ttyPS1 /
--    /dev/i2c-0 / /dev/spidev0; the fabric routes those signals into
--    the datalogger matrix with no external wiring. Chip self-validates.
--
--    datalogger_config.yml stays the source of truth for the underlying
--    slot indices — the mux is upstream of datalogger_gather:
--      capture_pin[13:12] ← I2C #4   PMOD ja[1:0]   OR  EMIO I2C0 SCL/SDA
--      capture_pin[15:14] ← I2C #5   PMOD ja[3:2]   (always external)
--      capture_pin[23:20] ← SPI #8   PMOD ja[7:4]   OR  EMIO SPI0 SS/MOSI/MISO/SCLK
--      capture_pin[36]    ← UART #12 PMOD jb[0]     OR  EMIO UART0 TX
--      capture_pin[37]    ← UART #13 PMOD jb[1]     (always external)
--      capture_pin[45:42] ← EDGE×4   PMOD jb[5:2]   (always external)
--
-- 2. PER-PROTOCOL DATALOGGERS — each emits 32-bit raw messages on its own
--    handshake.  The 32-bit word always begins with the 5-bit per-protocol
--    DEVICE ID in bits [31:27] (not the same as the framer's datalogger_id —
--    see caveat #1 below).
--      I2C    [31:27] dev_id  [26:24] type (100=EVENT, 00x=PAYLOAD, LSB=ACK)
--                     [23:8]  ts_slice (16b)  [7:0] event_code | sda_byte
--      UART   [31:27] dev_id  [26:24] type (100=STATUS, 010=PAYLOAD,
--                                          010+MSB=PAYLOAD9, 001=CONFIG)
--                     [23:9]  ts_slice (15b)  [8:0]  status/byte
--      SPI    same EVENT/PAYLOAD shape as I2C (qspi_datalogger in 4-pin mode)
--      EDGE   [31:27] dev_id  [26:24] {start,fall,edge} flags
--                     [23:8]  ts_slice  [7:0] ts_extension
--
-- 3. ARBITER FAN-IN (datalogger_gather.vhd)
--    Per-protocol round-robin arbiter inside each GEN_<PROTO>_BLOCK when
--    ≥2 instances (else pass-through), feeding a 4-input 2nd-layer burst
--    arbiter (C_BURST_{I2C,UART,SPI,EDGE}_INDEX → i_1st_layer_burst_arbiter).
--    Top output: DOUT_DATA[31:0] + DOUT_VALID, gated by
--    DOUT_REQ (back-pressure to the chain).  The stream-tier path is
--    exported but unused in this bitstream (C_NUM_STREAMS = 0; the arbiter
--    instance is generate-gated behind that — see DS-P2.35).
--
-- 4. ZS FRAMER  (zs_msg_framer.vhd) — see WHY-IS-THIS-NEEDED block below.
--    On every accepted source word, a 6-cycle FSM emits exactly
--    6 × 32-bit words = 24 bytes on OUT_DATA/OUT_VALID.  While emitting,
--    IN_READY drops and the burst arbiter holds.
--
-- 5. DDR DMA (rr.ddr_stream_axi)
--    fifo_depth_g=9, ddr_data_width_g=128, ddr_base_addr_g=G_DDR_BASE_ADDR,
--    buffer_width_g=28 → 256 MB ring.  Packs the 32-bit framer words into
--    128-bit AXI4 bursts on S_AXI_HP0 (HP port, 125 MHz / fclk_clk1).
--    The core's RING_WRITE_PTR counts the 32-bit framer words the stream
--    accepts (+6 per source message: 5 header + 1 payload).
--
-- ============================================================================
--  WHY THE ZS FRAMER EXISTS  ("ZS" = Zynq Datalogger, ASCII magic 0x5A53)
-- ============================================================================
--
-- Before DS-P3.28 the raw 32-bit DOUT words were pushed verbatim into
-- the DDR ring.  PS-side DdrRingDataSource (Phase B) walked the ring looking
-- for a self-describing record and saw nothing it could anchor on — every
-- byte was a `zs_resync` alarm.  Real hardware doesn't give the consumer the
-- luxury of "the producer never drops anything, never overruns the ring",
-- so the ring needs a self-describing wire format the SW can recover from.
-- The ZS framer is that wire format.  It buys:
--
--   * **Resync anchor**: the 0x5A53 magic ('Z','S' LE) lets the parser
--     re-acquire frame after a torn write, ring wrap, or partial mmap read.
--     PS scans forward for the magic; no out-of-band length channel needed.
--   * **Length-delimited records**: `header_len` (5 words) + `payload_len`
--     (bytes) tell the parser how far to advance.  Variable-size payloads
--     come for free when wider dataloggers land (multi-word UART batches, full
--     I2C transactions, SPI frames).
--   * **Source identification**: the `datalogger_id` byte multiplexes many
--     protocol instances onto one ring without sideband metadata.
--   * **Type field**: `msg_class` (DATA/STATUS/TRIGGER) + `msg_type`
--     (Severity|Domain|EventID) routes records on the SW side without
--     having to decode the payload first.
--   * **64-bit timestamp**: the inner 32-bit datalogger timestamps wrap every
--     2^32/100 MHz ≈ 42.95 s — useless for long captures.  The framer
--     header carries a full 64-bit counter (DS-P3.30, 2026-05-17 —
--     ~5800 years at 100 MHz). The per-protocol datalogger chain still
--     consumes a 32-bit slice via the same counter's low 32 bits.
--   * **Flags channel**: overflow / truncated / dropped-sample bits live
--     in 16 bits adjacent to the payload, not buried in protocol bytes.
--
-- Why 24 bytes and not 16/20?  20 bytes is the header alone (5 × 32-bit
-- words, `header_len`=5).  The PoC framer carries exactly one 32-bit
-- payload word per frame (G_PAYLOAD_WORDS=1), so the total on-wire frame
-- is 20 + 4 = 24 bytes.  Widening G_PAYLOAD_WORDS keeps the header
-- shape; only `payload_len` changes.
--
-- Mirror artefacts (keep them in lockstep when extending the format):
--   * hw/rtl/pkg/zs_msg_pkg.vhd                       (PL constants)
--   * docs/software/data_plane/message_formats/common_header.md   (spec)
--   * sw/stream/zs_msg.py                              (PS parser)
--
-- ============================================================================
--  WIRE FORMAT IN DDR  (24 bytes per source message, little-endian)
-- ============================================================================
--
--    Offset  Size  Field         v1 value (DS-P3.28)
--    ------  ----  ------------  ------------------------------------------
--     0      2     magic         0x5A53 ('Z','S')   bytes [0x53,0x5A] in mem
--     2      1     version       0x01
--     3      1     header_len    0x05               5 × 32-bit words = 20 B
--     4      1     datalogger_id    per-protocol device_id (5b) zero-ext to 8b
--     5      1     msg_class     0x00 DATA
--     6      2     msg_type      SEV_INFO | DOMAIN_<PROTO> | payload[26:24]
--     8      8     timestamp     LE uint64; full 64-bit clk_sys-tick count
--    16      2     payload_len   0x0004 (4 bytes per frame in PoC)
--    18      2     flags         0x0000
--    20      4     payload       raw 32-bit datalogger message verbatim
--                                (see "PER-PROTOCOL DATALOGGERS" above for
--                                 the layout inside this word)
--
-- ============================================================================
--  SW LOCATOR — four read-only regbank slots tell the PS where the ring is
-- ============================================================================
--
--    RING_BASE_LOW   = G_DDR_BASE_ADDR  (low 32b of physical base)
--    RING_BASE_HIGH  = 0                (Zynq-7 is 32-bit physical)
--    RING_BYTES      = 2^G_DDR_WIN_WIDTH = 256 MB
--    RING_WRITE_PTR  = framer words the DDR stream accepted (live; +6 per
--                      source message). An enqueue count, not a byte offset.
--    RING_COMMIT_PTR = the DMA's committed DDR write address (byte offset)
--
--  Walk recipe:
--      the position is RING_COMMIT_PTR; RING_WRITE_PTR * 4 diverges from it
--      (variable-length frames, whole-DDR-word commits) and is not a walk input.
--      scan forward at the committed offset for magic==0x5A53, then advance by
--      header_len*4 + payload_len.
--
-- ============================================================================
--  v1 CAVEATS (important for the SW parser)
-- ============================================================================
--
--  1. `datalogger_id` carries the per-protocol 5-bit device_id, zero-extended
--     to 8 bits (DS-P3.29 — landed 2026-05-17). Sourced from payload[31:27]
--     at the gather block. Frames now land on per-protocol channels in
--     RouteWave (uart_byte, i2c_frame, etc.) rather than the generic
--     pl_heartbeat lane.
--  2. `msg_type` = (SEV_INFO | DOMAIN_<PROTO> | payload[26:24])
--     (DS-P3.29). Severity is INFO for every frame in v1; per-event
--     ERROR severity (UART parity, I2C NACK, etc.) is a follow-on.
--  3. `timestamp` is a full 64-bit free-running counter (DS-P3.30,
--     2026-05-17). Wraparound at 2^64 / 100 MHz ≈ 5800 years.
--  4. No per-frame alignment marker beyond `magic`.  If a frame is truncated
--     by ring overrun, the next valid frame still starts at the next 0x5A53.
--  5. No back-pressure to the PS.  Ring overrun is detected after the fact
--     via the committed offset (RING_COMMIT_PTR) advancing past RING_READ_PTR +
--     RING_BYTES; DdrRingDataSource enforces this on the SW side.
--
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
library rr_rea;
use tich.rom_info_pkg.all;        -- C_GIT_HASH, C_SYSTEM_VERSION, C_BUILD_DATE, C_REGBANK_VERSION
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.system_regbank_pkg.all;  -- t_memArrayReg, C_INDEX_*, C_REGBANK_NWORDS
use tich.zs_msg_pkg.all;          -- C_ZS_DOMAIN_SYS, make_msg_type, ...
use tich.axi_pkg.C_ZYNQ7_S_AXI_HP_MAX_BURST;  -- DS-T2.65: single-sourced AXI3 write-burst cap

-- rr-ddr-stream lives in library `rr` (its ip.yml). Don't `use ... .all` —
-- the package re-declares constants (C_DATALOGGER_MSG_WIDTH etc.) that
-- collide with tich.datalogger_pkg's, making bare references ambiguous.
-- Qualify types fully (rr.ddr_stream_pkg.axi4_h2c_bus_t) instead.
library rr;

library ctu_can_fd_rtl;            -- DS-P3.109: CTU CAN FD soft cores (FD traffic source)

entity deepskopion_top is
  generic (
    -- DDR ring buffer parameters — PS-readable region in HP0 window.
    -- Default 256 MB at offset 0x1000_0000 of PS DDR.
    G_DDR_BASE_ADDR : unsigned(31 downto 0) := x"1000_0000";
    G_DDR_WIN_WIDTH : integer               := 28  -- 2^28 = 256 MB
  );
  port (
    -- ── PS DDR (dedicated bank) ──────────────────────────────────────────
    DDR_addr          : inout std_logic_vector(14 downto 0);
    DDR_ba            : inout std_logic_vector(2 downto 0);
    DDR_cas_n         : inout std_logic;
    DDR_ck_n          : inout std_logic;
    DDR_ck_p          : inout std_logic;
    DDR_cke           : inout std_logic;
    DDR_cs_n          : inout std_logic;
    DDR_dm            : inout std_logic_vector(3 downto 0);
    DDR_dq            : inout std_logic_vector(31 downto 0);
    DDR_dqs_n         : inout std_logic_vector(3 downto 0);
    DDR_dqs_p         : inout std_logic_vector(3 downto 0);
    DDR_odt           : inout std_logic;
    DDR_ras_n         : inout std_logic;
    DDR_reset_n       : inout std_logic;
    DDR_we_n          : inout std_logic;

    -- ── PS dedicated FIXED_IO (MIO + clk + porb + srstb) ────────────────
    FIXED_IO_ddr_vrn  : inout std_logic;
    FIXED_IO_ddr_vrp  : inout std_logic;
    FIXED_IO_mio      : inout std_logic_vector(53 downto 0);
    FIXED_IO_ps_clk   : inout std_logic;
    FIXED_IO_ps_porb  : inout std_logic;
    FIXED_IO_ps_srstb : inout std_logic;

    -- ── User-facing PL IO ────────────────────────────────────────────────
    -- Names match routertl/boards/zybo-z7-20/pinout.yml entries; rr board
    -- pins-for-target generates the XDC mapping automatically. JA + JB
    -- cover the PoC's capture_pin indices (12..15, 20..23, 36..37, 42..45
    -- per datalogger_config.yml); JC and JD are reserved for datalogger growth.
    led : out std_logic_vector(3 downto 0);  -- M14/M15/G14/D18
    ja  : in  std_logic_vector(7 downto 0);  -- PMOD JA (XADC bank 35)
    jb  : in  std_logic_vector(7 downto 0)   -- PMOD JB (diff-pair capable)
  );
end entity deepskopion_top;

architecture rtl of deepskopion_top is

  ---------------------------------------------------------------------------
  -- deepskopion_bd — Vivado-generated wrapper sourced from
  -- hw/bd/deepskopion_bd.tcl. Provides the PS7, two smartconnects,
  -- proc_sys_reset, and exposes M_AXI_GP0 / S_AXI_HP0 / fclk_clk0 /
  -- fclk_aresetn alongside the PS dedicated pins.
  ---------------------------------------------------------------------------
  component deepskopion_bd is
    port (
      -- PS DDR
      DDR_addr          : inout std_logic_vector(14 downto 0);
      DDR_ba            : inout std_logic_vector(2 downto 0);
      DDR_cas_n         : inout std_logic;
      DDR_ck_n          : inout std_logic;
      DDR_ck_p          : inout std_logic;
      DDR_cke           : inout std_logic;
      DDR_cs_n          : inout std_logic;
      DDR_dm            : inout std_logic_vector(3 downto 0);
      DDR_dq            : inout std_logic_vector(31 downto 0);
      DDR_dqs_n         : inout std_logic_vector(3 downto 0);
      DDR_dqs_p         : inout std_logic_vector(3 downto 0);
      DDR_odt           : inout std_logic;
      DDR_ras_n         : inout std_logic;
      DDR_reset_n       : inout std_logic;
      DDR_we_n          : inout std_logic;
      -- PS FIXED_IO
      FIXED_IO_ddr_vrn  : inout std_logic;
      FIXED_IO_ddr_vrp  : inout std_logic;
      FIXED_IO_mio      : inout std_logic_vector(53 downto 0);
      FIXED_IO_ps_clk   : inout std_logic;
      FIXED_IO_ps_porb  : inout std_logic;
      FIXED_IO_ps_srstb : inout std_logic;
      -- M_AXI_GP0 (master out, AXI4-Lite, 32-bit data / 32-bit addr)
      M_AXI_GP0_araddr  : out   std_logic_vector(31 downto 0);
      M_AXI_GP0_arprot  : out   std_logic_vector(2 downto 0);
      M_AXI_GP0_arready : in    std_logic;
      M_AXI_GP0_arvalid : out   std_logic;
      M_AXI_GP0_awaddr  : out   std_logic_vector(31 downto 0);
      M_AXI_GP0_awprot  : out   std_logic_vector(2 downto 0);
      M_AXI_GP0_awready : in    std_logic;
      M_AXI_GP0_awvalid : out   std_logic;
      M_AXI_GP0_bready  : out   std_logic;
      M_AXI_GP0_bresp   : in    std_logic_vector(1 downto 0);
      M_AXI_GP0_bvalid  : in    std_logic;
      M_AXI_GP0_rdata   : in    std_logic_vector(31 downto 0);
      M_AXI_GP0_rready  : out   std_logic;
      M_AXI_GP0_rresp   : in    std_logic_vector(1 downto 0);
      M_AXI_GP0_rvalid  : in    std_logic;
      M_AXI_GP0_wdata   : out   std_logic_vector(31 downto 0);
      M_AXI_GP0_wready  : in    std_logic;
      M_AXI_GP0_wstrb   : out   std_logic_vector(3 downto 0);
      M_AXI_GP0_wvalid  : out   std_logic;
      -- DS-P3.109: 2× APB master (M_AXI_GP1 → axi_apb_bridge) → CTU CAN FD slaves
      CANFD0_APB_paddr   : out std_logic_vector(31 downto 0);
      CANFD0_APB_penable : out std_logic;
      CANFD0_APB_prdata  : in  std_logic_vector(31 downto 0);
      CANFD0_APB_pready  : in  std_logic_vector(0 to 0);
      CANFD0_APB_psel    : out std_logic_vector(0 to 0);
      CANFD0_APB_pslverr : in  std_logic_vector(0 to 0);
      CANFD0_APB_pwdata  : out std_logic_vector(31 downto 0);
      CANFD0_APB_pwrite  : out std_logic;
      CANFD1_APB_paddr   : out std_logic_vector(31 downto 0);
      CANFD1_APB_penable : out std_logic;
      CANFD1_APB_prdata  : in  std_logic_vector(31 downto 0);
      CANFD1_APB_pready  : in  std_logic_vector(0 to 0);
      CANFD1_APB_psel    : out std_logic_vector(0 to 0);
      CANFD1_APB_pslverr : in  std_logic_vector(0 to 0);
      CANFD1_APB_pwdata  : out std_logic_vector(31 downto 0);
      CANFD1_APB_pwrite  : out std_logic;
      -- S_AXI_HP0 (slave in, AXI4, 128-bit data / 32-bit addr / 2-bit id)
      S_AXI_HP0_araddr  : in    std_logic_vector(31 downto 0);
      S_AXI_HP0_arburst : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_arcache : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_arid    : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_arlen   : in    std_logic_vector(7 downto 0);
      S_AXI_HP0_arlock  : in    std_logic_vector(0 downto 0);
      S_AXI_HP0_arprot  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_arqos   : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_arready : out   std_logic;
      S_AXI_HP0_arsize  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_arvalid : in    std_logic;
      S_AXI_HP0_awaddr  : in    std_logic_vector(31 downto 0);
      S_AXI_HP0_awburst : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_awcache : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_awid    : in    std_logic_vector(1 downto 0);
      S_AXI_HP0_awlen   : in    std_logic_vector(7 downto 0);
      S_AXI_HP0_awlock  : in    std_logic_vector(0 downto 0);
      S_AXI_HP0_awprot  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_awqos   : in    std_logic_vector(3 downto 0);
      S_AXI_HP0_awready : out   std_logic;
      S_AXI_HP0_awsize  : in    std_logic_vector(2 downto 0);
      S_AXI_HP0_awvalid : in    std_logic;
      S_AXI_HP0_bid     : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_bready  : in    std_logic;
      S_AXI_HP0_bresp   : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_bvalid  : out   std_logic;
      S_AXI_HP0_rdata   : out   std_logic_vector(127 downto 0);
      S_AXI_HP0_rid     : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_rlast   : out   std_logic;
      S_AXI_HP0_rready  : in    std_logic;
      S_AXI_HP0_rresp   : out   std_logic_vector(1 downto 0);
      S_AXI_HP0_rvalid  : out   std_logic;
      S_AXI_HP0_wdata   : in    std_logic_vector(127 downto 0);
      S_AXI_HP0_wlast   : in    std_logic;
      S_AXI_HP0_wready  : out   std_logic;
      S_AXI_HP0_wstrb   : in    std_logic_vector(15 downto 0);
      S_AXI_HP0_wvalid  : in    std_logic;
      -- Clock + reset out — two domains. fclk_clk0 / fclk_aresetn
      -- is the AXI-Lite + system clock (100 MHz); fclk_clk1 /
      -- fclk_clk1_aresetn is the HP0 DMA-path clock (125 MHz —
      -- not 150, the Zynq-7 PS PLL doesn't reliably lock at exactly
      -- 150 from the typical Zybo clock plan).
      -- Vivado emits reset ports as std_logic_vector(0 to 0).
      fclk_clk0           : out std_logic;
      fclk_aresetn        : out std_logic_vector(0 to 0);
      fclk_clk1           : out std_logic;
      fclk_clk1_aresetn   : out std_logic_vector(0 to 0);
      -- DS-P2.51 Phase 1: PS EMIO peripherals routed to the fabric for
      -- the self-validation testbench. Flat std_logic per leg; the
      -- top body models the open-drain busses (I2C) and the master/
      -- (future) slave dialogue (SPI) before feeding the datalogger.
      UART0_TX            : out std_logic;
      UART0_RX            : in  std_logic;
      I2C0_SCL_I          : in  std_logic;
      I2C0_SCL_O          : out std_logic;
      I2C0_SCL_T          : out std_logic;
      I2C0_SDA_I          : in  std_logic;
      I2C0_SDA_O          : out std_logic;
      I2C0_SDA_T          : out std_logic;
      SPI0_SCLK_I         : in  std_logic;
      SPI0_SCLK_O         : out std_logic;
      SPI0_SCLK_T         : out std_logic;
      SPI0_MOSI_I         : in  std_logic;
      SPI0_MOSI_O         : out std_logic;
      SPI0_MOSI_T         : out std_logic;
      SPI0_MISO_I         : in  std_logic;
      SPI0_MISO_O         : out std_logic;
      SPI0_MISO_T         : out std_logic;
      SPI0_SS_I           : in  std_logic;
      SPI0_SS_O           : out std_logic;
      SPI0_SS_T           : out std_logic;
      -- DS-T2.16: EMIO GPIO (width 1). GPIO_O(0) is the fabric SPI chip-select
      -- (cs-gpios in the DT) that replaces the dead native SPI0_SS_O.
      GPIO_O              : out std_logic_vector(0 downto 0);
      GPIO_I              : in  std_logic_vector(0 downto 0);
      GPIO_T              : out std_logic_vector(0 downto 0);
      -- DS-P3.102: CAN0 + CAN1 on EMIO (ds_emio_can.tcl ps7_hook). Digital
      -- pre-transceiver CAN — PHY_TX = controller out, PHY_RX = controller in.
      -- deepskopion_top wires the two into a 2-node fabric wired-AND bus (no
      -- transceiver needed) that the passive can_datalogger (id 20) taps.
      CAN0_PHY_TX         : out std_logic;
      CAN0_PHY_RX         : in  std_logic;
      CAN1_PHY_TX         : out std_logic;
      CAN1_PHY_RX         : in  std_logic;
      -- DS-P3.60 / DS-P3.109: PL→PS fabric interrupts (IRQ_F2P width 3).
      --   [0] ring watermark alarm → GIC SPI #29 (level-high)   — ds_irq_f2p_on.tcl
      --   [1] CTU CAN FD core A irq → GIC SPI #30 (ctucanfd can2)
      --   [2] CTU CAN FD core B irq → GIC SPI #31 (ctucanfd can3)
      -- Width widened by PCW_NUM_F2P_INTR_INPUTS {3} in ds_irq_f2p_on.tcl.
      IRQ_F2P             : in  std_logic_vector(2 downto 0)
    );
  end component deepskopion_bd;

  ---------------------------------------------------------------------------
  -- Clock + reset (from BD's fclk_clk0 / fclk_aresetn)
  ---------------------------------------------------------------------------
  signal clk_sys      : std_logic;                   -- 100 MHz from BD FCLK_CLK0
  signal rst_sys      : std_logic;
  signal aresetn      : std_logic_vector(0 to 0);
  signal clk_ddr      : std_logic;                   -- 125 MHz from BD FCLK_CLK1
  signal rst_ddr      : std_logic;
  signal ddr_aresetn  : std_logic_vector(0 to 0);

  ---------------------------------------------------------------------------
  -- AXI4 master record bundles to/from rr-ddr-stream (internal-only)
  ---------------------------------------------------------------------------
  signal axi_h2c : rr.ddr_stream_pkg.axi4_h2c_bus_t;
  signal axi_c2h : rr.ddr_stream_pkg.axi4_c2h_bus_t;

  -- DS-P2.45: rr-ddr-stream RR--P3.6 widened the AXI record data lanes
  -- from 128 to 512 bits (independent user/DDR widths). The Zynq-7 BD's
  -- S_AXI_HP0 slave is 128 bits with a 16-bit WSTRB. Slice the record
  -- to the BD's native width at the boundary; the upper record bits
  -- stay constant zero (the design only drives [ddr_data_width_g-1:0]).
  signal s_axi_hp0_rdata : std_logic_vector(127 downto 0);
  signal s_axi_hp0_wdata : std_logic_vector(127 downto 0);
  signal s_axi_hp0_wstrb : std_logic_vector(15 downto 0);

  ---------------------------------------------------------------------------
  -- AXI-Lite slave from BD's M_AXI_GP0 (flat signals; sliced into regbank's
  -- 11-bit address window).
  ---------------------------------------------------------------------------
  signal gp0_awaddr  : std_logic_vector(31 downto 0);
  signal gp0_awprot  : std_logic_vector(2 downto 0);
  signal gp0_awvalid : std_logic;
  signal gp0_awready : std_logic;
  signal gp0_wdata   : std_logic_vector(31 downto 0);
  signal gp0_wstrb   : std_logic_vector(3 downto 0);
  signal gp0_wvalid  : std_logic;
  signal gp0_wready  : std_logic;
  signal gp0_bresp   : std_logic_vector(1 downto 0);
  signal gp0_bvalid  : std_logic;
  signal gp0_bready  : std_logic;
  signal gp0_araddr  : std_logic_vector(31 downto 0);
  signal gp0_arprot  : std_logic_vector(2 downto 0);
  signal gp0_arvalid : std_logic;
  signal gp0_arready : std_logic;
  signal gp0_rdata   : std_logic_vector(31 downto 0);
  signal gp0_rresp   : std_logic_vector(1 downto 0);
  signal gp0_rvalid  : std_logic;
  signal gp0_rready  : std_logic;

  ---------------------------------------------------------------------------
  -- Free-running timestamp counter (DS-P3.30, 2026-05-17).
  --
  -- Full counter is 64-bit so the zs_msg header's timestamp field
  -- carries an absolute time that doesn't wrap on any practical
  -- capture (~5800 years at 100 MHz). The per-protocol datalogger chain
  -- still consumes a 32-bit TIMESTAMP slice (C_TIMESTAMP_WIDTH) — the
  -- datalogger payload formats embed only short ts slices anyway — so
  -- we expose two views of the SAME counter:
  --   timestamp   : std_logic_vector(31:0)  → datalogger_gather port
  --                                            (per-protocol slicing)
  --   timestamp64 : std_logic_vector(63:0)  → zs_msg_framer header
  ---------------------------------------------------------------------------
  signal timestamp_r : unsigned(63 downto 0) := (others => '0');
  signal timestamp   : std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);

  ---------------------------------------------------------------------------
  -- Per-datalogger CONFIG bus — driven by regbank DATALOGGER_CONFIG.
  ---------------------------------------------------------------------------
  signal config_word : std_logic_vector(C_DATALOGGER_CONFIG_WIDTH-1 downto 0);

  ---------------------------------------------------------------------------
  -- datalogger_gather → zs_msg_framer wiring. DS-P3.29: per-source
  -- metadata (datalogger_id + msg_type) now threaded through the chain
  -- alongside the 32-bit payload.
  ---------------------------------------------------------------------------
  signal dout_payload    : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal dout_datalogger_id : std_logic_vector(7 downto 0);
  signal dout_msg_type   : std_logic_vector(15 downto 0);
  signal dout_valid      : std_logic;
  signal fifo_csr        : t_fifo_csr;
  signal dout_req             : std_logic;
  signal ddr_busy              : std_logic;
  -- DDR-P3.10: input-side back-pressure from rr-ddr-stream's skid.
  signal ddr_s_axis_ready      : std_logic;

  ---------------------------------------------------------------------------
  -- Heartbeat / liveness LEDs
  ---------------------------------------------------------------------------
  signal hb_div : unsigned(26 downto 0) := (others => '0');

  ---------------------------------------------------------------------------
  -- Logical capture_pin matrix — 64-bit bus, gather slices per
  -- datalogger_config.yml indices.
  ---------------------------------------------------------------------------
  signal capture_pin : std_logic_vector(C_NUM_DATALOGGER_PINS-1 downto 0) := (others => '0');

  ---------------------------------------------------------------------------
  -- Telemetry fan-in into regbank wrapper.
  ---------------------------------------------------------------------------
  signal heartbeat_cnt           : unsigned(31 downto 0) := (others => '0');
  signal fifo_overflow_cnt : unsigned(31 downto 0) := (others => '0');
  signal telemetry_in            : t_memArrayReg := (others => (data => (others => '0'), valid => '0'));

  -- Per-protocol PKT_CNT_*/ERR_CNT_* bundle driven by datalogger_gather
  -- (DS-P3.26). Indexed by datalogger_config_pkg.C_PROTO_IDX_<NAME>.
  signal proto_counters : t_proto_counter_array(0 to C_NUM_PROTO_COUNTERS-1)
                        := (others => C_INIT_PROTO_COUNTER);

  ---------------------------------------------------------------------------
  -- DS-P2.51 Phase 1: PS EMIO wires + per-protocol pre-gather mux.
  --
  -- EMIO_MUX_SEL slot decode — only the low 3 bits are meaningful:
  --   bit[0] = I2C #4  source select  (0 = PMOD, 1 = EMIO I2C0)
  --   bit[1] = UART #12 source select (0 = PMOD, 1 = EMIO UART0)
  --   bit[2] = SPI #8  source select  (0 = PMOD, 1 = EMIO SPI0)
  ---------------------------------------------------------------------------
  signal regs_out_local : t_memArrayReg;
  signal emio_mux_sel   : std_logic_vector(2 downto 0);
  signal emio_edge_sel  : std_logic;   -- DS-P3.71: EMIO_MUX_SEL bit[3] (edge self-test)

  -- EMIO legs (out from BD, into our bus model)
  signal uart0_tx_w     : std_logic;
  signal uart0_rx_w     : std_logic;  -- driven back into BD; idle '1' in Phase 1
  signal i2c0_scl_o_w   : std_logic;
  signal i2c0_scl_t_w   : std_logic;
  signal i2c0_sda_o_w   : std_logic;
  signal i2c0_sda_t_w   : std_logic;
  signal spi0_sclk_o_w  : std_logic;
  signal spi0_sclk_t_w  : std_logic;  -- unused in master mode; PS keeps '0'
  signal spi0_mosi_o_w  : std_logic;
  signal spi0_mosi_t_w  : std_logic;
  signal spi0_miso_o_w  : std_logic;  -- ignored (slave-mode tri-state); we
  signal spi0_miso_t_w  : std_logic;  -- model master → no MISO drive
  signal spi0_ss_o_w    : std_logic;
  signal spi0_ss_t_w    : std_logic;

  -- DS-P3.102: CAN0/CAN1 EMIO legs + the fabric wired-AND CAN bus. The two PS
  -- CAN controllers (EMIO) form a real 2-node bus entirely in fabric (open-drain
  -- wired-AND: dominant '0' wins, recessive '1' idle), so no transceiver is
  -- needed for bring-up. The passive can_datalogger (id 20) taps it on capture_pin(28).
  -- Idle (both controllers recessive) → can_bus_w = '1' → pin 28 idle high, so
  -- this is harmless when CAN is unused. See docs/BRING_UP_CAN.md.
  signal can0_phy_tx_w  : std_logic;   -- PS CAN0 controller TX (into the AND)
  signal can1_phy_tx_w  : std_logic;   -- PS CAN1 controller TX (into the AND)
  signal can_bus_w      : std_logic;   -- the wired-AND bus (→ both RX + datalogger)
  -- DS-P3.109: 2× CTU CAN FD soft cores join the SAME wired-AND bus as FD traffic
  -- nodes (classic PS-CAN and FD-CTU coexist — whichever pair is enabled drives;
  -- the disabled pair idles recessive '1' and the AND is unaffected). Build 1:
  -- APB idle-tied (cores present → validates Z7-20 fit/timing); the live loop wires
  -- M_AXI_GP1 → 2× axi_apb_bridge for SW config + IRQ_F2P + the ctucanfd driver.
  signal ctu_a_tx_w     : std_logic;   -- CTU CAN FD node A TX (into the AND)
  signal ctu_b_tx_w     : std_logic;   -- CTU CAN FD node B TX (into the AND)
  signal ctu_arstn_w    : std_logic;   -- active-low reset for the CTU cores
  signal ctu_a_irq_w    : std_logic;   -- CTU A irq → IRQ_F2P[1] (GIC SPI #30)
  signal ctu_b_irq_w    : std_logic;   -- CTU B irq → IRQ_F2P[2] (GIC SPI #31)
  signal irq_f2p_w      : std_logic_vector(2 downto 0);  -- [2:0] = {ctu_b, ctu_a, ring_alarm}
  -- DS-P3.109: APB buses from the BD (M_AXI_GP1 → axi_apb_bridge, APB3 — no
  -- pstrb/pprot) → CTU s_apb_* slaves. psel/pready/pslverr are 1-bit (0 to 0)
  -- vectors as the bridge emits them. BD master → CTU: paddr/penable/psel/pwrite/
  -- pwdata; CTU → BD master: prdata/pready/pslverr.
  signal canfd0_apb_paddr_w   : std_logic_vector(31 downto 0);
  signal canfd0_apb_penable_w : std_logic;
  signal canfd0_apb_psel_w    : std_logic_vector(0 to 0);
  signal canfd0_apb_pwrite_w  : std_logic;
  signal canfd0_apb_pwdata_w  : std_logic_vector(31 downto 0);
  signal canfd0_apb_prdata_w  : std_logic_vector(31 downto 0);
  signal canfd0_apb_pready_w  : std_logic_vector(0 to 0);
  signal canfd0_apb_pslverr_w : std_logic_vector(0 to 0);
  signal canfd1_apb_paddr_w   : std_logic_vector(31 downto 0);
  signal canfd1_apb_penable_w : std_logic;
  signal canfd1_apb_psel_w    : std_logic_vector(0 to 0);
  signal canfd1_apb_pwrite_w  : std_logic;
  signal canfd1_apb_pwdata_w  : std_logic_vector(31 downto 0);
  signal canfd1_apb_prdata_w  : std_logic_vector(31 downto 0);
  signal canfd1_apb_pready_w  : std_logic_vector(0 to 0);
  signal canfd1_apb_pslverr_w : std_logic_vector(0 to 0);

  -- DS-T2.16: EMIO GPIO bit 0 = fabric-driven SPI chip-select. The Linux SPI
  -- core toggles it around each transfer via cs-gpios (project.yml
  -- linux.dt_nodes) — replacing the native SPI0_SS_O that never asserts at the
  -- EMIO seam. spi0_cs_gpio_w is the resolved CS the SPI injector watches.
  signal gpio_o_w       : std_logic_vector(0 downto 0);
  signal gpio_i_w       : std_logic_vector(0 downto 0);
  signal gpio_t_w       : std_logic_vector(0 downto 0);
  signal spi0_cs_gpio_w : std_logic;
  -- DS-T2.18 timing: registered copy of the GPIO CS for the high-fanout SPI
  -- DATALOGGER path (gpio→resolution→ds_emio_mux→qspi_datalogger was a long
  -- combinational net, WNS -0.051). Registering it breaks the path; the 1-cycle
  -- (10 ns) skew vs SCLK/MOSI is negligible at SPI's ~µs bit period. The SPI
  -- INJECTOR keeps the unregistered spi0_cs_gpio_w (its path met timing in v9).
  signal spi0_cs_gpio_r : std_logic := '1';

  -- Bus levels (the actual wire the datalogger sees + the PS reads back via _I)
  signal i2c0_bus_scl_w : std_logic;
  signal i2c0_bus_sda_w : std_logic;
  signal spi0_bus_sclk_w: std_logic;
  signal spi0_bus_mosi_w: std_logic;
  signal spi0_bus_miso_w: std_logic;
  signal spi0_bus_ss_w  : std_logic;

  -- Muxed sources feeding capture_pin (chosen between PMOD pin and EMIO bus).
  signal src_i2c_scl_w  : std_logic;
  signal src_i2c_sda_w  : std_logic;
  signal src_uart_tx_w  : std_logic;  -- COMBINATIONAL EMIO UART0 TX (async to clk_sys);
                                      -- conditioned by the gather's per-pin glitch_filter
                                      -- (DS-P2.10), not a separate top-level filter.
  signal src_spi_ss_w   : std_logic;
  signal src_spi_mosi_w : std_logic;
  signal src_spi_miso_w : std_logic;
  signal src_spi_sclk_w : std_logic;

  ---------------------------------------------------------------------------
  -- DS-P2.51 Phase 2: I2C/SPI injector responders.
  --
  -- Each injector wraps the matching slave block with a regbank-fed TX
  -- FIFO. The slave drives its half of the bus open-drain (I2C) or
  -- push-pull (SPI MISO when CS asserted); the bus model below combines
  -- master + responder drives into the resolved `*_bus_*` signals.
  ---------------------------------------------------------------------------
  -- I2C injector
  signal i2c_inj_control_w  : std_logic_vector(31 downto 0);
  signal i2c_inj_datain_w   : std_logic_vector(31 downto 0);
  signal i2c_inj_datain_vld : std_logic;
  signal i2c_inj_status_w   : std_logic_vector(31 downto 0);
  signal i2c_inj_sda_o_w    : std_logic;
  signal i2c_inj_sda_t_w    : std_logic;

  -- SPI injector
  signal spi_inj_control_w  : std_logic_vector(31 downto 0);
  signal spi_inj_datain_w   : std_logic_vector(31 downto 0);
  signal spi_inj_datain_vld : std_logic;
  signal spi_inj_status_w   : std_logic_vector(31 downto 0);
  signal spi_inj_miso_o_w   : std_logic;
  signal spi_inj_miso_t_w   : std_logic;

  -- DS-T9 / DS-T2.11: EMIO-loop REA probe word (32 bits) + regbank tap.
  -- The probe_word spans the full EMIO loopback seam (PS master out,
  -- injector drive-back, PS read-in, muxed datalogger sources, mux sel, framer
  -- liveness). It feeds BOTH the rr_rea_xilinx7 REA core (cycle-accurate JTAG
  -- waveform) and the EMIO_PROBE regbank RO slot (static AXI/devmem snapshot —
  -- only the low 31 bits, so sticky&probe stays in one 32-bit reg; the new
  -- SPI SS read-in leg is REA-capture-only).
  --
  -- DS-T9 widening (2026-05-31): bit 31 adds the SPI SS read-in (spi0_bus_ss_w)
  -- — the most diagnostic still-unprobed read-in leg: it confirms chip-select
  -- reaches the bus the SPI injector samples ("does the injector get
  -- triggered"). CAPPED AT 32 BITS: rr_rea_xilinx7's trigger value/mask are
  -- 32-bit JTAG regs (rr_rea_regbank trig_value_r), so G_SAMPLE_W must be <= 32
  -- — G_SAMPLE_W=35 failed synth with [Synth 8-11324] array-index-out-of-range
  -- (RTL-P2.658). The SPI SCLK/MOSI + UART RX read-in legs are deferred until
  -- the IP supports wider triggers.
  -- NOTE: I2C SCL/SDA and SPI MISO read-in legs are NOT added — u_bd's *_I
  -- ports are fed by the resolved i2c0_bus_*_w / spi0_bus_miso_w nets, which
  -- bits 4/3/11 ALREADY probe (I2C0_SCL_I=>i2c0_bus_scl_w,
  -- SPI0_MISO_I=>spi0_bus_miso_w). Re-probing them would be duplication.
  signal emio_probe_word  : std_logic_vector(31 downto 0);
  signal rea_trigger_out  : std_logic;
  signal rea_trig_sticky  : std_logic := '0';
  -- DS-P3.56 P3: the DDR-commit REA's BSCANE2 tap, routed to the jtag_datalogger
  -- so it watches the live USER1 scan (datalogger-watching-REA bring-up).
  signal rea_tap_tck      : std_logic;
  signal rea_tap_tms      : std_logic;
  signal rea_tap_tdi      : std_logic;
  signal rea_tap_tdo      : std_logic;

  -- ── DS-T2.18 DDR-commit instrumentation ──────────────────────────────────
  -- Diagnoses why frames enqueue (RING_WRITE_PTR advances when the stream accepts a framer word)
  -- but never land in DRAM (cold-cache DRAM read = 100% zero). Three observers:
  --   1. clk_ddr liveness: a free-running divider in the clk_ddr (fclk_clk1,
  --      125 MHz) domain — deliberately NOT reset by rst_ddr, so it ticks even
  --      if rst_ddr is stuck. CDC'd to clk_sys + edge-counted → CLK_DDR_TICKS.
  --      Distinguishes "dead clock" (count stuck 0) from "stuck reset".
  --   2. rst_ddr observed in clk_sys: live state + a sticky "ever deasserted"
  --      flag (persistent from config) → DDR_RESET_STATUS.
  --   3. The HP0 write handshake on a clk_ddr-clocked REA core (repurposes the
  --      single USER1 BSCANE2 chain — rr's xsdb capture path is USER1-only).
  attribute async_reg : string;
  -- clk_ddr-domain free-running liveness divider (init-valued, no reset).
  signal clk_ddr_div_r       : unsigned(7 downto 0) := (others => '0');
  signal clk_ddr_toggle_r    : std_logic := '0';
  -- clk_sys-side synchroniser + edge counter for the toggle.
  signal clk_ddr_tgl_sync    : std_logic_vector(1 downto 0) := (others => '0');
  attribute async_reg of clk_ddr_tgl_sync : signal is "TRUE";
  signal clk_ddr_tgl_sync_d  : std_logic := '0';
  signal clk_ddr_live_cnt    : unsigned(31 downto 0) := (others => '0');
  -- rst_ddr observed in clk_sys: 2-FF sync (init reset-asserted) + sticky.
  signal rst_ddr_sync        : std_logic_vector(1 downto 0) := (others => '1');
  attribute async_reg of rst_ddr_sync : signal is "TRUE";
  signal rst_ddr_ever_deassert : std_logic := '0';
  -- clk_ddr-domain benign power-on reset for the REA core (rst_sys synced into
  -- clk_ddr — NOT the suspect rst_ddr, so a stuck rst_ddr can't blind the REA).
  signal rst_sys_in_ddr      : std_logic_vector(1 downto 0) := (others => '1');
  attribute async_reg of rst_sys_in_ddr : signal is "TRUE";
  -- DDR-commit REA probe word (clk_ddr domain). Drives the existing
  -- rea_trigger_out / rea_trig_sticky anchor (repurposed from the EMIO core).
  signal ddr_commit_probe_word : std_logic_vector(31 downto 0);

  -- DS-T2.34: i2c REA probe word (re-repurposes the single USER1 REA core back
  -- to the i2c seam + slave FSM, on clk_sys). Captures the real bus SCL/SDA, the
  -- injector drive/release, and the slave's bit counter + detected edges + FSM
  -- state — to localise the premature-SDA-release / SCL-miscount that fails ~27%
  -- of multi-byte master reads on silicon (sim is 9/9). LSB-first layout matches
  -- debug/i2c_seam.yml.
  signal i2c_slave_dbg_w : std_logic_vector(19 downto 0);
  signal i2c_dbg_fifo_w  : std_logic_vector(15 downto 0);  -- DS-T2.34 FIFO internals
  signal i2c_dbg_shift_w : std_logic_vector(7 downto 0);   -- DS-T2.34 slave latched byte
  signal i2c_dbg2_w      : std_logic_vector(7 downto 0);   -- DS-T2.34 bail-decision conds
  signal i2c_probe_word  : std_logic_vector(31 downto 0);

  -- REA probe bus from core to u_rea.
  signal rea_probe_w          : std_logic_vector(31 downto 0);


  -- DS-T2.26 probe: discriminate where the in-band loss frame is lost on silicon.
  signal emit_valid_cnt_r : unsigned(31 downto 0) := (others => '0');  -- cycles err_fr_valid=1
  signal emit_done_cnt_r  : unsigned(31 downto 0) := (others => '0');  -- emitter→framer handshakes

  signal ddr_backpressure_cnt_r : unsigned(31 downto 0) := (others => '0');  -- DS-T2.22 mech B
  -- DS-P3.91: burst arbiter grant counter. The regbank ARB_GRANT_CNT slot
  -- was undriven (read the ROM-init 0). Counts gather burst-arbiter GRANTS — the
  -- valid&&ready handshakes (dout_valid & dout_req). NOT raw valid cycles:
  -- the arbiter HOLDS valid until the downstream READY accepts (datalogger_gather
  -- comment "IN_READY drops and the burst arbiter holds"), so counting valid
  -- alone would over-count one grant as many.
  signal arb_grant_cnt_r : unsigned(31 downto 0) := (others => '0');

  -- ── DS-P3.60: ring-fill watermark IRQ (hysteresis, drives IRQ_F2P[0]) ──────
  -- The PoC drain is polled (DS-P2.59 SW read pointer). DS-T2.22 made the
  -- eventual ring-full loss VISIBLE after the fact; DS-P3.60 raises an
  -- out-of-band PL→PS interrupt at a HIGH watermark (75% full) so the PS
  -- drains BEFORE overflow — early warning, not a post-mortem counter.
  --
  -- Schmitt-trigger (two watermarks) to avoid interrupt thrash: the alarm
  -- LATCH sets when fill crosses HIGH (75%) and clears ONLY when the consumer
  -- has drained back below LOW (50%). So one fill excursion = exactly one
  -- interrupt episode; the line stays asserted through the whole drain and
  -- re-arms only after a deep (≥25% headroom) drain. Level-sensitive (GIC
  -- <0 29 4> = level-high) so a slow PS can never miss the edge.
  --
  -- fill = (commit_off - read_off) mod RING_BYTES, both taken as the low
  -- G_DDR_WIN_WIDTH bits (G_DDR_BASE_ADDR = 0x1000_0000 is exactly 2^28-
  -- aligned, so the low 28 bits of the absolute commit address ARE the
  -- ring-relative offset; same frame as the SW-written RING_READ_PTR). The
  -- watermark + hysteresis FSM lives in the u_ring_watermark instance below.
  signal ring_alarm_w     : std_logic;                       -- live alarm level
  signal ring_alarm_cnt_w : std_logic_vector(31 downto 0);   -- # excursions ≥ HIGH
  signal ring_fill_w      : std_logic_vector(G_DDR_WIN_WIDTH - 1 downto 0);
  signal ring_alarm_irq   : std_logic_vector(0 downto 0);    -- → IRQ_F2P[0]

  -- zs_msg_framer wiring (DS-P3.28 v1). Datalogger-source tagging is
  -- placeholder: datalogger_id is a per-frame counter, domain is SYS.
  -- DS-P3.29 (2026-05-17) — landed: per-source (datalogger_id, msg_type)
  -- threaded through the arbiter chain. Frames now land on
  -- per-protocol channels in RouteWave instead of the generic
  -- pl_heartbeat lane.
  signal framer_in_valid    : std_logic;
  signal framer_in_ready    : std_logic;
  signal framer_out_data    : std_logic_vector(31 downto 0);
  signal framer_out_valid   : std_logic;
  signal timestamp64        : std_logic_vector(63 downto 0);

  -- DS-P2.47 Phase B2 step 3: variable-payload framer + EDGE_BATCH path.
  -- The framer is now configured for up to G_PAYLOAD_WORDS_MAX = 32 words
  -- per frame. A 2:1 mux at its input picks between:
  --   * the non-EDGE single-word pass-through (datalogger_gather → framer),
  --   * the edge_batcher_aggregator's variable-N batches (DOMAIN_EDGE
  --     events accumulated by per-channel edge_batchers).
  constant C_PAYLOAD_WORDS_MAX : integer := 32;

  -- DS-P3.50: the edge aggregator no longer demuxes EV_WORD[31:27] directly as
  -- the batcher index (which forced max(edge id)+1 = 7 batchers with 0/4/5 dead,
  -- DS-T10). It now maps the SPARSE global ids (1,2,3,6) onto C_EDGE_DATALOGGERS
  -- DENSE batchers via G_CHANNEL_IDS => C_EDGE_IDS_FLAT_W — exactly 4 batchers,
  -- no dead ones, ~2.7k LUTs reclaimed, and the dead-batcher[4] WNS path gone.

  -- Gather output classifier: msg_type[11:8] = DOMAIN_EDGE (0x6) routes
  -- the event to the aggregator; everything else bypasses to the framer.
  signal gather_is_edge_w   : std_logic;
  signal gather_to_agg_v_w  : std_logic;   -- valid → aggregator
  signal gather_to_fr_v_w   : std_logic;   -- valid → non-EDGE pass-through

  -- Aggregator handshake + outputs.
  signal agg_ev_ready_w     : std_logic;
  signal agg_fr_valid_w     : std_logic;
  signal agg_fr_ready_w     : std_logic;
  signal agg_fr_datalogger_id_w : std_logic_vector(7  downto 0);
  signal agg_fr_msg_class_w  : std_logic_vector(7  downto 0);
  signal agg_fr_msg_type_w   : std_logic_vector(15 downto 0);
  signal agg_fr_flags_w      : std_logic_vector(15 downto 0);
  signal agg_fr_timestamp_w  : std_logic_vector(63 downto 0);
  signal agg_fr_payload_w    : std_logic_vector(C_PAYLOAD_WORDS_MAX*32 - 1 downto 0);
  signal agg_fr_words_w      : std_logic_vector(7  downto 0);

  -- Non-EDGE pass-through tuple (gather output, zero-padded to the wide
  -- framer PAYLOAD width).
  signal nonedge_ready_w     : std_logic;
  signal nonedge_payload_w   : std_logic_vector(C_PAYLOAD_WORDS_MAX*32 - 1 downto 0);

  -- 2:1 arb output → framer input. Aggregator wins on tie (its batches
  -- are rarer than per-event non-EDGE traffic; a starvation guard would
  -- be a follow-up if a hostile EDGE workload monopolises the framer).
  signal framer_datalogger_w    : std_logic_vector(7  downto 0);
  signal framer_msg_class_w  : std_logic_vector(7  downto 0);
  signal framer_msg_type_w   : std_logic_vector(15 downto 0);
  signal framer_flags_w      : std_logic_vector(15 downto 0);
  signal framer_timestamp_w  : std_logic_vector(63 downto 0);
  signal framer_payload_w    : std_logic_vector(C_PAYLOAD_WORDS_MAX*32 - 1 downto 0);
  signal framer_words_live_w : std_logic_vector(7  downto 0);

  -- DS-P3.70 piece 3: in-band error-event emitter — the 3rd (top-priority)
  -- framer input. Coalesces ERR_CNT_EDGE deltas into rate-limited SEV_ERROR
  -- frames (edge internal-FIFO-overflow lost_count) so RouteWave can mark the
  -- loss inline. Error frames are rare + important → win the framer arbitration.
  signal err_fr_valid_w      : std_logic;
  signal err_fr_ready_w      : std_logic;
  signal err_fr_datalogger_id_w : std_logic_vector(7  downto 0);
  signal err_fr_msg_class_w  : std_logic_vector(7  downto 0);
  signal err_fr_msg_type_w   : std_logic_vector(15 downto 0);
  signal err_fr_flags_w      : std_logic_vector(15 downto 0);
  signal err_fr_timestamp_w  : std_logic_vector(63 downto 0);
  signal err_fr_payload32_w  : std_logic_vector(31 downto 0);
  signal err_fr_words_w      : std_logic_vector(7  downto 0);

  -- DS-T3.18: per-EDGE-instance running loss counters from the gather (channel k
  -- in bits [(k+1)*32-1 : k*32]). Each feeds a per-channel emitter inside the
  -- error_event_bank so the in-band loss frame names the overflowing channel.
  signal edge_err_cnts_w     : std_logic_vector(C_EDGE_DATALOGGERS*32-1 downto 0);

  -- DS-P3.117 piece 4: in-band max-frequency bank — the 4th framer input. Per
  -- EDGE channel, watches the gather's re-exported min-interval tap and emits an
  -- INFO | DOMAIN_EDGE | EVT_EDGE_MAXFREQ frame (2-word payload) naming the
  -- channel whose min just got smaller (or was re-armed via CONFIG[2]). Mirrors
  -- the error_event_bank precedent; lowest framer priority (the running min is
  -- not loss-critical). Payload is 64 bits (word1|word0), unlike the error path's
  -- 32-bit single word.
  signal edge_max_ivals_w    : std_logic_vector(C_EDGE_DATALOGGERS*C_EDGE_MAXFREQ_WIDTH-1 downto 0);
  signal edge_max_new_w      : std_logic_vector(C_EDGE_DATALOGGERS-1 downto 0);
  signal mf_fr_valid_w       : std_logic;
  signal mf_fr_ready_w       : std_logic;
  signal mf_fr_datalogger_id_w  : std_logic_vector(7  downto 0);
  signal mf_fr_msg_class_w   : std_logic_vector(7  downto 0);
  signal mf_fr_msg_type_w    : std_logic_vector(15 downto 0);
  signal mf_fr_flags_w       : std_logic_vector(15 downto 0);
  signal mf_fr_timestamp_w   : std_logic_vector(63 downto 0);
  signal mf_fr_payload64_w   : std_logic_vector(63 downto 0);
  signal mf_fr_words_w       : std_logic_vector(7  downto 0);

  -- Pack the per-EDGE-channel device ids into the flat generic the
  -- error_event_bank expects (channel k id → bits [(k+1)*8-1 : k*8]).
  function f_edge_ids_flat return std_logic_vector is
    variable v : std_logic_vector(C_EDGE_DATALOGGERS*8-1 downto 0) := (others => '0');
  begin
    for k in 0 to C_EDGE_DATALOGGERS-1 loop
      v((k+1)*8-1 downto k*8) := std_logic_vector(to_unsigned(C_EDGE_IDS(k), 8));
    end loop;
    return v;
  end function f_edge_ids_flat;
  constant C_EDGE_IDS_FLAT_W : std_logic_vector(C_EDGE_DATALOGGERS*8-1 downto 0)
                             := f_edge_ids_flat;

  -- Compile-time ring locator constants exposed to PS firmware via the
  -- regbank RO slots (RING_BASE_LOW/HIGH, RING_BYTES). Derived from
  -- generics; SW doesn't need a recompile when the ring moves or
  -- resizes — it just re-reads these slots after a bitstream load.
  constant C_RING_BASE_LOW  : std_logic_vector(31 downto 0)
    := std_logic_vector(G_DDR_BASE_ADDR);
  constant C_RING_BASE_HIGH : std_logic_vector(31 downto 0)
    := (others => '0');  -- Zynq-7: 32-bit physical address space
  constant C_RING_BYTES     : std_logic_vector(31 downto 0)
    := std_logic_vector(to_unsigned(2**G_DDR_WIN_WIDTH, 32));

begin
  ---------------------------------------------------------------------------
  -- deepskopion_bd — PS + smartconnects + reset wrapped as a component.
  ---------------------------------------------------------------------------
  u_bd : component deepskopion_bd
    port map (
      -- PS dedicated pins propagate from this entity straight through.
      DDR_addr          => DDR_addr,
      DDR_ba            => DDR_ba,
      DDR_cas_n         => DDR_cas_n,
      DDR_ck_n          => DDR_ck_n,
      DDR_ck_p          => DDR_ck_p,
      DDR_cke           => DDR_cke,
      DDR_cs_n          => DDR_cs_n,
      DDR_dm            => DDR_dm,
      DDR_dq            => DDR_dq,
      DDR_dqs_n         => DDR_dqs_n,
      DDR_dqs_p         => DDR_dqs_p,
      DDR_odt           => DDR_odt,
      DDR_ras_n         => DDR_ras_n,
      DDR_reset_n       => DDR_reset_n,
      DDR_we_n          => DDR_we_n,
      FIXED_IO_ddr_vrn  => FIXED_IO_ddr_vrn,
      FIXED_IO_ddr_vrp  => FIXED_IO_ddr_vrp,
      FIXED_IO_mio      => FIXED_IO_mio,
      FIXED_IO_ps_clk   => FIXED_IO_ps_clk,
      FIXED_IO_ps_porb  => FIXED_IO_ps_porb,
      FIXED_IO_ps_srstb => FIXED_IO_ps_srstb,
      -- M_AXI_GP0 master out → flat GP0 signals (drive regbank slave)
      M_AXI_GP0_araddr  => gp0_araddr,
      M_AXI_GP0_arprot  => gp0_arprot,
      M_AXI_GP0_arready => gp0_arready,
      M_AXI_GP0_arvalid => gp0_arvalid,
      M_AXI_GP0_awaddr  => gp0_awaddr,
      M_AXI_GP0_awprot  => gp0_awprot,
      M_AXI_GP0_awready => gp0_awready,
      M_AXI_GP0_awvalid => gp0_awvalid,
      M_AXI_GP0_bready  => gp0_bready,
      M_AXI_GP0_bresp   => gp0_bresp,
      M_AXI_GP0_bvalid  => gp0_bvalid,
      M_AXI_GP0_rdata   => gp0_rdata,
      M_AXI_GP0_rready  => gp0_rready,
      M_AXI_GP0_rresp   => gp0_rresp,
      M_AXI_GP0_rvalid  => gp0_rvalid,
      M_AXI_GP0_wdata   => gp0_wdata,
      M_AXI_GP0_wready  => gp0_wready,
      M_AXI_GP0_wstrb   => gp0_wstrb,
      M_AXI_GP0_wvalid  => gp0_wvalid,
      -- DS-P3.109: APB masters → CTU CAN FD register slaves
      CANFD0_APB_paddr   => canfd0_apb_paddr_w,
      CANFD0_APB_penable => canfd0_apb_penable_w,
      CANFD0_APB_prdata  => canfd0_apb_prdata_w,
      CANFD0_APB_pready  => canfd0_apb_pready_w,
      CANFD0_APB_psel    => canfd0_apb_psel_w,
      CANFD0_APB_pslverr => canfd0_apb_pslverr_w,
      CANFD0_APB_pwdata  => canfd0_apb_pwdata_w,
      CANFD0_APB_pwrite  => canfd0_apb_pwrite_w,
      CANFD1_APB_paddr   => canfd1_apb_paddr_w,
      CANFD1_APB_penable => canfd1_apb_penable_w,
      CANFD1_APB_prdata  => canfd1_apb_prdata_w,
      CANFD1_APB_pready  => canfd1_apb_pready_w,
      CANFD1_APB_psel    => canfd1_apb_psel_w,
      CANFD1_APB_pslverr => canfd1_apb_pslverr_w,
      CANFD1_APB_pwdata  => canfd1_apb_pwdata_w,
      CANFD1_APB_pwrite  => canfd1_apb_pwrite_w,
      -- S_AXI_HP0 slave in ← rr-ddr-stream master record
      S_AXI_HP0_araddr  => axi_h2c.ARADDR,
      S_AXI_HP0_arburst => axi_h2c.ARBURST,
      S_AXI_HP0_arcache => axi_h2c.ARCACHE,
      S_AXI_HP0_arid    => axi_h2c.ARID,
      S_AXI_HP0_arlen   => axi_h2c.ARLEN,
      S_AXI_HP0_arlock  => axi_h2c.ARLOCK,
      S_AXI_HP0_arprot  => axi_h2c.ARPROT,
      S_AXI_HP0_arqos   => axi_h2c.ARQOS,
      S_AXI_HP0_arready => axi_c2h.ARREADY,
      S_AXI_HP0_arsize  => axi_h2c.ARSIZE,
      S_AXI_HP0_arvalid => axi_h2c.ARVALID,
      S_AXI_HP0_awaddr  => axi_h2c.AWADDR,
      S_AXI_HP0_awburst => axi_h2c.AWBURST,
      S_AXI_HP0_awcache => axi_h2c.AWCACHE,
      S_AXI_HP0_awid    => axi_h2c.AWID,
      S_AXI_HP0_awlen   => axi_h2c.AWLEN,
      S_AXI_HP0_awlock  => axi_h2c.AWLOCK,
      S_AXI_HP0_awprot  => axi_h2c.AWPROT,
      S_AXI_HP0_awqos   => axi_h2c.AWQOS,
      S_AXI_HP0_awready => axi_c2h.AWREADY,
      S_AXI_HP0_awsize  => axi_h2c.AWSIZE,
      S_AXI_HP0_awvalid => axi_h2c.AWVALID,
      S_AXI_HP0_bid     => axi_c2h.BID,
      S_AXI_HP0_bready  => axi_h2c.BREADY,
      S_AXI_HP0_bresp   => axi_c2h.BRESP,
      S_AXI_HP0_bvalid  => axi_c2h.BVALID,
      S_AXI_HP0_rdata   => s_axi_hp0_rdata,
      S_AXI_HP0_rid     => axi_c2h.RID,
      S_AXI_HP0_rlast   => axi_c2h.RLAST,
      S_AXI_HP0_rready  => axi_h2c.RREADY,
      S_AXI_HP0_rresp   => axi_c2h.RRESP,
      S_AXI_HP0_rvalid  => axi_c2h.RVALID,
      S_AXI_HP0_wdata   => s_axi_hp0_wdata,
      S_AXI_HP0_wlast   => axi_h2c.WLAST,
      S_AXI_HP0_wready  => axi_c2h.WREADY,
      S_AXI_HP0_wstrb   => s_axi_hp0_wstrb,
      S_AXI_HP0_wvalid  => axi_h2c.WVALID,
      -- Clock + reset out — two domains.
      fclk_clk0           => clk_sys,
      fclk_aresetn        => aresetn,
      fclk_clk1           => clk_ddr,
      fclk_clk1_aresetn   => ddr_aresetn,
      -- DS-P2.51 Phase 1: PS EMIO peripherals exposed to the fabric.
      UART0_TX            => uart0_tx_w,
      UART0_RX            => uart0_rx_w,
      I2C0_SCL_I          => i2c0_bus_scl_w,
      I2C0_SCL_O          => i2c0_scl_o_w,
      I2C0_SCL_T          => i2c0_scl_t_w,
      I2C0_SDA_I          => i2c0_bus_sda_w,
      I2C0_SDA_O          => i2c0_sda_o_w,
      I2C0_SDA_T          => i2c0_sda_t_w,
      SPI0_SCLK_I         => spi0_bus_sclk_w,
      SPI0_SCLK_O         => spi0_sclk_o_w,
      SPI0_SCLK_T         => spi0_sclk_t_w,
      SPI0_MOSI_I         => spi0_bus_mosi_w,
      SPI0_MOSI_O         => spi0_mosi_o_w,
      SPI0_MOSI_T         => spi0_mosi_t_w,
      SPI0_MISO_I         => spi0_bus_miso_w,
      SPI0_MISO_O         => spi0_miso_o_w,
      SPI0_MISO_T         => spi0_miso_t_w,
      SPI0_SS_I           => spi0_bus_ss_w,
      SPI0_SS_O           => spi0_ss_o_w,
      SPI0_SS_T           => spi0_ss_t_w,
      -- DS-T2.16: EMIO GPIO bit 0 = fabric SPI chip-select.
      GPIO_O              => gpio_o_w,
      GPIO_I              => gpio_i_w,
      GPIO_T              => gpio_t_w,
      -- DS-P3.102: CAN0/CAN1 EMIO. Each controller's TX feeds the fabric
      -- wired-AND; the resolved bus is fed BACK to both controllers' RX (so they
      -- hear each other + their own arbitration/ACK), and to the datalogger.
      CAN0_PHY_TX         => can0_phy_tx_w,
      CAN0_PHY_RX         => can_bus_w,
      CAN1_PHY_TX         => can1_phy_tx_w,
      CAN1_PHY_RX         => can_bus_w,
      -- DS-P3.60 / DS-P3.109: fabric interrupts → PS7 GIC.
      --   [0] ring alarm, [1] CTU A irq, [2] CTU B irq.
      IRQ_F2P             => irq_f2p_w
    );

  rst_sys <= not aresetn(0);
  rst_ddr <= not ddr_aresetn(0);

  -- The portable assembly owns all product behavior.  This wrapper owns only
  -- PS7 ports, native-width adaptation and the Xilinx JTAG primitive.
  u_core : entity work.deepskopion_core
    generic map (
      G_DDR_BASE_ADDR => G_DDR_BASE_ADDR,
      G_DDR_WIN_WIDTH => G_DDR_WIN_WIDTH,
      G_AXI_MAX_BURST => C_ZYNQ7_S_AXI_HP_MAX_BURST,
      G_DDR_DATA_WIDTH => 128,
      -- DS-P2.67: two TXT buffers per CTU core close 100 MHz on xc7z020-1.
      G_CTU_TXT_BUFFERS => 2,
      -- DS-P1.90: the PS EMIO drives the emio_* legs here and stim_*_o is left
      -- open below, so the stimulus generator drives the legs inside the core
      -- while a protocol's STIM_*_CONTROL enable bit is set (PS EMIO otherwise).
      G_STIM_DRIVES_EMIO => true)
    port map (
      clk_sys_i => clk_sys, rst_sys_i => rst_sys,
      clk_ddr_i => clk_ddr, rst_ddr_i => rst_ddr,
      led => led, ja => ja, jb => jb,
      s_axil_awaddr_i => gp0_awaddr, s_axil_awprot_i => gp0_awprot,
      s_axil_awvalid_i => gp0_awvalid, s_axil_awready_o => gp0_awready,
      s_axil_wdata_i => gp0_wdata, s_axil_wstrb_i => gp0_wstrb,
      s_axil_wvalid_i => gp0_wvalid, s_axil_wready_o => gp0_wready,
      s_axil_bresp_o => gp0_bresp, s_axil_bvalid_o => gp0_bvalid,
      s_axil_bready_i => gp0_bready,
      s_axil_araddr_i => gp0_araddr, s_axil_arprot_i => gp0_arprot,
      s_axil_arvalid_i => gp0_arvalid, s_axil_arready_o => gp0_arready,
      s_axil_rdata_o => gp0_rdata, s_axil_rresp_o => gp0_rresp,
      s_axil_rvalid_o => gp0_rvalid, s_axil_rready_i => gp0_rready,
      canfd0_apb_paddr_i => canfd0_apb_paddr_w,
      canfd0_apb_penable_i => canfd0_apb_penable_w,
      canfd0_apb_psel_i => canfd0_apb_psel_w(0),
      canfd0_apb_pwrite_i => canfd0_apb_pwrite_w,
      canfd0_apb_pwdata_i => canfd0_apb_pwdata_w,
      canfd0_apb_prdata_o => canfd0_apb_prdata_w,
      canfd0_apb_pready_o => canfd0_apb_pready_w(0),
      canfd0_apb_pslverr_o => canfd0_apb_pslverr_w(0),
      canfd1_apb_paddr_i => canfd1_apb_paddr_w,
      canfd1_apb_penable_i => canfd1_apb_penable_w,
      canfd1_apb_psel_i => canfd1_apb_psel_w(0),
      canfd1_apb_pwrite_i => canfd1_apb_pwrite_w,
      canfd1_apb_pwdata_i => canfd1_apb_pwdata_w,
      canfd1_apb_prdata_o => canfd1_apb_prdata_w,
      canfd1_apb_pready_o => canfd1_apb_pready_w(0),
      canfd1_apb_pslverr_o => canfd1_apb_pslverr_w(0),
      m_axi_h2c_o => axi_h2c, m_axi_c2h_i => axi_c2h,
      irq_o => irq_f2p_w,
      emio_uart_tx_i => uart0_tx_w, emio_uart_rx_o => uart0_rx_w,
      emio_i2c_scl_o_i => i2c0_scl_o_w, emio_i2c_scl_t_i => i2c0_scl_t_w,
      emio_i2c_sda_o_i => i2c0_sda_o_w, emio_i2c_sda_t_i => i2c0_sda_t_w,
      emio_i2c_scl_i_o => i2c0_bus_scl_w, emio_i2c_sda_i_o => i2c0_bus_sda_w,
      emio_spi_sclk_o_i => spi0_sclk_o_w, emio_spi_sclk_t_i => spi0_sclk_t_w,
      emio_spi_mosi_o_i => spi0_mosi_o_w, emio_spi_mosi_t_i => spi0_mosi_t_w,
      emio_spi_miso_o_i => spi0_miso_o_w, emio_spi_miso_t_i => spi0_miso_t_w,
      emio_spi_ss_o_i => spi0_ss_o_w, emio_spi_ss_t_i => spi0_ss_t_w,
      emio_spi_sclk_i_o => spi0_bus_sclk_w, emio_spi_mosi_i_o => spi0_bus_mosi_w,
      emio_spi_miso_i_o => spi0_bus_miso_w, emio_spi_ss_i_o => spi0_bus_ss_w,
      emio_gpio_o_i => gpio_o_w(0), emio_gpio_t_i => gpio_t_w(0),
      emio_gpio_i_o => gpio_i_w(0),
      emio_can0_tx_i => can0_phy_tx_w, emio_can1_tx_i => can1_phy_tx_w,
      emio_can_rx_o => can_bus_w,
      -- DS-P2.100: the fabric-side stimulus generator is a DE25-only
      -- loopback (Zynq-7 PS7 already provides a real EMIO master via the
      -- emio_*_i legs above) — left open here.
      stim_uart_tx_o => open,
      stim_i2c_scl_o_o => open, stim_i2c_scl_t_o => open,
      stim_i2c_sda_o_o => open, stim_i2c_sda_t_o => open,
      stim_spi_sclk_o_o => open, stim_spi_mosi_o_o => open, stim_spi_ss_o_o => open,
      -- DS-P2.126 MSS fabric source is PolarFire-only. Bits [11:8] stay 0
      -- here, so these idle ties never reach a datalogger.
      mss_uart_tx_i => '1', mss_uart_rx_o => open,
      mss_i2c_scl_oe_i => '0', mss_i2c_sda_oe_i => '0',
      mss_i2c_scl_o => open, mss_i2c_sda_o => open,
      mss_spi_clk_i => '0', mss_spi_clk_oe_i => '0',
      mss_spi_do_i => '0', mss_spi_do_oe_i => '0',
      mss_spi_ss_i => '1', mss_spi_ss_oe_i => '0',
      mss_spi_di_o => open,
      mss_can_tx_i => '1', mss_can_rx_o => open,
      rea_probe_o => rea_probe_w,
      rea_trigger_i => rea_trigger_out,
      rea_tap_tck_i => rea_tap_tck, rea_tap_tms_i => rea_tap_tms,
      rea_tap_tdi_i => rea_tap_tdi, rea_tap_tdo_i => rea_tap_tdo);

  -- Native PS7 HP0 is 128-bit while rr-ddr-stream's portable record is 512-bit.
  axi_c2h.RDATA(127 downto 0) <= s_axi_hp0_rdata;
  axi_c2h.RDATA(rr.ddr_stream_pkg.C_AXI_DATA_WIDTH - 1 downto 128) <= (others => '0');
  s_axi_hp0_wdata <= axi_h2c.WDATA(127 downto 0);
  s_axi_hp0_wstrb <= axi_h2c.WSTRB(15 downto 0);

  u_rea : entity rr_rea.rr_rea_xilinx7
    generic map (G_SAMPLE_W => 32, G_DEPTH => 4096, G_TIMESTAMP_W => 0,
                 G_NUM_CHAN => 1, G_CTRL_CHAIN => 1)
    port map (sample_clk_i => clk_sys, sample_rst_i => rst_sys,
              probe_i => rea_probe_w,
              trigger_o => rea_trigger_out,
              tap_tck => rea_tap_tck, tap_tms => rea_tap_tms,
              tap_tdi => rea_tap_tdi, tap_tdo => rea_tap_tdo);

end architecture rtl;
