-- rr unknown-llm (AI-generated)
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- axil_demux4 — PolarFire CSR decoder (DS-P2.128). Derived from
-- hw/rtl/de25/axil_demux3.vhd (same single-outstanding FSM, same DS-T1.15
-- completion watchdog) with a FOURTH slave port: CPU offsets 0x00000, 0x10000,
-- 0x20000, 0x30000 select regbank, CTU-A, CTU-B and the REA register door.
-- axil_demux3 stays untouched: the DE25 image tooling reads its DECERR selector
-- literal out of the RTL text (tools/de25_linux_image.py), and selector 3 is
-- DECERR there by design. Here all four selectors decode, so the DECERR states
-- and responses are removed; everything else is axil_demux3's FSM unchanged.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity axil_demux4 is
  generic (
    -- DS-T1.15: AXI response watchdog bound in clk cycles. 0 = disabled (default;
    -- keeps the DS-T1.7 regression and all other instances byte-identical). A
    -- normal CTU aperture access completes in a handful of cycles, so any non-zero
    -- bound far above that (de25_top uses 4096) never trips in normal operation.
    wd_cycles_g : natural := 0
  );
  port (
    clk_i, rst_i : in std_logic;
    s_awaddr_i : in std_logic_vector(17 downto 0);
    s_awprot_i : in std_logic_vector(2 downto 0);
    s_awvalid_i : in std_logic; s_awready_o : out std_logic;
    s_wdata_i : in std_logic_vector(31 downto 0);
    s_wstrb_i : in std_logic_vector(3 downto 0);
    s_wvalid_i : in std_logic; s_wready_o : out std_logic;
    s_bresp_o : out std_logic_vector(1 downto 0);
    s_bvalid_o : out std_logic; s_bready_i : in std_logic;
    s_araddr_i : in std_logic_vector(17 downto 0);
    s_arprot_i : in std_logic_vector(2 downto 0);
    s_arvalid_i : in std_logic; s_arready_o : out std_logic;
    s_rdata_o : out std_logic_vector(31 downto 0);
    s_rresp_o : out std_logic_vector(1 downto 0);
    s_rvalid_o : out std_logic; s_rready_i : in std_logic;
    m_awaddr_o : out std_logic_vector(63 downto 0);
    m_awprot_o : out std_logic_vector(11 downto 0);
    m_awvalid_o : out std_logic_vector(3 downto 0);
    m_awready_i : in std_logic_vector(3 downto 0);
    m_wdata_o : out std_logic_vector(127 downto 0);
    m_wstrb_o : out std_logic_vector(15 downto 0);
    m_wvalid_o : out std_logic_vector(3 downto 0);
    m_wready_i : in std_logic_vector(3 downto 0);
    m_bresp_i : in std_logic_vector(7 downto 0);
    m_bvalid_i : in std_logic_vector(3 downto 0);
    m_bready_o : out std_logic_vector(3 downto 0);
    m_araddr_o : out std_logic_vector(63 downto 0);
    m_arprot_o : out std_logic_vector(11 downto 0);
    m_arvalid_o : out std_logic_vector(3 downto 0);
    m_arready_i : in std_logic_vector(3 downto 0);
    m_rdata_i : in std_logic_vector(127 downto 0);
    m_rresp_i : in std_logic_vector(7 downto 0);
    m_rvalid_i : in std_logic_vector(3 downto 0);
    m_rready_o : out std_logic_vector(3 downto 0)
  );
end entity;

architecture rtl of axil_demux4 is
  -- DS-T1.15: WR_FORCE / RD_FORCE are the watchdog force-complete states.
  type wr_state_t is (WR_COLLECT, WR_SEND, WR_RESP, WR_FORCE);
  type rd_state_t is (RD_IDLE, RD_SEND, RD_RESP, RD_FORCE);
  signal wr_state_r : wr_state_t := WR_COLLECT;
  signal rd_state_r : rd_state_t := RD_IDLE;
  signal aw_hold_r, w_hold_r, aw_sent_r, w_sent_r : std_logic := '0';
  signal awaddr_r, araddr_r : std_logic_vector(17 downto 0) := (others => '0');
  signal awprot_r, arprot_r : std_logic_vector(2 downto 0) := (others => '0');
  signal wdata_r : std_logic_vector(31 downto 0) := (others => '0');
  signal wstrb_r : std_logic_vector(3 downto 0) := (others => '0');
  signal wr_sel_r, rd_sel_r : integer range 0 to 3 := 0;
  -- DS-T1.15 watchdog cycle counters (only advance while waiting for a response).
  signal wr_wd_r, rd_wd_r : natural range 0 to wd_cycles_g := 0;
