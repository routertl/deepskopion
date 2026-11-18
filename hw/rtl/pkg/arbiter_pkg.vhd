-------------------------------------------------------------------------------
-- File       : arbiter_pkg.vhd
-- Standard   : VHDL 2008
-- Description: Package constants for the slim N-input round-robin arbiter
--              (post-D.4 strip, post-flatten 2026-05-17). The previous
--              t_arbiter_input_record / t_arbiter_output_record / t_array_*
--              types are gone — every port is now a flat std_logic_vector
--              so consumers don't need wrapper flatteners and the chain
--              is uniform AXI-Stream-style ready/valid throughout.
--              All consumers instantiate entity tich.arbiter directly; the
--              package omits a component arbiter port(...) declaration so
--              package-level port parsing does not shadow the top entity's
--              ports during board-pinout materialization.
-------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
library tich;
use tich.datalogger_pkg.all;

package arbiter_pkg is

  constant C_INPUT_WIDTH : integer := C_DATALOGGER_MSG_WIDTH;  -- 32 (from datalogger_pkg)

end package arbiter_pkg;
