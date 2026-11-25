-- rr unknown-llm (AI-generated)

-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
-------------------------------------------------------------------------------
-- Title      : tb_commit_addr_crossing
-- Project    : deepskopion (DS-T1.21)
-------------------------------------------------------------------------------
-- Description: Testbench harness wrapping deepskopion_core to verify the
--   coherent toggle req/ack handshake crossing for ddr_wr_commit_addr ->
--   ddr_commit_addr_sys across clk_ddr and clk_sys domains.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_config_pkg.all;

library rr;

entity tb_commit_addr_crossing is
  port (
    CLK_DDR                : in  std_logic;
    RST_DDR                : in  std_logic;
    CLK_SYS                : in  std_logic;
    RST_SYS                : in  std_logic;
    DDR_WR_COMMIT_ADDR_HI  : in  std_logic_vector(15 downto 0);
    DDR_WR_COMMIT_ADDR_LO  : in  std_logic_vector(15 downto 0);
    DDR_COMMIT_ADDR_SYS    : out std_logic_vector(31 downto 0);
    COMMIT_ADDR_HELD_DDR   : out std_logic_vector(31 downto 0);
    COMMIT_REQ_TGL_DDR     : out std_logic;
    COMMIT_ACK_SYNC_DDR    : out std_logic_vector(1 downto 0);
    COMMIT_REQ_SYNC_SYS    : out std_logic_vector(1 downto 0);
    COMMIT_ACK_TGL_SYS     : out std_logic
  );
end entity tb_commit_addr_crossing;

architecture str of tb_commit_addr_crossing is
  signal dummy_axi_h2c : rr.ddr_stream_pkg.axi4_h2c_bus_t;
  signal dummy_axi_c2h : rr.ddr_stream_pkg.axi4_c2h_bus_t := (
    AWREADY => '0', WREADY => '0',
    BID => (others => '0'), BRESP => (others => '0'), BVALID => '0',
    ARREADY => '0',
    RID => (others => '0'), RDATA => (others => '0'), RRESP => (others => '0'),
    RLAST => '0', RVALID => '0'
  );
