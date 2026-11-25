-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : spi_injector
-- Project    : deepskopion / datalogger self-validation (DS-P2.51 Phase 2)
-------------------------------------------------------------------------------
-- File       : spi_injector.vhd
-- Author     : Daniel J. Mazure  <djmazure@routertl.com>
-- Created    : 2026-05-22
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Wraps tich.spi_slave with an AXI-Lite-fed TX FIFO so the PS pre-loads
--   the byte sequence the slave returns on MISO during EMIO master
--   transactions. Mirror of i2c_injector — the responder is the slave;
--   the injector is the regbank-fed FIFO that feeds the slave's TX path.
--
--   Same regbank slot trio (CONTROL / DATAIN / STATUS) as i2c_injector
--   for symmetry. CONTROL is currently a thin shell — bit[0] = enable,
--   bit[9] = fifo_reset — leaving room for SPI mode bits (CPOL/CPHA),
--   bit-order, response length when those land in later phases.
--
-- CONTROL layout (32 bits):
--   [0]    = enable
--   [8:1]  = reserved (future: SPI mode bits, response length)
--   [9]    = fifo_reset (self-clearing — DS-T12 edge-detect)
--   [31:10]= reserved
--
-- STATUS layout (RO): same shape as i2c_injector.STATUS, including the
-- DS-T3.2/DS-T12 drain_busy bit at [15].
--
-- DS-T12 (2026-05-28): mirrors the DS-T3.2 fix applied to i2c_injector
-- (f4ace13). The previous design (a) level-sampled CONTROL[9] into
-- fifo_reset_r — so a regbank slot left at '1' held the FIFO in
-- perpetual reset — and (b) routed fifo_reset_r straight into the FIFO's
-- RST. The FIFO boundary's reset-busy guard then stays asserted
-- AFTER the RST pulse, silently absorbing any DATAIN writes inside
-- the window. A regbank-driven CONTROL[9] = "1" + back-to-back DATAIN
-- preload burst would lose every byte.
--
-- Fix mirrors i2c_injector exactly:
--   (1) Edge-detect (CONTROL[9] and ENABLE) so fifo_reset_r is a genuine
--       one-cycle pulse on the rising edge — implements the documented
--       "self-clearing" contract regardless of how long the regbank slot
--       holds the level high.
--   (2) Replace fifo_rst route with a drain-mode FSM: on the
--       fifo_reset_r pulse, drain_mode_r='1' and fifo_rd_en is forced
--       '1' every cycle until fifo_empty='1'. The FIFO is NOT in
--       reset during drain, so concurrent DATAIN writes land normally.
--       SPI_TX_VLD to the slave is masked while draining so drained
--       bytes aren't returned to a master that happens to clock SCLK.
--       drain_busy surfaced as STATUS[15].
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;

entity spi_injector is
  generic (
    G_FIFO_DEPTH : integer := 6;  -- 2^6 = 64 bytes of pre-loaded response
    G_FIFO_FWFT  : integer := 1
  );
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;

    -- Regbank-driven control plane.
    ENABLE      : in  std_logic;
    CONTROL     : in  std_logic_vector(31 downto 0);
    DATAIN      : in  std_logic_vector(31 downto 0);
    DATAIN_VLD  : in  std_logic;
    STATUS      : out std_logic_vector(31 downto 0);

    -- SPI bus legs.
    SCLK        : in  std_logic;
    MOSI        : in  std_logic;
    CS_N        : in  std_logic;
    MISO_O      : out std_logic;
    MISO_T      : out std_logic
  );
end entity spi_injector;

architecture rtl of spi_injector is

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

  -- DS-T12: edge-detect register + drain-mode FSM (mirror of i2c_injector
  -- DS-T3.2 fix). See header note for the architecture.
  signal fifo_reset_r     : std_logic := '0';
  signal ctrl9_d          : std_logic := '0';  -- 1-cycle delay for edge-detect
  signal drain_mode_r     : std_logic := '0';
  signal fifo_rd_en_slave : std_logic;         -- slave's natural read-strobe

  -- DS-T2.14 (mirror of i2c_injector): edge-detect DATAIN_VLD so one AXI
  -- write produces one FIFO push. Regbank holds .valid high for ~5
  -- sys_clk cycles per write; previous level form pushed each byte 5×.
  signal datain_vld_d     : std_logic := '0';
  signal fifo_wr_pulse    : std_logic;

begin

  ---------------------------------------------------------------------------
  -- CONTROL decode — DS-T12 edge-detect on (CONTROL(9) and ENABLE).
  ---------------------------------------------------------------------------
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

  ---------------------------------------------------------------------------
  -- DS-T12: drain-mode FSM. Enter on fifo_reset_r edge; exit on empty.
  ---------------------------------------------------------------------------
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

  -- DS-T2.14: edge-detect DATAIN_VLD → one AXI write = one FIFO push.
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

  -- DS-T12: fifo_rst is hard-RST only. CONTROL[9] handled via drain mode.
  fifo_rst   <= RST;
  fifo_din   <= DATAIN(C_FIFO_WIDTH-1 downto 0);
  fifo_wr_en <= fifo_wr_pulse;

  ---------------------------------------------------------------------------
  -- TX FIFO
  ---------------------------------------------------------------------------
  u_tx_fifo : entity tich.fifo_sync
    generic map (
      G_FIFO_DEPTH  => G_FIFO_DEPTH,
      G_FIFO_WIDTH  => C_FIFO_WIDTH,
      G_FIFO_FWFT   => G_FIFO_FWFT,
      G_CALLER_NAME => "spi_injector"
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
  -- SPI slave — drives MISO from the FIFO when CS is asserted.
  ---------------------------------------------------------------------------
  u_slave : entity tich.spi_slave
    port map (
      CLK         => CLK,
      RST         => RST,
      SCLK        => SCLK,
      MOSI        => MOSI,
      CS_N        => CS_N,
      MISO_O      => MISO_O,
      MISO_T      => MISO_T,
      SPI_TX_DIN  => fifo_dout,
      -- DS-T12: mask TX_VLD during drain so the slave doesn't shift
      -- drained bytes onto MISO if a master clocks SCLK mid-drain.
      SPI_TX_VLD  => (not fifo_empty) and (not drain_mode_r),
      SPI_TX_RDY  => fifo_rd_en_slave,
      SPI_RX_DOUT => open,
      SPI_RX_VLD  => open
    );

  -- DS-T12: fifo_rd_en = slave's natural read-strobe OR forced-high in
  -- drain mode (drains at one byte per cycle).
  fifo_rd_en <= fifo_rd_en_slave or drain_mode_r;

  ---------------------------------------------------------------------------
  -- STATUS pack — same layout as i2c_injector for symmetry, including
  -- the DS-T3.2/DS-T12 drain_busy bit at [15].
  ---------------------------------------------------------------------------
  STATUS(0)                                               <= fifo_empty;
  STATUS(1)                                               <= fifo_prog_full;
  STATUS(2)                                               <= '0';  -- no slave-side error path yet
  STATUS(G_FIFO_DEPTH + 3 downto 3)                       <= fifo_data_cnt;
  STATUS(14 downto G_FIFO_DEPTH + 4)                      <= (others => '0');
  STATUS(15)                                              <= drain_mode_r;
  STATUS(31 downto 16)                                    <= (others => '0');

end architecture rtl;
