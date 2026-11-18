-------------------------------------------------------------------------------
-- Title      : uart_pkg.vhd
-- Project    : deepskopion
-------------------------------------------------------------------------------
-- File       : uart_pkg.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    : RouteRTL
-- Created    : 2025-11-05
-- Last update: 2025-11-05
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: UART Datalogger. Creates a datalogger message when UART data is
--              received. The message contains the timestamp, the data and
--              status flags.
-------------------------------------------------------------------------------
-- Copyright (c) Daniel J. Mazure 2025
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2025-11-05  1.0      dmazure Created
-------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;

package uart_pkg is

  constant C_TIMESTAMP_WIDTH              : integer := 16;
  -- DS-P2.154: the system clock is NOT a package-wide truth — it differs per
  -- target (100 MHz Zynq/PFSoC, 50 MHz DE25 CLOCK2_50). Every clock-derived
  -- value an instance uses must come from the f_* functions below, fed with
  -- the instance's real clock (G_SYSTEM_CLK_FREQ, threaded from G_SYS_CLK_HZ
  -- at the top, as DS-T1.10 did for the gather/glitch-filter path).
  -- C_SYSTEM_CLK_FREQ remains ONLY as the 100 MHz reference for the legacy
  -- constants below (standalone elaboration defaults); do not use it to size
  -- anything that must be correct on a non-100 MHz target.
  constant C_SYSTEM_CLK_FREQ              : integer := 100_000_000; -- 100 MHz reference default
  constant C_UART_MAX_BAUDRATE            : integer := 1000000;  -- 1 Mbps (validated range 9600..1M @ 100 MHz)
  constant C_UART_MIN_BAUDRATE            : integer := 9600;    -- 9.6 Kbps
  constant C_MAX_UART_BITS                : integer := 12;      -- 1 start, 9 data, 1 parity, 2 stop
  -- Prescaler compresses stored edge distances. DS-T2.40: it sets the sampler
  -- resolution = (clk/max_baud)/prescaler ticks per bit. Prescaler 8 left only
  -- 12.5 ticks/bit at 1 Mbps -> too coarse, calibration never locked. Prescaler 2
  -- gave the resolution but its finer buffer timing degraded mid-baud autoSync.
  -- Prescaler 4 is the sweet spot: 25 ticks/bit at 1 Mbps (clean 1 Mbps decode)
  -- yet coarse enough to leave mid-baud calibration/autoSync intact (validated
  -- 9600..1M: payload_verify 15/15, scenarios@230400 24/24, 1M/921k/115200 clean).
  constant C_TICK_PRESCALER                : integer := C_SYSTEM_CLK_FREQ / (25 * C_UART_MAX_BAUDRATE); -- = 4
  -- constant C_MIN_BITWIDTH_UART_WIDTH      : integer := integer(ceil(log2(real(C_TICK_PRESCALER)))) + 1;
  constant C_MAX_BITWIDTH_UART_CYCLES     : integer := C_SYSTEM_CLK_FREQ / C_UART_MIN_BAUDRATE;
  constant C_MAX_BITWIDTH_UART_WIDTH      : integer := integer(ceil(log2(real(C_MAX_BITWIDTH_UART_CYCLES)))) + 1;
  constant C_MAX_RELATIVE_DISTANCE_CYCLES : integer := C_MAX_BITWIDTH_UART_CYCLES * C_MAX_UART_BITS; -- Max distance in clock cycles (used as reference)
  constant C_BUFFER_TIMEOUT_LIMIT         : integer := C_UART_MIN_BAUDRATE*16; -- Number of timestamp ticks since last edge before considering triggering calibration
  constant C_POSSIBLE_UART_CONFS          : integer := 5; -- 5-9 bits + parity when not 9 bits

  constant C_FIFO_DEPTH     : integer := 10; -- Number of edges we store
  constant C_BRAM_ADDR_INIT : unsigned(C_FIFO_DEPTH-1 downto 0) := (others => '1');

  ---------------------------------------------------------------------------
  -- Clock-parametrised derivation functions (DS-P2.154)
  -- Same formulas as the legacy constants above, but computed from the
  -- instance's REAL clock so a 50 MHz target derives prescaler 2 (keeping the
  -- validated 25 ticks/bit at C_UART_MAX_BAUDRATE) instead of inheriting the
  -- 100 MHz prescaler 4 (12.5 ticks/bit — the calibration-never-locked regime
  -- documented at C_TICK_PRESCALER).
  ---------------------------------------------------------------------------
  function f_tick_prescaler(clk_hz : integer) return integer;
  -- Resolve the effective prescaler: an explicit generic override (> 0) wins;
  -- 0 means "derive from the clock".
  function f_resolve_prescaler(explicit_prescaler : integer; clk_hz : integer) return integer;
  function f_max_bitwidth_uart_cycles(clk_hz : integer) return integer;
  function f_max_relative_distance_cycles(clk_hz : integer) return integer;

  ---------------------------------------------------------------------------
  -- Tick-based width computation functions
  -- These compute the minimum required bit-widths based on the actual
  -- clock and prescaler values, avoiding the over-provisioning of cycle-based
  -- constants. DS-P2.154: they take the clock too — the tick capacity is
  -- (clk-derived max distance)/prescaler, and sizing it from the 100 MHz
  -- C_MAX_RELATIVE_DISTANCE_CYCLES was wrong on any other clock.
  ---------------------------------------------------------------------------
  function f_dist_width(clk_hz : integer; prescaler : integer) return integer;
  function f_bram_width(clk_hz : integer; prescaler : integer) return integer;
  function f_fifo_width(clk_hz : integer; prescaler : integer) return integer;


  type t_wave is record
    level     : std_logic;
    timestamp : std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
  end record t_wave;

  constant C_INIT_WAVE : t_wave := (
    level        => '1',
    timestamp    => (others => '0')
    );

end package uart_pkg;


package body uart_pkg is

  function f_tick_prescaler(clk_hz : integer) return integer is
  begin
    -- 25 ticks per bit at the max supported baudrate — the validated sampler
    -- resolution (see the C_TICK_PRESCALER rationale above). 100 MHz -> 4,
    -- 50 MHz (DE25) -> 2.
    return clk_hz / (25 * C_UART_MAX_BAUDRATE);
  end function;

  function f_resolve_prescaler(explicit_prescaler : integer; clk_hz : integer) return integer is
  begin
    if explicit_prescaler > 0 then
      return explicit_prescaler;
    end if;
    return f_tick_prescaler(clk_hz);
  end function;

  function f_max_bitwidth_uart_cycles(clk_hz : integer) return integer is
  begin
    return clk_hz / C_UART_MIN_BAUDRATE;
  end function;

  function f_max_relative_distance_cycles(clk_hz : integer) return integer is
  begin
    return f_max_bitwidth_uart_cycles(clk_hz) * C_MAX_UART_BITS;
  end function;

  function f_dist_width(clk_hz : integer; prescaler : integer) return integer is
    variable max_ticks : integer;
  begin
    max_ticks := f_max_relative_distance_cycles(clk_hz) / prescaler;
    return integer(ceil(log2(real(max_ticks)))) + 1;
  end function;

  function f_bram_width(clk_hz : integer; prescaler : integer) return integer is
  begin
    return 1 + f_dist_width(clk_hz, prescaler); -- 1 level bit + distance
  end function;

  function f_fifo_width(clk_hz : integer; prescaler : integer) return integer is
  begin
    return C_TIMESTAMP_WIDTH + 1 + f_dist_width(clk_hz, prescaler); -- timestamp + level + distance
  end function;

end uart_pkg;
