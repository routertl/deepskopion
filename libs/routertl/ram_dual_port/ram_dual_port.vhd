-- SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT

-------------------------------------------------------------------------------
-- Title      : ram_dual_port
-- Project    : ssi
------------------------------------------------------------------------------
-- File       : ram_dual_port.vhd
-- Author     : Daniel  <daniel.mazure.external@tich.com>
-- Company    : RouteRTL SDK
-- Created    : 2024-06-19
-- Last update: 2024-08-16
-- Platform   :
-- Standard   : VHDL 2008
-------------------------------------------------------------------------------
-- Description: Dual Clock RAM Distributed or BRAM depending on Generics
-- https://docs.amd.com/r/en-US/ug901-vivado-synthesis/RAM_STYLE
-------------------------------------------------------------------------------
-- Copyright (c) 2024 Daniel J.Mazure
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2024-08-16  1.0      dmazure Created
-------------------------------------------------------------------------------


library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;

entity ram_dual_port is -- dual port
  generic(
    G_WIDTH     : natural := 32; -- Data width
    G_DEPTH     : natural := 6;  -- Ram depth = 2^G_DEPTH
    G_REG_OUT   : string  := "false";
    G_RAM_STYLE : string  := "auto" -- distributed, block, ultra
    );
  port(
    CLKA  : in  std_logic;
    CLKB  : in  std_logic;
    WEA   : in  std_logic;
    ADDRA : in  std_logic_vector(G_DEPTH-1 downto 0);
    ADDRB : in  std_logic_vector(G_DEPTH-1 downto 0);
    DIA   : in  std_logic_vector(G_WIDTH-1 downto 0);
    DOA   : out std_logic_vector(G_WIDTH-1 downto 0);
    DOB   : out std_logic_vector(G_WIDTH-1 downto 0)
    );
end ram_dual_port;


architecture behavioral of ram_dual_port is

  -----------------------------------------------------------------------------
  -- Constants
  -----------------------------------------------------------------------------

  -----------------------------------------------------------------------------
  -- Types
  -----------------------------------------------------------------------------
  type t_RAM is array (natural range 0 to 2**G_DEPTH-1) of std_logic_vector(G_WIDTH-1 downto 0);  --type INT_ARRAY is array (integer range <>)

  -----------------------------------------------------------------------------
  -- Signals
  -----------------------------------------------------------------------------
  signal RAM                                 : t_RAM;
  signal wr_op                               : std_logic_vector(1 downto 0);
  signal wAddr, rAddr, ptrWnext, w_ptr_succ  : unsigned(G_DEPTH-1 downto 0);
  signal wData, dataA_r, dataB_r             : std_logic_vector(G_WIDTH-1 downto 0);

  -----------------------------------------------------------------------------
  -- Attributes
  -----------------------------------------------------------------------------
  attribute ram_style        : string;
  attribute ram_style of RAM : signal is G_RAM_STYLE;

begin

  wData <= DIA;

  wAddr <= unsigned(ADDRA);
  rAddr <= unsigned(ADDRB);

  pr_wr_clka: process(CLKA)
  begin
    if rising_edge(CLKA) then
      if WEA = '1' then
        RAM(to_integer(wAddr)) <= wData;  -- Write from port A
        dataA_r                <= wData;  -- r_data <= w_data --> write first mode
      else
        -- read port
        dataA_r <= RAM(to_integer(wAddr));  -- Force creation of Dual Port
      end if;
    end if;
  end process;


  pr_rd_clkb: process(CLKB)
  begin
    if rising_edge(CLKB) then
      -- read port
      dataB_r <= RAM(to_integer(rAddr));  -- Force creation of Dual Port
    end if;
  end process;

  GEN_OUTPUT_REG : if G_REG_OUT = "true" generate
    process(CLKA)
    begin
      if rising_edge(CLKA) then
        DOA <= dataA_r;
      end if;
    end process;
    --
    process(CLKB)
    begin
      if rising_edge(CLKB) then
        DOB <= dataB_r;
      end if;
    end process;
  --
  end generate GEN_OUTPUT_REG;

  GEN_OUTPUT : if G_REG_OUT = "false" generate
    DOA <= dataA_r;
    DOB <= dataB_r;
  end generate GEN_OUTPUT;


end behavioral;
