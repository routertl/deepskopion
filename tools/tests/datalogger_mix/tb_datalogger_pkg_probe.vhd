-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
-------------------------------------------------------------------------------
-- Title      : tb_datalogger_pkg_probe
-- Project    : deepskopion (DS-P1.65)
-------------------------------------------------------------------------------
-- Description: Package-only probe. Analyses nothing but the generated
--              datalogger_config_pkg and exports the EDGE interface
--              constants the core and the gather size their EDGE ports and
--              signals with, so the cocotb bench can read them back for a
--              mix that has no EDGE instance.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_config_pkg.all;

entity tb_datalogger_pkg_probe is
  port (
    CLK                : in  std_logic;
    EDGE_MAXFREQ_WIDTH : out std_logic_vector(7 downto 0);
    EDGE_DATALOGGERS   : out std_logic_vector(7 downto 0)
  );
end entity tb_datalogger_pkg_probe;

architecture str of tb_datalogger_pkg_probe is
begin

  EDGE_MAXFREQ_WIDTH <= std_logic_vector(to_unsigned(C_EDGE_MAXFREQ_WIDTH, 8));
  EDGE_DATALOGGERS   <= std_logic_vector(to_unsigned(C_EDGE_DATALOGGERS, 8));

end architecture str;
