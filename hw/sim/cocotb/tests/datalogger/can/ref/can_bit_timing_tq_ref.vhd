-------------------------------------------------------------------------------
-- can_bit_timing_tq_ref.vhd  —  FROZEN v0.1 TQ bit-timing block (equivalence ref)
-------------------------------------------------------------------------------
-- This is a VERBATIM copy of the silicon-validated v0.1 can_bit_timing
-- (DS-P3.100, validated on Zybo Z7-20 as v0.3.0-can-emio-silicon), with ONLY the
-- entity name changed (can_bit_timing → can_bit_timing_tq_ref). It exists solely
-- as the golden reference for the DS-P2.64 B2 refactor equivalence test
-- (CAN-REQ-23): the new direct-count can_bit_timing must produce bit-identical
-- BIT_VALID/BIT_VALUE/BIT_START on the nominal config.
--
-- DO NOT "improve" this file. Its whole value is being a frozen snapshot of the
-- behaviour proven on silicon. It is sim-only (under hw/sim/.../ref/) and is NOT
-- part of the synthesised design.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity can_bit_timing_tq_ref is
  generic (
    G_NUM_TQ    : positive := 16;   -- time quanta per CAN bit
    G_SAMPLE_TQ : positive := 12;   -- sample point (TQ index, 75% of 16)
    G_PRESC_W   : positive := 10    -- prescaler width (clk cycles per TQ)
  );
  port (
    CLK       : in  std_logic;
    RST       : in  std_logic;
    ENABLE    : in  std_logic;                            -- gate (CONFIG enable)
    RX        : in  std_logic;                            -- CAN line (dom='0'/rec='1')
    PRESCALER : in  std_logic_vector(G_PRESC_W-1 downto 0); -- clk cycles per TQ (>=1)
    BIT_VALID : out std_logic;                            -- 1-clk strobe at the sample point
    BIT_VALUE : out std_logic;                            -- RX sampled at the sample point
    BIT_START : out std_logic;                            -- 1-clk strobe at each bit start (TQ 0)
    SYNCED    : out std_logic                             -- locked (>=1 falling edge seen)
  );
end entity can_bit_timing_tq_ref;

architecture rtl of can_bit_timing_tq_ref is
  signal rx_m, rx_s, rx_d : std_logic := '1';
  signal tq_cnt  : unsigned(G_PRESC_W-1 downto 0) := (others => '0');  -- cycles within a TQ
  signal tq_idx  : integer range 0 to G_NUM_TQ-1   := 0;              -- TQ within a bit
  signal synced_r : std_logic := '0';
  signal presc_u : unsigned(G_PRESC_W-1 downto 0);
begin
  presc_u <= unsigned(PRESCALER);
  SYNCED  <= synced_r;

  process(CLK)
    variable falling : boolean;
  begin
    if rising_edge(CLK) then
      BIT_VALID <= '0';
      BIT_START <= '0';

      rx_m <= RX;
      rx_s <= rx_m;
      rx_d <= rx_s;
      falling := (rx_d = '1' and rx_s = '0');

      if ENABLE = '1' then
        if falling then
          tq_cnt    <= (others => '0');
          tq_idx    <= 0;
          synced_r  <= '1';
          BIT_START <= '1';
        elsif synced_r = '1' then
          if tq_cnt >= presc_u - 1 then
            tq_cnt <= (others => '0');
            if tq_idx = G_NUM_TQ - 1 then
              tq_idx    <= 0;
              BIT_START <= '1';
            else
              tq_idx <= tq_idx + 1;
            end if;
          else
            tq_cnt <= tq_cnt + 1;
          end if;
        end if;

        if synced_r = '1' and tq_cnt = 0 and tq_idx = G_SAMPLE_TQ then
          BIT_VALID <= '1';
          BIT_VALUE <= rx_s;
        end if;
      end if;

      if RST = '1' or ENABLE = '0' then
        tq_cnt   <= (others => '0');
        tq_idx   <= 0;
        synced_r <= '0';
      end if;
    end if;
  end process;

end architecture rtl;
