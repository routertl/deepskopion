-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- tb_error_event_bank.vhd  —  DS-T3.18 unit harness
-------------------------------------------------------------------------------
-- Fixes the error_event_bank's std_logic_vector generics in VHDL (4 channels,
-- distinct, easy-to-read datalogger ids) so the cocotb test only drives integer
-- ports + the integer G_FLUSH_CYCLES generic. Channel ids, packed low-byte-
-- first per the bank's G_DATALOGGER_IDS contract:
--     ch0 = 0x11   ch1 = 0x22   ch2 = 0x33   ch3 = 0x44
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

library tich;

entity tb_error_event_bank is
  generic (
    G_FLUSH_CYCLES : positive := 20
  );
  port (
    CLK           : in  std_logic;
    RST           : in  std_logic;
    ERR_CNTS      : in  std_logic_vector(4*32-1 downto 0);
    TIMESTAMP     : in  std_logic_vector(63 downto 0);
    FR_VALID      : out std_logic;
    FR_READY      : in  std_logic;
    FR_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    FR_MSG_CLASS  : out std_logic_vector(7 downto 0);
    FR_MSG_TYPE   : out std_logic_vector(15 downto 0);
    FR_FLAGS      : out std_logic_vector(15 downto 0);
    FR_TIMESTAMP  : out std_logic_vector(63 downto 0);
    FR_PAYLOAD    : out std_logic_vector(31 downto 0);
    FR_WORDS      : out std_logic_vector(7 downto 0)
  );
end entity tb_error_event_bank;

architecture tb of tb_error_event_bank is
begin
  dut : entity tich.error_event_bank
    generic map (
      G_NUM_CH       => 4,
      G_DATALOGGER_IDS  => x"44332211",   -- ch3|ch2|ch1|ch0 = 0x44|0x33|0x22|0x11
      G_FLUSH_CYCLES => G_FLUSH_CYCLES
    )
    port map (
      CLK           => CLK,
      RST           => RST,
      ERR_CNTS      => ERR_CNTS,
      TIMESTAMP     => TIMESTAMP,
      FR_VALID      => FR_VALID,
      FR_READY      => FR_READY,
      FR_DATALOGGER_ID => FR_DATALOGGER_ID,
      FR_MSG_CLASS  => FR_MSG_CLASS,
      FR_MSG_TYPE   => FR_MSG_TYPE,
      FR_FLAGS      => FR_FLAGS,
      FR_TIMESTAMP  => FR_TIMESTAMP,
      FR_PAYLOAD    => FR_PAYLOAD,
      FR_WORDS      => FR_WORDS
    );
end architecture tb;
