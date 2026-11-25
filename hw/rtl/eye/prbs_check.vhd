-- rr unknown-llm (AI-generated)
--------------------------------------------------------------------------------
-- File       : prbs_check.vhd
-- Project    : deepskopion — eye-scan / BERT instrument (DS-P2.66)
-- Purpose    : Self-synchronising PRBS checker — the RX-side bit-error counter for
--              the PMOD eye-scan loopback. One (freq, phase) point's BER comes
--              from ERROR_COUNT / BIT_COUNT after a measurement window.
--
-- How it locks: it applies the SAME two taps as prbs_gen.vhd to the LAST N
--   RECEIVED bits and predicts the next: predicted = rxsr(N-1) xor rxsr(M-1).
--   error = predicted xor RX. Because the prediction depends only on received
--   bits (not on any hidden TX state), it self-synchronises in N bits regardless
--   of loopback latency or sample phase — the textbook telecom BERT structure.
--
--   Lock = G_LOCK_OK consecutive matches (any mismatch resets the lock counter).
--   BIT_COUNT / ERROR_COUNT accumulate ONLY post-lock, so the fill/align phase is
--   never miscounted as errors. A disconnected/garbage input never locks → its
--   BER reads as "no measurement", not a false 0 (adversarial-safe).
--
-- Standard   : VHDL-2019 (IEEE 1076-2019)
-- Licence    : CERN-OHL-W-2.0
--------------------------------------------------------------------------------

library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

entity prbs_check is
  generic (
    G_ORDER   : positive := 7;    -- must match the prbs_gen feeding the loopback
    G_LOCK_OK : positive := 32;   -- consecutive matches required to declare LOCKED
    G_CNT_W   : positive := 48    -- width of the bit/error counters
  );
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;  -- sync: clears counters + lock, restarts a window
    EN          : in  std_logic;  -- RX carries a valid bit this cycle when '1'
    RX          : in  std_logic;  -- received serial bit (post-IDELAY sample)
    LOCKED      : out std_logic;
    ERROR_COUNT : out std_logic_vector(G_CNT_W-1 downto 0);
    BIT_COUNT   : out std_logic_vector(G_CNT_W-1 downto 0);
    -- REA debug tap: the live recovered (RX) shift register. Compared against the
    -- TX generator's DBG_SR in an on-chip analyzer, this is the loopback's ground
    -- truth — they align once the link is real. Backward-compatible output.
    DBG_RXSR    : out std_logic_vector(G_ORDER-1 downto 0)
  );
end entity prbs_check;

architecture rtl of prbs_check is

  function second_tap (n : positive) return positive is
  begin
    case n is
      when 7      => return 6;
      when 15     => return 14;
      when 23     => return 18;
      when 31     => return 28;
      when others => return n - 1;
    end case;
  end function second_tap;

  constant C_M : positive := second_tap(G_ORDER);

  signal rxsr_r    : std_logic_vector(G_ORDER-1 downto 0) := (others => '0');
  signal locked_r  : std_logic := '0';
  signal lockcnt_r : unsigned(15 downto 0) := (others => '0');   -- holds up to 65535 matches
  signal err_r     : unsigned(G_CNT_W-1 downto 0) := (others => '0');
  signal bit_r     : unsigned(G_CNT_W-1 downto 0) := (others => '0');

  signal predicted  : std_logic;
  signal mismatch   : std_logic;
  signal degenerate : std_logic;   -- rxsr all-0 or all-1 = PRBS forbidden state (stuck line)

begin

  predicted  <= rxsr_r(G_ORDER-1) xor rxsr_r(C_M-1);
  mismatch   <= predicted xor RX;
  -- A maximal PRBS-N never produces N identical consecutive bits, so an all-0 or
  -- all-1 history means the input is stuck/disconnected — must NOT count as lock.
  degenerate <= '1' when (rxsr_r = (rxsr_r'range => '0')) or
                         (rxsr_r = (rxsr_r'range => '1')) else '0';

  P_Check : process (CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        rxsr_r    <= (others => '0');
        locked_r  <= '0';
        lockcnt_r <= (others => '0');
        err_r     <= (others => '0');
        bit_r     <= (others => '0');
      elsif EN = '1' then
        rxsr_r <= rxsr_r(G_ORDER-2 downto 0) & RX;   -- newest in at bit 0

        if locked_r = '0' then
          -- alignment phase: count consecutive correct predictions on REAL PRBS
          -- activity (a stuck all-0/all-1 line matches trivially but never locks).
          if mismatch = '0' and degenerate = '0' then
            if lockcnt_r = to_unsigned(G_LOCK_OK-1, lockcnt_r'length) then
              locked_r <= '1';
            else
              lockcnt_r <= lockcnt_r + 1;
            end if;
          else
            lockcnt_r <= (others => '0');
          end if;
        else
          -- measurement phase: every checked bit counts; mismatches are errors
          bit_r <= bit_r + 1;
          if mismatch = '1' then
            err_r <= err_r + 1;
          end if;
        end if;
      end if;
    end if;
  end process P_Check;

  LOCKED      <= locked_r;
  ERROR_COUNT <= std_logic_vector(err_r);
  BIT_COUNT   <= std_logic_vector(bit_r);
  DBG_RXSR    <= rxsr_r;

end architecture rtl;
