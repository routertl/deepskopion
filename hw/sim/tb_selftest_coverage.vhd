-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
-------------------------------------------------------------------------------
-- Title      : tb_selftest_coverage
-- Project    : deepskopion / DS-P1.104
-------------------------------------------------------------------------------
-- File       : tb_selftest_coverage.vhd
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Self-test coverage bench: which data loggers of the default mix receive
--   traffic from a board-alone source. The real tich.deepskopion_core with the
--   default mix, as tb_default_mix_core, except:
--     * the header inputs JA/JB idle HIGH (the header pins carry weak
--       pull-ups and nothing is connected), and are never driven here;
--     * the board-alone sources are ports the test drives: the PS CAN pair
--       (emio_can0/1_tx, Zybo), the PolarFire MSS UART and CAN TX legs; the
--       in-core stimulus generator drives the EMIO legs (G_STIM_DRIVES_EMIO,
--       true in all three product tops' wiring of the generator).
--   The records are read at the generated gather's output, as in
--   tb_default_mix_core.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
library rr;

entity tb_selftest_coverage is
  port (
    CLK : in std_logic;
    RST : in std_logic;

    -- Header inputs of the core, one scalar port per pin so a bus model can
    -- drive a single line; packed into ja/jb below. Idle-low unless driven.
    -- Board-alone sources the test drives; idle levels by default.
    PS_CAN0_TX, PS_CAN1_TX : in std_logic := '1';
    MSS_UART_TX, MSS_CAN_TX : in std_logic := '1';

    -- AXI4-Lite slave onto deepskopion_core's own s_axil_* port.
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

    -- Observation: the gather output inside the core (read-only probes).
    PROBE_DOUT_VALID         : out std_logic;
    PROBE_DOUT_REQ           : out std_logic;
    PROBE_DOUT_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    PROBE_DOUT_MSG_TYPE      : out std_logic_vector(15 downto 0);
    PROBE_DOUT_PAYLOAD       : out std_logic_vector(31 downto 0);
    -- DDR write bursts accepted by the responder (activity counter).
    DDR_BURSTS               : out std_logic_vector(31 downto 0)
  );
end entity tb_selftest_coverage;

architecture str of tb_selftest_coverage is

  signal ja_w, jb_w : std_logic_vector(7 downto 0);

  signal canfd_apb_paddr_idle  : std_logic_vector(31 downto 0) := (others => '0');
  signal canfd_apb_pwdata_idle : std_logic_vector(31 downto 0) := (others => '0');

  signal axi_h2c : rr.ddr_stream_pkg.axi4_h2c_bus_t;
  signal axi_c2h : rr.ddr_stream_pkg.axi4_c2h_bus_t;

  signal b_pending : unsigned(15 downto 0) := (others => '0');
  signal b_id      : std_logic_vector(axi_c2h.BID'range) := (others => '0');
  signal bursts    : unsigned(31 downto 0) := (others => '0');

begin

  -- Header pins: nothing connected, weak pull-ups.
  ja_w <= (others => '1');
  jb_w <= (others => '1');

  u_core : entity tich.deepskopion_core
    generic map (
      G_DDR_BASE_ADDR => x"10000000", G_DDR_WIN_WIDTH => 28,
      G_AXI_MAX_BURST => 16, G_DDR_DATA_WIDTH => 128,
      G_CTU_TXT_BUFFERS => 2,
      G_STIM_DRIVES_EMIO => true
    )
    port map (
      clk_sys_i => CLK, rst_sys_i => RST, clk_ddr_i => CLK, rst_ddr_i => RST,
      led => open, ja => ja_w, jb => jb_w,

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

      m_axi_h2c_o => axi_h2c, m_axi_c2h_i => axi_c2h, irq_o => open,

      -- PS EMIO legs at their idle levels (no PS traffic in this bench).
      emio_uart_tx_i => '1', emio_uart_rx_o => open,
      emio_i2c_scl_o_i => '0', emio_i2c_scl_t_i => '1',
      emio_i2c_sda_o_i => '0', emio_i2c_sda_t_i => '1',
      emio_i2c_scl_i_o => open, emio_i2c_sda_i_o => open,
      emio_spi_sclk_o_i => '0', emio_spi_sclk_t_i => '0',
      emio_spi_mosi_o_i => '0', emio_spi_mosi_t_i => '0',
      emio_spi_miso_o_i => '0', emio_spi_miso_t_i => '1',
      emio_spi_ss_o_i => '1', emio_spi_ss_t_i => '0',
      emio_spi_sclk_i_o => open, emio_spi_mosi_i_o => open,
      emio_spi_miso_i_o => open, emio_spi_ss_i_o => open,
      emio_gpio_o_i => '1', emio_gpio_t_i => '0', emio_gpio_i_o => open,
      emio_can0_tx_i => PS_CAN0_TX, emio_can1_tx_i => PS_CAN1_TX, emio_can_rx_o => open,

      stim_uart_tx_o => open,
      stim_i2c_scl_o_o => open, stim_i2c_scl_t_o => open,
      stim_i2c_sda_o_o => open, stim_i2c_sda_t_o => open,
      stim_spi_sclk_o_o => open, stim_spi_mosi_o_o => open,
      stim_spi_ss_o_o => open,
      mss_uart_tx_i => MSS_UART_TX, mss_uart_rx_o => open,
      mss_i2c_scl_oe_i => '0', mss_i2c_sda_oe_i => '0',
      mss_i2c_scl_o => open, mss_i2c_sda_o => open,
      mss_spi_clk_i => '0', mss_spi_clk_oe_i => '0',
      mss_spi_do_i => '0', mss_spi_do_oe_i => '0',
      mss_spi_ss_i => '1', mss_spi_ss_oe_i => '0',
      mss_spi_di_o => open,
      mss_can_tx_i => MSS_CAN_TX, mss_can_rx_o => open,

      rea_probe_o => open, rea_trigger_i => '0',
      rea_tap_tck_i => '0', rea_tap_tms_i => '1', rea_tap_tdi_i => '1', rea_tap_tdo_i => '1'
    );

  ---------------------------------------------------------------------------
  -- DDR write responder: every address and data beat accepted at once, one
  -- OKAY response per WLAST. The read channel is never used by the writer.
  ---------------------------------------------------------------------------
  axi_c2h.AWREADY <= '1';
  axi_c2h.WREADY  <= '1';
  axi_c2h.BRESP   <= (others => '0');
  axi_c2h.BID     <= b_id;
  axi_c2h.BVALID  <= '1' when b_pending /= 0 else '0';
  axi_c2h.ARREADY <= '0';
  axi_c2h.RID     <= (others => '0');
  axi_c2h.RDATA   <= (others => '0');
  axi_c2h.RRESP   <= (others => '0');
  axi_c2h.RLAST   <= '0';
  axi_c2h.RVALID  <= '0';

  p_resp : process (CLK)
    variable inc, dec : boolean;
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        b_pending <= (others => '0');
        bursts <= (others => '0');
      else
        if axi_h2c.AWVALID = '1' then
          b_id <= axi_h2c.AWID;
        end if;
        inc := axi_h2c.WVALID = '1' and axi_h2c.WLAST = '1';
        dec := b_pending /= 0 and axi_h2c.BREADY = '1';
        if inc and not dec then
          b_pending <= b_pending + 1;
        elsif dec and not inc then
          b_pending <= b_pending - 1;
        end if;
        if inc then
          bursts <= bursts + 1;
        end if;
      end if;
    end if;
  end process p_resp;
  DDR_BURSTS <= std_logic_vector(bursts);

  ---------------------------------------------------------------------------
  -- Read-only probes on the gather output inside u_core (VHDL-2008 external
  -- names, in a block after the instance; see tb_stimulus_core_seam_zybo.vhd).
  ---------------------------------------------------------------------------
  probe_blk : block
    alias dout_valid_ext is << signal .tb_selftest_coverage.u_core.dout_valid : std_logic >>;
    alias dout_req_ext is << signal .tb_selftest_coverage.u_core.dout_req : std_logic >>;
    alias dout_id_ext is << signal .tb_selftest_coverage.u_core.dout_datalogger_id : std_logic_vector(7 downto 0) >>;
    alias dout_type_ext is << signal .tb_selftest_coverage.u_core.dout_msg_type : std_logic_vector(15 downto 0) >>;
    alias dout_payload_ext is << signal .tb_selftest_coverage.u_core.dout_payload : std_logic_vector(31 downto 0) >>;
  begin
    PROBE_DOUT_VALID         <= dout_valid_ext;
    PROBE_DOUT_REQ           <= dout_req_ext;
    PROBE_DOUT_DATALOGGER_ID <= dout_id_ext;
    PROBE_DOUT_MSG_TYPE      <= dout_type_ext;
    PROBE_DOUT_PAYLOAD       <= dout_payload_ext;
  end block probe_blk;

end architecture str;
