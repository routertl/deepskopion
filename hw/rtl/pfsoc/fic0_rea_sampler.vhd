-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- DS-P2.326: the analyzer sample word for the PolarFire FIC0 read-path capture.
--
-- sample_o, registered on clk_i, LSB first:
--   [31:0]  probe_i: the fabric's fic0_rd_probe_o ([0] ARVALID [1] ARREADY [2] RVALID [3] RREADY
--           [5:4] RRESP [13:6] ARADDR(7:0) [21:14] ARID [29:22] RID [30] RLAST [31] '0')
--   [32] stall  [33] outstanding  [34] trig  [35] heartbeat   (from fic0_stall_trigger)
--   [36] event: an AR handshake, an R handshake, or a change of trig in this clock. A stall's own
--        rise and fall are not events: every normal read stalls for its latency, so they would double
--        the stored samples per read; the trigger sample carries stall/outstanding when it fires.
-- The analyzer stores only samples with event = 1 (one storage qualifier, bit 36 EQ 1), so its
-- depth counts bus events, not clock cycles. The register keeps the probe nets to one fanout
-- inside the fabric's FIC0 timing paths.
--
-- Bench-only: instantiated by deepskopion_pfsoc_top_fic0_rea, never by the product top.
library ieee;
use ieee.std_logic_1164.all;

entity fic0_rea_sampler is
  port (
    clk_i         : in  std_logic;
    probe_i       : in  std_logic_vector(31 downto 0);
    stall_i       : in  std_logic;
    outstanding_i : in  std_logic;
    trig_i        : in  std_logic;
    heartbeat_i   : in  std_logic;
    sample_o      : out std_logic_vector(36 downto 0)
  );
end entity fic0_rea_sampler;

architecture rtl of fic0_rea_sampler is
  signal trig_q   : std_logic := '0';
  signal event_w  : std_logic;
  signal sample_r : std_logic_vector(36 downto 0) := (others => '0');
begin

  event_w <= (probe_i(0) and probe_i(1)) or (probe_i(2) and probe_i(3))
             or (trig_i xor trig_q);

  p_sample : process (clk_i)
  begin
    if rising_edge(clk_i) then
      trig_q   <= trig_i;
      sample_r <= event_w & heartbeat_i & trig_i & outstanding_i & stall_i & probe_i;
    end if;
  end process;

  sample_o <= sample_r;

end architecture rtl;
