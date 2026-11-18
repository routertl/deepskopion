-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
-------------------------------------------------------------------------------
-- File       : tb_pfsoc_fic0_regbank_read.vhd
-- Description: DS-P2.305 seam: the PolarFire FIC0 CSR read path from the MSS
--              AXI4 port to the regbank, as deepskopion_pfsoc_top wires it:
--                FIC0 AXI4 (64-bit data, 8-bit id, 38-bit address)
--                -> data lane shim (the three assignments of the top, copied)
--                -> lwh2f_axilite_bridge (id 8, addr 38, lite 18)
--                -> axil_demux4 (no generic map, as in the top)
--                -> selector 0: system_regbank_wrapper (ARADDR bits 10:0,
--                   as deepskopion_core maps gp0_araddr)
--              Selectors 1..3 (the CAN APB shims and the REA door on the
--              fabric) are not driven: their ready/valid inputs are '0'.
--              TELEMETRY_IN is tied to zero.
--
--              The shim is a copy, not an instance: the top has it inline.
--              The test that drives this bench checks the copy against the
--              top's text so the two cannot drift silently.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library tich;
use tich.rom_info_pkg.all;
use tich.system_regbank_pkg.all;

entity tb_pfsoc_fic0_regbank_read is
  generic (
    G_REGBANK_HEX_FILE : string := C_REGBANK_HEX;
    G_CONFIG_HEX_FILE  : string := C_CONFIG_HEX
  );
  port (
    clk : in std_logic;
    rst : in std_logic;
    -- FIC0 AXI4 slave view (the MSS is the initiator)
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
    s_axi_rready  : in  std_logic
  );
end entity tb_pfsoc_fic0_regbank_read;

architecture str of tb_pfsoc_fic0_regbank_read is
  -- FIC0 side after the lane shim (names as in deepskopion_pfsoc_top)
  signal fic0_wdata64 : std_logic_vector(63 downto 0);
  signal fic0_wstrb8  : std_logic_vector(7 downto 0);
  signal fic0_wdata   : std_logic_vector(31 downto 0);
  signal fic0_wstrb   : std_logic_vector(3 downto 0);
  signal fic0_rdata   : std_logic_vector(31 downto 0);
  signal fic0_rdata64 : std_logic_vector(63 downto 0);
  -- bridge -> demux
  signal br_awaddr, br_araddr : std_logic_vector(17 downto 0);
  signal br_awprot, br_arprot : std_logic_vector(2 downto 0);
  signal br_awvalid, br_awready, br_wvalid, br_wready, br_bvalid, br_bready : std_logic;
  signal br_arvalid, br_arready, br_rvalid, br_rready : std_logic;
  signal br_wdata, br_rdata : std_logic_vector(31 downto 0);
  signal br_wstrb : std_logic_vector(3 downto 0);
  signal br_bresp, br_rresp : std_logic_vector(1 downto 0);
  -- demux -> windows
  signal m_awaddr, m_araddr : std_logic_vector(63 downto 0);
  signal m_awprot, m_arprot : std_logic_vector(11 downto 0);
  signal m_awvalid, m_awready, m_wvalid, m_wready, m_bvalid, m_bready : std_logic_vector(3 downto 0);
  signal m_arvalid, m_arready, m_rvalid, m_rready : std_logic_vector(3 downto 0);
  signal m_wdata, m_rdata : std_logic_vector(127 downto 0);
  signal m_wstrb : std_logic_vector(15 downto 0);
  signal m_bresp, m_rresp : std_logic_vector(7 downto 0);
  -- regbank
  signal rb_awready, rb_wready, rb_bvalid, rb_arready, rb_rvalid : std_logic;
  signal rb_bresp, rb_rresp : std_logic_vector(1 downto 0);
  signal rb_rdata : std_logic_vector(31 downto 0);
  signal telemetry_w : t_memArrayReg := (others => (data => (others => '0'), valid => '0'));
  signal regs_out_w  : t_memArrayReg;
  signal datalogger_cfg_w : std_logic_vector(15 downto 0);
