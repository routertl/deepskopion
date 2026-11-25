-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- Title      : deepskopion_legacy_functions_pkg
-- Project    : deepskopion
-------------------------------------------------------------------------------
-- DS-P2.35 (2026-05-16): the inner package name was renamed from
-- `functions_pkg` → `deepskopion_legacy_functions_pkg` to break a
-- name collision with rr-ddr-stream's
-- `libs/routertl/rr_ddr_stream/rtl/support/functions_pkg.vhd`. routertl's
-- DependencyResolver scans every .vhd file in `paths.sources` parent
-- dirs (not just sources.syn entries) when building its
-- package→library map; finding two `package functions_pkg` declarations
-- on disk left the rr-ddr-stream copy orphaned in xil_defaultlib and
-- broke `use rr.functions_pkg.all` at synth time.
--
-- This file is NOT in sources.syn (vestigial scaffolding from the
-- early project carve, never referenced). The rename keeps the file
-- on-disk per user request while neutralising the collision.
-------------------------------------------------------------------------------
-- File       : functions_pkg.vhd  (filename unchanged for git history)
-- Description: Legacy helper functions (package deepskopion_legacy_functions_pkg).
-- Author     :   <djmazure@routertl.com>
-- Standard   : VHDL-2008
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;
use ieee.std_logic_textio.all;
use std.textio.all;

package deepskopion_legacy_functions_pkg is

    -- Function declarations

    -- Overloaded adataloggergn_with_padding functions
    function adataloggergn_with_padding(a: unsigned; b: unsigned) return unsigned;
    function adataloggergn_with_padding(a: std_logic_vector; b: std_logic_vector) return std_logic_vector;
    function adataloggergn_with_padding(a: unsigned; b: std_logic_vector) return unsigned;
    function adataloggergn_with_padding(a: std_logic_vector; b: unsigned) return std_logic_vector;

    function get_burst_from_distance(distance: unsigned; bytes_per_beat : integer) return unsigned;
    function closest_power_of_two(number_in : unsigned) return unsigned;
    function and_reduce(input : std_logic_vector) return std_logic;
    function bigger(a: unsigned; b: unsigned) return unsigned;
    function bigger(a: std_logic_vector; b: std_logic_vector) return unsigned;


end package deepskopion_legacy_functions_pkg;

package body deepskopion_legacy_functions_pkg is

    -- Function definitions
    function adataloggergn_with_padding(a: unsigned; b: unsigned) return unsigned is
        variable result : unsigned(a'range);
    begin
        result := (others => '0');
        result(b'length-1 downto 0) := b;
        return result;
    end function adataloggergn_with_padding;

    function adataloggergn_with_padding(a: std_logic_vector; b: std_logic_vector) return std_logic_vector is
        variable result : std_logic_vector(a'range);
    begin
        result := (others => '0');
        result(b'length-1 downto 0) := b;
        return result;
    end function adataloggergn_with_padding;

    function adataloggergn_with_padding(a: std_logic_vector; b: unsigned) return std_logic_vector is
        variable result : std_logic_vector(a'range);
    begin
        result := (others => '0');
        result(b'length-1 downto 0) := std_logic_vector(b);
        return result;
    end function adataloggergn_with_padding;

    function adataloggergn_with_padding(a: unsigned; b: std_logic_vector) return unsigned is
        variable result : unsigned(a'range);
    begin
        result := (others => '0');
        result(b'length-1 downto 0) := unsigned(b);
        return result;
    end function adataloggergn_with_padding;

    function get_burst_from_distance(distance: unsigned; bytes_per_beat : integer) return unsigned is
        constant C_WIDTH_PER_BURST     : integer := integer(ceil(log2(real(bytes_per_beat))));
        constant C_MAX_AXI_BURST       : integer := 256;
        constant C_AXI_4K_BOUNDARY     : integer := 4096;
        constant C_MAX_AXI_BURST_WIDTH : integer := integer(ceil(log2(real(C_MAX_AXI_BURST)))); -- 8
        -- with 16 bytes per beat, 256 bursts
        constant C_MAX_DESIGN_BURST    : integer := C_AXI_4K_BOUNDARY/bytes_per_beat;
        --
        variable result : unsigned(C_MAX_AXI_BURST_WIDTH-1 downto 0) := (others => '0');
    begin
        result := (others => '0');
        -- get the usable bits from the range that would mark the burst size
        for i in 0 to C_MAX_AXI_BURST_WIDTH-1 loop
          result(i) := distance(i+C_WIDTH_PER_BURST);
        end loop;
        -- The bits that are above the burst size area might be signaling a bigger burst
        if distance(distance'length-1 downto C_MAX_AXI_BURST_WIDTH+C_WIDTH_PER_BURST) /= 0 then
          result := (others => '1');
        else
          -- As we want the result in bursts, if distance is 1, the burst is 0
          result := result -1;
        end if;
        -- ... but the burst size is limited not only by the AXI4 spec but also by the design
        -- if the number of bytes per beat is, e.g, 64, the maximum burst length is 64 (64x64=4096)
        if result >= C_MAX_DESIGN_BURST then
          -- As we want the result in bursts, if distance is 256, the burst is 255
          result := to_unsigned(C_MAX_DESIGN_BURST-1, C_MAX_AXI_BURST_WIDTH);
        end if;
        return result;
    end function get_burst_from_distance;

      -- Obtain the closest power of two lower than fifoEpRdDataCnt
  function closest_power_of_two(number_in : unsigned) return unsigned is
    variable sel_v  : std_logic := '0';
    variable log2_v : unsigned(number_in'length - 1 downto 0) := (others => '0');
  begin  -- process SEL_LEFTMOST_BIT
    log2_v := (others => '0');
    sel_v  := '0';
    for i in number_in'length - 1 downto 0 loop
      if number_in(i) = '1' then
        if sel_v = '0' then               -- only the first ocurrence
          log2_v(i) := '1';
          sel_v     := '1';
        end if;
      end if;
    end loop;  -- i
    return log2_v;
  end function closest_power_of_two;

  function and_reduce(input : std_logic_vector) return std_logic is
    variable result : std_logic := '0';
    begin
    for i in input'range loop
        result := result and input(i);
    end loop;
    return result;
    end function and_reduce;

    function bigger(a: unsigned; b: unsigned) return unsigned is
        variable result : unsigned(a'length-1 downto 0);
    begin
        if a >= b then
            result := a;
        else
            result := b;
        end if;
    return result;
    end function bigger;

    function bigger(a: std_logic_vector; b: std_logic_vector) return unsigned is
        variable result : unsigned(a'length-1 downto 0);
    begin
        if unsigned(a) >= unsigned(b) then
            result := unsigned(a);
        else
            result := unsigned(b);
        end if;
    return result;
    end function bigger;

end package body deepskopion_legacy_functions_pkg;