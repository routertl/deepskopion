--------------------------------------------------------------------------------
-- tb_can_datalogger_cond — can_datalogger behind the SAME RX conditioning the
-- datalogger_gather puts in front on silicon: the per-pin glitch_filter (2-FF
-- synchroniser + de-glitcher / debounce). Test-only wrapper for DS-T2.49 sim
-- faithfulness — the bare can_datalogger-level testbench drives CAN_PINS directly
-- and so BYPASSES this conditioning; the back-to-back FD CRC bug is silicon-only
-- and we want the synchroniser + de-glitcher in the loop to see if it reproduces.
--
-- Conditioning matches the gather GEN_*_PIN_MAP for the CAN line exactly:
--   glitch_filter(G_CLK_HZ => G_SYS_CLK, G_MAX_FREQ_HZ => C_CAN_MAX_FREQ_HZ).
--------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

library tich;
use tich.datalogger_pkg.all;       -- C_DATALOGGER_MSG_WIDTH

entity tb_can_datalogger_cond is
  generic (
    G_CAN_DEV_ID      : natural  := 20;
    G_SYS_CLK         : positive := 100_000_000;
    G_CAN_MAX_FREQ_HZ : positive := 1_000_000     -- protocols.yml CAN max_freq_hz
  );
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;
    CAN_PINS    : in  std_logic_vector(0 downto 0);   -- RAW async RX (pre-conditioning)
    CONFIG      : in  std_logic_vector(15 downto 0);
    TIMESTAMP   : in  std_logic_vector(15 downto 0);
    CAN_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
    CAN_MSG_VLD : out std_logic;
    CAN_MSG_RDY : in  std_logic;
    CAN_ERRORS  : out std_logic_vector(1 downto 0)
  );
end entity tb_can_datalogger_cond;

architecture rtl of tb_can_datalogger_cond is
  signal rx_cond : std_logic;
begin

  -- per-pin RX conditioning (synchroniser + de-glitcher), exactly as the gather.
  i_cond : entity tich.glitch_filter
    generic map (
      G_CLK_HZ      => G_SYS_CLK,
      G_MAX_FREQ_HZ => G_CAN_MAX_FREQ_HZ
    )
    port map (
      CLK     => CLK,
      RST     => RST,
      D_ASYNC => CAN_PINS(0),
      Q       => rx_cond
    );

  i_can : entity tich.can_datalogger
    generic map (
      G_CAN_DEV_ID => G_CAN_DEV_ID
    )
    port map (
      CLK         => CLK,
      RST         => RST,
      CAN_PINS(0) => rx_cond,
      CONFIG      => CONFIG,
      TIMESTAMP   => TIMESTAMP,
      CAN_MSG_OUT => CAN_MSG_OUT,
      CAN_MSG_VLD => CAN_MSG_VLD,
      CAN_MSG_RDY => CAN_MSG_RDY,
      CAN_ERRORS  => CAN_ERRORS,
      DBG_BUS     => open
    );

end architecture rtl;
