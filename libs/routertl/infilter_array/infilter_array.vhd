-- SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT

-------------------------------------------------------------------------------
-- Title      : infilter_array
-- Project    : 
-------------------------------------------------------------------------------
-- File       : infilter_array.vhd
-- Author     : Daniel  <daniel.mazure.external@tich.com>
-- Company    : RouteRTL SDK
-- Created    : 2024-08-16
-- Last update: 2024-08-21
-- Platform   : 
-- Standard   : VHDL 2008
-------------------------------------------------------------------------------
-- Description: Instantiates multiple infilters
-------------------------------------------------------------------------------
-- Copyright (c) 2024 
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2024-08-21  1.0      dmazure Created
-------------------------------------------------------------------------------

library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;
use IEEE.math_real.all;
library tich;



entity infilter_array is
  generic(
    G_DEB_CYCLES          : integer := 32;
    G_ARRAY_SIZE          : integer := 32
    );
  port (
    CLK       : in  std_logic;
    CE        : in  std_logic;
    PULSE_IN  : in  std_logic_vector(G_ARRAY_SIZE-1 downto 0);
    PULSE_OUT : out std_logic_vector(G_ARRAY_SIZE-1 downto 0)
    );
end infilter_array;

architecture RTL of infilter_array is

begin  -- architecture RTL

  GEN_INFILTER_ARRAY : for i in 0 to G_ARRAY_SIZE-1 generate
    i_infilter : entity tich.infilter
      generic map (
        G_DEB_CYCLES => G_DEB_CYCLES
        )
      port map (
        CLK       => CLK,
        CE        => CE,
        PULSE_IN  => PULSE_IN(i),
        PULSE_OUT => PULSE_OUT(i)
        );
  end generate GEN_INFILTER_ARRAY;

end architecture RTL;
