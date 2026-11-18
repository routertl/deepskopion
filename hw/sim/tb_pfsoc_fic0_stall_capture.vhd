-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- DS-P2.326: testbench for the PolarFire FIC0 read-path capture logic.
--
-- tb_pfsoc_fic0_regbank_read (the FIC0 lane shim, lwh2f_axilite_bridge, axil_demux4 and the regbank,
-- wired as deepskopion_pfsoc_fabric wires them) is instantiated unchanged and driven on its FIC0 AXI4
-- slave ports. The FIC0 read channel at those ports is the bridge input the fabric probes (the lane
-- shim touches only data lanes), packed into fic0_rd_probe with the fabric's own expression
-- (test_fic0_stall_capture_ds_p2_326 compares the two texts). fic0_stall_trigger and fic0_rea_sampler,
-- the units the bench top deepskopion_pfsoc_top_fic0_rea instantiates, run on that probe.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library tich;
use tich.rom_info_pkg.all;

entity tb_pfsoc_fic0_stall_capture is
  generic (
    G_REGBANK_HEX_FILE : string   := C_REGBANK_HEX;
    G_CONFIG_HEX_FILE  : string   := C_CONFIG_HEX;
    G_STALL_CYCLES     : positive := 64
  );
  port (
    clk : in std_logic;
    rst : in std_logic;
    s_axi_awid    : in  std_logic_vector(7 downto 0);
    s_axi_awaddr  : in  std_logic_vector(37 downto 0);
    s_axi_awprot  : in  std_logic_vector(2 downto 0);
    s_axi_awlen   : in  std_logic_vector(7 downto 0);
    s_axi_awsize  : in  std_logic_vector(2 downto 0);
    s_axi_awburst : in  std_logic_vector(1 downto 0);
    s_axi_awcache : in  std_logic_vector(3 downto 0);
    s_axi_awqos   : in  std_logic_vector(3 downto 0);
    s_axi_awvalid : in  std_logic;
    s_axi_awready : out std_logic;
    s_axi_wdata   : in  std_logic_vector(63 downto 0);
    s_axi_wstrb   : in  std_logic_vector(7 downto 0);
    s_axi_wlast   : in  std_logic;
    s_axi_wvalid  : in  std_logic;
    s_axi_wready  : out std_logic;
    s_axi_bid     : out std_logic_vector(7 downto 0);
    s_axi_bresp   : out std_logic_vector(1 downto 0);
    s_axi_bvalid  : out std_logic;
    s_axi_bready  : in  std_logic;
    s_axi_arid    : in  std_logic_vector(7 downto 0);
    s_axi_araddr  : in  std_logic_vector(37 downto 0);
    s_axi_arprot  : in  std_logic_vector(2 downto 0);
    s_axi_arlen   : in  std_logic_vector(7 downto 0);
    s_axi_arsize  : in  std_logic_vector(2 downto 0);
    s_axi_arburst : in  std_logic_vector(1 downto 0);
    s_axi_arcache : in  std_logic_vector(3 downto 0);
    s_axi_arqos   : in  std_logic_vector(3 downto 0);
    s_axi_arvalid : in  std_logic;
    s_axi_arready : out std_logic;
    s_axi_rid     : out std_logic_vector(7 downto 0);
    s_axi_rdata   : out std_logic_vector(63 downto 0);
    s_axi_rresp   : out std_logic_vector(1 downto 0);
    s_axi_rlast   : out std_logic;
    s_axi_rvalid  : out std_logic;
    s_axi_rready  : in  std_logic;
    -- capture outputs
    sample_o      : out std_logic_vector(36 downto 0);
    trig_o        : out std_logic
  );
end entity tb_pfsoc_fic0_stall_capture;

