-------------------------------------------------------------------------------
-- Title      : edge_datalogger
-- Project    : datalogger
-------------------------------------------------------------------------------
-- File       : edge_datalogger.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    :
-- Created    : 2025-06-18
-- Last update: 2025-06-18
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: Edge detector
-------------------------------------------------------------------------------
-- Copyright (c) 2025 RouteRTL
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2025-06-18  1.0      dmazure Created
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use IEEE.MATH_REAL.all;
use ieee.std_logic_misc.all;

library tich;
use tich.datalogger_pkg.all;

entity edge_datalogger is
  generic (
    G_ED_ID_WIDTH     : integer                                     := 5;
    G_ED_DEV_ID       : natural range 0 to (2 ** G_ED_ID_WIDTH) - 1 := 28;
    G_TIMESTAMP_WIDTH : natural range 16 to 32                      := 16;
    G_CONFIG_WIDTH    : integer                                     := 16;
    G_ERROR_WIDTH     : integer                                     := 2;
    G_ENABLE_INFILTER : integer                                     := 0;
    G_ILA_EN          : integer                                     := 0;
    -- DS-P3.117: max-frequency (min edge-to-edge interval) tracker.
    --   G_MAXFREQ_WIDTH       : width of the min-interval register in CLK ticks.
    --                           A real line's half-period (in clocks) must fit;
    --                           24 bits @100 MHz spans down to ~6 Hz half-period.
    --   G_MAXFREQ_MIN_INTERVAL: GLITCH FLOOR. An edge-to-edge interval STRICTLY
    --                           below this many clocks is treated as glitch
    --                           residue and CANNOT latch a new max — it is
    --                           rejected, not stuck. Pairs with the gather's
    --                           glitch_filter (DS-T3.5): a half-period below the
    --                           filter's rejection window is not a real edge.
    --                           Floor of 2 mirrors glitch_filter's own floor.
    --   G_MAXFREQ_CONFIRM     : CONFIRMATION COUNT N (DS-P3.117). A new max is
    --                           only latched once N CONSECUTIVE intervals are
    --                           all at or below it: the value offered is the
    --                           LARGEST of the last N intervals. A runt pulse
    --                           that clears the floor still splits one
    --                           half-period into 3 short pieces, so with N >= 4
    --                           every window of N intervals holds a full
    --                           half-period and the runt cannot raise the max
    --                           (a burst of k runts in one half-period needs
    --                           N >= 2k+2). The price: genuine activity faster
    --                           than the running max must last N intervals to
    --                           register. N = 1 is the single-interval tracker.
    G_MAXFREQ_WIDTH        : integer range 4 to 32 := 24;
    G_MAXFREQ_MIN_INTERVAL : integer               := 2;
    G_MAXFREQ_CONFIRM      : integer range 1 to 8  := 1;
    -- DS-P2.16: Stream-RLE tick-counter width. A gap of 2**G_RLE_CNT_WIDTH - 1
    -- ticks with no edge emits an IDLE word; 24 (the payload width) is the only
    -- production value. Narrower is for simulation, so an IDLE is reachable.
    G_RLE_CNT_WIDTH        : integer range 9 to 24 := 24
  );
  port (
    CLK       : in std_logic;
    RST       : in std_logic;
    EDGE_IN   : in std_logic;
    CONFIG    : in std_logic_vector(G_CONFIG_WIDTH - 1 downto 0);
    TIMESTAMP : in std_logic_vector(G_TIMESTAMP_WIDTH - 1 downto 0);
    -- QSPI
    ED_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0) := (others => '0');
    ED_MSG_VLD : out std_logic;
    ED_MSG_RDY : in std_logic;
    -- DS-T2.84: '1' while ED_MSG_OUT carries the ENABLED/DISABLED lifecycle
    -- frame, '0' for edge events and readbacks. The frame's type "100" is
    -- bit-identical to an S=1/E=0 readback, so the word alone cannot say which
    -- it is; the gather tags it EVT_EDGE_LIFECYCLE from this bit and the core
    -- frames it on its own instead of batching it onto EDGE_EVENTS.
    ED_MSG_LIFECYCLE : out std_logic := '0';
    -- DS-P2.16: '1' while ED_MSG_OUT carries a Stream-RLE word (SYNC / PACK /
    -- LONG / IDLE, see the RLE block below), '0' for event-mode words and the
    -- lifecycle frame. The RLE type codes reuse bits [26:24], which an
    -- event-mode word spends on S/F/E, so the word alone cannot say which it
    -- is. The gather tags these words EVT_EDGE_RLE and the core frames each on
    -- its own instead of batching it onto EDGE_EVENTS.
    ED_MSG_STREAM : out std_logic := '0';
    --
    ED_ERRORS : out std_logic_vector(G_ERROR_WIDTH - 1 downto 0);
    -- DS-P3.117: max-frequency telemetry (out-of-band side channel). The FPGA is
    -- authoritative — it measures the SMALLEST validated edge-to-edge interval
    -- seen on the line since the last re-arm. freq_max = G_SYS_CLK / (2 * value)
    -- (a full square-wave period is two half-periods). ED_MAX_INTERVAL holds the
    -- min interval in CLK ticks (all-ones = "no edge measured yet / re-armed").
    -- ED_MAX_NEW pulses one CLK each time a NEW (smaller) min interval is latched
    -- — event-driven, so a downstream CSR/in-band emitter need not poll. Both
    -- have safe defaults so legacy instantiations (e.g. older datalogger_gather)
    -- may leave them unconnected.
    ED_MAX_INTERVAL : out std_logic_vector(G_MAXFREQ_WIDTH - 1 downto 0) := (others => '1');
    ED_MAX_NEW      : out std_logic                                       := '0'
  );
