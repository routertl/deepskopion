-- SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT

-------------------------------------------------------------------------------
-- Title      : infilter
-- Project    : 
-------------------------------------------------------------------------------
-- File       : infilter.vhd
-- Author     : Daniel  <daniel.mazure.external@tich.com>
-- Company    : RouteRTL SDK
-- Created    : 2024-08-16
-- Last update: 2024-09-04
-- Platform   : 
-- Standard   : VHDL 2008
-------------------------------------------------------------------------------
-- Description: Infilter
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
use ieee.std_logic_misc.all;

entity infilter is
  generic(
    G_DEB_CYCLES : integer := 3
    );
  port (
    CLK       : in  std_logic;
    CE        : in  std_logic;
    PULSE_IN  : in  std_logic;
    PULSE_OUT : out std_logic := '0'
    );
end infilter;

architecture RTL of infilter is

  constant C_CNT_WIDTH : integer := integer(ceil(log2(real(G_DEB_CYCLES))));

  signal dataPath_r : std_logic_vector(G_DEB_CYCLES-1 downto 0) := (others => '0');

begin

  PR_DEB : process(CLK)
    variable v_dataPathAnd : std_logic := '0';
    variable v_dataPathNor : std_logic := '0';
  begin
    if rising_edge(CLK) then
      if CE = '1' then
        dataPath_r <= dataPath_r(G_DEB_CYCLES-2 downto 0) & PULSE_IN;

        v_dataPathAnd := and_reduce(dataPath_r);     -- '1' when all '1'
        v_dataPathNor := not or_reduce(dataPath_r);  -- '1' when all '0'

        if v_dataPathAnd = '1' or v_dataPathNor = '1' then
          PULSE_OUT <= dataPath_r(G_DEB_CYCLES-1);
        end if;
      end if;
    end if;
  end process;

end RTL;
