-------------------------------------------------------------------------------
-- Title      : i2c_injector
-- Project    : deepskopion / datalogger self-validation (DS-P2.51 Phase 2)
-------------------------------------------------------------------------------
-- File       : i2c_injector.vhd
-- Author     : Daniel J. Mazure  <djmazure@routertl.com>
-- Created    : 2026-05-22
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Wraps tich.i2c_slave with an AXI-Lite-fed TX FIFO so the PS (or any
--   AXI master) can pre-load the byte sequence the slave will return
--   on master reads. The slave handles the bus protocol directly —
--   START/STOP/ACK/NACK detection, address match, open-drain SDA drive.
--
--   Architecture (clean-room implementation — i2c_slave is the responder;
--   the injector is just the regbank-fed FIFO that feeds the slave's
--   I2C_TX_DIN/VLD/RDY ports):
--
--                   CONTROL                  ┌──────────────┐
--               ───────────────────────────▶│              │
--                                            │  i2c_slave   │ ──▶ SDA_O / SDA_T
--               ┌──────────────────┐  TX     │ (responder)  │ ◀── SCL / SDA_I
--      DATAIN ─▶│ portable FIFO 8b │────────▶│ I2C_TX_*      │
--      DATAIN_VLD                  │         └──────────────┘
--               └──────────────────┘
--               STATUS ◀───────────────  fifo flags + slave errors
--
-- CONTROL layout (32 bits; matches the existing i2c_slave G_CTRL_WIDTH=16
-- field expectations, zero-padded high):
--   [0]    = enable           (active = '1'; '0' disables FSM)
--   [7:1]  = i2c_dev_address  (7-bit I2C address the slave responds to)
--   [8]    = reserved
--   [9]    = fifo_reset       (write '1' to drain TX FIFO; self-clearing)
--   [31:10]= reserved
--
-- STATUS layout (RO):
--   [0]    = fifo_empty
--   [1]    = fifo_prog_full
--   [2]    = slave_error  (sticky)
--   [G_FIFO_DEPTH+3-1 downto 3] = fifo data count
--   [15]   = drain_busy   (DS-T3.2) — CONTROL[9] drain in progress
--   rest   = 0
--
-- DS-T3.2 (regbank-front-door defect, 2026-05-28): the previous design
-- (a) level-sampled CONTROL[9] into fifo_reset_r (so a regbank slot left
-- at '1' held the FIFO in perpetual reset) AND (b) routed fifo_reset_r
-- straight into the FIFO's RST. The boundary's reset-busy guard then
-- asserted for ~5 cycles AFTER the RST pulse — any DATAIN write landing
-- inside that window was silently dropped. The canonical PS firmware
-- "reset, then preload 4 bytes" sequence (back-to-back AXI writes through
-- the regbank) lost ALL or PART of the preload. See
-- tb_regbank_i2c_inj_seam test_reset_then_immediate_preload_race.
--
-- Two changes:
--   (1) Edge-detect (CONTROL[9] and ENABLE) so fifo_reset_r is a genuine
--       one-cycle pulse on the RISING edge — implements the documented
--       "self-clearing" contract regardless of how long the host leaves
--       the level high in the regbank slot.
--   (2) Replace the fifo_rst route with a "drain mode": on the
--       fifo_reset_r pulse, enter drain_mode_r='1' and force fifo_rd_en
--       to '1' every cycle until fifo_empty='1'. The FIFO is NOT in
--       reset during drain, so concurrent DATAIN writes land normally.
--       I2C_TX_VLD to the slave is masked while draining so drained bytes
--       aren't consumed as response data. drain_busy is surfaced as
--       STATUS[15] for hosts that want to poll, but isn't required for
--       correctness — the documented "reset, then preload" PS sequence
--       no longer loses bytes regardless of pacing.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_pkg.all;

entity i2c_injector is
  generic (
    G_FIFO_DEPTH  : integer := 6;  -- 2^6 = 64 bytes of pre-loaded response
    G_FIFO_FWFT   : integer := 1;  -- first-word-fall-through; slave reads DOUT immediately
    G_CLK_FREQ_HZ : natural := 100_000_000  -- CLK freq → i2c_slave SCL-stuck-low timeout
  );
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;

    -- Timestamp slice fed straight through to the slave (used for SCL-stuck
    -- timeout + glitch detector).
    TIMESTAMP   : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);

    -- Regbank-driven control plane.
    ENABLE      : in  std_logic;                       -- master gate (e.g. regbank-slot valid)
    CONTROL     : in  std_logic_vector(31 downto 0);   -- enable / addr / fifo_reset (see header)
    DATAIN      : in  std_logic_vector(31 downto 0);   -- low 8 bits push into TX FIFO
    DATAIN_VLD  : in  std_logic;                       -- WR_EN strobe
    STATUS      : out std_logic_vector(31 downto 0);

    -- I2C bus legs — wire to the open-drain bus model in deepskopion_top.
    SCL         : in  std_logic;
    SDA_I       : in  std_logic;
    SDA_O       : out std_logic;
    SDA_T       : out std_logic;
    -- DS-T2.34 REA probe: i2c_slave FSM-meaningful internals, passed through.
    DBG         : out std_logic_vector(19 downto 0) := (others => '0');
    -- DS-T2.34 FIFO-handshake probe: [15:8]=fifo_dout, [7:4]=fifo_data_cnt(3:0),
    -- [3]=fifo_empty, [2]=fifo_rd_en(pop), [1]=I2C_TX_VLD, [0]=0.
    DBG_FIFO    : out std_logic_vector(15 downto 0) := (others => '0');
    DBG_SHIFT   : out std_logic_vector(7 downto 0)  := (others => '0');  -- slave's latched byte
    -- DS-T2.34 bail-decision probe (slave non-sequential abort conditions).
    DBG2        : out std_logic_vector(7 downto 0)  := (others => '0')
  );
