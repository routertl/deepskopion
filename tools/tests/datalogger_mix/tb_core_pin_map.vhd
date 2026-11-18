-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
-------------------------------------------------------------------------------
-- Title      : tb_core_pin_map
-- Project    : deepskopion (DS-P1.66)
-------------------------------------------------------------------------------
-- Description: deepskopion_core with every capture source it has as a bench
--   port: PMOD JA/JB, the four REA JTAG tap inputs and one CAN controller TX
--   leg (the fabric CAN bus is the wired-AND of the TX legs). The cocotb bench
--   toggles one source at a time and reads the core's own capture_pin bus
--   (CAPTURE_PIN_OBS), so it measures which logical capture pin each board
--   input really reaches. Nothing here reads the generated pin map: the map
--   is what the bench checks, not what it uses.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_config_pkg.all;

library rr;

entity tb_core_pin_map is
  port (
    CLK             : in  std_logic;
    RST             : in  std_logic;
    JA              : in  std_logic_vector(7 downto 0);
    JB              : in  std_logic_vector(7 downto 0);
    REA_TCK         : in  std_logic;
    REA_TMS         : in  std_logic;
    REA_TDI         : in  std_logic;
    REA_TDO         : in  std_logic;
    CAN0_TX         : in  std_logic;
    CAPTURE_PIN_OBS : out std_logic_vector(C_NUM_DATALOGGER_PINS - 1 downto 0)
  );
end entity tb_core_pin_map;

architecture str of tb_core_pin_map is

  signal dummy_axi_h2c : rr.ddr_stream_pkg.axi4_h2c_bus_t;
  signal dummy_axi_c2h : rr.ddr_stream_pkg.axi4_c2h_bus_t := (
    AWREADY => '0', WREADY => '0',
    BID => (others => '0'), BRESP => (others => '0'), BVALID => '0',
    ARREADY => '0',
    RID => (others => '0'), RDATA => (others => '0'), RRESP => (others => '0'),
    RLAST => '0', RVALID => '0'
  );

begin

  CAPTURE_PIN_OBS <= << signal .tb_core_pin_map.u_core.capture_pin
                        : std_logic_vector(C_NUM_DATALOGGER_PINS - 1 downto 0) >>;

  u_core : entity tich.deepskopion_core
    port map (
      clk_sys_i            => CLK,
      rst_sys_i            => RST,
      clk_ddr_i            => CLK,
      rst_ddr_i            => RST,
      led                  => open,
      ja                   => JA,
      jb                   => JB,
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
      emio_spi_sclk_o_i    => '1',
      emio_spi_sclk_t_i    => '1',
      emio_spi_mosi_o_i    => '1',
      emio_spi_mosi_t_i    => '1',
      emio_spi_miso_o_i    => '1',
      emio_spi_miso_t_i    => '1',
      emio_spi_ss_o_i      => '1',
      emio_spi_ss_t_i      => '1',
      emio_spi_sclk_i_o    => open,
      emio_spi_mosi_i_o    => open,
      emio_spi_miso_i_o    => open,
      emio_spi_ss_i_o      => open,
      emio_gpio_o_i        => '1',
      emio_gpio_t_i        => '1',
      emio_gpio_i_o        => open,
      emio_can0_tx_i       => CAN0_TX,
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
      mss_spi_clk_i        => '1',
      mss_spi_clk_oe_i     => '0',
      mss_spi_do_i         => '1',
      mss_spi_do_oe_i      => '0',
      mss_spi_ss_i         => '1',
      mss_spi_ss_oe_i      => '0',
      mss_spi_di_o         => open,
      mss_can_tx_i         => '1',
      mss_can_rx_o         => open,
      rea_probe_o          => open,
      rea_trigger_i        => '0',
      rea_tap_tck_i        => REA_TCK,
      rea_tap_tms_i        => REA_TMS,
      rea_tap_tdi_i        => REA_TDI,
      rea_tap_tdo_i        => REA_TDO
    );

end architecture str;
