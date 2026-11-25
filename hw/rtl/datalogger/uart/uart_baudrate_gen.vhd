-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : uart_frame_gen.vhd
-- Project    : deepskopion
-------------------------------------------------------------------------------
-- File       : uart_frame_gen.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    : RouteRTL
-- Created    : 2025-11-05
-- Last update: 2025-11-05
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: uart_frame_gen, generates a frame from sampling the wave
--              with the input baudrate
-------------------------------------------------------------------------------
-- Copyright (c) Daniel J. Mazure 2025
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2025-11-11  1.0      dmazure Created
-------------------------------------------------------------------------------


library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;

entity uart_baudrate_gen is
  generic (
    G_DIST_WIDTH : integer := 16
    );
  port (
    CLK       : in  std_logic;
    RST       : in  std_logic;
    ENABLE    : in  std_logic;
    PAUSE     : in  std_logic;
    BR_CYCLES : in  std_logic_vector(G_DIST_WIDTH-1 downto 0);
    TICK      : out std_logic
    );
end uart_baudrate_gen;

architecture rtl of uart_baudrate_gen is

    --
    constant C_MAX_UART_BITS : integer := 12; -- 1 start, 8 data, 1 parity, 2 stop
    constant C_PLAIN_OFFSET  : unsigned(C_MAX_UART_BITS-1 downto 0) := (others => '0');
    constant C_HALF_OFFSET   : unsigned(C_MAX_UART_BITS-1 downto 0) := x"555";
    --
    signal distCounter_r : unsigned(G_DIST_WIDTH-1 downto 0) := (others => '0');
    signal tick_r        : std_logic := '1';
    signal offset_r      : unsigned(C_MAX_UART_BITS-1 downto 0) := C_PLAIN_OFFSET;

begin

    pr_baudrate_gen : process(CLK)
    begin
        if rising_edge(CLK) then
            if ENABLE = '1' then
                if PAUSE = '0' then
                    distCounter_r <= distCounter_r + 1;
                    tick_r        <=  '0';
                    if distCounter_r = unsigned(BR_CYCLES(G_DIST_WIDTH-1 downto 1)) then
                        tick_r        <=  '1';
                    -- DS-P2.236: the bit period is exactly BR_CYCLES clocks
                    -- (counter 0 .. BR_CYCLES - 1). It was BR_CYCLES + 1, which
                    -- offset a recorder that dropped one tick per interval.
                    elsif distCounter_r = unsigned(BR_CYCLES) - 1 - offset_r(0) then
                        distCounter_r <= (others => '0');
                        offset_r      <= offset_r(C_MAX_UART_BITS-2 downto 0) & offset_r(C_MAX_UART_BITS-1);
                    end if;
                end if;
            else
                distCounter_r <= ((1) => '1', others => '0'); -- first time it starts with and offset to compensate the start detection delay
                tick_r        <= '0';
                if BR_CYCLES(0) = '1' then
                    offset_r <= C_PLAIN_OFFSET; -- C_HALF_OFFSET;
                else
                    offset_r <= C_PLAIN_OFFSET;
                end if;
            end if;
            if RST = '1' then
                distCounter_r <= (others => '0');
                tick_r        <= '0';
            end if;
        end if;
    end process pr_baudrate_gen;

    TICK <= tick_r;

end rtl;