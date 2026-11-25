-- rr unknown-llm (AI-generated)
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
--
-- DS-T1.15 falsifiable guard for the axil_demux3 AXI completion watchdog.
--
-- MECHANISM THIS GUARDS (observed on DE25 silicon 2026-09-06, bare-metal U-Boot
-- md/mw + Linux devmem): a CTU CAN-FD aperture READ issued right after the
-- driver's MODE.RST soft-reset write never returns its AXI RRESP, so the demux's
-- RD_RESP state waits forever for the selected slave's m_rvalid, s_rvalid never
-- rises, and the HPS lwh2f master (hence the whole CPU, single-threaded in
-- U-Boot / the ctucanfd probe in Linux) hangs until a cold power cycle.
--
-- WHY THE STIMULUS IS PRODUCIBLE HERE (unlike the retired shim-side guard): the
-- demux's m_rvalid_i / m_bvalid_i are REAL AXI response inputs from the selected
-- downstream slave. A wedged slave holding them low is exactly the integrated,
-- shipped-topology condition — not the unreachable "hold the CTU apb pready low"
-- surface (apb_ifc.vhd:156 ties pready<='1', so no shipped shim ever sees a low
-- pready). This TB holds m_rvalid_i(sel)/m_bvalid_i(sel) low forever and asserts
-- the demux FORCE-COMPLETES the transaction (s_rvalid/s_bvalid) with an OKAY
-- response rather than deadlocking the master. A TB cycle-budget catches a wedge.
--
-- BASELINE (wd_cycles_g = 0, the default = pre-fix demux): s_rvalid never
--   asserts -> the budget trips -> "DE25_DEMUX_WEDGE: read never completed". RED.
-- MUTATION (delete the RD_FORCE/WR_FORCE branches): reverts to BASELINE -> RED.
-- WITH FIX (wd_cycles_g /= 0): force-completes -> DE25_DEMUX_WEDGE_GUARD_PASS.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.env.all;

entity tb_axil_demux3_wedge_guard is end entity;
architecture test of tb_axil_demux3_wedge_guard is
  constant WD    : natural := 8;           -- small watchdog bound for a fast test
  constant SEL   : natural := 1;           -- CTU-A aperture (offset 0x10000)
  signal clk, rst : std_logic := '0';
  signal awaddr, araddr : std_logic_vector(17 downto 0) := (others => '0');
  signal awprot, arprot : std_logic_vector(2 downto 0) := (others => '0');
  signal awvalid, awready, wvalid, wready, bvalid, bready : std_logic := '0';
  signal arvalid, arready, rvalid, rready : std_logic := '0';
  signal wdata, rdata : std_logic_vector(31 downto 0) := (others => '0');
  signal wstrb : std_logic_vector(3 downto 0) := "1111";
  signal bresp, rresp : std_logic_vector(1 downto 0);
  signal mawaddr, maraddr : std_logic_vector(47 downto 0);
  signal mawprot, marprot : std_logic_vector(8 downto 0);
  signal mawvalid, mawready, mwvalid, mwready, mbvalid, mbready : std_logic_vector(2 downto 0) := (others => '0');
  signal mwdata, mrdata : std_logic_vector(95 downto 0) := (others => '0');
  signal mwstrb : std_logic_vector(11 downto 0);
  signal mbresp, mrresp : std_logic_vector(5 downto 0) := (others => '0');
  signal marvalid, marready, mrvalid, mrready : std_logic_vector(2 downto 0) := (others => '0');
begin
  clk <= not clk after 5 ns;
  -- m_rvalid_i / m_bvalid_i are NEVER driven high for the selected slave: the
  -- downstream CTU aperture is wedged (the silicon read-after-MODE.RST condition).
  dut: entity work.axil_demux3
    generic map (wd_cycles_g => WD)
    port map (
      clk, rst, awaddr, awprot, awvalid, awready, wdata, wstrb, wvalid, wready,
      bresp, bvalid, bready, araddr, arprot, arvalid, arready, rdata, rresp,
      rvalid, rready, mawaddr, mawprot, mawvalid, mawready, mwdata, mwstrb,
      mwvalid, mwready, mbresp, mbvalid, mbready, maraddr, marprot, marvalid,
      marready, mrdata, mrresp, mrvalid, mrready);

  stimulus: process
    variable budget : natural;
  begin
    rst <= '1'; wait until rising_edge(clk); wait until rising_edge(clk); rst <= '0';
    wait until rising_edge(clk);

    -- 1) READ that wedges (models read-after-MODE.RST). Accept AR, then hold the
    --    slave's m_rvalid(SEL) low forever.
    araddr <= (others => '0'); araddr(17 downto 16) <= std_logic_vector(to_unsigned(SEL, 2));
    arvalid <= '1'; rready <= '1';
    wait until rising_edge(clk); arvalid <= '0';
    wait until marvalid(SEL) = '1';         -- demux forwarded AR to the slave
    marready(SEL) <= '1'; wait until rising_edge(clk); marready(SEL) <= '0';
    -- mrvalid(SEL) stays '0': the wedge. The watchdog must force-complete.
    budget := 4 * (WD + 8);                 -- generous; well past latch+setup+bound
    while rvalid = '0' and budget > 0 loop
      wait until rising_edge(clk); budget := budget - 1;
    end loop;
    assert rvalid = '1'
      report "DE25_DEMUX_WEDGE: read never completed (demux deadlocked on stuck m_rvalid)"
      severity failure;
    assert rresp = "00"
      report "forced read completion must be OKAY (a DECERR/SLVERR would abort the kernel)"
      severity failure;
    assert rdata = x"FFFFFFFF"
      report "forced read data must be all-ones (a non-CTU-signature poll value)"
      severity failure;
    wait until rising_edge(clk); rready <= '0';
    wait until rising_edge(clk);

    -- 2) WRITE that wedges: hold m_bvalid(SEL) low forever; must still return B (OKAY).
    awaddr <= (others => '0'); awaddr(17 downto 16) <= std_logic_vector(to_unsigned(SEL, 2));
    wdata <= x"00000001"; awvalid <= '1'; wvalid <= '1'; bready <= '1';
    wait until rising_edge(clk); awvalid <= '0'; wvalid <= '0';
    wait until mawvalid(SEL) = '1' and mwvalid(SEL) = '1';
    mawready(SEL) <= '1'; mwready(SEL) <= '1';
    wait until rising_edge(clk); mawready(SEL) <= '0'; mwready(SEL) <= '0';
    -- mbvalid(SEL) stays '0': the wedge. The watchdog must force-complete.
    budget := 4 * (WD + 8);
    while bvalid = '0' and budget > 0 loop
      wait until rising_edge(clk); budget := budget - 1;
    end loop;
    assert bvalid = '1'
      report "DE25_DEMUX_WEDGE: write never completed (demux deadlocked on stuck m_bvalid)"
      severity failure;
    assert bresp = "00"
      report "forced write completion must be OKAY" severity failure;
    wait until rising_edge(clk); bready <= '0';

    report "DE25_DEMUX_WEDGE_GUARD_PASS" severity note;
    stop;
  end process;
end architecture;
