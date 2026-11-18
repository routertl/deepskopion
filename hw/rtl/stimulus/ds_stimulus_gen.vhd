-------------------------------------------------------------------------------
-- Title      : ds_stimulus_gen
-- Project    : deepskopion / datalogger self-validation (DS-P2.100, DS-D.38 option a)
-------------------------------------------------------------------------------
-- File       : ds_stimulus_gen.vhd
-- Author     : Daniel J. Mazure  <djmazure@routertl.com>
-- Created    : 2026-08-25
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   DS-D.38 Route 2 (fabric-side stimulus): the DE25 HPS has no fabric EMIO
--   injection path the way Zynq-7 PS7 does (DS-P2.95 — deepskopion_de25_top
--   tied the seam off with constants, so EMIO_MUX_SEL and the i2c/spi
--   injectors were "regbank-engaged but idle": fully wired, permanently
--   quiet). This block REPLACES the tied-off constants with real,
--   regbank-driven, FABRIC-GENERATED traffic — a UART transmitter, an I2C
--   master, an SPI master, and a free-running edge/pulse source.
--
--   Design honesty (ticket requirement): every byte this block puts on the
--   bus originates IN FABRIC from a regbank write. It is stimulus for the
--   self-validation loop, not a proxy for an external bus or a real PS
--   peripheral — nothing here claims HPS/EMIO provenance.
--
--   Scope (v1, deliberately minimal — a self-test generator, not a
--   general-purpose driver):
--     * UART: 8N1, LSB-first, configurable clk_sys-cycles-per-bit. One byte
--       per DATA write while enabled and idle; a write that lands while a
--       frame is in flight is dropped (no queue — matches the "trigger on
--       write" idiom the injectors already use for DATAIN).
--       BREAK PATTERN (CONTROL[1]=1): a DATA write sends DATA[15:0] "breaks"
--       (0 = 1), each the line held low for CONTROL[15:8] bit-times (0 = 1)
--       followed by one high bit-time. With N >= 13 no UART format fits (every
--       candidate stop bit samples low), which forces an autoSync calibration
--       failure in the UART datalogger on silicon.
--     * I2C: bit-banged open-drain master. START, 7-bit address + W, one to
--       four packed data bytes, STOP. No clock stretching or NACK-abort — the responder
--       (i2c_injector -> tich.i2c_slave) doesn't stretch, and completing the
--       byte unconditionally is what proves the wire-level protocol AND
--       exercises the responder's own ACK path; the master doesn't need to
--       branch on it. One transaction per DATA write while enabled and idle.
--     * SPI: mode 0 (CPOL=0/CPHA=0), MSB-first, 8 bits, CS framing. Push-pull
--       SCLK/MOSI/SS — no _T ports needed (ds_emio_mux only reads master
--       _O legs for SPI; see its header). One transfer per DATA write while
--       enabled and idle.
--     * Edge/pulse source: a free-running 50% duty toggle or the finite
--       canonical four-transition capture vector, independent of the three
--       protocol generators, for the EDGE self-test tap
--       (EMIO_MUX_SEL bit[7] — see deepskopion_core.vhd). Gives the EDGE
--       datalogger channel a live source that isn't just protocol traffic
--       reuse, so it can be enabled/observed on its own.
--
--   Control-plane idiom mirrors hw/rtl/responder/{i2c,spi}_injector.vhd:
--   CONTROL = enable (+ per-protocol config), DATA = write-strobe that
--   both supplies the payload AND triggers one transaction. DATA_VLD is
--   the regbank's per-slot valid pulse, which per DS-T3.2/DS-T2.14 is
--   held HIGH for several clk_sys cycles per single AXI write — this
--   block edge-detects it exactly like the injectors do, so one PS write
--   produces exactly one transaction trigger.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity ds_stimulus_gen is
  generic (
    -- Used only by the finite EDGE oracle.  Both supported sample clocks are
    -- exact multiples of 50 MHz, making its 20 ns quantum exact.
    G_CLK_HZ : positive := 100_000_000
  );
  port (
    CLK : in std_logic;
    RST : in std_logic;

    -- ── UART TX generator ──────────────────────────────────────────────
    UART_CONTROL  : in  std_logic_vector(31 downto 0);  -- bit[0]=enable, bit[1]=break pattern, [15:8]=low bit-times
    UART_BAUD_DIV : in  std_logic_vector(31 downto 0);  -- clk_sys cycles/bit (>=2)
    UART_DATA     : in  std_logic_vector(31 downto 0);  -- [7:0]=byte to send
    UART_DATA_VLD : in  std_logic;                       -- write-strobe = trigger
    UART_TX_O     : out std_logic;

    -- ── I2C master generator (open-drain) ──────────────────────────────
    I2C_CONTROL  : in  std_logic_vector(31 downto 0);   -- bit[0]=enable, [7:1]=7b addr, [9:8]=byte_count-1
    I2C_CLK_DIV  : in  std_logic_vector(31 downto 0);    -- clk_sys cycles / SCL quarter-period (>=2)
    I2C_DATA     : in  std_logic_vector(31 downto 0);    -- packed bytes, first byte in [7:0]
    I2C_DATA_VLD : in  std_logic;                        -- write-strobe = trigger
    I2C_SCL_O    : out std_logic;
    I2C_SCL_T    : out std_logic;                        -- '0'=drive O, '1'=release (pulled up)
    I2C_SDA_O    : out std_logic;
    I2C_SDA_T    : out std_logic;

    -- ── SPI master generator (mode 0, push-pull) ───────────────────────
    SPI_CONTROL  : in  std_logic_vector(31 downto 0);   -- bit[0]=enable
    SPI_CLK_DIV  : in  std_logic_vector(31 downto 0);    -- clk_sys cycles / SCLK half-period (>=1)
    SPI_DATA     : in  std_logic_vector(31 downto 0);    -- [7:0]=byte to shift out
    SPI_DATA_VLD : in  std_logic;                        -- write-strobe = trigger
    SPI_SCLK_O   : out std_logic;
    SPI_MOSI_O   : out std_logic;
    SPI_SS_O     : out std_logic;                        -- active-low

    -- ── Edge/pulse source ───────────────────────────────────────────────
    EDGE_CONTROL : in  std_logic_vector(31 downto 0);   -- bit[0]=enable, bit[1]=finite canonical mode, [19:4]=periodic half-period
    EDGE_O       : out std_logic
  );
end entity ds_stimulus_gen;

architecture rtl of ds_stimulus_gen is

  ---------------------------------------------------------------------------
  -- Shared write-strobe edge-detect (DS-T3.2/DS-T2.14 idiom).
  ---------------------------------------------------------------------------
  signal uart_vld_d, uart_vld_pulse : std_logic := '0';
  signal i2c_vld_d,  i2c_vld_pulse  : std_logic := '0';
  signal spi_vld_d,  spi_vld_pulse  : std_logic := '0';

  ---------------------------------------------------------------------------
  -- UART TX
  ---------------------------------------------------------------------------
  type t_uart_state is (S_IDLE, S_START, S_DATA, S_STOP, S_BRK_LOW, S_BRK_HIGH);
  signal uart_state    : t_uart_state := S_IDLE;
  signal uart_shift_r  : std_logic_vector(7 downto 0) := (others => '0');
  signal uart_bit_cnt  : unsigned(3 downto 0) := (others => '0');
  signal uart_baud_cnt : unsigned(31 downto 0) := (others => '0');
  signal uart_tx_r     : std_logic := '1';
  signal uart_brk_bits : unsigned(7 downto 0)  := (others => '0');  -- low bit-times done in this break
  signal uart_brk_low  : unsigned(7 downto 0)  := (others => '0');  -- low bit-times per break (latched)
  signal uart_brk_left : unsigned(15 downto 0) := (others => '0');  -- breaks left, this one included

  ---------------------------------------------------------------------------
  -- I2C master (bit-banged, 4 phases/bit: SCL-low-setup, SCL-rise,
  -- SCL-high-hold, SCL-fall — same pattern used industry-wide for a
  -- simple synchronous bit-bang master).
  ---------------------------------------------------------------------------
  type t_i2c_state is (S_I2C_IDLE, S_I2C_START, S_I2C_ADDR, S_I2C_ADDR_ACK,
                        S_I2C_DATA, S_I2C_DATA_ACK, S_I2C_STOP);
  type t_i2c_phase is (P_LOW, P_RISE, P_HIGH, P_FALL);
  signal i2c_state     : t_i2c_state := S_I2C_IDLE;
  signal i2c_phase     : t_i2c_phase := P_LOW;
  signal i2c_clk_cnt   : unsigned(31 downto 0) := (others => '0');
  signal i2c_bit_cnt   : unsigned(3 downto 0) := (others => '0');
  signal i2c_addr_r    : std_logic_vector(6 downto 0) := (others => '0');
  signal i2c_shift_r   : std_logic_vector(7 downto 0) := (others => '0');
  signal i2c_word_r    : std_logic_vector(31 downto 0) := (others => '0');
  signal i2c_bytes_left_r : unsigned(2 downto 0) := to_unsigned(1, 3);
  signal i2c_scl_low_r : std_logic := '0';  -- '1' = actively pull SCL low
  signal i2c_sda_low_r : std_logic := '0';  -- '1' = actively pull SDA low

  ---------------------------------------------------------------------------
  -- SPI master (mode 0)
  ---------------------------------------------------------------------------
  type t_spi_state is (S_SPI_IDLE, S_SPI_CS_SETUP, S_SPI_LOW, S_SPI_HIGH, S_SPI_CS_HOLD);
  signal spi_state    : t_spi_state := S_SPI_IDLE;
  signal spi_clk_cnt  : unsigned(31 downto 0) := (others => '0');
  signal spi_bit_cnt  : unsigned(3 downto 0) := (others => '0');
  signal spi_shift_r  : std_logic_vector(7 downto 0) := (others => '0');
  signal spi_sclk_r   : std_logic := '0';
  signal spi_mosi_r   : std_logic := '0';
  signal spi_ss_r     : std_logic := '1';  -- idle deselected (active-low)

  ---------------------------------------------------------------------------
  -- Edge/pulse source: free-running toggle, half-period from EDGE_CONTROL.
  ---------------------------------------------------------------------------
  signal edge_cnt : unsigned(15 downto 0) := (others => '0');
  signal edge_r   : std_logic := '0';
  signal edge_step_r : natural range 0 to 4 := 0;

  constant C_EDGE_QUANTUM_CYCLES : positive := G_CLK_HZ / 50_000_000;

begin

  assert G_CLK_HZ >= 50_000_000 and G_CLK_HZ mod 50_000_000 = 0
    report "finite EDGE mode requires a sample clock that exactly represents 20 ns"
    severity failure;

  -----------------------------------------------------------------------------
  -- Write-strobe edge detect
  -----------------------------------------------------------------------------
  p_vld_edge : process(CLK)
  begin
    if rising_edge(CLK) then
      uart_vld_d <= UART_DATA_VLD;
      i2c_vld_d  <= I2C_DATA_VLD;
      spi_vld_d  <= SPI_DATA_VLD;
    end if;
  end process;
  uart_vld_pulse <= UART_DATA_VLD and not uart_vld_d;
  i2c_vld_pulse  <= I2C_DATA_VLD  and not i2c_vld_d;
  spi_vld_pulse  <= SPI_DATA_VLD  and not spi_vld_d;

  -----------------------------------------------------------------------------
  -- UART TX — 8N1, LSB-first.
  -----------------------------------------------------------------------------
  UART_TX_O <= uart_tx_r;

  p_uart : process(CLK)
    variable v_div : unsigned(31 downto 0);
  begin
    if rising_edge(CLK) then
      if RST = '1' or UART_CONTROL(0) = '0' then
        uart_state    <= S_IDLE;
        uart_tx_r     <= '1';
        uart_baud_cnt <= (others => '0');
      else
        v_div := unsigned(UART_BAUD_DIV);
        if v_div < 2 then
          v_div := to_unsigned(2, 32);
        end if;

        case uart_state is
          when S_IDLE =>
            uart_tx_r <= '1';
            if uart_vld_pulse = '1' and UART_CONTROL(1) = '1' then
              uart_brk_bits <= (others => '0');
              if unsigned(UART_CONTROL(15 downto 8)) = 0 then
                uart_brk_low <= to_unsigned(1, 8);
              else
                uart_brk_low <= unsigned(UART_CONTROL(15 downto 8));
              end if;
              if unsigned(UART_DATA(15 downto 0)) = 0 then
                uart_brk_left <= to_unsigned(1, 16);
              else
                uart_brk_left <= unsigned(UART_DATA(15 downto 0));
              end if;
              uart_state    <= S_BRK_LOW;
              uart_baud_cnt <= (others => '0');
            elsif uart_vld_pulse = '1' then
              uart_shift_r  <= UART_DATA(7 downto 0);
              uart_state    <= S_START;
              uart_baud_cnt <= (others => '0');
            end if;

          when S_START =>
            uart_tx_r <= '0';
            if uart_baud_cnt = v_div - 1 then
              uart_baud_cnt <= (others => '0');
              uart_bit_cnt  <= (others => '0');
              uart_state    <= S_DATA;
            else
              uart_baud_cnt <= uart_baud_cnt + 1;
            end if;

          when S_DATA =>
            uart_tx_r <= uart_shift_r(0);
            if uart_baud_cnt = v_div - 1 then
              uart_baud_cnt <= (others => '0');
              uart_shift_r  <= '0' & uart_shift_r(7 downto 1);
              if uart_bit_cnt = 7 then
                uart_state <= S_STOP;
              else
                uart_bit_cnt <= uart_bit_cnt + 1;
              end if;
            else
              uart_baud_cnt <= uart_baud_cnt + 1;
            end if;

          when S_STOP =>
            uart_tx_r <= '1';
            if uart_baud_cnt = v_div - 1 then
              uart_baud_cnt <= (others => '0');
              uart_state    <= S_IDLE;
            else
              uart_baud_cnt <= uart_baud_cnt + 1;
            end if;

          when S_BRK_LOW =>                 -- low for uart_brk_low bit-times
            uart_tx_r <= '0';
            if uart_baud_cnt = v_div - 1 then
              uart_baud_cnt <= (others => '0');
              if uart_brk_bits = uart_brk_low - 1 then
                uart_state <= S_BRK_HIGH;
              else
                uart_brk_bits <= uart_brk_bits + 1;
              end if;
            else
              uart_baud_cnt <= uart_baud_cnt + 1;
            end if;

          when S_BRK_HIGH =>                -- one high bit-time between breaks
            uart_tx_r <= '1';
            if uart_baud_cnt = v_div - 1 then
              uart_baud_cnt <= (others => '0');
              uart_brk_bits <= (others => '0');
              if uart_brk_left = 1 then
                uart_state <= S_IDLE;
              else
                uart_brk_left <= uart_brk_left - 1;
                uart_state    <= S_BRK_LOW;
              end if;
            else
              uart_baud_cnt <= uart_baud_cnt + 1;
            end if;
        end case;
      end if;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- I2C master — open-drain bit-bang. O is always driven '0' (the only level
  -- a master ever needs to assert); T is the active leg: '0' pulls the line
  -- low, '1' releases it (ds_emio_mux resolves "not pulled low by anyone" to
  -- '1' — see its header). Matches the polarity i2c_injector/ds_emio_mux use.
  -----------------------------------------------------------------------------
  I2C_SCL_O <= '0';
  I2C_SDA_O <= '0';
  I2C_SCL_T <= '0' when i2c_scl_low_r = '1' else '1';
  I2C_SDA_T <= '0' when i2c_sda_low_r = '1' else '1';

  p_i2c : process(CLK)
    variable v_div : unsigned(31 downto 0);
  begin
    if rising_edge(CLK) then
      if RST = '1' or I2C_CONTROL(0) = '0' then
        i2c_state     <= S_I2C_IDLE;
        i2c_phase     <= P_LOW;
        i2c_scl_low_r <= '0';
        i2c_sda_low_r <= '0';
        i2c_clk_cnt   <= (others => '0');
      else
        v_div := unsigned(I2C_CLK_DIV);
        if v_div < 2 then
          v_div := to_unsigned(2, 32);
        end if;

        if i2c_state /= S_I2C_IDLE and i2c_clk_cnt /= v_div - 1 then
          i2c_clk_cnt <= i2c_clk_cnt + 1;
        else
          i2c_clk_cnt <= (others => '0');

          case i2c_state is
            when S_I2C_IDLE =>
              i2c_scl_low_r <= '0';
              i2c_sda_low_r <= '0';
              if i2c_vld_pulse = '1' then
                i2c_addr_r  <= I2C_CONTROL(7 downto 1);
                i2c_shift_r <= I2C_DATA(7 downto 0);
                i2c_word_r  <= I2C_DATA;
                i2c_bytes_left_r <= resize(unsigned(I2C_CONTROL(9 downto 8)), 3) + 1;
                i2c_state   <= S_I2C_START;
                i2c_phase   <= P_LOW;
              end if;

            -- START: SDA falls while SCL is high, then SCL falls.
            when S_I2C_START =>
              case i2c_phase is
                when P_LOW  => i2c_sda_low_r <= '0'; i2c_scl_low_r <= '0'; i2c_phase <= P_RISE;
                when P_RISE => i2c_sda_low_r <= '1';                       i2c_phase <= P_HIGH;
                when P_HIGH => i2c_scl_low_r <= '1';                       i2c_phase <= P_FALL;
                when P_FALL =>
                  i2c_bit_cnt <= (others => '0');
                  i2c_state   <= S_I2C_ADDR;
                  i2c_phase   <= P_LOW;
              end case;

            -- 7-bit address + W(0), MSB-first, 4-phase-per-bit.
            when S_I2C_ADDR =>
              case i2c_phase is
                when P_LOW =>
                  if i2c_bit_cnt < 7 then
                    i2c_sda_low_r <= not i2c_addr_r(6);  -- MSB first
                  else
                    i2c_sda_low_r <= '1';  -- R/W = write ('0') -> pull SDA low
                  end if;
                  i2c_phase <= P_RISE;
                when P_RISE => i2c_scl_low_r <= '0'; i2c_phase <= P_HIGH;
                when P_HIGH => i2c_phase <= P_FALL;
                when P_FALL =>
                  i2c_scl_low_r <= '1';
                  if i2c_bit_cnt = 7 then
                    i2c_state   <= S_I2C_ADDR_ACK;
                    i2c_phase   <= P_LOW;
                  else
                    i2c_addr_r  <= i2c_addr_r(5 downto 0) & '0';
                    i2c_bit_cnt <= i2c_bit_cnt + 1;
                    i2c_phase   <= P_LOW;
                  end if;
              end case;

            -- ACK slot: master releases SDA so the responder can drive it.
            when S_I2C_ADDR_ACK =>
              case i2c_phase is
                when P_LOW  => i2c_sda_low_r <= '0'; i2c_phase <= P_RISE;
                when P_RISE => i2c_scl_low_r <= '0'; i2c_phase <= P_HIGH;
                when P_HIGH => i2c_phase <= P_FALL;
                when P_FALL =>
                  i2c_scl_low_r <= '1';
                  i2c_bit_cnt   <= (others => '0');
                  i2c_state     <= S_I2C_DATA;
                  i2c_phase     <= P_LOW;
              end case;

            when S_I2C_DATA =>
              case i2c_phase is
                when P_LOW  => i2c_sda_low_r <= not i2c_shift_r(7); i2c_phase <= P_RISE;  -- MSB first
                when P_RISE => i2c_scl_low_r <= '0'; i2c_phase <= P_HIGH;
                when P_HIGH => i2c_phase <= P_FALL;
                when P_FALL =>
                  i2c_scl_low_r <= '1';
                  if i2c_bit_cnt = 7 then
                    i2c_state   <= S_I2C_DATA_ACK;
                    i2c_phase   <= P_LOW;
                  else
                    i2c_shift_r <= i2c_shift_r(6 downto 0) & '0';
                    i2c_bit_cnt <= i2c_bit_cnt + 1;
                    i2c_phase   <= P_LOW;
                  end if;
              end case;

            when S_I2C_DATA_ACK =>
              case i2c_phase is
                when P_LOW  => i2c_sda_low_r <= '0'; i2c_phase <= P_RISE;
                when P_RISE => i2c_scl_low_r <= '0'; i2c_phase <= P_HIGH;
                when P_HIGH => i2c_phase <= P_FALL;
                when P_FALL =>
                  i2c_scl_low_r <= '1';
                  if i2c_bytes_left_r > 1 then
                    i2c_word_r       <= X"00" & i2c_word_r(31 downto 8);
                    i2c_shift_r      <= i2c_word_r(15 downto 8);
                    i2c_bytes_left_r <= i2c_bytes_left_r - 1;
                    i2c_bit_cnt      <= (others => '0');
                    i2c_state        <= S_I2C_DATA;
                  else
                    i2c_sda_low_r <= '1';
                    i2c_state     <= S_I2C_STOP;
                  end if;
                  i2c_phase     <= P_LOW;
              end case;

            -- STOP: SDA held low while SCL rises, then SDA released (rises).
            when S_I2C_STOP =>
              case i2c_phase is
                when P_LOW  => i2c_phase <= P_RISE;
                when P_RISE => i2c_scl_low_r <= '0'; i2c_phase <= P_HIGH;
                when P_HIGH => i2c_sda_low_r <= '0'; i2c_phase <= P_FALL;
                when P_FALL => i2c_state <= S_I2C_IDLE; i2c_phase <= P_LOW;
              end case;
          end case;
        end if;
      end if;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- SPI master — mode 0 (CPOL=0/CPHA=0), MSB-first. SCLK idles low; MOSI is
  -- set up while SCLK is low and sampled by the slave on the SCLK rising
  -- edge (spi_slave.vhd's convention — the responder is the existing
  -- clean-room slave this master must speak to correctly).
  -----------------------------------------------------------------------------
  SPI_SCLK_O <= spi_sclk_r;
  SPI_MOSI_O <= spi_mosi_r;
  SPI_SS_O   <= spi_ss_r;

  p_spi : process(CLK)
    variable v_div : unsigned(31 downto 0);
  begin
    if rising_edge(CLK) then
      if RST = '1' or SPI_CONTROL(0) = '0' then
        spi_state   <= S_SPI_IDLE;
        spi_sclk_r  <= '0';
        spi_ss_r    <= '1';
        spi_clk_cnt <= (others => '0');
      else
        v_div := unsigned(SPI_CLK_DIV);
        if v_div = 0 then
          v_div := to_unsigned(1, 32);
        end if;

        case spi_state is
          when S_SPI_IDLE =>
            spi_sclk_r <= '0';
            if spi_vld_pulse = '1' then
              spi_shift_r <= SPI_DATA(7 downto 0);
              spi_ss_r    <= '0';
              spi_bit_cnt <= (others => '0');
              spi_clk_cnt <= (others => '0');
              spi_state   <= S_SPI_CS_SETUP;
            end if;

          when S_SPI_CS_SETUP =>
            if spi_clk_cnt = v_div - 1 then
              spi_clk_cnt <= (others => '0');
              spi_mosi_r  <= spi_shift_r(7);  -- MSB first, set up before 1st rising edge
              spi_state   <= S_SPI_LOW;
            else
              spi_clk_cnt <= spi_clk_cnt + 1;
            end if;

          when S_SPI_LOW =>
            if spi_clk_cnt = v_div - 1 then
              spi_clk_cnt <= (others => '0');
              spi_sclk_r  <= '1';
              spi_state   <= S_SPI_HIGH;
            else
              spi_clk_cnt <= spi_clk_cnt + 1;
            end if;

          when S_SPI_HIGH =>
            if spi_clk_cnt = v_div - 1 then
              spi_clk_cnt <= (others => '0');
              spi_sclk_r  <= '0';
              if spi_bit_cnt = 7 then
                spi_state <= S_SPI_CS_HOLD;
              else
                spi_shift_r <= spi_shift_r(6 downto 0) & '0';
                spi_mosi_r  <= spi_shift_r(6);
                spi_bit_cnt <= spi_bit_cnt + 1;
                spi_state   <= S_SPI_LOW;
              end if;
            else
              spi_clk_cnt <= spi_clk_cnt + 1;
            end if;

          when S_SPI_CS_HOLD =>
            if spi_clk_cnt = v_div - 1 then
              spi_ss_r  <= '1';
              spi_state <= S_SPI_IDLE;
            else
              spi_clk_cnt <= spi_clk_cnt + 1;
            end if;
        end case;
      end if;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- Edge/pulse source — free-running 50% duty toggle. Independent of the
  -- protocol generators above so it can drive the EDGE self-test tap on its
  -- own (deepskopion_core EMIO_MUX_SEL bit[7]).
  -----------------------------------------------------------------------------
  EDGE_O <= edge_r;

  p_edge : process(CLK)
    variable v_half : unsigned(15 downto 0);
  begin
    if rising_edge(CLK) then
      if RST = '1' or EDGE_CONTROL(0) = '0' then
        edge_cnt <= (others => '0');
        edge_r   <= '0';
        edge_step_r <= 0;
      elsif EDGE_CONTROL(1) = '1' then
        -- Canonical finite vector, in a 20 ns quantum common to the 50 MHz
        -- Intel and 100 MHz Xilinx sample clocks:
        --   t=10 us -> 1, then intervals 200 ns -> 0, 320 ns -> 1,
        --   280 ns -> 0, then hold.  No free-running tail can contaminate a
        --   transition-exact live capture.
        case edge_step_r is
          when 0 =>
            if edge_cnt = 500*C_EDGE_QUANTUM_CYCLES - 1 then
              edge_cnt <= (others => '0'); edge_r <= '1'; edge_step_r <= 1;
            else edge_cnt <= edge_cnt + 1; end if;
          when 1 =>
            if edge_cnt = 10*C_EDGE_QUANTUM_CYCLES - 1 then
              edge_cnt <= (others => '0'); edge_r <= '0'; edge_step_r <= 2;
            else edge_cnt <= edge_cnt + 1; end if;
          when 2 =>
            if edge_cnt = 16*C_EDGE_QUANTUM_CYCLES - 1 then
              edge_cnt <= (others => '0'); edge_r <= '1'; edge_step_r <= 3;
            else edge_cnt <= edge_cnt + 1; end if;
          when 3 =>
            if edge_cnt = 14*C_EDGE_QUANTUM_CYCLES - 1 then
              edge_cnt <= (others => '0'); edge_r <= '0'; edge_step_r <= 4;
            else edge_cnt <= edge_cnt + 1; end if;
          when others => edge_r <= '0';
        end case;
      else
        v_half := unsigned(EDGE_CONTROL(19 downto 4));
        if v_half = 0 then
          v_half := to_unsigned(1, 16);
        end if;
        if edge_cnt = v_half - 1 then
          edge_cnt <= (others => '0');
          edge_r   <= not edge_r;
        else
          edge_cnt <= edge_cnt + 1;
        end if;
      end if;
    end if;
  end process;

end architecture rtl;
