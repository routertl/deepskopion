-- rr unknown-llm (AI-generated)
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
--
-- pfsoc_rea_door — the PolarFire REA MMIO door (DS-P2.128). One AXI4-Lite
-- slave (16-bit window, axil_demux4 selector 3 = FIC0 base + 0x3_0000) onto an
-- upstream rr_rea_top elaborated with G_REG_IFACE = "external", through the
-- package's own rr_rea_axi4lite bridge. The probe is the DS-P2.132 known-answer
-- word on clk_i:
--   [7:0] counter[7:0] | [11:8] 0xA | [12] heartbeat | [15:13] "101"
-- so a capture read over /dev/mem is checkable bit-exactly against its
-- construction (consecutive samples differ by +1 in [7:0], [15:8] fixed but for
-- the slow heartbeat). Factored out of deepskopion_pfsoc_top so the cocotb
-- harness (hw/sim/tb_pfsoc_rea_csr.vhd) exercises the SAME RTL the fabric uses.

library ieee;
use ieee.std_logic_1164.all;
library rr_rea;

entity pfsoc_rea_door is
  port (
    clk_i, rst_i : in std_logic;
    s_awaddr_i  : in  std_logic_vector(15 downto 0);
    s_awvalid_i : in  std_logic; s_awready_o : out std_logic;
    s_wdata_i   : in  std_logic_vector(31 downto 0);
    s_wstrb_i   : in  std_logic_vector(3 downto 0);
    s_wvalid_i  : in  std_logic; s_wready_o : out std_logic;
    s_bresp_o   : out std_logic_vector(1 downto 0);
    s_bvalid_o  : out std_logic; s_bready_i : in std_logic;
    s_araddr_i  : in  std_logic_vector(15 downto 0);
    s_arvalid_i : in  std_logic; s_arready_o : out std_logic;
    s_rdata_o   : out std_logic_vector(31 downto 0);
    s_rresp_o   : out std_logic_vector(1 downto 0);
    s_rvalid_o  : out std_logic; s_rready_i : in std_logic;
    -- The sampled word, exported so a testbench can compare against it.
    probe_o     : out std_logic_vector(15 downto 0)
  );
end entity pfsoc_rea_door;

architecture rtl of pfsoc_rea_door is
  signal aresetn : std_logic;
  signal ka_probe : std_logic_vector(15 downto 0);
  signal reg_wr_en, reg_rd_en : std_logic;
  signal reg_addr : std_logic_vector(15 downto 0);
  signal reg_wdata, reg_rdata : std_logic_vector(31 downto 0);
begin
  aresetn <= not rst_i;
  probe_o <= ka_probe;

  u_axil : entity rr_rea.rr_rea_axi4lite
    generic map (G_ADDR_W => 16)
    port map (
      aclk_i => clk_i, aresetn_i => aresetn,
      awaddr_i => s_awaddr_i, awvalid_i => s_awvalid_i, awready_o => s_awready_o,
      wdata_i => s_wdata_i, wstrb_i => s_wstrb_i, wvalid_i => s_wvalid_i, wready_o => s_wready_o,
      bresp_o => s_bresp_o, bvalid_o => s_bvalid_o, bready_i => s_bready_i,
      araddr_i => s_araddr_i, arvalid_i => s_arvalid_i, arready_o => s_arready_o,
      rdata_o => s_rdata_o, rresp_o => s_rresp_o, rvalid_o => s_rvalid_o, rready_i => s_rready_i,
      reg_wr_en_o => reg_wr_en, reg_rd_en_o => reg_rd_en,
      reg_addr_o => reg_addr, reg_wdata_o => reg_wdata, reg_rdata_i => reg_rdata);

  u_rea : entity rr_rea.rr_rea_top
    generic map (G_SAMPLE_W => 16, G_DEPTH => 4096, G_TIMESTAMP_W => 32,
                 G_NUM_CHAN => 1, G_REG_IFACE => "external")
    port map (
      sample_clk_i => clk_i, sample_rst_i => rst_i,
      probe_i => ka_probe, source_o => open, trigger_o => open,
      -- JTAG door unused: under G_REG_IFACE external it is not instantiated.
      arst_i => '0', tck_i => '0', tdi_i => '0', tdo_o => open,
      capture_i => '0', shift_en_i => '0', update_i => '0', sel_i => '0',
      reg_clk_i => clk_i, reg_rst_i => rst_i,
      reg_wr_en_i => reg_wr_en, reg_rd_en_i => reg_rd_en,
      reg_addr_i => reg_addr, reg_wdata_i => reg_wdata, reg_rdata_o => reg_rdata,
      m_axis_tdata_o => open, m_axis_tvalid_o => open, m_axis_tlast_o => open);

  u_ka : entity work.pf_known_answer_core
    generic map (G_KA => 90, G_BLINK_BIT => 24)
    port map (ref_clk_50mhz => clk_i, led => open, probe_o => ka_probe);
end architecture rtl;
