-- SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT

-------------------------------------------------------------------------------
-- Title      : edge_detector
-- Project    : 
-------------------------------------------------------------------------------
-- File       : edge_detector.vhd
-- Author     : Daniel  <daniel.mazure.external@tich.com>
-- Company    : RouteRTL SDK
-- Created    : 2024-11-18
-- Last update: 2024-11-18
-- Platform   : 
-- Standard   : VHDL 2008
-------------------------------------------------------------------------------
-- Description: Detects edges
-------------------------------------------------------------------------------
-- Copyright (c) 2024 
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2024-08-12  1.0      dmazure Created
-------------------------------------------------------------------------------

library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;

entity edge_detector is
  generic (
    G_EDGE_TYPE : integer := 0    -- 0: rising,  1: falling, 2: both
    );
  port (
    CLK : in  std_logic;
    R   : in  std_logic := '0';
    CE  : in  std_logic;
    D   : in  std_logic;                -- signal to monitor
    Q   : out std_logic
    );

end entity edge_detector;

architecture RTL of edge_detector is

  signal D_r : std_logic := '0';
  signal Q_r : std_logic := '0';

begin  -- architecture RTL

  process(CLK)
  begin
    if rising_edge(CLK) then
      if CE = '1' then
        D_r <= D;
        Q_r <= '1' when (D and not D_r) = '1' and G_EDGE_TYPE = 0 else -- rising edge
               '1' when (D_r and not D) = '1' and G_EDGE_TYPE = 1 else -- falling edge
               '1' when (D xor D_r)     = '1' and G_EDGE_TYPE = 2 else -- both edges
               '0';
      end if;  -- CE
      if R = '1' then
        D_r <= D;
        Q_r <= '0';
      end if;  -- R
    end if;  -- CLK
  end process;

  Q <= Q_r;

end architecture RTL;
