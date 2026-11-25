-- rr unknown-llm (AI-generated)
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
--
-- pf_known_answer_core.vhd — shared body of the minimal fabric-only
-- "known-answer" design for the PolarFire SoC Discovery Kit
-- (MPFS095T-1FCSG325E). DS-P2.132 (PF-T9 programming validation + first-silicon
-- REA witness over the RTL-P3.1706 UJTAG adapter).
--
-- The known-answer constant G_KA is set by the instantiating TOP WRAPPER via an
-- RTL generic map (pf_known_answer_top_a => 165 / 0xA5, pf_known_answer_top_b
-- => 90 / 0x5A, pf_known_answer_top_rea => 90). It is deliberately NOT taken
-- from build_options.generics: on the Microchip/Libero flow the tool-level
-- `-hdl_param` override is silently dropped (observed DS-P2.132: two builds with
-- G_KA=165 vs 90 both synthesised G_KA=165 -> byte-identical bitstreams that
-- `rr program verify` could not tell apart). An RTL generic map is honoured by
-- Synplify unconditionally, so the two images genuinely diverge.
--
-- This core owns NO vendor primitive: it exposes the deterministic known-answer
-- word on probe_o, and the REA (rr_rea_jtag_microchip / Microchip UJTAG) is
-- added only by pf_known_answer_top_rea.vhd. Keeping the REA out of the core is
-- what lets the Leg-1 (no-REA) images build without dragging the UJTAG sim BFM
-- into synthesis (where it would shadow the real UJTAG macro).

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity pf_known_answer_core is
  generic (
    -- Known-answer constant, 0..255. Low nibble is the per-image "answer".
    G_KA        : integer range 0 to 255 := 165;
    -- Counter bit that drives the visible LED blink (2**G_BLINK_BIT / 50 MHz).
    G_BLINK_BIT : integer := 24
    );
  port (
    ref_clk_50mhz : in  std_logic;
    led           : out std_logic_vector(3 downto 0);
    -- Deterministic REA known-answer word (see concatenation below). Left open
    -- by the Leg-1 wrappers; wired to the REA probe by pf_known_answer_top_rea.
    probe_o       : out std_logic_vector(15 downto 0)
    );
end entity pf_known_answer_core;

architecture rtl of pf_known_answer_core is

  signal counter   : unsigned(31 downto 0) := (others => '0');
  signal heartbeat : std_logic := '0';

  -- The known-answer nibble, resolved from the integer generic (low 4 bits).
  constant C_KA_NIB : std_logic_vector(3 downto 0) :=
    std_logic_vector(to_unsigned(G_KA mod 16, 4));

begin

  -- Free-running counter off the 50 MHz oscillator.
  process(ref_clk_50mhz)
  begin
    if rising_edge(ref_clk_50mhz) then
      counter   <= counter + 1;
      heartbeat <= counter(G_BLINK_BIT);
    end if;
  end process;

  -- LEDs: the visible blink XOR'd with the image's known-answer nibble, so the
  -- steady-state LED logic (and the fabric bitstream) differs between images.
  led <= std_logic_vector(counter(G_BLINK_BIT + 3 downto G_BLINK_BIT)) xor C_KA_NIB;

  -- Deterministic REA known-answer word (LSB-first):
  --   [7:0]  counter[7:0]  — increments every sample; proves the capture is live
  --   [11:8] C_KA_NIB      — the image's constant nibble (the "known answer")
  --   [12]   heartbeat     — slow toggle
  --   [15:13] "101"        — fixed sync marker to prove field alignment
  probe_o <= "101" & heartbeat & C_KA_NIB & std_logic_vector(counter(7 downto 0));

end architecture rtl;
