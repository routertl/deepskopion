-- SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT

-------------------------------------------------------------------------------
-- Title      : edge_counter
-- Project    : 
-------------------------------------------------------------------------------
-- File       : edge_counter.vhd
-- Author     : Daniel  <daniel.mazure.external@tich.com>
-- Company    : RouteRTL SDK
-- Created    : 2024-08-16
-- Last update: 2024-09-05
-- Platform   : 
-- Standard   : VHDL 2008
-------------------------------------------------------------------------------
-- Description: Counts rising, falling or both edges on a signal
-------------------------------------------------------------------------------
-- Copyright (c) 2024 
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2024-08-12  1.0      dmazure Created
-- 2026-02-19  2.0      dmazure Refactor: integer generic (aligned w/ edge_detector)
-------------------------------------------------------------------------------

library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;

entity edge_counter is
  generic (
    G_CNT_WIDTH : integer := 32;
    G_EDGE_TYPE : integer := 0          -- 0: rising,  1: falling, 2: both
    );
  port (
    CLK        : in  std_logic;
    RST        : in  std_logic;
    D          : in  std_logic;         -- signal to monitor
    EDGE_COUNT : out std_logic_vector(G_CNT_WIDTH-1 downto 0)
    );
end entity edge_counter;

architecture RTL of edge_counter is

  signal cnt_r : unsigned(G_CNT_WIDTH-1 downto 0) := (others => '0');
  signal D_r   : std_logic                        := '0';

begin  -- architecture RTL

  PR_EDGE_CNT : process(CLK)
  begin
    if rising_edge(CLK) then
      D_r <= D;
      if G_EDGE_TYPE = 1 then
        -- Falling edge
        if D_r = '1' and D = '0' then
          cnt_r <= cnt_r + 1;
        end if;
      elsif G_EDGE_TYPE = 2 then
        -- Both edges
        if D_r /= D then
          cnt_r <= cnt_r + 1;
        end if;
      else
        -- Rising edge (default)
        if D_r = '0' and D = '1' then
          cnt_r <= cnt_r + 1;
        end if;
      end if;
      if RST = '1' then
        cnt_r <= (others => '0');
        D_r   <= D;
      end if;
    end if;
  end process;

  EDGE_COUNT <= std_logic_vector(cnt_r);

end architecture RTL;
