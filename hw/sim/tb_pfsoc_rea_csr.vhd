-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
--
-- tb_pfsoc_rea_csr — DS-P2.128 cocotb harness for the PolarFire CSR plane below
-- the FIC0 bridge: axil_demux4 fanning out to three stand-in window slaves
-- (selectors 0..2, where the fabric has the regbank and the two CAN APB shims)
-- and the REAL pfsoc_rea_door on selector 3. Each stand-in answers a read with
-- a word that names its own selector and the offset it was given, and pulses
-- its hit_o bit once per write it accepts, so a mis-route or a double
-- completion is visible. The REA door is the exact entity the top instantiates.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity tb_pfsoc_rea_csr is
  port (
    clk_i, rst_i : in std_logic;
    s_awaddr_i : in std_logic_vector(17 downto 0);
    s_awvalid_i : in std_logic; s_awready_o : out std_logic;
    s_wdata_i : in std_logic_vector(31 downto 0);
    s_wstrb_i : in std_logic_vector(3 downto 0);
    s_wvalid_i : in std_logic; s_wready_o : out std_logic;
    s_bresp_o : out std_logic_vector(1 downto 0);
    s_bvalid_o : out std_logic; s_bready_i : in std_logic;
    s_araddr_i : in std_logic_vector(17 downto 0);
    s_arvalid_i : in std_logic; s_arready_o : out std_logic;
    s_rdata_o : out std_logic_vector(31 downto 0);
    s_rresp_o : out std_logic_vector(1 downto 0);
    s_rvalid_o : out std_logic; s_rready_i : in std_logic;
    hit_o : out std_logic_vector(2 downto 0);
    probe_o : out std_logic_vector(15 downto 0)
  );
end entity tb_pfsoc_rea_csr;

architecture sim of tb_pfsoc_rea_csr is
  signal m_awaddr, m_araddr : std_logic_vector(63 downto 0);
  signal m_awprot, m_arprot : std_logic_vector(11 downto 0);
  signal m_awvalid, m_awready, m_wvalid, m_wready : std_logic_vector(3 downto 0);
  signal m_bvalid, m_bready, m_arvalid, m_arready, m_rvalid, m_rready : std_logic_vector(3 downto 0);
  signal m_wdata, m_rdata : std_logic_vector(127 downto 0);
  signal m_wstrb : std_logic_vector(15 downto 0);
  signal m_bresp, m_rresp : std_logic_vector(7 downto 0);
begin
  u_demux : entity work.axil_demux4
    port map (clk_i => clk_i, rst_i => rst_i,
      s_awaddr_i => s_awaddr_i, s_awprot_i => "000", s_awvalid_i => s_awvalid_i, s_awready_o => s_awready_o,
      s_wdata_i => s_wdata_i, s_wstrb_i => s_wstrb_i, s_wvalid_i => s_wvalid_i, s_wready_o => s_wready_o,
      s_bresp_o => s_bresp_o, s_bvalid_o => s_bvalid_o, s_bready_i => s_bready_i,
      s_araddr_i => s_araddr_i, s_arprot_i => "000", s_arvalid_i => s_arvalid_i, s_arready_o => s_arready_o,
      s_rdata_o => s_rdata_o, s_rresp_o => s_rresp_o, s_rvalid_o => s_rvalid_o, s_rready_i => s_rready_i,
      m_awaddr_o => m_awaddr, m_awprot_o => m_awprot, m_awvalid_o => m_awvalid, m_awready_i => m_awready,
      m_wdata_o => m_wdata, m_wstrb_o => m_wstrb, m_wvalid_o => m_wvalid, m_wready_i => m_wready,
      m_bresp_i => m_bresp, m_bvalid_i => m_bvalid, m_bready_o => m_bready,
      m_araddr_o => m_araddr, m_arprot_o => m_arprot, m_arvalid_o => m_arvalid, m_arready_i => m_arready,
      m_rdata_i => m_rdata, m_rresp_i => m_rresp, m_rvalid_i => m_rvalid, m_rready_o => m_rready);

  -- Stand-in window slaves 0..2: one outstanding transaction each, OKAY.
  -- Read data = x"5E1" & sel(3:0) & offset(15:0) -> 0x5E1S_OOOO.
  gen_stub : for n in 0 to 2 generate
    signal bvalid_r, rvalid_r, aw_r, w_r : std_logic := '0';
    signal rdata_r : std_logic_vector(31 downto 0) := (others => '0');
  begin
    m_awready(n) <= not aw_r and not bvalid_r;
    m_wready(n)  <= not w_r and not bvalid_r;
    m_bvalid(n)  <= bvalid_r;
    m_bresp(2*n+1 downto 2*n) <= "00";
    m_arready(n) <= not rvalid_r;
    m_rvalid(n)  <= rvalid_r;
    m_rresp(2*n+1 downto 2*n) <= "00";
    m_rdata(32*n+31 downto 32*n) <= rdata_r;
    process(clk_i)
    begin
      if rising_edge(clk_i) then
        hit_o(n) <= '0';
        if rst_i = '1' then
          bvalid_r <= '0'; rvalid_r <= '0'; aw_r <= '0'; w_r <= '0';
        else
          if m_awvalid(n) = '1' and aw_r = '0' and bvalid_r = '0' then aw_r <= '1'; end if;
          if m_wvalid(n) = '1' and w_r = '0' and bvalid_r = '0' then w_r <= '1'; end if;
          if aw_r = '1' and w_r = '1' then
            aw_r <= '0'; w_r <= '0'; bvalid_r <= '1'; hit_o(n) <= '1';
          end if;
          if bvalid_r = '1' and m_bready(n) = '1' then bvalid_r <= '0'; end if;
          if m_arvalid(n) = '1' and rvalid_r = '0' then
            rvalid_r <= '1';
            rdata_r <= x"5E1" & std_logic_vector(to_unsigned(n, 4)) & m_araddr(16*n+15 downto 16*n);
          elsif rvalid_r = '1' and m_rready(n) = '1' then
            rvalid_r <= '0';
          end if;
        end if;
      end if;
    end process;
  end generate;

  u_door : entity work.pfsoc_rea_door
    port map (
      clk_i => clk_i, rst_i => rst_i,
      s_awaddr_i => m_awaddr(63 downto 48), s_awvalid_i => m_awvalid(3), s_awready_o => m_awready(3),
      s_wdata_i => m_wdata(127 downto 96), s_wstrb_i => m_wstrb(15 downto 12), s_wvalid_i => m_wvalid(3), s_wready_o => m_wready(3),
      s_bresp_o => m_bresp(7 downto 6), s_bvalid_o => m_bvalid(3), s_bready_i => m_bready(3),
      s_araddr_i => m_araddr(63 downto 48), s_arvalid_i => m_arvalid(3), s_arready_o => m_arready(3),
      s_rdata_o => m_rdata(127 downto 96), s_rresp_o => m_rresp(7 downto 6), s_rvalid_o => m_rvalid(3), s_rready_i => m_rready(3),
      probe_o => probe_o);
end architecture sim;