begin
  gen_bus : for n in 0 to 3 generate
    m_awaddr_o(16*n+15 downto 16*n) <= awaddr_r(15 downto 0);
    m_awprot_o(3*n+2 downto 3*n) <= awprot_r;
    m_wdata_o(32*n+31 downto 32*n) <= wdata_r;
    m_wstrb_o(4*n+3 downto 4*n) <= wstrb_r;
    m_araddr_o(16*n+15 downto 16*n) <= araddr_r(15 downto 0);
    m_arprot_o(3*n+2 downto 3*n) <= arprot_r;
  end generate;

  s_awready_o <= '1' when wr_state_r = WR_COLLECT and aw_hold_r = '0' else '0';
  s_wready_o <= '1' when wr_state_r = WR_COLLECT and w_hold_r = '0' else '0';
  -- DS-T1.15: WR_FORCE force-completes with OKAY; the ungated-BVALID property the
  -- DS-T1.7 gate proves is untouched (WR_RESP still tracks m_bvalid_i(sel)).
  s_bvalid_o <= '1' when wr_state_r = WR_FORCE else
                m_bvalid_i(wr_sel_r) when wr_state_r = WR_RESP and wr_sel_r < 4 else '0';
  s_bresp_o <= "00" when wr_state_r = WR_FORCE else
               m_bresp_i(2*wr_sel_r+1 downto 2*wr_sel_r) when wr_sel_r < 4 else "11";
  s_arready_o <= '1' when rd_state_r = RD_IDLE else '0';
  s_rvalid_o <= '1' when rd_state_r = RD_FORCE else
                m_rvalid_i(rd_sel_r) when rd_state_r = RD_RESP and rd_sel_r < 4 else '0';
  s_rdata_o <= (others => '1') when rd_state_r = RD_FORCE else
               m_rdata_i(32*rd_sel_r+31 downto 32*rd_sel_r) when rd_sel_r < 4 else (others => '0');
  s_rresp_o <= "00" when rd_state_r = RD_FORCE else
               m_rresp_i(2*rd_sel_r+1 downto 2*rd_sel_r) when rd_sel_r < 4 else "11";

  process(all)
  begin
    m_awvalid_o <= (others => '0'); m_wvalid_o <= (others => '0');
    m_bready_o <= (others => '0'); m_arvalid_o <= (others => '0');
    m_rready_o <= (others => '0');
    if wr_sel_r < 4 then
      if wr_state_r = WR_SEND then
        if aw_sent_r = '0' then m_awvalid_o(wr_sel_r) <= '1'; end if;
        if w_sent_r = '0' then m_wvalid_o(wr_sel_r) <= '1'; end if;
      elsif wr_state_r = WR_RESP then m_bready_o(wr_sel_r) <= s_bready_i;
      end if;
    end if;
    if rd_sel_r < 4 then
      if rd_state_r = RD_SEND then m_arvalid_o(rd_sel_r) <= '1';
      elsif rd_state_r = RD_RESP then m_rready_o(rd_sel_r) <= s_rready_i;
      end if;
    end if;
  end process;

  process(clk_i)
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        wr_state_r <= WR_COLLECT; rd_state_r <= RD_IDLE;
        aw_hold_r <= '0'; w_hold_r <= '0'; aw_sent_r <= '0'; w_sent_r <= '0';
        wr_wd_r <= 0; rd_wd_r <= 0;
      else
        case wr_state_r is
          when WR_COLLECT =>
            if s_awvalid_i = '1' and aw_hold_r = '0' then
              awaddr_r <= s_awaddr_i; awprot_r <= s_awprot_i; aw_hold_r <= '1';
            end if;
            if s_wvalid_i = '1' and w_hold_r = '0' then
              wdata_r <= s_wdata_i; wstrb_r <= s_wstrb_i; w_hold_r <= '1';
            end if;
            if aw_hold_r = '1' and w_hold_r = '1' then
              wr_sel_r <= to_integer(unsigned(awaddr_r(17 downto 16)));
              aw_hold_r <= '0'; w_hold_r <= '0'; aw_sent_r <= '0'; w_sent_r <= '0';
              wr_state_r <= WR_SEND;  -- all four selectors decode (no DECERR window)
            end if;
          when WR_SEND =>
            if aw_sent_r = '0' and m_awready_i(wr_sel_r) = '1' then aw_sent_r <= '1'; end if;
            if w_sent_r = '0' and m_wready_i(wr_sel_r) = '1' then w_sent_r <= '1'; end if;
            if (aw_sent_r = '1' or m_awready_i(wr_sel_r) = '1') and
               (w_sent_r = '1' or m_wready_i(wr_sel_r) = '1') then
              wr_state_r <= WR_RESP; wr_wd_r <= 0;
            end if;
          when WR_RESP =>
            if m_bvalid_i(wr_sel_r) = '1' and s_bready_i = '1' then wr_state_r <= WR_COLLECT;
            elsif wd_cycles_g /= 0 and wr_wd_r = wd_cycles_g then wr_state_r <= WR_FORCE;
            elsif wr_wd_r < wd_cycles_g then wr_wd_r <= wr_wd_r + 1;
            end if;
          when WR_FORCE => if s_bready_i = '1' then wr_state_r <= WR_COLLECT; end if;
        end case;
        case rd_state_r is
          when RD_IDLE =>
            if s_arvalid_i = '1' then
              araddr_r <= s_araddr_i; arprot_r <= s_arprot_i;
              rd_sel_r <= to_integer(unsigned(s_araddr_i(17 downto 16)));
              rd_state_r <= RD_SEND;  -- all four selectors decode (no DECERR window)
            end if;
          when RD_SEND =>
            if m_arready_i(rd_sel_r) = '1' then rd_state_r <= RD_RESP; rd_wd_r <= 0; end if;
          when RD_RESP =>
            if m_rvalid_i(rd_sel_r) = '1' and s_rready_i = '1' then rd_state_r <= RD_IDLE;
            elsif wd_cycles_g /= 0 and rd_wd_r = wd_cycles_g then rd_state_r <= RD_FORCE;
            elsif rd_wd_r < wd_cycles_g then rd_wd_r <= rd_wd_r + 1;
            end if;
          when RD_FORCE => if s_rready_i = '1' then rd_state_r <= RD_IDLE; end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
