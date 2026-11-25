-- rr unknown-llm (AI-generated)
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
--
-- pf_known_answer_top_b.vhd — PolarFire SoC known-answer IMAGE B wrapper
-- (DS-P2.132, PF-T9). Instantiates pf_known_answer_core with G_KA = 90 (0x5A,
-- low nibble 0xA) via an RTL generic map, so the constant reaches synthesis
-- reliably (the tool-level build_options.generics override is dropped by the
-- Microchip flow — see pf_known_answer_core.vhd header). No REA.

library ieee;
use ieee.std_logic_1164.all;

entity pf_known_answer_top_b is
  port (
    ref_clk_50mhz : in  std_logic;
    led           : out std_logic_vector(3 downto 0)
    );
end entity pf_known_answer_top_b;

architecture rtl of pf_known_answer_top_b is
begin
  u_core : entity work.pf_known_answer_core
    generic map (
      G_KA        => 90,                -- 0x5A, image B
      G_BLINK_BIT => 24
      )
    port map (
      ref_clk_50mhz => ref_clk_50mhz,
      led           => led,
      probe_o       => open
      );
end architecture rtl;
