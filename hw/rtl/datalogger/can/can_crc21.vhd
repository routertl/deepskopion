-------------------------------------------------------------------------------
-- can_crc21.vhd  —  CAN-FD CRC-21 generator (DS-P2.64, FD payloads > 16 bytes)
-------------------------------------------------------------------------------
-- The serial CRC-21 a CAN-FD node computes over an FD frame whose data field is
-- 20..64 bytes. Generator polynomial (ISO 11898-1:2015 / Bosch CAN FD v1.0):
--   x^21 + x^20 + x^13 + x^11 + x^7 + x^4 + x^3 + 1
-- Catalogued as CRC-21/CAN-FD: poly=0x102899, init=0, refin=false, refout=false,
-- xorout=0, check("123456789")=0x0ED841 (reveng CRC catalogue). The XOR mask below
-- is the generator minus its x^21 term (the bit shifted out) = 0x102899 (21 bits).
--
-- COVERAGE (driven by the frame FSM, not here): the FD CRC spans SOF..stuff-count
-- with DYNAMIC stuff bits INCLUDED but FIXED stuff bits EXCLUDED (ISO 11898-1,
-- silicon-anchored DS-T2.49). The ISO-FD init is the register MSB preset to 1
-- (init=0x100000) — NOT realisable by feeding a leading data bit, so it is a real
-- register LOAD: drive INIT_MSB='1' and the CLR pulse presets crc_r(20)=1.
-- INIT_MSB defaults '0' so the engine still matches the catalogued init=0
-- parametrisation (check=0x0ED841) when unused (unit test). Fixed stuff bits NOT
-- fed here. CRC-17 vs CRC-21 is selected by the FSM from the decoded DLC->bytes.
--
-- Bosch serial algorithm, identical in shape to can_crc15/17:
--   crcnext = DIN xor CRC_RG(20)
--   CRC_RG  = CRC_RG << 1            (drop top, shift 0 in)
--   if crcnext then CRC_RG = CRC_RG xor 0x102899
--
-- Pure engine: CLR (sync clear/preset) / EN (process DIN) / DIN / INIT_MSB. No
-- frame knowledge, so it stays independently testable against an external oracle.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

entity can_crc21 is
  port (
    CLK : in  std_logic;
    RST : in  std_logic;
    CLR : in  std_logic;                         -- sync clear/preset CRC_RG (frame start)
    EN  : in  std_logic;                         -- process DIN this cycle (covered bit strobe)
    DIN : in  std_logic;                         -- covered bit (dynamic-stuffed feed)
    INIT_MSB : in std_logic := '0';              -- ISO-FD init: preset crc_r(20) on CLR
    CRC : out std_logic_vector(20 downto 0)      -- current CRC-21 register
    );
end entity can_crc21;

architecture rtl of can_crc21 is
  -- generator minus x^21: 0x102899 (21-bit) = bits 20,13,11,7,4,3,0
  constant C_POLY : std_logic_vector(20 downto 0) := "100000010100010011001";
  signal crc_r    : std_logic_vector(20 downto 0) := (others => '0');
begin
  process(CLK)
    variable crcnext : std_logic;
    variable shifted : std_logic_vector(20 downto 0);
  begin
    if rising_edge(CLK) then
      if RST = '1' or CLR = '1' then
        crc_r <= (others => '0');
        crc_r(20) <= INIT_MSB;                  -- ISO-FD MSB preset (init=0x100000)
      elsif EN = '1' then
        crcnext := DIN xor crc_r(20);
        shifted := crc_r(19 downto 0) & '0';
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
