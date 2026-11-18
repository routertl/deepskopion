-------------------------------------------------------------------------------
-- File       : datalogger_records_pkg.vhd
-- Description: Stable structural types + constants for datalogger_gather and
--              its consumers. Hand-written, never generated — the
--              datalogger-builder owns counts/IDs/pin mappings, but record
--              shapes are a build-time invariant of the design.
--
--              Contract: docs/hardware/datalogger_gather_contract.md
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

-- No tich dependencies on purpose: this package only declares structural
-- types and width constants. Consumers (datalogger_gather, the future top
-- wrapper) bring their own `use tich.datalogger_pkg.all` for the message
-- width / timestamp width.

package datalogger_records_pkg is

  -- Width of the per-datalogger runtime CONFIG word selected via regbank.
  -- Single source of truth — every X_datalogger entity wires
  -- G_CONFIG_WIDTH => C_DATALOGGER_CONFIG_WIDTH.
  constant C_DATALOGGER_CONFIG_WIDTH : integer := 16;

  -- PS-facing fill-level counter width — matches a typical FIFO depth
  -- (1024 needs 11 bits; 16 is comfortable and 32-bit-aligned-friendly
  -- to pad). Kept narrow on purpose; the regbank exporter pads to 32.
  constant C_FIFO_LEVEL_WIDTH    : integer := 16;
  constant C_FIFO_OVERFLOW_WIDTH : integer := 32;  -- saturating overflow event counter

  --------------------------------------------------------------------------
  -- Per-FIFO status — exactly the signals the PS exporter and the
  -- internal arbiter back-pressure care about. Anything else is
  -- diagnostic noise and lives outside this record.
  --------------------------------------------------------------------------
  type t_fifoStatus is record
    full     : std_logic;  -- arbiter cannot accept more this cycle
    empty    : std_logic;  -- nothing to drain
    overflow : std_logic;  -- one-shot pulse on lost word
  end record t_fifoStatus;

  constant C_INIT_FIFO_STATUS : t_fifoStatus := (
    full     => '0',
    empty    => '1',
    overflow => '0'
  );

  --------------------------------------------------------------------------
  -- Per-path CSR. Three fields, all consumed:
  --   - fifoStatus      : live flags (telemetry_counters_spec.yml wires
  --                       .full into BURST_FIFO_FULL / STREAM_FIFO_FULL)
  --   - level           : current fill (PS reads via regbank)
  --   - overflowCnt     : saturating count of overflow events since reset
  --------------------------------------------------------------------------
  type t_fifoCsr is record
    fifoStatus   : t_fifoStatus;
    level        : std_logic_vector(C_FIFO_LEVEL_WIDTH-1 downto 0);
    overflowCnt  : std_logic_vector(C_FIFO_OVERFLOW_WIDTH-1 downto 0);
  end record t_fifoCsr;

  constant C_INIT_FIFO_CSR : t_fifoCsr := (
    fifoStatus  => C_INIT_FIFO_STATUS,
    level       => (others => '0'),
    overflowCnt => (others => '0')
  );

  --------------------------------------------------------------------------
  -- Per-protocol pkt / err counter bundle. datalogger_gather aggregates the
  -- handshake pulses across every instance of a given protocol type, so
  -- the top-level fan-in into telemetry_in is a single array indexed by
  -- the sorted-name C_PROTO_IDX_<NAME> constants the datalogger-builder
  -- emits into datalogger_config_pkg.vhd. DS-P3.26.
  --------------------------------------------------------------------------
  type t_protoCounter is record
    pkt_cnt  : std_logic_vector(31 downto 0);
    err_cnt  : std_logic_vector(31 downto 0);
    drop_cnt : std_logic_vector(31 downto 0);  -- DS-T2.22: VLD && !RDY backpressure
  end record t_protoCounter;

  constant C_INIT_PROTO_COUNTER : t_protoCounter := (
    pkt_cnt  => (others => '0'),
    err_cnt  => (others => '0'),
    drop_cnt => (others => '0')
  );

  type t_protoCounterArray is array (natural range <>) of t_protoCounter;

end package datalogger_records_pkg;

package body datalogger_records_pkg is
end package body datalogger_records_pkg;
