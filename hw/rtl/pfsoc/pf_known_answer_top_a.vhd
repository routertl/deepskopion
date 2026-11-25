-- rr unknown-llm (AI-generated)
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
--
-- pf_known_answer_top_a.vhd — PolarFire SoC known-answer IMAGE A wrapper
-- (DS-P2.132, PF-T9). Instantiates pf_known_answer_core with G_KA = 165 (0xA5,
-- low nibble 0x5) via an RTL generic map, so the constant reaches synthesis
-- reliably (the tool-level build_options.generics override is dropped by the
-- Microchip flow — see pf_known_answer_core.vhd header). No REA.

library ieee;
use ieee.std_logic_1164.all;

entity pf_known_answer_top_a is
  port (
    ref_clk_50mhz : in  std_logic;
    led           : out std_logic_vector(3 downto 0)
    );
end entity pf_known_answer_top_a;

architecture rtl of pf_known_answer_top_a is
begin
  u_core : entity work.pf_known_answer_core
    generic map (
      G_KA        => 165,               -- 0xA5, image A
      G_BLINK_BIT => 24
      )
    port map (
      ref_clk_50mhz => ref_clk_50mhz,
      led           => led,
      probe_o       => open
      );
end architecture rtl;