begin
  << signal .tb_commit_addr_crossing.u_core.ddr_wr_commit_addr : std_logic_vector(31 downto 0) >>
    <= DDR_WR_COMMIT_ADDR_HI & DDR_WR_COMMIT_ADDR_LO;

  DDR_COMMIT_ADDR_SYS  <= << signal .tb_commit_addr_crossing.u_core.ddr_commit_addr_sys : std_logic_vector(31 downto 0) >>;
  COMMIT_ADDR_HELD_DDR <= << signal .tb_commit_addr_crossing.u_core.commit_addr_held_ddr : std_logic_vector(31 downto 0) >>;
  COMMIT_REQ_TGL_DDR   <= << signal .tb_commit_addr_crossing.u_core.commit_req_tgl_ddr : std_logic >>;
  COMMIT_ACK_SYNC_DDR  <= << signal .tb_commit_addr_crossing.u_core.commit_ack_sync_ddr : std_logic_vector(1 downto 0) >>;
  COMMIT_REQ_SYNC_SYS  <= << signal .tb_commit_addr_crossing.u_core.commit_req_sync_sys : std_logic_vector(1 downto 0) >>;
  COMMIT_ACK_TGL_SYS   <= << signal .tb_commit_addr_crossing.u_core.commit_ack_tgl_sys : std_logic >>;

  u_core : entity tich.deepskopion_core
    generic map (
      G_DDR_BACKEND => "none"
    )
    port map (
      clk_sys_i            => CLK_SYS,
      rst_sys_i            => RST_SYS,
      clk_ddr_i            => CLK_DDR,
      rst_ddr_i            => RST_DDR,
      led                  => open,
      ja                   => (others => '0'),
      jb                   => (others => '0'),
      s_axil_awaddr_i      => (others => '0'),
      s_axil_awprot_i      => (others => '0'),
      s_axil_awvalid_i     => '0',
      s_axil_awready_o     => open,
      s_axil_wdata_i       => (others => '0'),
      s_axil_wstrb_i       => (others => '0'),
      s_axil_wvalid_i      => '0',
      s_axil_wready_o      => open,
      s_axil_bresp_o       => open,
      s_axil_bvalid_o      => open,
      s_axil_bready_i      => '1',
      s_axil_araddr_i      => (others => '0'),
      s_axil_arprot_i      => (others => '0'),
      s_axil_arvalid_i     => '0',
      s_axil_arready_o     => open,
      s_axil_rdata_o       => open,
      s_axil_rresp_o       => open,
      s_axil_rvalid_o      => open,
      s_axil_rready_i      => '1',
      canfd0_apb_paddr_i   => (others => '0'),
      canfd0_apb_penable_i => '0',
      canfd0_apb_psel_i    => '0',
      canfd0_apb_pwrite_i  => '0',
      canfd0_apb_pwdata_i  => (others => '0'),
      canfd0_apb_prdata_o  => open,
      canfd0_apb_pready_o  => open,
      canfd0_apb_pslverr_o => open,
      canfd1_apb_paddr_i   => (others => '0'),
      canfd1_apb_penable_i => '0',
      canfd1_apb_psel_i    => '0',
      canfd1_apb_pwrite_i  => '0',
      canfd1_apb_pwdata_i  => (others => '0'),
      canfd1_apb_prdata_o  => open,
      canfd1_apb_pready_o  => open,
      canfd1_apb_pslverr_o => open,
      m_axi_h2c_o          => dummy_axi_h2c,
      m_axi_c2h_i          => dummy_axi_c2h,
      irq_o                => open,
      emio_uart_tx_i       => '1',
      emio_uart_rx_o       => open,
      emio_i2c_scl_o_i     => '1',
      emio_i2c_scl_t_i     => '1',
      emio_i2c_sda_o_i     => '1',
      emio_i2c_sda_t_i     => '1',
      emio_i2c_scl_i_o     => open,
      emio_i2c_sda_i_o     => open,
      emio_spi_sclk_o_i    => '0',
      emio_spi_sclk_t_i    => '1',
      emio_spi_mosi_o_i    => '0',
      emio_spi_mosi_t_i    => '1',
      emio_spi_miso_o_i    => '0',
      emio_spi_miso_t_i    => '1',
      emio_spi_ss_o_i      => '1',
      emio_spi_ss_t_i      => '1',
      emio_spi_sclk_i_o    => open,
      emio_spi_mosi_i_o    => open,
      emio_spi_miso_i_o    => open,
      emio_spi_ss_i_o      => open,
      emio_gpio_o_i        => '0',
      emio_gpio_t_i        => '1',
      emio_gpio_i_o        => open,
      emio_can0_tx_i       => '1',
      emio_can1_tx_i       => '1',
      emio_can_rx_o        => open,
      stim_uart_tx_o       => open,
      stim_i2c_scl_o_o     => open,
      stim_i2c_scl_t_o     => open,
      stim_i2c_sda_o_o     => open,
      stim_i2c_sda_t_o     => open,
      stim_spi_sclk_o_o    => open,
      stim_spi_mosi_o_o    => open,
      stim_spi_ss_o_o      => open,
      mss_uart_tx_i        => '1',
      mss_uart_rx_o        => open,
      mss_i2c_scl_oe_i     => '0',
      mss_i2c_sda_oe_i     => '0',
      mss_i2c_scl_o        => open,
      mss_i2c_sda_o        => open,
      mss_spi_clk_i        => '0',
      mss_spi_clk_oe_i     => '0',
      mss_spi_do_i         => '0',
      mss_spi_do_oe_i      => '0',
      mss_spi_ss_i         => '1',
      mss_spi_ss_oe_i      => '0',
      mss_spi_di_o         => open,
      mss_can_tx_i         => '1',
      mss_can_rx_o         => open,
      rea_probe_o          => open,
      rea_trigger_i        => '0',
      rea_tap_tck_i        => '0',
      rea_tap_tms_i        => '0',
      rea_tap_tdi_i        => '0',
      rea_tap_tdo_i        => '0'
    );
end architecture;