begin
  fic0_wdata64 <= s_axi_wdata;
  fic0_wstrb8  <= s_axi_wstrb;
  s_axi_rdata  <= fic0_rdata64;

  -- Lane shim: copied from deepskopion_pfsoc_top.vhd (DS-P2.152).
  fic0_wdata <= fic0_wdata64(63 downto 32) when fic0_wstrb8(7 downto 4) /= "0000"
                else fic0_wdata64(31 downto 0);
  fic0_wstrb <= fic0_wstrb8(7 downto 4) or fic0_wstrb8(3 downto 0);
  fic0_rdata64 <= fic0_rdata & fic0_rdata;

  u_fic0_bridge : entity work.lwh2f_axilite_bridge
    generic map (id_width_g => 8, addr_width_g => 38, lite_addr_g => 18)
    port map (
      clk_i => clk, rst_i => rst,
      s_awid => s_axi_awid, s_awaddr => s_axi_awaddr, s_awprot => s_axi_awprot,
      s_awvalid => s_axi_awvalid, s_awready => s_axi_awready,
      s_wdata => fic0_wdata, s_wstrb => fic0_wstrb, s_wvalid => s_axi_wvalid, s_wready => s_axi_wready,
      s_bid => s_axi_bid, s_bresp => s_axi_bresp, s_bvalid => s_axi_bvalid, s_bready => s_axi_bready,
      s_arid => s_axi_arid, s_araddr => s_axi_araddr, s_arprot => s_axi_arprot,
      s_arvalid => s_axi_arvalid, s_arready => s_axi_arready,
      s_rid => s_axi_rid, s_rdata => fic0_rdata, s_rresp => s_axi_rresp, s_rlast => s_axi_rlast,
      s_rvalid => s_axi_rvalid, s_rready => s_axi_rready,
      m_awaddr => br_awaddr, m_awprot => br_awprot, m_awvalid => br_awvalid, m_awready => br_awready,
      m_wdata => br_wdata, m_wstrb => br_wstrb, m_wvalid => br_wvalid, m_wready => br_wready,
      m_bresp => br_bresp, m_bvalid => br_bvalid, m_bready => br_bready,
      m_araddr => br_araddr, m_arprot => br_arprot, m_arvalid => br_arvalid, m_arready => br_arready,
      m_rdata => br_rdata, m_rresp => br_rresp, m_rvalid => br_rvalid, m_rready => br_rready);

  u_demux : entity work.axil_demux4
    port map (clk_i => clk, rst_i => rst,
      s_awaddr_i => br_awaddr, s_awprot_i => br_awprot, s_awvalid_i => br_awvalid, s_awready_o => br_awready,
      s_wdata_i => br_wdata, s_wstrb_i => br_wstrb, s_wvalid_i => br_wvalid, s_wready_o => br_wready,
      s_bresp_o => br_bresp, s_bvalid_o => br_bvalid, s_bready_i => br_bready,
      s_araddr_i => br_araddr, s_arprot_i => br_arprot, s_arvalid_i => br_arvalid, s_arready_o => br_arready,
      s_rdata_o => br_rdata, s_rresp_o => br_rresp, s_rvalid_o => br_rvalid, s_rready_i => br_rready,
      m_awaddr_o => m_awaddr, m_awprot_o => m_awprot, m_awvalid_o => m_awvalid, m_awready_i => m_awready,
      m_wdata_o => m_wdata, m_wstrb_o => m_wstrb, m_wvalid_o => m_wvalid, m_wready_i => m_wready,
      m_bresp_i => m_bresp, m_bvalid_i => m_bvalid, m_bready_o => m_bready,
      m_araddr_o => m_araddr, m_arprot_o => m_arprot, m_arvalid_o => m_arvalid, m_arready_i => m_arready,
      m_rdata_i => m_rdata, m_rresp_i => m_rresp, m_rvalid_i => m_rvalid, m_rready_o => m_rready);

  -- Selector 0: the regbank. Selectors 1..3 are not driven.
  m_awready <= "000" & rb_awready;
  m_wready  <= "000" & rb_wready;
  m_bvalid  <= "000" & rb_bvalid;
  m_bresp   <= "000000" & rb_bresp;
  m_arready <= "000" & rb_arready;
  m_rvalid  <= "000" & rb_rvalid;
  m_rresp   <= "000000" & rb_rresp;
  m_rdata   <= (127 downto 32 => '0') & rb_rdata;

  u_wrapper : entity tich.system_regbank_wrapper
    generic map (
      G_REGBANK_HEX_FILE => G_REGBANK_HEX_FILE,
      G_CONFIG_HEX_FILE  => G_CONFIG_HEX_FILE
    )
    port map (
      CLK => clk, RST => rst,
      S_AXI_AWADDR => m_awaddr(10 downto 0), S_AXI_AWPROT => m_awprot(2 downto 0),
      S_AXI_AWVALID => m_awvalid(0), S_AXI_AWREADY => rb_awready,
      S_AXI_WDATA => m_wdata(31 downto 0), S_AXI_WSTRB => m_wstrb(3 downto 0),
      S_AXI_WVALID => m_wvalid(0), S_AXI_WREADY => rb_wready,
      S_AXI_BRESP => rb_bresp, S_AXI_BVALID => rb_bvalid, S_AXI_BREADY => m_bready(0),
      S_AXI_ARADDR => m_araddr(10 downto 0), S_AXI_ARPROT => m_arprot(2 downto 0),
      S_AXI_ARVALID => m_arvalid(0), S_AXI_ARREADY => rb_arready,
      S_AXI_RDATA => rb_rdata, S_AXI_RRESP => rb_rresp, S_AXI_RVALID => rb_rvalid,
      S_AXI_RREADY => m_rready(0),
      DATALOGGER_CONFIG => datalogger_cfg_w,
      TELEMETRY_IN => telemetry_w,
      REGS_OUT => regs_out_w
    );
end architecture str;
