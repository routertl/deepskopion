-------------------------------------------------------------------------------
-- ring_watermark.vhd — DS-P3.60 ring-fill watermark alarm with hysteresis.
--
-- Raises a LEVEL alarm (→ IRQ_F2P) when an SPSC ring's occupancy crosses a
-- HIGH watermark, so the consumer is told to drain BEFORE the ring overflows
-- (the early-warning complement to DS-T2.22's after-the-fact overflow counter).
--
-- The alarm is a Schmitt trigger (two watermarks) to avoid interrupt thrash:
--   * SET   when fill rises to/through HIGH (default 75% of the ring),
--   * CLEAR only when the consumer has drained back below LOW (default 50%).
-- So one fill excursion = exactly one interrupt episode; the line stays
-- asserted through the whole drain and re-arms only after a deep drain. The
-- alarm is level-sensitive so a slow PS can never miss it (GIC level-high).
--
-- fill = (commit_off - read_off) mod 2^G_ADDR_WIDTH, computed by natural
-- unsigned wraparound on the ring-relative byte offsets. The caller passes
-- the low G_ADDR_WIDTH bits of the commit (write) and read pointers — for a
-- ring whose base is 2^G_ADDR_WIDTH-aligned those are the relative offsets.
--
-- Watermarks are fractions of the ring (G_HIGH_NUM/G_DEN, G_LOW_NUM/G_DEN) so
-- the unit test can shrink the ring (small G_ADDR_WIDTH) without changing the
-- thresholds' meaning. G_LOW_NUM < G_HIGH_NUM must hold (real hysteresis).
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity ring_watermark is
  generic (
    G_ADDR_WIDTH : integer := 28;  -- log2(RING_BYTES); 28 → 256 MB
    G_HIGH_NUM   : integer := 3;   -- HIGH = G_HIGH_NUM/G_DEN of full (3/4 = 75%)
    G_LOW_NUM    : integer := 2;   -- LOW  = G_LOW_NUM /G_DEN of full (2/4 = 50%)
    G_DEN        : integer := 4
  );
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;  -- synchronous, active-high
    commit_off : in  std_logic_vector(G_ADDR_WIDTH - 1 downto 0);  -- ring write offset
    read_off   : in  std_logic_vector(G_ADDR_WIDTH - 1 downto 0);  -- ring read  offset
    fill       : out std_logic_vector(G_ADDR_WIDTH - 1 downto 0);  -- occupancy (RO obs)
    alarm      : out std_logic;                       -- LEVEL alarm → IRQ_F2P
    alarm_cnt  : out std_logic_vector(31 downto 0)    -- # excursions ≥ HIGH
  );
end entity ring_watermark;

architecture rtl of ring_watermark is
  constant C_HIGH : unsigned(G_ADDR_WIDTH - 1 downto 0)
    := to_unsigned((2**G_ADDR_WIDTH / G_DEN) * G_HIGH_NUM, G_ADDR_WIDTH);
  constant C_LOW  : unsigned(G_ADDR_WIDTH - 1 downto 0)
    := to_unsigned((2**G_ADDR_WIDTH / G_DEN) * G_LOW_NUM,  G_ADDR_WIDTH);

  signal fill_r  : unsigned(G_ADDR_WIDTH - 1 downto 0) := (others => '0');
  signal alarm_r : std_logic := '0';
  signal cnt_r   : unsigned(31 downto 0) := (others => '0');
begin
  -- elaboration guard: hysteresis only makes sense with LOW strictly below HIGH
  assert G_LOW_NUM < G_HIGH_NUM
    report "ring_watermark: G_LOW_NUM must be < G_HIGH_NUM (need real hysteresis)"
    severity failure;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        fill_r  <= (others => '0');
        alarm_r <= '0';
        cnt_r   <= (others => '0');
      else
        fill_r <= unsigned(commit_off) - unsigned(read_off);  -- mod 2^width
        if alarm_r = '0' then
          if fill_r >= C_HIGH then        -- crossed HIGH while armed → fire once
            alarm_r <= '1';
            cnt_r   <= cnt_r + 1;
          end if;
        else
          if fill_r < C_LOW then          -- drained below LOW → re-arm
            alarm_r <= '0';
          end if;
        end if;
      end if;
    end if;
  end process;

  fill      <= std_logic_vector(fill_r);
  alarm     <= alarm_r;
  alarm_cnt <= std_logic_vector(cnt_r);
end architecture rtl;
