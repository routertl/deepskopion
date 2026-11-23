-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
-------------------------------------------------------------------------------
-- Title      : tb_datalogger_mix_sweep
-- Project    : deepskopion (DS-P1.65)
-------------------------------------------------------------------------------
-- Description: Integration testbench wrapper that instantiates:
--   1) tich.deepskopion_core (verifying elaboration of core across arbitrary mixes)
--   2) tich.datalogger_gather (verifying per-protocol routing & smoke traffic)
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.system_regbank_pkg.all;
use tich.zs_msg_pkg.all;

library rr;

entity tb_datalogger_mix_sweep is
  generic (
    G_SYS_CLK : natural := 100_000_000
  );
  port (
    CLK                : in  std_logic;
    RST                : in  std_logic;
    CAPTURE_PINS       : in  std_logic_vector(C_NUM_DATALOGGER_PINS - 1 downto 0);
    CONFIG             : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH - 1 downto 0);
    TIMESTAMP          : in  std_logic_vector(C_TIMESTAMP_WIDTH - 1 downto 0);
    DOUT_REQ           : in  std_logic;
    DOUT_PAYLOAD       : out std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0);
    DOUT_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    DOUT_MSG_TYPE      : out std_logic_vector(15 downto 0);
    DOUT_VALID         : out std_logic;
    PROTO_PKT_CNT_FLAT : out std_logic_vector(C_NUM_PROTO_COUNTERS*32 - 1 downto 0);

    -- S_AXIL slave interface to deepskopion_core system_regbank
    S_AXIL_AWADDR      : in  std_logic_vector(31 downto 0) := (others => '0');
    S_AXIL_AWPROT      : in  std_logic_vector(2 downto 0)  := (others => '0');
    S_AXIL_AWVALID     : in  std_logic                     := '0';
    S_AXIL_AWREADY     : out std_logic;
    S_AXIL_WDATA       : in  std_logic_vector(31 downto 0) := (others => '0');
    S_AXIL_WSTRB       : in  std_logic_vector(3 downto 0)  := (others => '1');
    S_AXIL_WVALID      : in  std_logic                     := '0';
    S_AXIL_WREADY      : out std_logic;
    S_AXIL_BRESP       : out std_logic_vector(1 downto 0);
    S_AXIL_BVALID      : out std_logic;
    S_AXIL_BREADY      : in  std_logic                     := '1';
    S_AXIL_ARADDR      : in  std_logic_vector(31 downto 0) := (others => '0');
    S_AXIL_ARPROT      : in  std_logic_vector(2 downto 0)  := (others => '0');
    S_AXIL_ARVALID     : in  std_logic                     := '0';
    S_AXIL_ARREADY     : out std_logic;
    S_AXIL_RDATA       : out std_logic_vector(31 downto 0);
    S_AXIL_RRESP       : out std_logic_vector(1 downto 0);
    S_AXIL_RVALID      : out std_logic;
    S_AXIL_RREADY      : in  std_logic                     := '1'
  );
end entity tb_datalogger_mix_sweep;

architecture str of tb_datalogger_mix_sweep is

  signal dummy_led        : std_logic_vector(3 downto 0);
  signal dummy_axi_h2c    : rr.ddr_stream_pkg.axi4_h2c_bus_t;
  signal dummy_axi_c2h    : rr.ddr_stream_pkg.axi4_c2h_bus_t := (
    AWREADY => '0', WREADY => '0',
    BID => (others => '0'), BRESP => (others => '0'), BVALID => '0',
    ARREADY => '0',
    RID => (others => '0'), RDATA => (others => '0'), RRESP => (others => '0'),
    RLAST => '0', RVALID => '0'
  );
  signal dummy_irq        : std_logic_vector(2 downto 0);
  signal dummy_rea_probe  : std_logic_vector(31 downto 0);

  signal core_ja          : std_logic_vector(7 downto 0);
  signal core_jb          : std_logic_vector(7 downto 0);
  signal board_drive      : std_logic_vector(C_NUM_BOARD_PINS - 1 downto 0);

  signal gather_fifo_csr  : t_fifo_csr;
  signal gather_proto_cnt : t_proto_counter_array(0 to C_NUM_PROTO_COUNTERS - 1);

