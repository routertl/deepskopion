-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- error_event_emitter.vhd  —  DS-P3.70 piece 3 (in-band error provenance)
-------------------------------------------------------------------------------
-- Turns an out-of-band, monotonically-rising loss counter (e.g. ERR_CNT_EDGE,
-- which DS-P3.24 made = the edge internal-FIFO lost-edge count) into a RATE-
-- LIMITED, COALESCED in-band zs_msg ERROR frame:
--
--     severity = ERROR | domain = G_DOMAIN | event_id = G_EVENT_ID
--     payload  = lost_count DELTA since the previous report
--
-- Why coalesce: the underlying loss is a per-cycle pulse — under heavy
-- backpressure thousands of edges drop per millisecond. Emitting a frame per
-- drop would flood the very ring that is backed up. Instead we emit at most one
-- frame per G_FLUSH_CYCLES, carrying the accumulated delta, so RouteWave can
-- render a single "N edges lost here (edge internal FIFO)" marker. No accounting
-- is lost: while a report waits for FR_READY, further drops accrue in ERR_CNT
-- and are captured by the NEXT report's delta.
--
-- This rides the framer's normal input (3:1 mux in deepskopion_top, ERROR
-- priority). Because the loss is UPSTREAM of the ring, the report itself
-- traverses the (not-necessarily-full) ring even while the datalogger's internal
-- FIFO overflows — exactly the provenance class an in-band frame can carry
-- (ring-full loss stays out-of-band via DDR_BACKPRESSURE_CNT).
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.zs_msg_pkg.all;   -- make_msg_type, C_ZS_SEV_ERROR, C_ZS_CLASS_STATUS

entity error_event_emitter is
  generic (
    G_DOMAIN       : std_logic_vector(3 downto 0) := C_ZS_DOMAIN_EDGE;
    G_EVENT_ID     : std_logic_vector(7 downto 0) := C_ZS_ERR_EVT_INTERNAL_FIFO_OVERFLOW;
    G_DATALOGGER_ID   : std_logic_vector(7 downto 0) := (others => '0');
    G_FLUSH_CYCLES : positive                     := 100_000  -- ~1 ms @ 100 MHz
  );
  port (
    CLK           : in  std_logic;
    RST           : in  std_logic;
    -- Monotonic running loss/error count for this provenance (wraps at 2^32).
    ERR_CNT       : in  std_logic_vector(31 downto 0);
    TIMESTAMP     : in  std_logic_vector(63 downto 0);
    -- Framer-input handshake (one ERROR frame per flush when ERR_CNT advanced).
    FR_VALID      : out std_logic;
    FR_READY      : in  std_logic;
    FR_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    FR_MSG_CLASS  : out std_logic_vector(7 downto 0);
    FR_MSG_TYPE   : out std_logic_vector(15 downto 0);
    FR_FLAGS      : out std_logic_vector(15 downto 0);
    FR_TIMESTAMP  : out std_logic_vector(63 downto 0);
    FR_PAYLOAD    : out std_logic_vector(31 downto 0);   -- lost_count delta
    FR_WORDS      : out std_logic_vector(7 downto 0)
  );
end entity error_event_emitter;

architecture rtl of error_event_emitter is
  signal flush_cnt_r : unsigned(31 downto 0) := (others => '0');
  signal last_cnt_r  : unsigned(31 downto 0) := (others => '0');  -- ERR_CNT at last report
  signal snap_r      : unsigned(31 downto 0) := (others => '0');  -- ERR_CNT snapshot for pending report
  signal delta_r     : unsigned(31 downto 0) := (others => '0');  -- lost_count to send
  signal ts_r        : std_logic_vector(63 downto 0) := (others => '0');
  signal pending_r   : std_logic := '0';
begin
  process (CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        flush_cnt_r <= (others => '0');
        last_cnt_r  <= (others => '0');
        snap_r      <= (others => '0');
        delta_r     <= (others => '0');
        ts_r        <= (others => '0');
        pending_r   <= '0';
      else
        -- Flush timer: every G_FLUSH_CYCLES, latch a coalesced report if the
        -- count advanced and no report is already in flight.
        if flush_cnt_r = to_unsigned(G_FLUSH_CYCLES - 1, flush_cnt_r'length) then
          flush_cnt_r <= (others => '0');
          if pending_r = '0' and unsigned(ERR_CNT) /= last_cnt_r then
            snap_r    <= unsigned(ERR_CNT);
            delta_r   <= unsigned(ERR_CNT) - last_cnt_r;   -- mod 2^32 wraparound
            ts_r      <= TIMESTAMP;
            pending_r <= '1';
          end if;
        else
          flush_cnt_r <= flush_cnt_r + 1;
        end if;

        -- Report handshake: advance the baseline only once the framer accepts.
        if pending_r = '1' and FR_READY = '1' then
          last_cnt_r <= snap_r;
          pending_r  <= '0';
        end if;
      end if;
    end if;
  end process;

  FR_VALID      <= pending_r;
  FR_DATALOGGER_ID <= G_DATALOGGER_ID;
  FR_MSG_CLASS  <= C_ZS_CLASS_STATUS;
  FR_MSG_TYPE   <= make_msg_type(C_ZS_SEV_ERROR, G_DOMAIN, G_EVENT_ID);
  FR_FLAGS      <= (others => '0');
  FR_TIMESTAMP  <= ts_r;
  FR_PAYLOAD    <= std_logic_vector(delta_r);
  FR_WORDS      <= std_logic_vector(to_unsigned(1, 8));
end architecture rtl;
