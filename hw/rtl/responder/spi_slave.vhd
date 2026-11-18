-------------------------------------------------------------------------------
-- Title      : spi_slave
-- Project    : deepskopion / datalogger self-validation (DS-P2.51 Phase 2)
-------------------------------------------------------------------------------
-- File       : spi_slave.vhd
-- Author     : Daniel J. Mazure  <djmazure@routertl.com>
-- Created    : 2026-05-22
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Clean-room SPI slave responder for the chip-internal EMIO testbench.
--   Mode 0 (CPOL=0, CPHA=0): master samples MOSI on SCLK rising edge,
--   drives the next MOSI bit on the falling edge. Slave shifts MISO out
--   on falling edges and samples MOSI on rising edges. MSB-first.
--
--   Bus IO is push-pull (real SPI). MISO is tri-stated when CS = '1'.
--
--   Async sampling of SCLK / MOSI / CS through 2-stage synchronisers —
--   the EMIO master clock is asynchronous to clk_sys. SPI clock rates
--   we expect (≤ 25 MHz from Zynq-7 SPI controller) leave plenty of
--   margin against the 100 MHz clk_sys.
--
--   TX path mirrors i2c_slave's I2C_TX_DIN/VLD/RDY handshake so the
--   spi_injector wrapper can drop a FIFO in front and pre-load
--   response bytes from regbank writes. SPI_TX_RDY pulses for one
--   clk_sys cycle when the slave latches a new TX byte (start of
--   each MISO byte = CS falling edge OR after the 8th MOSI rising
--   edge of the previous byte).
--
--   RX path (received MOSI bytes) is exposed for symmetry but not
--   used by the injector wrapper.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity spi_slave is
  generic (
    G_CPOL : integer := 0;  -- SPI mode 0 only in v1; CPOL/CPHA wired = 0
    G_CPHA : integer := 0
  );
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;

    -- SPI bus legs.
    SCLK        : in  std_logic;
    MOSI        : in  std_logic;
    CS_N        : in  std_logic;  -- active low chip-select
    MISO_O      : out std_logic;
    MISO_T      : out std_logic;  -- '0' = drive MISO; '1' = release (when CS_N='1')

    -- TX byte source (from injector FIFO).
    SPI_TX_DIN  : in  std_logic_vector(7 downto 0);
    SPI_TX_VLD  : in  std_logic;
    SPI_TX_RDY  : out std_logic;  -- pulses '1' when slave latches a TX byte

    -- RX byte sink (MOSI bytes captured each 8 SCLKs).
    SPI_RX_DOUT : out std_logic_vector(7 downto 0);
    SPI_RX_VLD  : out std_logic
  );
end entity spi_slave;

architecture rtl of spi_slave is

  -- Async sync chain — two FFs deep on each bus leg.
  signal sclk_sync_r : std_logic_vector(2 downto 0) := (others => '0');
  signal mosi_sync_r : std_logic_vector(1 downto 0) := (others => '0');
  signal cs_n_sync_r : std_logic_vector(2 downto 0) := (others => '1');

  -- Synchronised current values + edge detectors.
  signal sclk_q      : std_logic;
  signal sclk_q_dly  : std_logic;
  signal cs_n_q      : std_logic;
  signal cs_n_q_dly  : std_logic;
  signal sclk_re     : std_logic;
  signal sclk_fe     : std_logic;
  signal cs_n_fe     : std_logic;  -- CS_N falling edge = transaction start

  -- Shift registers (MSB-first).
  signal mosi_shift_r : std_logic_vector(7 downto 0) := (others => '0');
  signal miso_shift_r : std_logic_vector(7 downto 0) := (others => '0');

  -- Bit index inside the current byte (0..7). Resets on CS_N falling edge.
  signal bit_idx_r   : unsigned(3 downto 0) := (others => '0');

  signal tx_rdy_r    : std_logic := '0';
  signal rx_vld_r    : std_logic := '0';
  signal rx_dout_r   : std_logic_vector(7 downto 0) := (others => '0');

  -- DS-T2.8: defer the next-byte MISO reload from rising-edge-of-bit-7 to the
  -- following SCLK falling edge — otherwise the unconditional sclk_fe shift
  -- below double-shifts the freshly loaded byte and drops its MSB.
  signal load_pending_r : std_logic := '0';

