-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
-------------------------------------------------------------------------------
-- Title      : tb_datalogger_gather_probe
-- Project    : deepskopion (DS-P1.65)
-------------------------------------------------------------------------------
-- Description: Gather-only bench. Instantiates the generated
--              datalogger_gather and maps its EDGE port interface exactly as
--              deepskopion_core does (EDGE_ERR_CNTS / EDGE_MAX_IVALS /
--              EDGE_MAX_NEW, sized from datalogger_config_pkg), without the
--              core, so the gather's port contract is exercised on its own.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;

entity tb_datalogger_gather_probe is
  generic (
    G_SYS_CLK : natural := 100_000_000
  );
  port (
    CLK                : in  std_logic;
    RST                : in  std_logic;
    CAPTURE_PINS       : in  std_logic_vector(C_NUM_DATALOGGER_PINS - 1 downto 0);
    CONFIG             : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH - 1 downto 0);
    TIMESTAMP          : in  std_logic_vector(C_TIMESTAMP_WIDTH - 1 downto 0);
    DOUT_REQ           : in  std_logic;
    DOUT_PAYLOAD       : out std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0);
    DOUT_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    DOUT_MSG_TYPE      : out std_logic_vector(15 downto 0);
    DOUT_VALID         : out std_logic;
    EDGE_PORT_BITS     : out std_logic_vector(15 downto 0)
  );
end entity tb_datalogger_gather_probe;

architecture str of tb_datalogger_gather_probe is

  signal gather_fifo_csr  : t_fifoCsr;
  signal gather_proto_cnt : t_protoCounterArray(0 to C_NUM_PROTO_COUNTERS - 1);
  signal edge_err_cnts    : std_logic_vector(C_EDGE_DATALOGGERS*32-1 downto 0);
  signal edge_max_ivals   : std_logic_vector(C_EDGE_DATALOGGERS*C_EDGE_MAXFREQ_WIDTH-1 downto 0);
  signal edge_max_new     : std_logic_vector(C_EDGE_DATALOGGERS-1 downto 0);

begin

  u_gather : entity tich.datalogger_gather
    generic map (
      G_SYS_CLK => G_SYS_CLK
    )
    port map (
      CLK                => CLK,
      RST                => RST,
      CAPTURE_PINS       => CAPTURE_PINS,
      CONFIG             => CONFIG,
      TIMESTAMP          => TIMESTAMP,
      DOUT_PAYLOAD       => DOUT_PAYLOAD,
      DOUT_DATALOGGER_ID => DOUT_DATALOGGER_ID,
      DOUT_MSG_TYPE      => DOUT_MSG_TYPE,
      DOUT_VALID         => DOUT_VALID,
      DOUT_REQ           => DOUT_REQ,
      FIFO_CSR           => gather_fifo_csr,
      PROTO_COUNTERS     => gather_proto_cnt,
      EDGE_ERR_CNTS      => edge_err_cnts,
      EDGE_MAX_IVALS     => edge_max_ivals,
      EDGE_MAX_NEW       => edge_max_new
    );

  EDGE_PORT_BITS <= std_logic_vector(to_unsigned(
    edge_err_cnts'length + edge_max_ivals'length + edge_max_new'length, 16));

end architecture str;
