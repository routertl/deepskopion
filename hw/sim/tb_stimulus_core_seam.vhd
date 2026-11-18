-------------------------------------------------------------------------------
-- Title      : tb_stimulus_core_seam
-- Project    : deepskopion / DS-P2.100 landing-review Finding B
-------------------------------------------------------------------------------
-- File       : tb_stimulus_core_seam.vhd
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   The DS-P2.100 shard suite (test_stimulus_uart/i2c/spi/edge.py) proves
--   ds_stimulus_gen against tb_stimulus_gen_loop.vhd, which instantiates
--   ds_stimulus_gen + ds_emio_mux + the injectors DIRECTLY — a topology
--   proof, but it re-DECLARES its own EMIO_MUX_SEL/STIM_* ports rather than
--   compiling deepskopion_core itself. A defect in deepskopion_core's OWN
--   wiring (the C_INDEX_STIM_* regbank unpack at core.vhd:1417-1433, or the
--   capture_pin(42) bit[7] priority-mux at core.vhd:874-880) is INVISIBLE to
--   that suite — nothing there compiles core.vhd.
--
--   This bench closes that gap: it instantiates the REAL tich.deepskopion_
--   core (pure fabric RTL — no HPS/BD/PS instance lives inside core, so no
--   Zynq/Agilex HPS dependency here) and wires the SAME stim_*_o -> emio_*_i
--   loopback deepskopion_de25_top.vhd:731-756 wires (mirrored here, not
--   reused, since that loopback is top-level plumbing, not an entity) --
--   plus a flat AXI4-Lite slave onto core's OWN s_axil_* port (cocotbext.
--   axi.AxiLiteMaster drives PS-style register writes, exactly the access
--   pattern real firmware uses).
--
--   Three observation taps:
--     OBS_STIM_UART_TX   — the stim_uart_tx_o -> emio_uart_tx_i loopback
--                           wire itself (this file's OWN signal, upstream
--                           of the loopback — kept for debugging, but see
--                           PROBE_CAPTURE_UART36 below for the tap that's
--                           actually mutation-sensitive to the loopback).
--     PROBE_CAPTURE_EDGE0  — a VHDL-2008 external name reaching INSIDE
--                           u_core down to its private `capture_pin(42)`
--                           signal (core.vhd:197,874) — the exact bit-[7]-
--                           selected mux tap DS-P2.100 added. There is no
--                           port for this (deepskopion_core does not
--                           expose capture_pin), so this is the only way to
--                           witness that specific internal wire without
--                           either running the full datalogger->DDR chain
--                           (a much heavier proof already out of scope for
--                           this ticket) or adding a debug-only port to
--                           production RTL.
--     PROBE_CAPTURE_UART36 — same external-name mechanism, `capture_pin(36)`
--                           (core.vhd: fed unconditionally from
--                           ds_emio_mux.SRC_UART_TX, itself gated by
--                           EMIO_MUX_SEL(1)) — DOWNSTREAM of both the
--                           C_INDEX_STIM_UART_* unpack AND the
--                           stim_uart_tx_o -> emio_uart_tx_i loopback, so
--                           this is the tap that actually proves the
--                           loopback is load-bearing (OBS_STIM_UART_TX
--                           taps before it and would stay "correct" even
--                           if the loopback were reverted to constants).
--
--   Mutation-tested (see the stimulus_core_seam/ test suite's docstrings):
--     (1) reverting THIS FILE's loopback (stim_uart_tx_o -> emio_uart_tx_i)
--         to the DS-P2.95 tied-off constants (mirroring the exact mutation
--         of deepskopion_de25_top.vhd:731-756) turns
--         test_core_seam_uart_write_reaches_capture_pin_36 RED — proves the
--         loopback, not just the core's internal wiring, is load-bearing;
--     (2) breaking a C_INDEX_STIM_* unpack line in deepskopion_core.vhd
--         (e.g. C_INDEX_STIM_EDGE_CONTROL -> a different slot) turns
--         test_core_seam_edge_source_reaches_capture_pin_bit7 RED — proves
--         the regbank->core wiring, not just ds_stimulus_gen's own
--         internals, is under test;
--     (3) landing-review fourth-pass Finding 2/3: PROBE_CAPTURE_I2C_SCL/SDA
--         (capture_pin(12)/(13)) and PROBE_CAPTURE_SPI_CS (capture_pin(20)) close
--         the I2C/SPI + GPIO-CS gap the third pass left open. Killing
--         `stim_i2c_data_vld_w` at deepskopion_core.vhd:1427, or breaking
--         C_INDEX_STIM_I2C_*/C_INDEX_STIM_SPI_*, turns
--         test_stimulus_core_seam_i2c/_spi RED. Tying THIS FILE's
--         `emio_gpio_t_i` to '1' (GPIO tristated, mirroring the DS-T2.16
--         GPIO-CS decode being bypassed) turns
--         test_stimulus_core_seam_spi RED, because PROBE_CAPTURE_SPI_CS is
--         downstream of spi0_cs_gpio_w <= gpio_o_w(0) when gpio_t_w(0)='0'
--         else '1' (core.vhd:981) — the REAL silicon CS path (DS-T2.16),
--         not the dead native SPI0_SS_O.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
library rr;

entity tb_stimulus_core_seam is
  port (
    CLK : in std_logic;
    RST : in std_logic;

    -- ── Flat AXI4-Lite slave onto deepskopion_core's OWN s_axil_* port
    -- (32-bit address/data — core exposes the full PS-style AXI-Lite
    -- window directly, unlike the inner system_regbank's 11-bit slice). ──
    S_AXI_AWADDR  : in  std_logic_vector(31 downto 0);
    S_AXI_AWPROT  : in  std_logic_vector(2 downto 0) := (others => '0');
    S_AXI_AWVALID : in  std_logic;
    S_AXI_AWREADY : out std_logic;

    S_AXI_WDATA   : in  std_logic_vector(31 downto 0);
    S_AXI_WSTRB   : in  std_logic_vector(3 downto 0);
    S_AXI_WVALID  : in  std_logic;
    S_AXI_WREADY  : out std_logic;

    S_AXI_BRESP   : out std_logic_vector(1 downto 0);
    S_AXI_BVALID  : out std_logic;
    S_AXI_BREADY  : in  std_logic;

    S_AXI_ARADDR  : in  std_logic_vector(31 downto 0);
    S_AXI_ARPROT  : in  std_logic_vector(2 downto 0) := (others => '0');
    S_AXI_ARVALID : in  std_logic;
    S_AXI_ARREADY : out std_logic;

    S_AXI_RDATA   : out std_logic_vector(31 downto 0);
    S_AXI_RRESP   : out std_logic_vector(1 downto 0);
    S_AXI_RVALID  : out std_logic;
    S_AXI_RREADY  : in  std_logic;

    -- ── Observation taps. ──
    OBS_STIM_UART_TX  : out std_logic;
    PROBE_CAPTURE_EDGE0 : out std_logic;
    -- capture_pin(36) — fed unconditionally from ds_emio_mux.SRC_UART_TX
    -- (core.vhd: "capture_pin(36) <= src_uart_tx_w", no EMIO_MUX_SEL bit
    -- gates this specific assignment; SRC_UART_TX itself IS gated by
    -- EMIO_MUX_SEL(1), the test sets that). Unlike OBS_STIM_UART_TX
    -- (this file's own loopback wire, upstream of core's emio_uart_tx_i
    -- port), this probe is downstream of BOTH the C_INDEX_STIM_UART_*
    -- unpack AND the stim_uart_tx_o->emio_uart_tx_i loopback (mutating
    -- either turns it RED — the actual Finding B mutation targets).
    PROBE_CAPTURE_UART36 : out std_logic;

    -- DS-P2.100 landing-review (fourth pass) Finding 2/3: capture_pin(12)/(13)
    -- (core.vhd: SRC_I2C_SCL/SDA, EMIO_MUX_SEL(0)-gated) and capture_pin(20)
    -- (SRC_SPI_SS, EMIO_MUX_SEL(2)-gated) — downstream of the
    -- C_INDEX_STIM_I2C_*/C_INDEX_STIM_SPI_* unpack AND, for SPI, of the
    -- REAL GPIO-CS decode chain (gpio_o_w(0)/gpio_t_w(0) <- emio_gpio_o_i/
    -- emio_gpio_t_i -> spi0_cs_gpio_w -> spi0_cs_gpio_r -> ds_emio_mux's
    -- SPI0_CS_GPIO -> SRC_SPI_SS), which is DS-T2.16 / STIM-REQ-6's actual
    -- silicon CS source, not the dead native SPI0_SS_O.
    PROBE_CAPTURE_I2C_SCL : out std_logic;
    PROBE_CAPTURE_I2C_SDA : out std_logic;
    PROBE_CAPTURE_SPI_CS  : out std_logic
  );
end entity tb_stimulus_core_seam;

architecture str of tb_stimulus_core_seam is

  -- Stim-gen -> EMIO loopback wires — mirrors deepskopion_de25_top.vhd:
  -- 731-756 EXACTLY (the DS-P2.100 fix that replaced the DS-P2.95 tied-off
  -- constants). This is the mutation target for Finding B(1).
  signal stim_uart_tx_w    : std_logic;
  signal stim_i2c_scl_o_w, stim_i2c_scl_t_w : std_logic;
  signal stim_i2c_sda_o_w, stim_i2c_sda_t_w : std_logic;
  signal stim_spi_sclk_w   : std_logic;
  signal stim_spi_mosi_w   : std_logic;
  signal stim_spi_ss_w     : std_logic;

  -- Unexercised core inputs, tied to their DE25-top idle values.
  signal ja_idle, jb_idle : std_logic_vector(7 downto 0) := (others => '0');
  signal canfd_apb_paddr_idle  : std_logic_vector(31 downto 0) := (others => '0');
  signal canfd_apb_pwdata_idle : std_logic_vector(31 downto 0) := (others => '0');

  signal axi_c2h_idle : rr.ddr_stream_pkg.axi4_c2h_bus_t := (
    AWREADY => '0', WREADY => '0',
    BID => (others => '0'), BRESP => (others => '0'), BVALID => '0',
    ARREADY => '0',
    RID => (others => '0'), RDATA => (others => '0'), RRESP => (others => '0'),
    RLAST => '0', RVALID => '0'
  );

begin

  ---------------------------------------------------------------------------
  -- deepskopion_core — the REAL entity, straight off hw/rtl/. Pure fabric
  -- RTL: no HPS/BD/PS instance lives inside it (verified — its only
  -- sub-instances are ds_emio_mux, datalogger_gather, edge_batcher_aggregator,
  -- error/maxfreq event banks, zs_msg_framer, rr.ddr_stream_axi,
  -- system_regbank_wrapper, the i2c/spi injectors, ds_stimulus_gen,
  -- ring_watermark and the two CTU CAN-FD cores — none of them HPS/BD).
  ---------------------------------------------------------------------------
  u_core : entity tich.deepskopion_core
    generic map (
      -- G_DDR_BASE_ADDR deliberately NOT x"A0000000" (the real DE25 value,
      -- deepskopion_de25_top.vhd:715): deepskopion_core.vhd:1268 does
      -- `to_integer(G_DDR_BASE_ADDR)` on the unsigned generic, and
      -- 0xA0000000 (2,684,354,560) exceeds VHDL's default 32-bit signed
      -- INTEGER'high (2,147,483,647) — nvc aborts elaboration with
      -- "result ... cannot be represented as INTEGER" (reproduced
      -- directly). A real latent defect, invisible until now because
      -- deepskopion_core was never actually elaborated in simulation
      -- before this witness bench — flagged, not fixed here (out of
      -- scope: this seam proves the DS-P2.100 stim-gen wiring, not the
      -- DDR DMA base-address path, which this bench never exercises —
      -- m_axi_c2h_i is tied idle and m_axi_h2c_o is left open below).
      G_DDR_BASE_ADDR => x"10000000", G_DDR_WIN_WIDTH => 28,
      G_AXI_MAX_BURST => 16, G_DDR_DATA_WIDTH => 256
    )
    port map (
      clk_sys_i => CLK, rst_sys_i => RST, clk_ddr_i => CLK, rst_ddr_i => RST,
      led => open, ja => ja_idle, jb => jb_idle,

      s_axil_awaddr_i => S_AXI_AWADDR, s_axil_awprot_i => S_AXI_AWPROT,
      s_axil_awvalid_i => S_AXI_AWVALID, s_axil_awready_o => S_AXI_AWREADY,
      s_axil_wdata_i => S_AXI_WDATA, s_axil_wstrb_i => S_AXI_WSTRB,
      s_axil_wvalid_i => S_AXI_WVALID, s_axil_wready_o => S_AXI_WREADY,
      s_axil_bresp_o => S_AXI_BRESP, s_axil_bvalid_o => S_AXI_BVALID,
      s_axil_bready_i => S_AXI_BREADY,
      s_axil_araddr_i => S_AXI_ARADDR, s_axil_arprot_i => S_AXI_ARPROT,
      s_axil_arvalid_i => S_AXI_ARVALID, s_axil_arready_o => S_AXI_ARREADY,
      s_axil_rdata_o => S_AXI_RDATA, s_axil_rresp_o => S_AXI_RRESP,
      s_axil_rvalid_o => S_AXI_RVALID, s_axil_rready_i => S_AXI_RREADY,

      canfd0_apb_paddr_i => canfd_apb_paddr_idle, canfd0_apb_penable_i => '0',
      canfd0_apb_psel_i => '0', canfd0_apb_pwrite_i => '0',
      canfd0_apb_pwdata_i => canfd_apb_pwdata_idle,
      canfd0_apb_prdata_o => open, canfd0_apb_pready_o => open, canfd0_apb_pslverr_o => open,
      canfd1_apb_paddr_i => canfd_apb_paddr_idle, canfd1_apb_penable_i => '0',
      canfd1_apb_psel_i => '0', canfd1_apb_pwrite_i => '0',
      canfd1_apb_pwdata_i => canfd_apb_pwdata_idle,
      canfd1_apb_prdata_o => open, canfd1_apb_pready_o => open, canfd1_apb_pslverr_o => open,

      m_axi_h2c_o => open, m_axi_c2h_i => axi_c2h_idle, irq_o => open,

      -- DS-P2.100 loopback, mirrored verbatim from deepskopion_de25_top.vhd
      -- (the exact mutation target for Finding B(1) — see file header).
      emio_uart_tx_i => stim_uart_tx_w, emio_uart_rx_o => open,
      emio_i2c_scl_o_i => stim_i2c_scl_o_w, emio_i2c_scl_t_i => stim_i2c_scl_t_w,
      emio_i2c_sda_o_i => stim_i2c_sda_o_w, emio_i2c_sda_t_i => stim_i2c_sda_t_w,
      emio_i2c_scl_i_o => open, emio_i2c_sda_i_o => open,
      emio_spi_sclk_o_i => stim_spi_sclk_w, emio_spi_sclk_t_i => '0',
      emio_spi_mosi_o_i => stim_spi_mosi_w, emio_spi_mosi_t_i => '0',
      emio_spi_miso_o_i => '0', emio_spi_miso_t_i => '1',
      emio_spi_ss_o_i => stim_spi_ss_w, emio_spi_ss_t_i => '0',
      emio_spi_sclk_i_o => open, emio_spi_mosi_i_o => open,
      emio_spi_miso_i_o => open, emio_spi_ss_i_o => open,
      emio_gpio_o_i => stim_spi_ss_w, emio_gpio_t_i => '0', emio_gpio_i_o => open,
      emio_can0_tx_i => '1', emio_can1_tx_i => '1', emio_can_rx_o => open,

      stim_uart_tx_o => stim_uart_tx_w,
      stim_i2c_scl_o_o => stim_i2c_scl_o_w, stim_i2c_scl_t_o => stim_i2c_scl_t_w,
      stim_i2c_sda_o_o => stim_i2c_sda_o_w, stim_i2c_sda_t_o => stim_i2c_sda_t_w,
      stim_spi_sclk_o_o => stim_spi_sclk_w, stim_spi_mosi_o_o => stim_spi_mosi_w,
      stim_spi_ss_o_o => stim_spi_ss_w,
      mss_uart_tx_i => '1', mss_uart_rx_o => open,
      mss_i2c_scl_oe_i => '0', mss_i2c_sda_oe_i => '0',
      mss_i2c_scl_o => open, mss_i2c_sda_o => open,
      mss_spi_clk_i => '0', mss_spi_clk_oe_i => '0',
      mss_spi_do_i => '0', mss_spi_do_oe_i => '0',
      mss_spi_ss_i => '1', mss_spi_ss_oe_i => '0',
      mss_spi_di_o => open,
      mss_can_tx_i => '1', mss_can_rx_o => open,

      rea_probe_o => open, rea_trigger_i => '0',
      rea_tap_tck_i => '0', rea_tap_tms_i => '1', rea_tap_tdi_i => '1', rea_tap_tdo_i => '1'
    );

  OBS_STIM_UART_TX <= stim_uart_tx_w;

  ---------------------------------------------------------------------------
  -- White-box probe into u_core's private capture_pin(42) — VHDL-2008
  -- external name. Declared in a block AFTER u_core's instantiation
  -- statement: an external name is only resolvable once the target object
  -- has been elaborated, and within one architecture the declarative part
  -- elaborates before the concurrent statement part, so a same-region
  -- declarative alias to a not-yet-instantiated child fails ("external
  -- name ... not found") — proven empirically before adopting this shape.
  ---------------------------------------------------------------------------
  probe_blk : block
    alias capture_pin_ext is
      << signal .tb_stimulus_core_seam.u_core.capture_pin : std_logic_vector(63 downto 0) >>;
  begin
    PROBE_CAPTURE_EDGE0 <= capture_pin_ext(42);
    PROBE_CAPTURE_UART36 <= capture_pin_ext(36);
    PROBE_CAPTURE_I2C_SCL <= capture_pin_ext(12);
    PROBE_CAPTURE_I2C_SDA <= capture_pin_ext(13);
    PROBE_CAPTURE_SPI_CS  <= capture_pin_ext(20);
  end block probe_blk;

end architecture str;