end edge_datalogger;


architecture rtl of edge_datalogger is

  ------------------------------------------------------------
  -- Constants
  ------------------------------------------------------------
  constant C_TIMESTAMP_PART_WIDTH : integer := 16;
  constant C_FIFO_DEPTH           : integer := 8;
  -- [27] = Stream-RLE word flag (DS-P2.16), [26:0] = message body.
  constant C_FIFO_WIDTH           : integer := 28;
  --
  constant C_ENABLE_EDGE_CAPTURING_CODE     : std_logic_vector(1 downto 0) := "01";
  constant C_ENABLE_READ_TRIGGER_OFF_CODE  : std_logic_vector(1 downto 0) := "11"; -- This code allows reading and stops the datalogger
  constant C_ENABLE_READ_TRIGGER_ON_CODE   : std_logic_vector(1 downto 0) := "10"; -- This code allows reading without stopping the datalogger
  -- DS-T2.43: uniform enable/disable lifecycle frame, emitted via
  -- tich.datalogger_lifecycle exactly like i2c/uart. The frame reuses the edge
  -- message body layout — type[26:24] & timestamp[23:8] & code[7:0] — so it
  -- threads the gather's payload[26:24] event-id extraction like any other
  -- edge event. edge_datalogger previously emitted NO lifecycle frame, which made
  -- DS-P3.90's "uniform lifecycle across all dataloggers" false for the EDGE
  -- channel (the host could not observe an EDGE channel being armed/disarmed).
  constant C_ED_TYPE_EVENT     : std_logic_vector(2 downto 0) := "100";
  constant C_ED_EVENT_ENABLED  : std_logic_vector(7 downto 0) := x"03";
  constant C_ED_EVENT_DISABLED : std_logic_vector(7 downto 0) := x"00";
  -- FIFO signals
  signal fifoEdDin        : std_logic_vector(C_FIFO_WIDTH - 1 downto 0);
  signal fifoEdDout       : std_logic_vector(C_FIFO_WIDTH - 1 downto 0);
  signal fifoEdWrEn       : std_logic;
  signal fifoEdRdEn       : std_logic;
  signal fifoEdEmpty      : std_logic;
  signal fifoEdAlmostFull : std_logic;
  signal fifoEdRstBusy    : std_logic;
  signal fifoEdFull       : std_logic;
  signal timeStamp_i      : std_logic_vector(C_TIMESTAMP_PART_WIDTH - 1 downto 0);

  signal deviceLoadConfig : std_logic_vector(G_CONFIG_WIDTH - G_ED_ID_WIDTH - 1 downto 0);
  signal configEnable     : std_logic;
  signal triggerRead      : std_logic;
  signal triggerRead_r    : std_logic := '0';
  signal ReadEvent        : std_logic;
  signal ReadEvent_r      : std_logic := '0';
  -- DS-T2.84: polarity and timestamp of an edge that coincided with a read.
  -- The readback takes that cycle's FIFO write; the edge is written one cycle
  -- later from these registers, as an S=0 edge event with its own time.
  signal deferredE_r      : std_logic := '0';
  signal deferredTs_r     : std_logic_vector(23 downto 0) := (others => '0');

  signal edgeDetected : std_logic;
  -- The detector's state is defined (DS-P1.113 run9/run10 L-3): RST clears it to its init value (D_r = 0, Q_r = 0),
  -- and a detection it held from before an enable is never written. edgeQ is the raw detector output; edgeDetected
  -- is edgeQ in a cycle whose detection was made while the logger was enabled (configEnable_r).
  signal edgeQ          : std_logic;
  signal edgeD          : std_logic;
  signal configEnable_r : std_logic := '0';
  signal f_bit        : std_logic;
  signal e_bit        : std_logic;
  signal s_bit        : std_logic;

  -- DS-P3.24: internal-FIFO-overflow loss flag. Pulses for one CLK each time a
  -- detected edge is DROPPED because the FIFO is full (fifoEdWrEn suppressed by
  -- the `fifoEdFull` branch below). Registered, then exported on ED_ERRORS(0);
  -- the gather sums these pulses into ERR_CNT_EDGE (= the lost-edge count).
  signal edgeLost_r : std_logic := '0';

  -- DS-T2.43: lifecycle (ENABLED/DISABLED) frame, priority-muxed ahead of the
  -- FIFO output (same mechanism as i2c_datalogger / uart_datalogger Phase 1).
  signal lifecycleMsg : std_logic_vector(C_DATALOGGER_MSG_WIDTH - 1 downto 0);
  signal lifecycleVld : std_logic;

  -- DS-P3.117: max-frequency (min edge-to-edge interval) tracker.
  -- The interval counter ticks once per CLK and resets to 0 on a validated
  -- edge. min_interval_r latches the SMALLEST validated interval seen since the
  -- last re-arm. All-ones is the sentinel for "no max yet" (so the first real
  -- interval always beats it). reArm_r is the rising-edge-detected host re-ping.
  constant C_MAXFREQ_ALLONES : unsigned(G_MAXFREQ_WIDTH - 1 downto 0) :=
    (others => '1');
  signal interval_cnt_r  : unsigned(G_MAXFREQ_WIDTH - 1 downto 0) := (others => '0');
  signal min_interval_r  : unsigned(G_MAXFREQ_WIDTH - 1 downto 0) := (others => '1');
  signal maxNew_r        : std_logic := '0';
  signal reArm           : std_logic;
  signal reArm_prev_r    : std_logic := '0';
  signal sawFirstEdge_r  : std_logic := '0';
  -- DS-P3.117 confirmation window: the last G_MAXFREQ_CONFIRM-1 closed
  -- intervals (newest first) and how many intervals have closed since the window
  -- opened, saturating at G_MAXFREQ_CONFIRM. Sized at least 1 so N = 1 does not
  -- declare a null array.
  constant C_MAXFREQ_HIST : positive := maximum(1, G_MAXFREQ_CONFIRM - 1);
  type t_maxfreq_hist is array (0 to C_MAXFREQ_HIST - 1)
    of unsigned(G_MAXFREQ_WIDTH - 1 downto 0);
  signal ivalHist_r      : t_maxfreq_hist := (others => (others => '0'));
  signal ivalClosed_r    : integer range 0 to G_MAXFREQ_CONFIRM := 0;

  -- DS-P2.16: Stream-RLE (differential) mode. Selected by CONFIG[4:3] = "10"
  -- (deviceLoadConfig[4:3]); "00" is the event mode, "01"/"11" are reserved and
  -- behave as event mode. CONFIG[2] is taken by the max-freq re-arm, so the
  -- selector sits one bit higher than the [3:2] the v2 FDD sketched. The mode is
  -- latched on the enable cycle; a change while enabled applies on the next
  -- enable, so one capture never mixes the two word formats.
  constant C_MODE_RLE      : std_logic_vector(1 downto 0) := "10";
  -- Stream-RLE word types, bits [26:24] of a word with ED_MSG_STREAM = '1'.
  constant C_RLE_SYNC_LO   : std_logic_vector(2 downto 0) := "000";  -- anchor, line low
  constant C_RLE_SYNC_HI   : std_logic_vector(2 downto 0) := "001";  -- anchor, line high
  constant C_RLE_PACK      : std_logic_vector(2 downto 0) := "101";  -- 3 x 8-bit deltas
  constant C_RLE_LONG      : std_logic_vector(2 downto 0) := "110";  -- 1 edge, 24-bit delta
  constant C_RLE_IDLE      : std_logic_vector(2 downto 0) := "111";  -- advance, no edge
  constant C_RLE_SHORT_MAX : natural := 255;
  constant C_RLE_CNT_MAX   : unsigned(G_RLE_CNT_WIDTH - 1 downto 0) := (others => '1');

  signal modeSel     : std_logic_vector(1 downto 0);
  signal enable_r    : std_logic := '0';
  signal rleEn_r     : std_logic := '0';  -- this capture is Stream-RLE
  signal rleSyncPend_r : std_logic := '0';  -- next free cycle writes a SYNC
  signal rleCnt_r    : unsigned(G_RLE_CNT_WIDTH - 1 downto 0) := (others => '0');  -- ticks since last reference
  signal rleNSlot_r  : natural range 0 to 2 := 0;  -- deltas held in rleSlot*_r
  signal rleSlot0_r  : std_logic_vector(7 downto 0) := (others => '0');
  signal rleSlot1_r  : std_logic_vector(7 downto 0) := (others => '0');
  signal edgeIn_r    : std_logic := '0';
  signal rleWr       : std_logic;  -- the compressor wants a FIFO write this cycle
  signal rleWord     : std_logic_vector(26 downto 0);
  signal rleDrop     : std_logic;  -- ... and it is lost (FIFO full)
  signal rleLost_r   : std_logic := '0';
  signal eventPathOff : std_logic;  -- the event-mode writers are muted


