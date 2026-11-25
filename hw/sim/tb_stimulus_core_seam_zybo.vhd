-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : tb_stimulus_core_seam_zybo
-- Project    : deepskopion / DS-P1.90
-------------------------------------------------------------------------------
-- File       : tb_stimulus_core_seam_zybo.vhd
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   The REAL tich.deepskopion_core wired as the Zynq top wires it: the emio_*
--   legs are driven by the test (PS_* ports, standing in for the PS EMIO) and
--   the stim_*_o ports are left open. G_STIM_DRIVES_EMIO is true, as in
--   deepskopion_top.vhd.
--
--   Both sides of the DS-P1.90 mux are observed at the core's own capture
--   pins, the same taps as tb_stimulus_core_seam.vhd (36 UART, 12/13 I2C
--   SCL/SDA, 20 SPI GPIO-CS, 42 EDGE0):
--     * a protocol's STIM_*_CONTROL bit[0] set: the generator drives the leg,
--       while the PS_* port holds a conflicting level;
--     * bit[0] clear: the PS_* port drives the leg, as before DS-P1.90.
--   Everything else is tb_stimulus_core_seam.vhd unchanged, so see that
--   file for the probe and idle-tie rationale.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
library rr;

entity tb_stimulus_core_seam_zybo is
  generic (
    -- The value the Zynq top passes (deepskopion_top.vhd). Red-first runs
    -- set false to show the stimulus-side tests depend on it.
    G_STIM_DRIVES_EMIO : boolean := true
  );
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
    PROBE_CAPTURE_SPI_CS  : out std_logic;

    -- ── PS EMIO legs (DS-P1.90): driven by the test, as the Zynq PS drives
    -- them on the board. Defaults are the PS-idle levels.
    PS_UART_TX   : in std_logic := '1';
    PS_I2C_SCL_O : in std_logic := '0';
    PS_I2C_SCL_T : in std_logic := '1';
    PS_I2C_SDA_O : in std_logic := '0';
    PS_I2C_SDA_T : in std_logic := '1';
    PS_SPI_SCLK  : in std_logic := '0';
    PS_SPI_MOSI  : in std_logic := '0';
    PS_SPI_SS    : in std_logic := '1'   -- native SS and the GPIO chip-select
  );
end entity tb_stimulus_core_seam_zybo;

architecture str of tb_stimulus_core_seam_zybo is

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
      G_AXI_MAX_BURST => 16, G_DDR_DATA_WIDTH => 256,
      G_STIM_DRIVES_EMIO => G_STIM_DRIVES_EMIO
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

      -- Zynq wiring, as deepskopion_top.vhd: the emio_* legs come from the PS
      -- (here the PS_* ports) and stim_*_o has no consumer outside the core.
      emio_uart_tx_i => PS_UART_TX, emio_uart_rx_o => open,
      emio_i2c_scl_o_i => PS_I2C_SCL_O, emio_i2c_scl_t_i => PS_I2C_SCL_T,
      emio_i2c_sda_o_i => PS_I2C_SDA_O, emio_i2c_sda_t_i => PS_I2C_SDA_T,
      emio_i2c_scl_i_o => open, emio_i2c_sda_i_o => open,
      emio_spi_sclk_o_i => PS_SPI_SCLK, emio_spi_sclk_t_i => '0',
      emio_spi_mosi_o_i => PS_SPI_MOSI, emio_spi_mosi_t_i => '0',
      emio_spi_miso_o_i => '0', emio_spi_miso_t_i => '1',
      emio_spi_ss_o_i => PS_SPI_SS, emio_spi_ss_t_i => '0',
      emio_spi_sclk_i_o => open, emio_spi_mosi_i_o => open,
      emio_spi_miso_i_o => open, emio_spi_ss_i_o => open,
      emio_gpio_o_i => PS_SPI_SS, emio_gpio_t_i => '0', emio_gpio_i_o => open,
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
      << signal .tb_stimulus_core_seam_zybo.u_core.capture_pin : std_logic_vector(63 downto 0) >>;
  begin
    PROBE_CAPTURE_EDGE0 <= capture_pin_ext(42);
    PROBE_CAPTURE_UART36 <= capture_pin_ext(36);
    PROBE_CAPTURE_I2C_SCL <= capture_pin_ext(12);
    PROBE_CAPTURE_I2C_SDA <= capture_pin_ext(13);
    PROBE_CAPTURE_SPI_CS  <= capture_pin_ext(20);
  end block probe_blk;

end architecture str;
