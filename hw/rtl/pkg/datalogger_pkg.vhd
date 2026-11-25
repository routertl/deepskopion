-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- File       : datalogger_pkg.vhd
-- Description: Shared constants, subtypes and array types of the data loggers.
-- Standard   : VHDL-2008
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;
library tich;
use tich.datalogger_config_pkg.all;

package datalogger_pkg is

  -----------------------------------------------------------------------------
  -- Constants
  -----------------------------------------------------------------------------
  constant C_TIMESTAMP_WIDTH  : integer := 32; -- Timestamp width in bits
  constant C_LEVEL_INJ_PINS   : integer := 8;
  constant C_DATALOGGER_ID_WIDTH : integer := 5;

  -- Total datalogger count (used for array sizing)
  -- NOTE: C_LEVEL_DRIVERS and C_TIMESTAMP_SYNCS are generated from datalogger_config_pkg
  constant C_NUM_DATALOGGERS     : integer :=
    C_I2C_DATALOGGERS +
    C_SPI_DATALOGGERS +
    C_UART_DATALOGGERS +
    C_I2S_DATALOGGERS +
    C_QSPI_DATALOGGERS +
    C_LEVEL_DRIVERS +
    C_EDGE_DATALOGGERS +
    C_TIMESTAMP_SYNCS;

  -- 2nd layer arbiter interface count (one per protocol type)
  type t_num_interfaces is (I2C, UART, EDGES, SPI, I2S, QSPI, LEVELS, TIMESTAMPS);
  constant C_NUM_INTERFACES : integer := t_num_interfaces'pos(t_num_interfaces'right) + 1;
  constant C_NUM_ARBITRERS  : integer := C_NUM_STREAMS + C_NUM_ARB_INPUTS;
  --
  constant C_DATALOGGER_MSG_WIDTH : integer := 32;
  --
  -- 1280 is Max multi frame size in number of words
  -- (256 is the max, for 3 UBS Lines, x 2 for 48 bits, x 2 when 48 bits))
  constant C_MAX_MF_SIZE       : integer := 256;
  constant C_MAX_MF_SIZE_WIDTH : integer := integer(ceil(log2(real(C_MAX_MF_SIZE))));

  -- Bit Indexes
  constant C_MULTIFRAME_START_BIT : natural := 30;
  constant C_CC_CONFIG_VALID_BIT  : natural := 31;
  constant C_META_DATA_FORMAT_BIT : natural := 25;

  constant C_TIMESTAMP_SYNC_BIT      : integer := 14; -- Both edges = 16384 us ticks
  constant C_TIMESTAMP_SLICE_LSB_BIT : integer := 18; -- 31:14 = upper 18 bits
  --
  subtype t_meta_fs_range      is natural range 27 downto 26;
  subtype t_meta_ch_mode_range is natural range 31 downto 28;
  --
  constant C_META2_MF_SIZE_VLD_BIT : natural := 31;
  subtype  t_meta2_mf_size_range is natural range C_MAX_MF_SIZE_WIDTH-1 downto 0;
  constant C_META2_MF_START_BIT : natural := 30;
  constant C_META2_MF_LEAVE_BIT : natural := 29;
  -- Can be divided by 8 as sizes are multiple of 8
  subtype t_arbiter_mf_size_range  is natural range C_MAX_MF_SIZE_WIDTH-1+4 downto 4;
  subtype t_arbiter_mf_index_range is natural range 24 downto 16;
  subtype t_arbiter_mf_packet_range is natural range 2 downto 0;


  -- Arbiter token
  constant C_TOKEN_HEADER         : std_logic_vector(31 downto 8) := x"ADD6E5";
    --
  constant C_TOKEN_DEFAULT_OFFSET : integer := 1;

  constant C_ERROR_WIDTH : integer := 2; -- 2 bits for error code
  --
  constant C_EDGE_TYPE_RISING  : integer := 0;
  constant C_EDGE_TYPE_FALLING : integer := 1;
  constant C_EDGE_TYPE_BOTH    : integer := 2;
  -----------------------------------------------------------------------------
  -- Types
  -----------------------------------------------------------------------------

  -- Every register to write has an associated valid signal
  type t_inout_bus_rec is record
    tri       : std_logic;
    bufferOut : std_logic;
    -- bufferIn  : std_logic; -- Need to wait for VHDL 2019 to get inout recors
  end record t_inout_bus_rec;

  type t_inout_bus_rec_array is array (natural range <>) of t_inout_bus_rec;

  type t_array_of_errors is array (C_NUM_DATALOGGERS - 1 downto 0) of std_logic_vector(C_ERROR_WIDTH - 1 downto 0);
  type t_array_of_messages is array (C_NUM_DATALOGGERS - 1 downto 0) of
    std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0);
  type t_token_msg_array is array (natural range <>) of std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0);

  type t_weights_array is array (natural range 0 to C_NUM_ARBITRERS-1) of std_logic_vector;

  -- connects the arbiter FIFO count to the UBS buffer count datalogger
  type t_fifo_buffer_cnt is array (natural range <>) of unsigned;

  -- connects the arbiter FIFO count to the UBS buffer count datalogger
  type t_event_payload_array is array (natural range <>) of std_logic_vector;
  -- connects the arbiter FIFO count to the UBS buffer count datalogger
  type t_id_source_array is array (natural range <>) of natural range 0 to C_NUM_DATALOGGERS-1;


  constant C_INIT_INOUT_REC : t_inout_bus_rec := (
    tri       => '0',
    bufferOut => '0'
    -- bufferIn  => '0' -- Need to wait for VHDL 2019 to get inout recors
  );

  function generate_token_msg_array(
    header       : std_logic_vector(23 downto 0);
    num_elements : integer;
    offset       : integer
  ) return t_token_msg_array;


end package datalogger_pkg;

package body datalogger_pkg is

  function generate_token_msg_array(
    header       : std_logic_vector(23 downto 0);
    num_elements : integer;
    offset       : integer
  ) return t_token_msg_array is
    variable result : t_token_msg_array(0 to num_elements - 1);
  begin
    for i in 0 to num_elements - 1 loop
      result(i) := header & std_logic_vector(to_unsigned(i+offset, 8));
    end loop;
    return result;
  end function;



end datalogger_pkg;