end entity i2c_injector;

architecture rtl of i2c_injector is

  constant C_FIFO_WIDTH : integer := 8;

  signal fifo_rst       : std_logic;
  signal fifo_din       : std_logic_vector(C_FIFO_WIDTH-1 downto 0);
  signal fifo_dout      : std_logic_vector(C_FIFO_WIDTH-1 downto 0);
  signal fifo_wr_en     : std_logic;
  signal fifo_rd_en     : std_logic;
  signal fifo_full      : std_logic;
  signal fifo_empty     : std_logic;
  signal fifo_prog_full : std_logic;
  signal fifo_data_cnt  : std_logic_vector(G_FIFO_DEPTH downto 0);

  -- Slave-side control word: low bit = enable, bits [7:1] = address
  -- (zero-extended to 16 bits to match i2c_slave's G_CTRL_WIDTH=16).
  signal slave_ctrl     : std_logic_vector(15 downto 0);
  signal slave_errors   : std_logic_vector(0 downto 0);
  signal slave_err_sticky_r : std_logic := '0';

  -- Latched fifo-reset pulse from CONTROL[9]. Genuine one-cycle edge
  -- pulse (DS-T3.2) — implements the documented "self-clearing" contract.
  signal fifo_reset_r   : std_logic := '0';
  signal ctrl9_d        : std_logic := '0';   -- 1-cycle delay for edge-detect

  -- DS-T2.14: DATAIN_VLD from the regbank is held HIGH for several
  -- sys_clk cycles per AXI write (verified on bench: each write_u32 to
  -- I2C_INJ_DATAIN produced 5 FIFO writes). Edge-detect it so one AXI
  -- write produces one FIFO write strobe, matching the documented
  -- "DATAIN write-strobe" contract.
  signal datain_vld_d   : std_logic := '0';   -- 1-cycle delay for edge-detect
  signal fifo_wr_pulse  : std_logic;          -- one-cycle pulse per AXI write

  -- DS-T3.2 drain mode: entered on the fifo_reset_r pulse, held until
  -- the FIFO is empty. While active, fifo_rd_en is forced '1' so the
  -- FIFO drains at one byte per cycle WITHOUT entering XPM reset (which
  -- would re-introduce the wr_rst_busy window that dropped DATAIN writes).
  signal drain_mode_r     : std_logic := '0';
  signal fifo_rd_en_slave : std_logic;        -- slave's natural read-strobe
  signal i2c_tx_vld       : std_logic;        -- DS-T2.34: probed TX-valid to slave
  signal dbg_shift_s      : std_logic_vector(7 downto 0);

begin

  -- DS-T2.34: the data-available signal to the slave, as a named signal so the
  -- REA can probe it alongside fifo_dout / fifo_empty / fifo_rd_en.
  i2c_tx_vld <= (not fifo_empty) and (not drain_mode_r);
  DBG_SHIFT  <= dbg_shift_s;
  DBG_FIFO   <= fifo_dout
              & fifo_data_cnt(3 downto 0)
              & fifo_empty & fifo_rd_en & i2c_tx_vld & '0';

  ---------------------------------------------------------------------------
  -- CONTROL decode
  ---------------------------------------------------------------------------
  slave_ctrl <= CONTROL(15 downto 0);  -- enable, address, etc.

  -- Genuine one-cycle pulse on the RISING edge of (CONTROL[9] and ENABLE).
  -- DS-T3.2: implements the documented "self-clearing" contract — the host
  -- writes CONTROL[9]='1' once, and the pulse fires exactly once regardless
  -- of how long the host leaves the bit set in the regbank slot. The
  -- previous level-sampling form held fifo_reset_r high for the entire
  -- duration of CONTROL(9), which (with the old fifo_rst routing) kept
  -- the XPM FIFO in perpetual reset and silently dropped every DATAIN
  -- write until the host wrote CONTROL[9] back to '0'.
  p_fifo_reset : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        fifo_reset_r <= '0';
        ctrl9_d      <= '0';
      else
        ctrl9_d      <= CONTROL(9) and ENABLE;
        fifo_reset_r <= (CONTROL(9) and ENABLE) and not ctrl9_d;
      end if;
    end if;
  end process;

  -- DS-T3.2: drain-mode FSM. Enter on fifo_reset_r edge; exit when the
  -- FIFO drains empty. Hard-RST also clears the flag.
  p_drain_mode : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        drain_mode_r <= '0';
      elsif fifo_reset_r = '1' then
        drain_mode_r <= '1';
      elsif drain_mode_r = '1' and fifo_empty = '1' then
        drain_mode_r <= '0';
      end if;
    end if;
  end process;

  -- DS-T2.14: edge-detect DATAIN_VLD so one AXI write = one FIFO push.
  -- The regbank holds .valid high for ~5 sys_clk cycles per write; the
  -- previous level-sampled form pushed the same byte 5 times.
  p_datain_edge : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        datain_vld_d <= '0';
      else
        datain_vld_d <= DATAIN_VLD and ENABLE;
      end if;
    end if;
  end process;
  fifo_wr_pulse <= (DATAIN_VLD and ENABLE) and not datain_vld_d;

  -- DS-T3.2: fifo_rst is hard-RST only. CONTROL[9] is handled via the
  -- drain-mode FSM above so writes during reset don't get dropped by
  -- the XPM wr_rst_busy window.
  fifo_rst   <= RST;
  fifo_din   <= DATAIN(C_FIFO_WIDTH-1 downto 0);
  fifo_wr_en <= fifo_wr_pulse;

  ---------------------------------------------------------------------------
  -- TX FIFO (response data the master will read back)
  ---------------------------------------------------------------------------
  u_tx_fifo : entity tich.fifo_sync
    generic map (
      G_FIFO_DEPTH  => G_FIFO_DEPTH,
      G_FIFO_WIDTH  => C_FIFO_WIDTH,
      G_FIFO_FWFT   => G_FIFO_FWFT,
      G_CALLER_NAME => "i2c_injector"
    )
    port map (
      CLK        => CLK,
      RST        => fifo_rst,
      DIN        => fifo_din,
      WR_EN      => fifo_wr_en,
      RD_EN      => fifo_rd_en,
      DOUT       => fifo_dout,
      FULL       => fifo_full,
      EMPTY      => fifo_empty,
      PROG_FULL  => fifo_prog_full,
      PROG_EMPTY => open,
      DATA_CNT   => fifo_data_cnt,
      RST_BUSY   => open
    );

  ---------------------------------------------------------------------------
  -- I2C slave (the actual bus responder).
  --
  -- In FWFT mode the FIFO presents DOUT as soon as a word is available
  -- (EMPTY='0' implies DOUT is valid). The slave consumes one byte per
  -- master-read transaction by pulsing I2C_TX_RDY; we route that to RD_EN.
  ---------------------------------------------------------------------------
  u_slave : entity tich.i2c_slave
    generic map (
      G_CTRL_WIDTH  => 16,
      G_ERROR_WIDTH => 1,
      G_ILA_EN      => 0,
      G_CLK_FREQ_HZ => G_CLK_FREQ_HZ
    )
    port map (
      CLK         => CLK,
      RST         => RST,
      SCL         => SCL,
      SDA_I       => SDA_I,
      SDA_O       => SDA_O,
      SDA_T       => SDA_T,
      TIMESTAMP   => TIMESTAMP,
      I2C_CTRL    => slave_ctrl,
      I2C_TX_DIN  => fifo_dout,
      -- DS-T3.2: mask TX_VLD while draining so the slave doesn't consume
      -- drained bytes as response data. (DS-T2.34: named signal for probing.)
      I2C_TX_VLD  => i2c_tx_vld,
      I2C_TX_RDY  => fifo_rd_en_slave,
      I2C_RX_DOUT => open,
      I2C_RX_VLD  => open,
      I2C_RX_RDY  => '1',         -- always ready to absorb whatever the master writes
      I2C_ERRORS  => slave_errors,
      DBG         => DBG,         -- DS-T2.34 REA probe passthrough
      DBG_SHIFT   => dbg_shift_s,  -- DS-T2.34 latched-byte probe passthrough
      DBG2        => DBG2          -- DS-T2.34 bail-decision probe passthrough
    );

  -- DS-T3.2: fifo_rd_en = slave's natural read-strobe OR forced-high in
  -- drain mode (drains at one byte per cycle).
  fifo_rd_en <= fifo_rd_en_slave or drain_mode_r;

  -- Sticky error bit — clears only on RST or fifo_reset_r.
  p_err_sticky : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' or fifo_reset_r = '1' then
        slave_err_sticky_r <= '0';
      elsif slave_errors(0) = '1' then
        slave_err_sticky_r <= '1';
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- STATUS pack — drive each bit/slice once, tie remaining high bits to '0'.
  -- DS-T3.2: bit [15] = drain_busy. Sits above the data-count slice for
  -- any supported FIFO depth.
  ---------------------------------------------------------------------------
  STATUS(0)                                               <= fifo_empty;
  STATUS(1)                                               <= fifo_prog_full;
  STATUS(2)                                               <= slave_err_sticky_r;
  STATUS(G_FIFO_DEPTH + 3 downto 3)                       <= fifo_data_cnt;
  STATUS(14 downto G_FIFO_DEPTH + 4)                      <= (others => '0');
  STATUS(15)                                              <= drain_mode_r;
  STATUS(31 downto 16)                                    <= (others => '0');

end architecture rtl;
