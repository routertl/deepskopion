-- SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT

-------------------------------------------------------------------------------
-------------------------------------------------------------------------------
-- Title      : LFSR
-- Project    : 
-------------------------------------------------------------------------------
-- File       : lfsr.vhd
-- Author     : Daniel J. Mazure
-- Company    :
-- Created    : 2025-08-1
-- Last update: 2025-08-1
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: Simple LFSR with 19 possible polynomials.
-------------------------------------------------------------------------------
-- Copyright (c) 2025 Daniel J. Mazure
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2025-09-12  1.0      Daniel J.Mazure       Created
-------------------------------------------------------------------------------
library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;
library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;


entity lfsr is
  generic (
    G_LFSR_WIDTH  : natural range 2 to 19 := 7
  );
  port (
    CLK   : in  std_logic;
    RST   : in  std_logic;
    SEED  : in  std_logic_vector (G_LFSR_WIDTH-1 downto 0);
    EN    : in  std_logic;
    LFSR  : out std_logic_vector (G_LFSR_WIDTH-1 downto 0)
  );
end lfsr;

architecture rtl of lfsr is

  constant C_POLY2               : std_logic_vector(18 downto 0):= "0000000000000000011";  -- x^2+x+1
  constant C_POLY3               : std_logic_vector(18 downto 0):= "0000000000000000110";  -- x^3+x^2+1
  constant C_POLY4               : std_logic_vector(18 downto 0):= "0000000000000001100";  -- x^4+x^3+1
  constant C_POLY5               : std_logic_vector(18 downto 0):= "0000000000000010100";  -- x^5+x^3+1
  constant C_POLY6               : std_logic_vector(18 downto 0):= "0000000000000110000";  -- x^6+x^5+1
  constant C_POLY7               : std_logic_vector(18 downto 0):= "0000000000001100000";  -- x^7+x^6+1
  constant C_POLY8               : std_logic_vector(18 downto 0):= "0000000000010111000";  -- x^8+x^6+x^5+x^4+1
  constant C_POLY9               : std_logic_vector(18 downto 0):= "0000000000100010000";  -- x^9+x^5+x^4+1
  constant C_POLY10              : std_logic_vector(18 downto 0):= "0000000001001000000";  -- x^10+x^7+1
  constant C_POLY11              : std_logic_vector(18 downto 0):= "0000000010100000000";  -- x^11+x^9+1
  constant C_POLY12              : std_logic_vector(18 downto 0):= "0000000111000001000";  -- x^12+x^11+x^10+x^4+1
  constant C_POLY13              : std_logic_vector(18 downto 0):= "0000001110010000000";  -- x^13+x^12+x^11+x^8+1
  constant C_POLY14              : std_logic_vector(18 downto 0):= "0000011100000000010";  -- x^14+x^13+x^12+x^2+1
  constant C_POLY15              : std_logic_vector(18 downto 0):= "0000110000000000000";  -- x^15+x^14+1
  constant C_POLY16              : std_logic_vector(18 downto 0):= "0001011010000000000";  -- x^16+x^14+x^13+x^11+1
  constant C_POLY17              : std_logic_vector(18 downto 0):= "0010010000000000000";  -- x^17+x^14+1
  constant C_POLY18              : std_logic_vector(18 downto 0):= "0100000010000000000";  -- x^18+x^11+1
  constant C_POLY19              : std_logic_vector(18 downto 0):= "1110010000000000000";  -- x^19+x^18+x^17+x^14+1

  type poly_array_t is array(2 to 19) of std_logic_vector(18 downto 0);


  constant C_POLY_ARRAY : poly_array_t := (
    2  => C_POLY2,
    3  => C_POLY3,
    4  => C_POLY4,
    5  => C_POLY5,
    6  => C_POLY6,
    7  => C_POLY7,
    8  => C_POLY8,
    9  => C_POLY9,
    10 => C_POLY10,
    11 => C_POLY11,
    12 => C_POLY12,
    13 => C_POLY13,
    14 => C_POLY14,
    15 => C_POLY15,
    16 => C_POLY16,
    17 => C_POLY17,
    18 => C_POLY18,
    19 => C_POLY19
);

  signal lfsr_r : std_logic_vector(G_LFSR_WIDTH-1 downto 0) := (others => '0');
  signal mask   : std_logic_vector(G_LFSR_WIDTH-1 downto 0);

begin

  -- Generate feedback mask
  gen_mask: for i in 0 to G_LFSR_WIDTH-1 generate
    mask(i) <= C_POLY_ARRAY(G_LFSR_WIDTH-1+2)(i) and lfsr_r(0);
  end generate;

  -- Process for LFSR operation
  p_lfsr: process(CLK)
  begin
    if rising_edge(CLK) then
      if (EN = '1') then
        lfsr_r <= '0' & lfsr_r(G_LFSR_WIDTH-1 downto 1) xor mask;
      end if;
      if (RST = '1') then
        lfsr_r <= SEED;         -- Load seed on synchronous reset
      end if;
    end if;
  end process;

  -- Output
  LFSR <= lfsr_r;



end architecture;
