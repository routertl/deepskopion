-- SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT

-------------------------------------------------------------------------------
-- Title      : ram_lut
-- Project    : 
-------------------------------------------------------------------------------
-- File       : ram_lut.vhd
-- Author     : Daniel  <daniel.mazure.external@tich.com>
-- Company    : 
-- Created    : 2024-06-19
-- Last update: 2024-08-16
-- Platform   : 
-- Standard   : VHDL 2008
-------------------------------------------------------------------------------
-- Description: Lut-based RAM
-------------------------------------------------------------------------------
-- Copyright (c) 2024 
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2024-08-16  1.0      dmazure Created
-------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;


entity ram_lut is
  generic(
    G_BITS    : natural := 32;          -- Bits
    G_DEPTH   : natural := 6           -- Wide
    );
  port(
    CLK  : in  std_logic;
    RST  : in  std_logic;
    WE   : in  std_logic;
    ADDR : in  std_logic_vector(G_DEPTH-1 downto 0);
    DIA  : in  std_logic_vector(G_BITS-1 downto 0);
    DOA  : out std_logic_vector(G_BITS-1 downto 0)
    );
end ram_lut;

architecture behavioral of ram_lut is

  ------------------------------------------------------------------------------
  -- types
  ------------------------------------------------------------------------------
  type t_lut is array (0 to G_DEPTH-1) of std_logic_vector(G_BITS-1 downto 0);

  signal ram_lut_r : t_lut := (others => (others => '0'));

begin  -- architecture behavioral

  PR_WRITE : process(CLK)
  begin
    if rising_edge(CLK) then
      DOA <= ram_lut_r(to_integer(unsigned(ADDR)));
      if WE = '1' then
        ram_lut_r(to_integer(unsigned(ADDR))) <= DIA;
      end if;
    end if;
  end process;

end architecture behavioral;
