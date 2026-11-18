-------------------------------------------------------------------------------
-- can_bit_timing_equiv_top.vhd  —  equivalence harness for the B2 refactor
-------------------------------------------------------------------------------
-- Instantiates the NEW direct-count can_bit_timing (B2, DS-P2.64) on its NOMINAL
-- config (RATE_SEL='0', data ports unused) side-by-side with the FROZEN v0.1
-- reference can_bit_timing_tq_ref, both fed the SAME CLK/RST/ENABLE/RX/PRESCALER.
-- The cocotb equivalence test (test_can_bit_timing_equiv.py, CAN-REQ-23) drives
-- stimulus and asserts A_* (B2) == B_* (ref) every clock — proving the silicon-
-- validated classic behaviour transfers to B2 by construction.
--
-- Sim-only harness; never synthesised.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

library tich;

entity can_bit_timing_equiv_top is
  generic (
    G_NUM_TQ    : positive := 16;
    G_SAMPLE_TQ : positive := 12;
    G_PRESC_W   : positive := 10;
    G_CNT_W     : positive := 16
  );
  port (
    CLK       : in  std_logic;
    RST       : in  std_logic;
    ENABLE    : in  std_logic;
    RX        : in  std_logic;
    PRESCALER : in  std_logic_vector(G_PRESC_W-1 downto 0);
    -- B2 (new) outputs
    A_BIT_VALID : out std_logic;
    A_BIT_VALUE : out std_logic;
    A_BIT_START : out std_logic;
    A_SYNCED    : out std_logic;
    -- v0.1 reference outputs
    B_BIT_VALID : out std_logic;
    B_BIT_VALUE : out std_logic;
    B_BIT_START : out std_logic;
    B_SYNCED    : out std_logic
  );
end entity can_bit_timing_equiv_top;

architecture rtl of can_bit_timing_equiv_top is
begin
  -- NEW: B2 unified direct-count sampler, nominal config (RATE_SEL tied low,
  -- data-phase pair left at default 0 → never selected).
  U_NEW : entity tich.can_bit_timing
    generic map (
      G_NUM_TQ    => G_NUM_TQ,
      G_SAMPLE_TQ => G_SAMPLE_TQ,
      G_PRESC_W   => G_PRESC_W,
      G_CNT_W     => G_CNT_W)
    port map (
      CLK       => CLK,
      RST       => RST,
      ENABLE    => ENABLE,
      RX        => RX,
      PRESCALER => PRESCALER,
      RATE_SEL  => '0',
      BIT_VALID => A_BIT_VALID,
      BIT_VALUE => A_BIT_VALUE,
      BIT_START => A_BIT_START,
      SYNCED    => A_SYNCED);

  -- FROZEN v0.1 TQ reference.
  U_REF : entity tich.can_bit_timing_tq_ref
    generic map (
      G_NUM_TQ    => G_NUM_TQ,
      G_SAMPLE_TQ => G_SAMPLE_TQ,
      G_PRESC_W   => G_PRESC_W)
    port map (
      CLK       => CLK,
      RST       => RST,
      ENABLE    => ENABLE,
      RX        => RX,
      PRESCALER => PRESCALER,
      BIT_VALID => B_BIT_VALID,
      BIT_VALUE => B_BIT_VALUE,
      BIT_START => B_BIT_START,
      SYNCED    => B_SYNCED);

end architecture rtl;