architecture sim of tb_pfsoc_fic0_stall_capture is
  -- the bus as the fabric's bridge input sees it
  signal fic0_arid, fic0_rid       : std_logic_vector(7 downto 0);
  signal fic0_araddr               : std_logic_vector(37 downto 0);
  signal fic0_arvalid, fic0_arready, fic0_rvalid, fic0_rready, fic0_rlast : std_logic;
  signal fic0_rresp                : std_logic_vector(1 downto 0);
  signal fic0_rd_probe_o           : std_logic_vector(31 downto 0);
  signal stall, outstanding, trig, heartbeat : std_logic;
begin

  u_bus : entity work.tb_pfsoc_fic0_regbank_read
    generic map (G_REGBANK_HEX_FILE => G_REGBANK_HEX_FILE, G_CONFIG_HEX_FILE => G_CONFIG_HEX_FILE)
    port map (
      clk => clk, rst => rst,
      s_axi_awid => s_axi_awid, s_axi_awaddr => s_axi_awaddr, s_axi_awprot => s_axi_awprot,
      s_axi_awlen => s_axi_awlen, s_axi_awsize => s_axi_awsize, s_axi_awburst => s_axi_awburst,
      s_axi_awcache => s_axi_awcache, s_axi_awqos => s_axi_awqos, s_axi_awvalid => s_axi_awvalid,
      s_axi_awready => s_axi_awready, s_axi_wdata => s_axi_wdata, s_axi_wstrb => s_axi_wstrb,
      s_axi_wlast => s_axi_wlast, s_axi_wvalid => s_axi_wvalid, s_axi_wready => s_axi_wready,
      s_axi_bid => s_axi_bid, s_axi_bresp => s_axi_bresp, s_axi_bvalid => s_axi_bvalid,
      s_axi_bready => s_axi_bready,
      s_axi_arid => s_axi_arid, s_axi_araddr => s_axi_araddr, s_axi_arprot => s_axi_arprot,
      s_axi_arlen => s_axi_arlen, s_axi_arsize => s_axi_arsize, s_axi_arburst => s_axi_arburst,
      s_axi_arcache => s_axi_arcache, s_axi_arqos => s_axi_arqos, s_axi_arvalid => s_axi_arvalid,
      s_axi_arready => fic0_arready,
      s_axi_rid => fic0_rid, s_axi_rdata => s_axi_rdata, s_axi_rresp => fic0_rresp,
      s_axi_rlast => fic0_rlast, s_axi_rvalid => fic0_rvalid, s_axi_rready => s_axi_rready);

  fic0_arid    <= s_axi_arid;
  fic0_araddr  <= s_axi_araddr;
  fic0_arvalid <= s_axi_arvalid;
  fic0_rready  <= s_axi_rready;
  s_axi_arready <= fic0_arready;
  s_axi_rid     <= fic0_rid;
  s_axi_rresp   <= fic0_rresp;
  s_axi_rlast   <= fic0_rlast;
  s_axi_rvalid  <= fic0_rvalid;

  -- DS-P2.326: the FIC0 read channel at the bridge input, for a bench analyzer.
  fic0_rd_probe_o <= '0' & fic0_rlast & fic0_rid & fic0_arid & fic0_araddr(7 downto 0)
                     & fic0_rresp & fic0_rready & fic0_rvalid & fic0_arready & fic0_arvalid;

  u_trig : entity work.fic0_stall_trigger
    generic map (G_STALL_CYCLES => G_STALL_CYCLES, G_HEARTBEAT_BIT => 9)
    port map (
      clk_i => clk, rst_i => rst,
      arvalid_i => fic0_rd_probe_o(0), arready_i => fic0_rd_probe_o(1),
      rvalid_i => fic0_rd_probe_o(2), rready_i => fic0_rd_probe_o(3),
      stall_o => stall, outstanding_o => outstanding, trig_o => trig, heartbeat_o => heartbeat);

  u_sampler : entity work.fic0_rea_sampler
    port map (
      clk_i => clk, probe_i => fic0_rd_probe_o, stall_i => stall, outstanding_i => outstanding,
      trig_i => trig, heartbeat_i => heartbeat, sample_o => sample_o);

  trig_o <= trig;

end architecture sim;
