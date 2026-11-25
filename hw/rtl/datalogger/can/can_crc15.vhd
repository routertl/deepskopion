-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- can_crc15.vhd  —  CAN CRC-15 generator (DS-P3.100, Block 4 of 4)
-------------------------------------------------------------------------------
-- The serial CRC-15 every CAN node computes over a frame, generator polynomial
--   x^15 + x^14 + x^10 + x^8 + x^7 + x^4 + x^3 + 1  =  0x4599
-- (the XOR mask below is the generator minus its x^15 term — the bit shifted out).
--
-- COVERAGE: the CRC spans SOF .. end-of-DATA, over the DE-STUFFED bit stream
-- (stuff bits are NOT included — which is why can_destuff sits upstream of both
-- this block and the frame FSM). The frame FSM drives the window: CLR at SOF,
-- then EN on each covered de-stuffed bit through the last data bit; it stops
-- feeding when the CRC sequence begins, then compares this computed value against
-- the CRC captured off the wire (crc_error on mismatch — the compare lives in the
-- FSM, which owns both values).
--
-- Bosch serial algorithm (CRC_RG starts at 0):
--   crcnext = DIN xor CRC_RG(14)
--   CRC_RG  = CRC_RG << 1            (drop top, shift 0 in)
--   if crcnext then CRC_RG = CRC_RG xor 0x4599
--
-- Pure engine: CLR (sync clear to 0, frame start) / EN (process DIN) / DIN. No
-- frame knowledge here — that keeps it independently testable against an oracle.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

entity can_crc15 is
  port (
    CLK : in  std_logic;
    RST : in  std_logic;
    CLR : in  std_logic;                         -- sync clear CRC_RG to 0 (frame start)
    EN  : in  std_logic;                         -- process DIN this cycle (covered bit strobe)
    DIN : in  std_logic;                         -- de-stuffed covered bit
    CRC : out std_logic_vector(14 downto 0)      -- current CRC-15 register
    );
end entity can_crc15;

architecture rtl of can_crc15 is
  -- generator minus x^15: x^14+x^10+x^8+x^7+x^4+x^3+1 = 0x4599 (15-bit)
  constant C_POLY : std_logic_vector(14 downto 0) := "100010110011001";
  signal crc_r    : std_logic_vector(14 downto 0) := (others => '0');
begin
  process(CLK)
    variable crcnext : std_logic;
    variable shifted : std_logic_vector(14 downto 0);
  begin
    if rising_edge(CLK) then
      if RST = '1' or CLR = '1' then
        crc_r <= (others => '0');
      elsif EN = '1' then
        crcnext := DIN xor crc_r(14);
        shifted := crc_r(13 downto 0) & '0';
        if crcnext = '1' then
          crc_r <= shifted xor C_POLY;
        else
          crc_r <= shifted;
        end if;
      end if;
    end if;
  end process;

  CRC <= crc_r;
end architecture rtl;
