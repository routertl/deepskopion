-------------------------------------------------------------------------------
-- can_crc17.vhd  —  CAN-FD CRC-17 generator (DS-P2.64, FD payloads <= 16 bytes)
-------------------------------------------------------------------------------
-- The serial CRC-17 a CAN-FD node computes over an FD frame whose data field is
-- 0..16 bytes. Generator polynomial (ISO 11898-1:2015 / Bosch CAN FD v1.0):
--   x^17 + x^16 + x^14 + x^13 + x^11 + x^6 + x^4 + x^3 + x + 1
-- Catalogued as CRC-17/CAN-FD: poly=0x1685B, init=0, refin=false, refout=false,
-- xorout=0, check("123456789")=0x04F03 (reveng CRC catalogue). The XOR mask below
-- is the generator minus its x^17 term (the bit shifted out) = 0x1685B (17 bits).
--
-- COVERAGE (driven by the frame FSM, not here): the FD CRC spans SOF..stuff-count
-- with DYNAMIC stuff bits INCLUDED but FIXED stuff bits EXCLUDED (ISO 11898-1,
-- silicon-anchored DS-T2.49). The ISO-FD init is the register MSB preset to 1
-- (init=0x10000) — NOT realisable by feeding a leading data bit (the init term is
-- linear and frame-length-dependent), so it is a real register LOAD: drive
-- INIT_MSB='1' and the CLR pulse presets crc_r(16)=1. INIT_MSB defaults '0' so the
-- engine still matches the catalogued init=0 parametrisation (check=0x04F03) when
-- the preset is unused (the can_crc17 unit test). Fixed stuff bits NOT fed here.
--
-- Bosch serial algorithm, identical in shape to can_crc15:
--   crcnext = DIN xor CRC_RG(16)
--   CRC_RG  = CRC_RG << 1            (drop top, shift 0 in)
--   if crcnext then CRC_RG = CRC_RG xor 0x1685B
--
-- Pure engine: CLR (sync clear/preset) / EN (process DIN) / DIN / INIT_MSB. No
-- frame knowledge, so it stays independently testable against an external oracle.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;

entity can_crc17 is
  port (
    CLK : in  std_logic;
    RST : in  std_logic;
    CLR : in  std_logic;                         -- sync clear/preset CRC_RG (frame start)
    EN  : in  std_logic;                         -- process DIN this cycle (covered bit strobe)
    DIN : in  std_logic;                         -- covered bit (dynamic-stuffed feed)
    INIT_MSB : in std_logic := '0';              -- ISO-FD init: preset crc_r(16) on CLR
    CRC : out std_logic_vector(16 downto 0)      -- current CRC-17 register
    );
end entity can_crc17;

architecture rtl of can_crc17 is
  -- generator minus x^17: 0x1685B (17-bit) = bits 16,14,13,11,6,4,3,1,0
  constant C_POLY : std_logic_vector(16 downto 0) := "10110100001011011";
  signal crc_r    : std_logic_vector(16 downto 0) := (others => '0');
begin
  process(CLK)
    variable crcnext : std_logic;
    variable shifted : std_logic_vector(16 downto 0);
  begin
    if rising_edge(CLK) then
      if RST = '1' or CLR = '1' then
        crc_r <= (others => '0');
        crc_r(16) <= INIT_MSB;                  -- ISO-FD MSB preset (init=0x10000)
      elsif EN = '1' then
        crcnext := DIN xor crc_r(16);
        shifted := crc_r(15 downto 0) & '0';
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
