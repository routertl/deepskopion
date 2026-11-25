-- rr unknown-llm (AI-generated)
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- DE25 fabric reset (DS-P1.80).
--
-- Two asynchronous sources hold the fabric in reset:
--   ninit_done_i  SDM Reset Release IP output, HIGH while the SDM configures.
--   pll_locked_i  fabric IOPLL lock, LOW until outclk0 is stable.
-- Each is synchronised into clk_i by its own chain, and the two chain outputs
-- are combined and registered after the chains. No logic sits in front of a
-- first synchronizer stage, so each chain has one source domain (Quartus
-- Design Assistant CDC-50012).
--
-- rst_o is HIGH from configuration until both sources report ready, then
-- falls G_STAGES + 1 clk_i edges after the later of the two. It rises again
-- G_STAGES + 1 edges after either source reports not ready.
--
-- clk_i is the IOPLL product. Before lock it does not toggle, so every
-- register holds its power-up value: both chains hold "not ready" and rst_o
-- holds '1'.

library ieee;
use ieee.std_logic_1164.all;

entity de25_reset_ctrl is
  generic (
    G_STAGES : positive range 2 to 16 := 3
  );
  port (
    clk_i        : in  std_logic;
    ninit_done_i : in  std_logic;
    pll_locked_i : in  std_logic;
    rst_o        : out std_logic
  );
end entity de25_reset_ctrl;

architecture rtl of de25_reset_ctrl is
  -- '1' = SDM still configuring.
  signal init_sync_r : std_logic_vector(G_STAGES - 1 downto 0) := (others => '1');
  -- '1' = PLL locked.
  signal lock_sync_r : std_logic_vector(G_STAGES - 1 downto 0) := (others => '0');
  signal rst_r       : std_logic := '1';

  attribute async_reg : string;
  attribute async_reg of init_sync_r : signal is "TRUE";
  attribute async_reg of lock_sync_r : signal is "TRUE";
begin
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      init_sync_r <= init_sync_r(G_STAGES - 2 downto 0) & ninit_done_i;
      lock_sync_r <= lock_sync_r(G_STAGES - 2 downto 0) & pll_locked_i;
      rst_r       <= init_sync_r(G_STAGES - 1) or not lock_sync_r(G_STAGES - 1);
    end if;
  end process;

  rst_o <= rst_r;
end architecture rtl;
