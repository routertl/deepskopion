-- rr unknown-llm (AI-generated)
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- DS-P2.326: stall trigger for an on-chip analyzer on the PolarFire FIC0 read channel.
--
-- Watches one AXI read channel (single-beat reads: one R handshake per AR) and asserts
-- trig_o once a read has been stalled for G_STALL_CYCLES consecutive clocks. A read is
-- stalled while either
--   * an AR is offered and not accepted (ARVALID and not ARREADY), or
--   * an AR has been accepted and its R handshake has not yet happened.
-- trig_o is sticky until rst_i: the analyzer arms once and the trigger stays set however
-- long the bus stays stuck. stall_o shows the stall condition itself, and
-- outstanding_o whether an accepted read is still waiting for its R, so a capture tells
-- the two stall kinds apart. heartbeat_o is bit G_HEARTBEAT_BIT of a free-running
-- counter: a sample in which it still toggles proves clk_i was running.
--
-- Bench-only: instantiated by deepskopion_pfsoc_top_fic0_rea, never by the product top.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity fic0_stall_trigger is
  generic (
    G_STALL_CYCLES  : positive := 1_000_000;  -- 10 ms at 100 MHz
    G_HEARTBEAT_BIT : natural  := 9
  );
  port (
    clk_i         : in  std_logic;
    rst_i         : in  std_logic;
    arvalid_i     : in  std_logic;
    arready_i     : in  std_logic;
    rvalid_i      : in  std_logic;
    rready_i      : in  std_logic;
    stall_o       : out std_logic;
    outstanding_o : out std_logic;
    trig_o        : out std_logic;
    heartbeat_o   : out std_logic
  );
end entity fic0_stall_trigger;

architecture rtl of fic0_stall_trigger is
  signal outstanding_r : std_logic := '0';
  signal count_r       : natural range 0 to G_STALL_CYCLES := 0;
  signal trig_r        : std_logic := '0';
  signal hb_r          : unsigned(G_HEARTBEAT_BIT downto 0) := (others => '0');
  signal stall         : std_logic;
begin

  stall <= (arvalid_i and not arready_i) or outstanding_r;

  p_track : process (clk_i)
  begin
    if rising_edge(clk_i) then
      hb_r <= hb_r + 1;
      if rst_i = '1' then
        outstanding_r <= '0';
        count_r       <= 0;
        trig_r        <= '0';
      else
        -- An accepted AR opens a read; its R handshake closes it. Both in one clock
        -- (a new AR in the same cycle the previous R completes) keep it open.
        if arvalid_i = '1' and arready_i = '1' then
          outstanding_r <= '1';
        elsif rvalid_i = '1' and rready_i = '1' then
          outstanding_r <= '0';
        end if;

        if stall = '1' then
          if count_r < G_STALL_CYCLES then
            count_r <= count_r + 1;
          end if;
          if count_r = G_STALL_CYCLES - 1 then
            trig_r <= '1';
          end if;
        else
          count_r <= 0;
        end if;
      end if;
    end if;
  end process;

  stall_o       <= stall;
  outstanding_o <= outstanding_r;
  trig_o        <= trig_r;
  heartbeat_o   <= hb_r(G_HEARTBEAT_BIT);

end architecture rtl;