begin

  -- Register the configuration values
  i_config_control : entity tich.config_control
    generic map (
      G_DEV_ID_WIDTH => G_ED_ID_WIDTH,
      G_DEV_ID       => G_ED_DEV_ID,
      G_CONFIG_WIDTH => G_CONFIG_WIDTH
      )
    port map (
      CLK        => CLK,
      RST        => RST,
      CONFIG     => CONFIG,
      CONFIG_OUT => deviceLoadConfig,
      CAPTURE_RST  => open  -- DS-T2.31: per-datalogger reset not yet adopted here
      );

  --
  configEnable <= '1' when deviceLoadConfig(1 downto 0) = C_ENABLE_EDGE_CAPTURING_CODE else
                  '1' when deviceLoadConfig(1 downto 0) = C_ENABLE_READ_TRIGGER_ON_CODE else '0';
  triggerRead  <= '1' when deviceLoadConfig(1 downto 0) = C_ENABLE_READ_TRIGGER_OFF_CODE else
                  '1' when deviceLoadConfig(1 downto 0) = C_ENABLE_READ_TRIGGER_ON_CODE else '0';

  -- DS-P3.117: host-triggerable MAX-FREQ RE-PING / RE-ARM. CONFIG[2] (the first
  -- of the previously-reserved deviceLoadConfig[10:2] bits, edge_datalogger_protocol
  -- .yml config.reserved) clears the latched max so the host can recompute over a
  -- fresh window — the mandatory stale-latch escape. Level here, rising-edge
  -- detected in the tracker process so a held bit re-arms exactly once.
  reArm <= deviceLoadConfig(2);
  modeSel <= deviceLoadConfig(4 downto 3);

  -- Lifecycle messages retain their historical 16-bit timestamp field. Edge
  -- records carry the low 24 bits of the authoritative sample-clock counter;
  -- the production batcher reconstructs the rollover-safe 64-bit value.
  timeStamp_i <= TIMESTAMP(C_TIMESTAMP_PART_WIDTH - 1 downto 0);

  -- Detect the edge on the input signal.
  -- edge_detector updates D_r/Q_r only while CE = 1, so without a reset they hold whatever they had (power-up state,
  -- or a rise sampled in the last enabled cycle before a disable). Held Q_r = 1 / D_r = 1 at an enable with the line
  -- idle low wrote two level-0 records one tick apart. R => RST with D forced to 0 during RST clears both to 0 (R loads
  -- D_r <= D, Q_r <= 0); the line-high first enable after RST still writes its rise. The detection in the first enabled
  -- cycle was made before the enable, so it is masked by configEnable_r; a re-enable then writes at most the net level
  -- change since the last enabled sample, and a re-enable on a line held high writes nothing, as before.
  edgeD <= EDGE_IN and not RST;

  i_edge_event : entity tich.edge_detector
    generic map(
      G_EDGE_TYPE => C_EDGE_TYPE_BOTH
    )
    port map
    (
      CLK => CLK,
      R   => RST,
      CE  => configEnable,
      D   => edgeD,
      Q   => edgeQ
    );

  pr_enable_r : process (CLK)
  begin
    if rising_edge(CLK) then
      configEnable_r <= configEnable;
      if RST = '1' then
        configEnable_r <= '0';
      end if;
    end if;
  end process pr_enable_r;

  edgeDetected <= edgeQ and configEnable_r;


  -- Detect the Read command
  i_read_event : entity tich.edge_detector
    generic map(
      G_EDGE_TYPE => C_EDGE_TYPE_RISING
    )
    port map
    (
      CLK => CLK,
      CE  => '1',
      D   => triggerRead,
      Q   => ReadEvent
    );

  -- An edge that coincides with a read is deferred one cycle (the readback owns
  -- the write slot). DS-T2.84: the deferred word is an EDGE event — S=0, the
  -- polarity and timestamp of the cycle the edge was detected. It used to be
  -- written with S=1 and E = not EDGE_IN, i.e. as a second readback reporting
  -- the inverted level (silicon: sparse CS word 0x343cfa24 right after the
  -- true 0x353cfa1f readback).
  p_deferred_edge : process (CLK)
  begin
    if rising_edge(CLK) then
      ReadEvent_r <= ReadEvent and edgeDetected;
      if ReadEvent = '1' and edgeDetected = '1' then
        deferredE_r  <= not EDGE_IN;
        deferredTs_r <= std_logic_vector(resize(unsigned(TIMESTAMP), 24));
      end if;
    end if;
  end process;

  fifoEdDin <= '1' & rleWord when rleWr = '1' else
               '0' & s_bit & f_bit & deferredE_r & deferredTs_r
               when ReadEvent = '0' and ReadEvent_r = '1' else
               '0' & s_bit & f_bit & e_bit &
               std_logic_vector(resize(unsigned(TIMESTAMP), 24));

  s_bit <= ReadEvent;  -- only a software read is a level readback
  f_bit <= fifoEdAlmostFull;
  e_bit <= not EDGE_IN when ReadEvent = '0' else EDGE_IN; -- The polarity when there is an edge is inverted

  fifoEdWrEn <= '0' when fifoEdRstBusy = '1' else
                (rleWr and not fifoEdFull) when eventPathOff = '1' else
                '1' when ReadEvent     = '1' else -- allow reading command when config is not enabled
                '0' when configEnable  = '0' else
                '0' when fifoEdFull    = '1' else
                '1' when edgeDetected  = '1' else
                '1' when ReadEvent_r   = '1' else
                '0';

  fifoEdRdEn <= '0' when fifoEdRstBusy = '1' else
                '0' when lifecycleVld  = '1' else  -- DS-T2.43: hold FIFO while a lifecycle frame is on the bus
                '0' when fifoEdEmpty   = '1' else
                '1' when ED_MSG_RDY    = '1' else
                '0';


  i_fifo_buffer : entity tich.fifo_sync
    generic map
    (
      G_FIFO_DEPTH  => C_FIFO_DEPTH,
      G_FIFO_WIDTH  => C_FIFO_WIDTH,
      G_FIFO_FWFT   => 1,
      G_CALLER_NAME => "Edge Detector"
    )
    port map
    (
      RST       => RST,
      CLK       => CLK,
      DIN       => fifoEdDin,
      WR_EN     => fifoEdWrEn,
      RD_EN     => fifoEdRdEn,
      DOUT      => fifoEdDout,
      FULL      => fifoEdFull,
      PROG_FULL => fifoEdAlmostFull,
      EMPTY     => fifoEdEmpty,
      DATA_CNT  => open,
      RST_BUSY  => fifoEdRstBusy
    );


  -- DS-T2.43: uniform per-datalogger enable/disable lifecycle markers in the
  -- ALWAYS-ON (global RST) domain, mirroring i2c/uart. config_control here does
  -- not adopt CAPTURE_RST (line above, CAPTURE_RST => open), so configEnable is
  -- already in the global-RST domain — exactly where the lifecycle must live
  -- (H.96). The frame is priority-muxed ahead of the FIFO output below.
  i_lifecycle : entity tich.datalogger_lifecycle
    generic map (
      G_DEV_ID        => G_ED_DEV_ID,
      G_DEV_ID_WIDTH  => G_ED_ID_WIDTH,
      G_TYPE          => C_ED_TYPE_EVENT,
      G_TS_WIDTH      => C_TIMESTAMP_PART_WIDTH,
      G_CODE_ENABLED  => C_ED_EVENT_ENABLED,
      G_CODE_DISABLED => C_ED_EVENT_DISABLED
      )
    port map (
      CLK       => CLK,
      RST       => RST,
      CONFIG_EN => configEnable,
      TIMESTAMP => timeStamp_i,
      MSG_OUT   => lifecycleMsg,
      MSG_VLD   => lifecycleVld
      );

  -- DS-T2.43: lifecycle frame takes top priority so an enable/disable marker is
  -- never masked by a coincident FIFO edge event (mirrors i2c Phase-1 mux). The
  -- FIFO read is held while the lifecycle frame is on the bus (fifoEdRdEn guard
  -- above), so no queued edge is dropped — it simply waits one cycle.
  ED_MSG_OUT <= lifecycleMsg when lifecycleVld = '1' else
                std_logic_vector(to_unsigned(G_ED_DEV_ID, G_ED_ID_WIDTH)) & fifoEdDout(26 downto 0);
  ED_MSG_VLD <= '0' when fifoEdRstBusy = '1' else
                '1' when lifecycleVld  = '1' else
                '1' when fifoEdEmpty   = '0' else
                '0';
  ED_MSG_LIFECYCLE <= lifecycleVld and not fifoEdRstBusy;
  ED_MSG_STREAM    <= fifoEdDout(27) and not fifoEdEmpty and not lifecycleVld
                      and not fifoEdRstBusy;

  -- DS-P3.24 / DS-P3.70: internal-FIFO-overflow provenance. A detected edge is
  -- LOST when it arrives while the FIFO is FULL (and it isn't a read-command
  -- write, which has write priority) — fifoEdWrEn is suppressed by the
  -- `fifoEdFull` branch, so the edge is dropped UPSTREAM of the gather/ring.
  -- One registered pulse per dropped edge.
  p_overflow : process (CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        edgeLost_r <= '0';
      else
        edgeLost_r <= configEnable and fifoEdFull and (not ReadEvent)
                      and (not fifoEdRstBusy) and edgeDetected
                      and (not eventPathOff);  -- DS-P2.16: RLE loss is rleLost_r
      end if;
    end if;
  end process;

  ED_ERRORS <= (0 => edgeLost_r or rleLost_r, others => '0');


  -- ==========================================================================
  -- DS-P2.16: Stream-RLE (differential) compressor
  -- ==========================================================================
  -- Event mode spends a 32-bit word on every edge. In Stream-RLE mode each edge
  -- costs one 8-bit delta: the CLK ticks since the previous edge. Three deltas
  -- share one PACK word, so a line toggling every 1..255 ticks costs a third of
  -- the bus bandwidth. The stream is self-describing from its SYNC anchor:
  --
  --   SYNC_LO/HI : payload = TIMESTAMP[23:0]; the line level is in the type.
  --                Written on the first cycle after enable, and again after a
  --                word was lost to a full FIFO (the time base is then broken,
  --                so the compressor re-anchors instead of drifting).
  --   PACK       : payload = delta_2 & delta_1 & delta_0, delta_0 the oldest.
  --                Each delta is 1..255 ticks and toggles the line; 0x00 marks
  --                an empty slot (only at the high end of a flushed word).
  --   LONG       : one edge whose delta is 256..2^G_RLE_CNT_WIDTH-1 ticks.
  --   IDLE       : 2^G_RLE_CNT_WIDTH-1 ticks passed with no edge (payload, the
  --                full 2^24-1 in production); time advances,
  --                the line does not toggle.
  --
  -- Edge time = SYNC timestamp + sum of the deltas, which is the TIMESTAMP an
  -- event-mode word would have carried for that edge.
  --
  -- At most ONE word is written per cycle. A partly filled PACK is flushed on
  -- the cycle its newest delta reaches 255 ticks without a next edge, so a LONG
  -- or IDLE word never finds deltas waiting ahead of it. The same flush bounds
  -- the time an edge sits in the packer to 255 ticks. Disabling flushes the
  -- remaining deltas as a padded PACK.
  --
  -- The SYNC level is the line one cycle back (edgeIn_r): that is the level
  -- after every edge edge_detector has reported up to this cycle. The edge it
  -- reports on the SYNC cycle (on the enable cycle +1, a stale compare against
  -- the level frozen while disabled) is already in that level and is skipped.
  p_rle_comb : process (all)
    variable v_slot : std_logic_vector(7 downto 0);
  begin
    rleWr   <= '0';
    rleWord <= (others => '0');
    v_slot  := std_logic_vector(rleCnt_r(7 downto 0));
    if rleEn_r = '1' then
      if configEnable = '0' then
        -- disable cycle: flush what is held
        if rleNSlot_r = 1 then
          rleWr   <= '1';
          rleWord <= C_RLE_PACK & x"0000" & rleSlot0_r;
        elsif rleNSlot_r = 2 then
          rleWr   <= '1';
          rleWord <= C_RLE_PACK & x"00" & rleSlot1_r & rleSlot0_r;
        end if;
      elsif rleSyncPend_r = '1' then
        if fifoEdFull = '0' then
          rleWr <= '1';
          if edgeIn_r = '1' then
            rleWord(26 downto 24) <= C_RLE_SYNC_HI;
          else
            rleWord(26 downto 24) <= C_RLE_SYNC_LO;
          end if;
          rleWord(23 downto 0) <= std_logic_vector(resize(unsigned(TIMESTAMP), 24));
        end if;
      elsif edgeDetected = '1' then
        if rleCnt_r > C_RLE_SHORT_MAX then
          rleWr   <= '1';
          rleWord <= C_RLE_LONG & std_logic_vector(resize(rleCnt_r, 24));
        elsif rleNSlot_r = 2 then
          rleWr   <= '1';
          rleWord <= C_RLE_PACK & v_slot & rleSlot1_r & rleSlot0_r;
        end if;
      elsif rleCnt_r = C_RLE_SHORT_MAX and rleNSlot_r /= 0 then
        rleWr <= '1';
        if rleNSlot_r = 1 then
          rleWord <= C_RLE_PACK & x"0000" & rleSlot0_r;
        else
          rleWord <= C_RLE_PACK & x"00" & rleSlot1_r & rleSlot0_r;
        end if;
      elsif rleCnt_r = C_RLE_CNT_MAX then
        rleWr   <= '1';
        rleWord <= C_RLE_IDLE & std_logic_vector(resize(rleCnt_r, 24));
      end if;
    end if;
  end process;

  rleDrop <= rleWr and (fifoEdFull or fifoEdRstBusy);

  -- The event-mode writers (edges, readbacks) are muted for a Stream-RLE
  -- capture, from its enable cycle through its disable-flush cycle.
  eventPathOff <= '1' when rleEn_r = '1' else
                  '1' when configEnable = '1' and enable_r = '0'
                           and modeSel = C_MODE_RLE else
                  '0';

  p_rle : process (CLK)
  begin
    if rising_edge(CLK) then
      enable_r  <= configEnable;
      edgeIn_r  <= EDGE_IN;
      rleLost_r <= rleDrop;

      if rleCnt_r /= C_RLE_CNT_MAX then
        rleCnt_r <= rleCnt_r + 1;
      end if;

      if configEnable = '1' and enable_r = '0' then
        -- enable cycle: latch the mode, anchor on the next cycle
        if modeSel = C_MODE_RLE then
          rleEn_r <= '1';
        else
          rleEn_r <= '0';
        end if;
        rleSyncPend_r <= '1';
        rleNSlot_r    <= 0;
      elsif rleEn_r = '1' then
        if configEnable = '0' then
          rleEn_r    <= '0';
          rleNSlot_r <= 0;
        elsif rleSyncPend_r = '1' then
          if fifoEdFull = '0' then
            rleSyncPend_r <= '0';
            rleCnt_r      <= to_unsigned(1, rleCnt_r'length);
          end if;
        elsif edgeDetected = '1' then
          rleCnt_r <= to_unsigned(1, rleCnt_r'length);
          if rleCnt_r <= C_RLE_SHORT_MAX then
            if rleNSlot_r = 0 then
              rleSlot0_r <= std_logic_vector(rleCnt_r(7 downto 0));
              rleNSlot_r <= 1;
            elsif rleNSlot_r = 1 then
              rleSlot1_r <= std_logic_vector(rleCnt_r(7 downto 0));
              rleNSlot_r <= 2;
            else
              rleNSlot_r <= 0;
            end if;
          end if;
        elsif rleCnt_r = C_RLE_SHORT_MAX and rleNSlot_r /= 0 then
          rleNSlot_r <= 0;
        elsif rleCnt_r = C_RLE_CNT_MAX then
          rleCnt_r <= to_unsigned(1, rleCnt_r'length);
        end if;

        -- A lost word breaks the time base: drop what is held, re-anchor.
        if rleDrop = '1' and configEnable = '1' then
          rleSyncPend_r <= '1';
          rleNSlot_r    <= 0;
        end if;
      end if;

      if RST = '1' then
        enable_r      <= '0';
        rleEn_r       <= '0';
        rleSyncPend_r <= '0';
        rleNSlot_r    <= 0;
        rleCnt_r      <= (others => '0');
        rleLost_r     <= '0';
      end if;
    end if;
  end process;


  -- ==========================================================================
  -- DS-P3.117: max-frequency (min edge-to-edge interval) tracker
  -- ==========================================================================
  -- Authoritative HW measurement of the FASTEST activity seen on the line. A
  -- free-running interval counter resets on each VALIDATED edge; the just-elapsed
  -- count is the half-period. The smallest half-period == the highest frequency,
  -- so we latch a running MIN of the interval.
  --
  -- GLITCH ROBUSTNESS (the heart of the ticket). Two independent gates:
  --   1. Reuse of the existing min-pulse-width gate — on the real datapath
  --      EDGE_IN is already glitch_filter-conditioned in datalogger_gather (DS-T3.5),
  --      so edgeDetected only fires on edges that survived the rejection window.
  --   2. A local MIN-INTERVAL FLOOR (G_MAXFREQ_MIN_INTERVAL): an interval
  --      STRICTLY below the floor is impossibly fast for this line and is
  --      REJECTED — it can never latch a spuriously-high max. A one-off sub-ns
  --      runt that slips through gate 1 still cannot stick a false max.
  --
  --   3. CONFIRMATION (G_MAXFREQ_CONFIRM = N): the candidate is the LARGEST of
  --      the last N consecutive intervals, offered only once N have closed. A
  --      runt that clears the floor splits one half-period into three short
  --      intervals; any N >= 4 consecutive intervals still contain a full one,
  --      so the runt cannot raise the max. N = 1 offers each interval alone.
  --
  -- RE-PING / RE-ARM (stale-latch escape): a rising edge on `reArm` (CONFIG[2])
  -- clears the latch to all-ones and restarts the window. A subsequent SLOWER
  -- line then reports the new, lower max — proving the latch is not stuck.
  --
  -- The interval counter SATURATES at all-ones rather than wrapping, so a long
  -- idle gap cannot alias to a small interval and forge a high frequency.
  p_maxfreq : process (CLK)
    variable v_interval : unsigned(G_MAXFREQ_WIDTH - 1 downto 0);
    variable v_window   : unsigned(G_MAXFREQ_WIDTH - 1 downto 0);
  begin
    if rising_edge(CLK) then
      maxNew_r     <= '0';
      reArm_prev_r <= reArm;

      -- Saturating interval counter (no wrap → no aliasing of an idle gap).
      if interval_cnt_r /= C_MAXFREQ_ALLONES then
        interval_cnt_r <= interval_cnt_r + 1;
      end if;

      -- A validated edge closes the current interval and opens the next.
      if edgeDetected = '1' and configEnable = '1' then
        v_interval := interval_cnt_r;
        -- Re-open the window counting THIS edge's cycle as tick 1, so an edge N
        -- clocks after the previous one measures exactly N (not N-1).
        interval_cnt_r <= to_unsigned(1, G_MAXFREQ_WIDTH);
        if sawFirstEdge_r = '0' then
          -- First edge after enable/re-arm only OPENS the window — there is no
          -- prior edge, so its "interval" is meaningless and must not latch.
          sawFirstEdge_r <= '1';
        else
          -- Confirmation window: the slowest of this interval and the previous
          -- N-1. With N = 1 the loop is empty and the window is the interval.
          v_window := v_interval;
          for k in 0 to G_MAXFREQ_CONFIRM - 2 loop
            if ivalHist_r(k) > v_window then
              v_window := ivalHist_r(k);
            end if;
          end loop;
          -- Shift element by element: no slice, so N <= 2 never elaborates a
          -- null range (the DS-P2.35 Vivado elaborator crash). At N = 1 the
          -- single entry is written and never read.
          for k in C_MAXFREQ_HIST - 1 downto 1 loop
            ivalHist_r(k) <= ivalHist_r(k - 1);
          end loop;
          ivalHist_r(0) <= v_interval;
          if ivalClosed_r < G_MAXFREQ_CONFIRM then
            ivalClosed_r <= ivalClosed_r + 1;
          end if;
          if ivalClosed_r >= G_MAXFREQ_CONFIRM - 1
             and v_window >= to_unsigned(G_MAXFREQ_MIN_INTERVAL, G_MAXFREQ_WIDTH)
             and v_window < min_interval_r then
            -- N consecutive intervals at or below v_window → new max frequency.
            min_interval_r <= v_window;
            maxNew_r       <= '1';
          end if;
        end if;
      end if;

      -- Host re-ping: clear the latch + reopen a fresh window. Takes priority so
      -- a re-arm coincident with an edge still resets cleanly.
      if reArm = '1' and reArm_prev_r = '0' then
        min_interval_r <= (others => '1');
        interval_cnt_r <= (others => '0');
        sawFirstEdge_r <= '0';
        maxNew_r       <= '0';
        ivalClosed_r   <= 0;
      end if;

      if RST = '1' then
        interval_cnt_r <= (others => '0');
        min_interval_r <= (others => '1');
        sawFirstEdge_r <= '0';
        maxNew_r       <= '0';
        reArm_prev_r   <= '0';
        ivalClosed_r   <= 0;
      end if;
    end if;
  end process;

  ED_MAX_INTERVAL <= std_logic_vector(min_interval_r);
  ED_MAX_NEW      <= maxNew_r;


end architecture rtl;