begin

  -- DS-P1.66: the bench plays the board. Each logical pin's stimulus goes to
  -- the board pin the generated C_CAPTURE_BOARD_PIN assigns it, which is
  -- where a user wires that signal. (tb_core_pin_map checks that map on the
  -- core independently; here it only routes the traffic.)
  p_board : process (CAPTURE_PINS)
    variable v : std_logic_vector(C_NUM_BOARD_PINS - 1 downto 0);
  begin
    v := (others => '0');
    for i in CAPTURE_PINS'range loop
      if C_CAPTURE_BOARD_PIN(i) /= C_BOARD_PIN_NONE then
        v(C_CAPTURE_BOARD_PIN(i)) := CAPTURE_PINS(i);
      end if;
    end loop;
    board_drive <= v;
  end process p_board;

  core_ja(0) <= board_drive(C_BOARD_PIN_JA0);
  core_ja(1) <= board_drive(C_BOARD_PIN_JA1);
  core_ja(2) <= board_drive(C_BOARD_PIN_JA2);
  core_ja(3) <= board_drive(C_BOARD_PIN_JA3);
  core_ja(4) <= board_drive(C_BOARD_PIN_JA4);
  core_ja(5) <= board_drive(C_BOARD_PIN_JA5);
  core_ja(6) <= board_drive(C_BOARD_PIN_JA6);
  core_ja(7) <= board_drive(C_BOARD_PIN_JA7);

  core_jb(0) <= board_drive(C_BOARD_PIN_JB0);
  core_jb(1) <= board_drive(C_BOARD_PIN_JB1);
  core_jb(2) <= board_drive(C_BOARD_PIN_JB2);
  core_jb(3) <= board_drive(C_BOARD_PIN_JB3);
  core_jb(4) <= board_drive(C_BOARD_PIN_JB4);
  core_jb(5) <= board_drive(C_BOARD_PIN_JB5);
  core_jb(6) <= board_drive(C_BOARD_PIN_JB6);
  core_jb(7) <= board_drive(C_BOARD_PIN_JB7);

  -- 1) Instantiate deepskopion_core to exercise full elaboration across mixes
  u_core : entity tich.deepskopion_core
    port map (
      clk_sys_i           => CLK,
      rst_sys_i           => RST,
      clk_ddr_i           => CLK,
      rst_ddr_i           => RST,
      led                 => dummy_led,
      ja                  => core_ja,
      jb                  => core_jb,
      s_axil_awaddr_i     => S_AXIL_AWADDR,
      s_axil_awprot_i     => S_AXIL_AWPROT,
      s_axil_awvalid_i    => S_AXIL_AWVALID,
      s_axil_awready_o    => S_AXIL_AWREADY,
      s_axil_wdata_i      => S_AXIL_WDATA,
      s_axil_wstrb_i      => S_AXIL_WSTRB,
      s_axil_wvalid_i     => S_AXIL_WVALID,
      s_axil_wready_o     => S_AXIL_WREADY,
      s_axil_bresp_o      => S_AXIL_BRESP,
      s_axil_bvalid_o     => S_AXIL_BVALID,
      s_axil_bready_i     => S_AXIL_BREADY,
      s_axil_araddr_i     => S_AXIL_ARADDR,
      s_axil_arprot_i     => S_AXIL_ARPROT,
      s_axil_arvalid_i    => S_AXIL_ARVALID,
      s_axil_arready_o    => S_AXIL_ARREADY,
      s_axil_rdata_o      => S_AXIL_RDATA,
      s_axil_rresp_o      => S_AXIL_RRESP,
      s_axil_rvalid_o     => S_AXIL_RVALID,
      s_axil_rready_i     => S_AXIL_RREADY,
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
      m_axi_h2c_o         => dummy_axi_h2c,
      m_axi_c2h_i         => dummy_axi_c2h,
      irq_o               => dummy_irq,
      emio_uart_tx_i      => '1',
      emio_uart_rx_o      => open,
      emio_i2c_scl_o_i    => '1',
      emio_i2c_scl_t_i    => '1',
      emio_i2c_sda_o_i    => '1',
      emio_i2c_sda_t_i    => '1',
      emio_i2c_scl_i_o    => open,
      emio_i2c_sda_i_o    => open,
      emio_spi_sclk_o_i   => '1',
      emio_spi_sclk_t_i   => '1',
      emio_spi_mosi_o_i   => '1',
      emio_spi_mosi_t_i   => '1',
      emio_spi_miso_o_i   => '1',
      emio_spi_miso_t_i   => '1',
      emio_spi_ss_o_i     => '1',
      emio_spi_ss_t_i     => '1',
      emio_spi_sclk_i_o   => open,
      emio_spi_mosi_i_o   => open,
      emio_spi_miso_i_o   => open,
      emio_spi_ss_i_o     => open,
      emio_gpio_o_i       => '1',
      emio_gpio_t_i       => '1',
      emio_gpio_i_o       => open,
      emio_can0_tx_i      => '1',
      emio_can1_tx_i      => '1',
      emio_can_rx_o       => open,
      stim_uart_tx_o      => open,
      stim_i2c_scl_o_o    => open,
      stim_i2c_scl_t_o    => open,
      stim_i2c_sda_o_o    => open,
      stim_i2c_sda_t_o    => open,
      stim_spi_sclk_o_o   => open,
      stim_spi_mosi_o_o   => open,
      stim_spi_ss_o_o     => open,
      mss_uart_tx_i       => '1',
      mss_uart_rx_o       => open,
      mss_i2c_scl_oe_i    => '0',
      mss_i2c_sda_oe_i    => '0',
      mss_i2c_scl_o       => open,
      mss_i2c_sda_o       => open,
      mss_spi_clk_i       => '1',
      mss_spi_clk_oe_i    => '0',
      mss_spi_do_i        => '1',
      mss_spi_do_oe_i     => '0',
      mss_spi_ss_i        => '1',
      mss_spi_ss_oe_i     => '0',
      mss_spi_di_o        => open,
      mss_can_tx_i        => '1',
      mss_can_rx_o        => open,
      rea_probe_o         => dummy_rea_probe,
      rea_trigger_i       => '0',
      rea_tap_tck_i       => board_drive(C_BOARD_PIN_REA_TCK),
      rea_tap_tms_i       => board_drive(C_BOARD_PIN_REA_TMS),
      rea_tap_tdi_i       => board_drive(C_BOARD_PIN_REA_TDI),
      rea_tap_tdo_i       => board_drive(C_BOARD_PIN_REA_TDO)
    );

  -- 2) Instantiate datalogger_gather to drive direct traffic smoke tests
  u_gather : entity tich.datalogger_gather
    generic map (
      G_SYS_CLK => G_SYS_CLK
    )
    port map (
      CLK                => CLK,
      RST                => RST,
      CAPTURE_PINS       => CAPTURE_PINS,
      CONFIG             => CONFIG,
      TIMESTAMP          => TIMESTAMP,
      DOUT_PAYLOAD       => DOUT_PAYLOAD,
      DOUT_DATALOGGER_ID => DOUT_DATALOGGER_ID,
      DOUT_MSG_TYPE      => DOUT_MSG_TYPE,
      DOUT_VALID         => DOUT_VALID,
      DOUT_REQ           => DOUT_REQ,
      FIFO_CSR           => gather_fifo_csr,
      PROTO_COUNTERS     => gather_proto_cnt
    );

  gen_proto_cnts : for i in 0 to C_NUM_PROTO_COUNTERS - 1 generate
    PROTO_PKT_CNT_FLAT((i+1)*32 - 1 downto i*32) <= gather_proto_cnt(i).pkt_cnt;
  end generate gen_proto_cnts;

end architecture str;
