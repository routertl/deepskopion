--------------------------------------------------------------------------------
-- File       : prbs_gen.vhd
-- Project    : deepskopion — eye-scan / BERT instrument (DS-P2.66)
-- Purpose    : Serial PRBS generator (PRBS7/15/23/31), the TX data source for the
--              PMOD eye-scan loopback.
--
-- Formulation: the OUTPUT-RECURRENCE form, o[n] = o[n-N] xor o[n-M], rather than
--              wrapping a Galois LFSR. This is deliberate: prbs_check.vhd is a
--              self-synchronising checker that applies the SAME two taps to the
--              RECEIVED bit stream — so gen and check are guaranteed consistent
--              by construction (identical recurrence, no tap-indexing ambiguity).
--              Standard primitive 2-tap polynomials x^N + x^M + 1:
--                PRBS7  N=7  M=6 | PRBS15 N=15 M=14 | PRBS23 N=23 M=18 | PRBS31 N=31 M=28
--
--   `sr` holds the last N output bits: sr(N-1)=o[n-N] (oldest), sr(0)=o[n-1].
--   o[n] = sr(N-1) xor sr(M-1).  Seed = all-ones (non-zero → maximal length).
--
-- Standard   : VHDL-2019 (IEEE 1076-2019)
-- Licence    : CERN-OHL-W-2.0
--------------------------------------------------------------------------------

library ieee;
  use ieee.std_logic_1164.all;

entity prbs_gen is
  generic (
    G_ORDER : positive := 7   -- PRBS-N: one of 7, 15, 23, 31
  );
  port (
    CLK  : in  std_logic;
    RST  : in  std_logic;   -- sync, reloads the all-ones seed
    EN   : in  std_logic;   -- advance one bit when '1'
    DOUT : out std_logic;   -- registered serial PRBS bit (updates on EN)
    -- REA debug tap: the live LFSR shift register (newest bit at index 0). Lets
    -- an on-chip analyzer watch the generator "rotate". Backward-compatible
    -- output — existing instantiations may leave it open.
    DBG_SR : out std_logic_vector(G_ORDER-1 downto 0)
  );
end entity prbs_gen;

architecture rtl of prbs_gen is

  -- Second tap M for the standard primitive polynomial x^N + x^M + 1.
  function second_tap (n : positive) return positive is
  begin
    case n is
      when 7      => return 6;
      when 15     => return 14;
      when 23     => return 18;
      when 31     => return 28;
      when others => return n - 1;   -- fallback (not a guaranteed-maximal poly)
    end case;
  end function second_tap;

  constant C_M  : positive := second_tap(G_ORDER);

  signal sr_r   : std_logic_vector(G_ORDER-1 downto 0) := (others => '1');
  signal dout_r : std_logic := '1';

  -- o[n] from the two taps (combinational view of the next output bit).
  signal o_next : std_logic;

begin

  o_next <= sr_r(G_ORDER-1) xor sr_r(C_M-1);

  P_Prbs : process (CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        sr_r   <= (others => '1');
        dout_r <= '1';
      elsif EN = '1' then
        sr_r   <= sr_r(G_ORDER-2 downto 0) & o_next;   -- shift o[n] in, drop o[n-N]
        dout_r <= o_next;
      end if;
    end if;
  end process P_Prbs;

  DOUT   <= dout_r;
  DBG_SR <= sr_r;

end architecture rtl;