begin

  ---------------------------------------------------------------------------
  -- Async-to-sync chain
  ---------------------------------------------------------------------------
  p_sync : process(CLK)
  begin
    if rising_edge(CLK) then
      sclk_sync_r <= sclk_sync_r(1 downto 0) & SCLK;
      mosi_sync_r <= mosi_sync_r(0) & MOSI;
      cs_n_sync_r <= cs_n_sync_r(1 downto 0) & CS_N;
    end if;
  end process;

  sclk_q     <= sclk_sync_r(2);
  sclk_q_dly <= sclk_sync_r(2);    -- already 2-stage; "dly" form just for naming
  cs_n_q     <= cs_n_sync_r(2);

  -- Edge detectors (compare stage 2 vs stage 1 — separated by one CLK).
  sclk_re <= '1' when sclk_sync_r(1) = '1' and sclk_sync_r(2) = '0' else '0';
  sclk_fe <= '1' when sclk_sync_r(1) = '0' and sclk_sync_r(2) = '1' else '0';
  cs_n_fe <= '1' when cs_n_sync_r(1) = '0' and cs_n_sync_r(2) = '1' else '0';

  ---------------------------------------------------------------------------
  -- Bit counter + MOSI capture + MISO shift
  ---------------------------------------------------------------------------
  p_xfer : process(CLK)
  begin
    if rising_edge(CLK) then
      tx_rdy_r <= '0';
      rx_vld_r <= '0';

      if RST = '1' then
        bit_idx_r      <= (others => '0');
        mosi_shift_r   <= (others => '0');
        miso_shift_r   <= (others => '0');
        load_pending_r <= '0';
      else
        if cs_n_fe = '1' then
          -- Start of a transaction — preload the first TX byte (whatever the
          -- FIFO presents). MISO sits at MSB on the next falling SCLK edge.
          if SPI_TX_VLD = '1' then
            miso_shift_r <= SPI_TX_DIN;
            tx_rdy_r     <= '1';
          else
            miso_shift_r <= (others => '0');  -- no data ready → drive 0s
          end if;
          bit_idx_r      <= (others => '0');
          load_pending_r <= '0';
        elsif cs_n_q = '0' then
          -- Transaction in progress.
          if sclk_re = '1' then
            -- Sample MOSI into RX shift register (MSB first).
            mosi_shift_r <= mosi_shift_r(6 downto 0) & mosi_sync_r(1);
            bit_idx_r    <= bit_idx_r + 1;
            if bit_idx_r = 7 then
              -- 8th bit captured — emit RX byte.
              rx_dout_r <= mosi_shift_r(6 downto 0) & mosi_sync_r(1);
              rx_vld_r  <= '1';
              bit_idx_r <= (others => '0');
              -- Defer the TX reload to the next falling edge — see DS-T2.8.
              load_pending_r <= '1';
            end if;
          end if;
          if sclk_fe = '1' then
            if load_pending_r = '1' then
              -- Byte-boundary falling edge: take the new TX byte verbatim
              -- (no shift) so the MSB hits the wire on this cycle.
              if SPI_TX_VLD = '1' then
                miso_shift_r <= SPI_TX_DIN;
                tx_rdy_r     <= '1';
              else
                miso_shift_r <= (others => '0');
              end if;
              load_pending_r <= '0';
            else
              -- Normal mid-byte shift: consume the MSB.
              miso_shift_r <= miso_shift_r(6 downto 0) & '0';
            end if;
          end if;
        end if;
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- Outputs
  ---------------------------------------------------------------------------
  -- MISO drives the MSB of the shift register; the line is released when
  -- CS_N is high (no transaction).
  MISO_O      <= miso_shift_r(7);
  MISO_T      <= cs_n_q;                         -- '1' when idle → Hi-Z
  SPI_TX_RDY  <= tx_rdy_r;
  SPI_RX_DOUT <= rx_dout_r;
  SPI_RX_VLD  <= rx_vld_r;

end architecture rtl;
