-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
--
-- pf_known_answer_top_rea.vhd — PolarFire SoC known-answer IMAGE with an on-chip
-- REA (RouteRTL Embedded Analyzer) core. DS-P2.132 (PF-T10 / RTL-P3.1706
-- first-silicon witness). Instantiates pf_known_answer_core (G_KA = 90, low
-- nibble 0xA) and wires its deterministic known-answer word (probe_o) into
-- rr_rea_jtag_microchip, which taps the dedicated JTAG TAP via Microchip's
-- UJTAG primitive (zero user pins) so the trace is read back over the embedded
-- FlashPro5 + OpenOCD transport.
--
-- The REA sample word (probe_o) is, LSB-first: counter[7:0] :: 0xA :: heartbeat
-- :: "101". The exported VCD is checked for that EXACT pattern (nibble 0xA at
-- bits[11:8], sync "101" at bits[15:13], counter[7:0] incrementing) — not mere
-- non-emptiness — which is what makes this a real known-answer capture.

library ieee;
use ieee.std_logic_1164.all;

entity pf_known_answer_top_rea is
  port (
    ref_clk_50mhz : in  std_logic;
    led           : out std_logic_vector(3 downto 0);
    -- Dedicated JTAG TAP pins for the UJTAG macro. Libero maps these to the
    -- device's hardwired JTAG pads (dedicated bank) — they consume no user
    -- package pins — but they MUST be real top-level ports (see the wrapper).
    jtag_tck      : in  std_logic;
    jtag_tdi      : in  std_logic;
    jtag_tms      : in  std_logic;
    jtag_trstb    : in  std_logic;
    jtag_tdo      : out std_logic
    );
end entity pf_known_answer_top_rea;

architecture rtl of pf_known_answer_top_rea is

  signal probe : std_logic_vector(15 downto 0);

  -- rr_rea_ujtag_local: the Microchip UJTAG wrapper around the LOCAL rr_rea_top
  -- (hw/rtl/pfsoc/rr_rea_ujtag_local.vhd). The SDK's rr_rea_jtag_microchip.vhd
  -- targets a newer, incompatible rr_rea_top interface — see that file's header.
  component rr_rea_ujtag_local is
    generic (
      G_SAMPLE_W    : positive := 12;
      G_DEPTH       : positive := 4096;
      G_TIMESTAMP_W : natural  := 32;
      G_NUM_CHAN    : positive := 1;
      G_CTRL_CHAIN  : integer  := 1
      );
    port (
      sample_clk  : in  std_logic;
      sample_rst  : in  std_logic;
      probe_in    : in  std_logic_vector(G_SAMPLE_W - 1 downto 0);
      trigger_out : out std_logic;
      jtag_tck    : in  std_logic;
      jtag_tdi    : in  std_logic;
      jtag_tms    : in  std_logic;
      jtag_trstb  : in  std_logic;
      jtag_tdo    : out std_logic
      );
  end component rr_rea_ujtag_local;

begin

  u_core : entity work.pf_known_answer_core
    generic map (
      G_KA        => 90,                -- 0x5A, REA known-answer nibble 0xA
      G_BLINK_BIT => 24
      )
    port map (
      ref_clk_50mhz => ref_clk_50mhz,
      led           => led,
      probe_o       => probe
      );

  u_rea : rr_rea_ujtag_local
    generic map (
      G_SAMPLE_W    => 16,
      G_DEPTH       => 4096,
      G_TIMESTAMP_W => 32,
      G_NUM_CHAN    => 1,
      G_CTRL_CHAIN  => 1
      )
    port map (
      sample_clk  => ref_clk_50mhz,
      sample_rst  => '0',
      probe_in    => probe,
      trigger_out => open,
      jtag_tck    => jtag_tck,
      jtag_tdi    => jtag_tdi,
      jtag_tms    => jtag_tms,
      jtag_trstb  => jtag_trstb,
      jtag_tdo    => jtag_tdo
      );

end architecture rtl;
