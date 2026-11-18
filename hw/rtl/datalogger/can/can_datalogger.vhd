-------------------------------------------------------------------------------
-- Title      : can_datalogger  (DS-P3.100 / CAN P1)
-- Project    : deepskopion
-- Standard   : VHDL-2008
-- Style      : structural top — composes the four CAN blocks behind the shared
--              datalogger contract (UPPER_CASE ports, drops into datalogger_gather).
-------------------------------------------------------------------------------
-- Description: PASSIVE classic-CAN (2.0A/2.0B) datalogger. Watches a single
-- post-transceiver RX line as an INPUT ONLY (dominant='0' / recessive='1';
-- never drives or ACKs the bus), recovers CAN bit timing, removes stuff bits,
-- walks the frame FSM, captures arbitration ID + control + data + CRC-15,
-- VALIDATES the CRC, and emits 32-bit zs_msg messages per
-- can_datalogger_protocol.yml. Domain = DOMAIN_CAN (0xA, system_ids.yml).
--
-- BLOCK COMPOSITION (the data path the four siblings form):
--
--   RX ─► U_BitTiming ──BIT_VALID/BIT_VALUE──┬─────────────────────► U_FrameFsm
--          (Block 1)                          │                        (Block 3)
--                                             ▼                          ▲  │
--                                       U_Destuff ──DS_VALID/DS_BIT──────┘  │
--                                       (Block 2)   STUFF_ERROR ───────────┤
--                                          ▲                               │
--                                          └────────STUFF_EN───────────────┘
--
--                       DS_BIT ─► U_Crc15.DIN     CRC_CLR/CRC_EN ◄── U_FrameFsm
--                                 (Block 4)        U_Crc15.CRC ──► CRC_COMPUTED
--
-- The frame FSM owns the message stream AND the crc_error verdict: it frames the
-- CRC window (CRC_CLR while idle, CRC_EN per covered de-stuffed bit), and at the
-- end of the CRC field compares its captured wire CRC against the engine's
-- CRC_COMPUTED — a mismatch streams a crc_error event (0x42).
--
-- v0.1 SCOPE: classic CAN, std (11-bit) + ext (29-bit) ID, 0..8 data bytes, RTR,
-- a CONFIGURED bit rate (CONFIG.bit_prescaler — no auto-baud). No CDC: the slow
-- async RX is oversampled on clk_sys, exactly like the uart/i2c/spi/jtag P1
-- dataloggers. CAN-FD + auto-bitrate are reserved (P2). See CAN_DATALOGGER_SCOPING.md.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_pkg.all;       -- C_DATALOGGER_MSG_WIDTH (32)

entity can_datalogger is
  generic (
    G_CAN_ID_WIDTH    : integer                        := 5;
    G_CAN_DEV_ID      : natural range 0 to (2**5)-1     := 20;    -- system_ids.yml
    G_CAN_PINS        : natural range 1 to 1            := 1;     -- single RX line
    G_TIMESTAMP_WIDTH : natural range 16 to 32          := 16;
    G_CONFIG_WIDTH    : integer                        := 16;
    G_ERROR_WIDTH     : integer                        := 2;
    G_FIFO_DEPTH_LOG2 : integer                        := 4;      -- 16-deep msg FIFO
    G_NUM_TQ          : positive                        := 16;    -- time quanta per CAN bit
    G_SAMPLE_TQ       : positive                        := 12;    -- sample point (75 % of 16)
    G_PRESC_W         : positive                        := 10;    -- prescaler width (= CONFIG.bit_prescaler)
    -- CAN-FD data-phase bit timing (DS-P2.64 / DS-P3.108b). The data bit period is
    -- SW-settable at runtime via a BANKED config word — same device_id, a type bit
    -- (CONFIG[10]) selects {bit_prescaler,enable} (type 0) vs bank 1 (type 1), whose
    -- sub-words carry data_cyc and the nominal / data sample points (DS-T2.97, see
    -- P_DataCfg). One id, no pollution. G_DATA_CYC_PER_BIT is the power-on default
    -- (2 Mbit @ 100 MHz = 50 cyc/bit); both sample points reset to 192/256 = 75 %.
    G_CNT_W           : positive                        := 16;    -- direct cycle-counter width (bit_timing B2)
    G_DATA_CYC_PER_BIT: positive                        := 50     -- data-phase bit period default (clk cycles)
    );
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;
    -- CAN_PINS(0) = RX, the single observed post-transceiver line (never driven)
    CAN_PINS    : in  std_logic_vector(G_CAN_PINS-1 downto 0);
    CONFIG      : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
    TIMESTAMP   : in  std_logic_vector(G_TIMESTAMP_WIDTH-1 downto 0);
    CAN_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
    CAN_MSG_VLD : out std_logic;
    CAN_MSG_RDY : in  std_logic;
    CAN_ERRORS  : out std_logic_vector(G_ERROR_WIDTH-1 downto 0);
    -- DS-T2.49 REA verdict trace (DEBUG TARGET ONLY): [31:27] state
    -- [26:18] CRC17_COMPUTED[8:0] [17:1] captured Cap17 [0] verdict-mismatch pulse.
    DBG_BUS     : out std_logic_vector(31 downto 0) := (others => '0')
    );
end can_datalogger;

architecture Rtl of can_datalogger is

  -- per-device CONFIG (device_id stripped by config_control): [0]=enable,
  -- [G_PRESC_W:1]=bit_prescaler. Width = G_CONFIG_WIDTH - G_CAN_ID_WIDTH (=11).
  signal DeviceConfig : std_logic_vector(G_CONFIG_WIDTH-G_CAN_ID_WIDTH-1 downto 0);
  signal Enable       : std_logic;
  signal Prescaler    : std_logic_vector(G_PRESC_W-1 downto 0);

  -- 2nd addressed config (DS-P3.108): the CAN-FD data-phase bit period. Latched
  -- from a DATALOGGER_CONFIG write addressed to G_CAN_DATA_CFG_ID; 0 ⇒ unprogrammed
  -- ⇒ the G_DATA_CYC_PER_BIT default. DataSample = 75 % of DataCyc.
  signal DataConfig   : std_logic_vector(G_CONFIG_WIDTH-G_CAN_ID_WIDTH-1 downto 0);
  -- DataCyc inits to the default so the combinational DataSample is defined from
  -- t=0 (no numeric_std metavalue warning before the first config write).
  signal DataCyc      : unsigned(G_CNT_W-1 downto 0)
                          := to_unsigned(G_DATA_CYC_PER_BIT, G_CNT_W);
  signal DataSample   : unsigned(G_CNT_W-1 downto 0) := (others => '0');
  -- DS-T2.97: the bus's sample points, fractions of a bit in 1/256 (bank-1
  -- sub-words); reset and 0 = C_SP_DEFAULT (192/256 = 75 %, the pre-DS-T2.97 point).
  constant C_SP_DEFAULT : unsigned(7 downto 0) := to_unsigned(192, 8);
  signal NomSpFrac    : unsigned(7 downto 0) := C_SP_DEFAULT;
  signal DataSpFrac   : unsigned(7 downto 0) := C_SP_DEFAULT;
  signal NomSample    : unsigned(G_CNT_W-1 downto 0) := (others => '0');

  -- Block 1 → Block 2/3 (raw sampled bit stream)
  signal BitValid, BitValue, BitStart, Synced : std_logic;
  -- Block 2 → Block 3 (de-stuffed field-bit stream) + FD form error
  signal DsValid, DsBit, StuffError, FormError : std_logic;
  -- Block 3 → Block 2 (stuff-active window) and Block 3 ↔ Block 4 (CRC loop)
  signal StuffEn, StuffMode, RateSel, CrcClr, CrcEn : std_logic;
  signal CrcComputed                : std_logic_vector(14 downto 0);
  -- CAN-FD CRC-17/21 engines (region-aware feed) + window
  signal FdCrcClr, FdCrcEn          : std_logic;
  signal FdCrcDin                   : std_logic;
  -- DS-T2.49 verdict-tap signals (debug target)
  signal DbgState    : std_logic_vector(4 downto 0);
  signal DbgCrcFired : std_logic;                       -- FD CRC verdict pulse
  signal DbgCrcCap   : std_logic_vector(16 downto 0);   -- captured Cap17 at the verdict
  signal DbgCrcComp  : std_logic_vector(16 downto 0);   -- CRC17_COMPUTED at the verdict
  signal Crc17Computed              : std_logic_vector(16 downto 0);
  signal Crc21Computed              : std_logic_vector(20 downto 0);

begin

  -- ── Banked config (DS-P3.108b): ONE device_id, a type bit (CONFIG[10]) selects
  --    the bank. Two config_control instances on the SAME id latch independently:
  --    type 0 → primary {bit_prescaler, enable}; type 1 → {data_cyc}. Preserves
  --    one-id-one-datalogger; the data write never clobbers enable, and vice-versa.

  -- Bank 0 — primary: enable + bit_prescaler (the classic config; 0xA033 still
  -- reads as enable + prescaler 25, since its bit[10]=0 ⇒ type 0).
  U_ConfigControl : entity tich.config_control
    generic map (
      G_DEV_ID_WIDTH => G_CAN_ID_WIDTH,
      G_DEV_ID       => G_CAN_DEV_ID,
      G_CONFIG_WIDTH => G_CONFIG_WIDTH,
      G_BANK_EN      => 1,
      G_BANK_BIT     => G_CONFIG_WIDTH - G_CAN_ID_WIDTH - 1,   -- payload MSB = CONFIG[10]
      G_BANK_VAL     => '0')
    port map (
      CLK        => CLK,
      RST        => RST,
      CONFIG     => CONFIG,
      CONFIG_OUT => DeviceConfig,
      CAPTURE_RST  => open);                 -- per-datalogger reset not adopted here (mirror jtag)

  Enable    <= DeviceConfig(0);            -- can_config.enable
  -- bit_prescaler now [9:1] (9 bits, max 511); DeviceConfig(10)=0 (the type bit) is
  -- the MSB, so this slice still reads the prescaler unchanged for any value <512.
  Prescaler <= DeviceConfig(G_PRESC_W downto 1);

  -- Bank 1 — data: the CAN-FD data-phase cyc_per_bit (type 1 write, same id).
  U_DataBankControl : entity tich.config_control
    generic map (
      G_DEV_ID_WIDTH => G_CAN_ID_WIDTH,
      G_DEV_ID       => G_CAN_DEV_ID,
      G_CONFIG_WIDTH => G_CONFIG_WIDTH,
      G_BANK_EN      => 1,
      G_BANK_BIT     => G_CONFIG_WIDTH - G_CAN_ID_WIDTH - 1,
      G_BANK_VAL     => '1')
    port map (
      CLK        => CLK,
      RST        => RST,
      CONFIG     => CONFIG,
      CONFIG_OUT => DataConfig,
      CAPTURE_RST  => open);

  -- DataConfig[10] is the type bit (=1). Bank 1 carries three sub-words (DS-T2.97):
  --   [9]=0        data_cyc[8:0]          (0 ⇒ G_DATA_CYC_PER_BIT)
  --   [9]=1 [8]=0  nominal SP[7:0] / 256  (0 ⇒ 192 = 75 %)
  --   [9]=1 [8]=1  data SP[7:0] / 256     (0 ⇒ 192 = 75 %)
  -- config_control holds the LAST bank-1 word, so each register updates only while
  -- its own sub-word is the latched one and keeps its value otherwise: a data_cyc
  -- write never moves a sample point, and vice versa. Registered + RST-gated so the
  -- unsigned compares never see the t=0 metavalue (config_control resets its output
  -- to 0 synchronously). Config is static during a frame ⇒ latency is irrelevant.
  P_DataCfg : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        DataCyc    <= to_unsigned(G_DATA_CYC_PER_BIT, G_CNT_W);
        NomSpFrac  <= C_SP_DEFAULT;
        DataSpFrac <= C_SP_DEFAULT;
      elsif DataConfig(9) = '0' then
        if unsigned(DataConfig(8 downto 0)) = 0 then
          DataCyc <= to_unsigned(G_DATA_CYC_PER_BIT, G_CNT_W);
        else
          DataCyc <= resize(unsigned(DataConfig(8 downto 0)), G_CNT_W);
        end if;
      elsif DataConfig(8) = '0' then
        if unsigned(DataConfig(7 downto 0)) = 0 then
          NomSpFrac <= C_SP_DEFAULT;
        else
          NomSpFrac <= unsigned(DataConfig(7 downto 0));
        end if;
      else
        if unsigned(DataConfig(7 downto 0)) = 0 then
          DataSpFrac <= C_SP_DEFAULT;
        else
          DataSpFrac <= unsigned(DataConfig(7 downto 0));
        end if;
      end if;
      -- Sample offsets in cycles, floor(bit * SP / 256), recomputed per clk from
      -- static config. At the 192 default they equal the pre-DS-T2.97 values
      -- exactly (nominal = 12 * PRESCALER, data = floor(3 * DataCyc / 4)).
      NomSample  <= resize(shift_right(resize(unsigned(Prescaler), G_CNT_W + 8)
                                       * to_unsigned(G_NUM_TQ, 5) * NomSpFrac, 8), G_CNT_W);
      DataSample <= resize(shift_right(resize(DataCyc, G_CNT_W + 8) * DataSpFrac, 8), G_CNT_W);
    end if;
  end process;

  -- ── Block 1: bit-clock recovery (oversample RX, sample at TQ 12/16) ─────────
  U_BitTiming : entity tich.can_bit_timing
    generic map (
      G_NUM_TQ    => G_NUM_TQ,
      G_SAMPLE_TQ => G_SAMPLE_TQ,
      G_PRESC_W   => G_PRESC_W,
      G_CNT_W     => G_CNT_W)
    port map (
      CLK              => CLK,
      RST              => RST,
      ENABLE           => Enable,
      RX               => CAN_PINS(0),
      PRESCALER        => Prescaler,
      RATE_SEL         => RateSel,                              -- FD data-phase switch (from FSM)
      DATA_CYC_PER_BIT => std_logic_vector(DataCyc),            -- SW-settable (DS-P3.108) or default
      DATA_SAMPLE_CYC  => std_logic_vector(DataSample),         -- data SP x DataCyc / 256 (DS-T2.97)
      NOMINAL_SAMPLE_CYC => std_logic_vector(NomSample),          -- nominal SP x bit / 256 (DS-T2.97)
      BIT_VALID        => BitValid,
      BIT_VALUE        => BitValue,
      BIT_START        => BitStart,
      SYNCED           => Synced);

  -- ── Block 2: de-stuff (gated to the SOF..CRC window by the FSM's STUFF_EN) ──
  U_Destuff : entity tich.can_destuff
    port map (
      CLK         => CLK,
      RST         => RST,
      STUFF_EN    => StuffEn,
      STUFF_MODE  => StuffMode,        -- FD fixed stuffing in stuff-count+CRC (from FSM)
      IN_VALID    => BitValid,
      IN_BIT      => BitValue,
      OUT_VALID   => DsValid,
      OUT_BIT     => DsBit,
      STUFF_ERROR => StuffError,
      FORM_ERROR  => FormError,        -- FD fixed-stuff form error → FSM
      DBG_DESTUFF => open);            -- (destuff debug tap unused in the verdict probe)

  -- ── Block 4: CRC-15 engine, fed the de-stuffed covered bits via the FSM window
  U_Crc15 : entity tich.can_crc15
    port map (
      CLK => CLK,
      RST => RST,
      CLR => CrcClr,
      EN  => CrcEn,
      DIN => DsBit,
      CRC => CrcComputed);

  -- ── Block 4b: CAN-FD CRC-17/21 engines (DS-P2.64; coverage fixed DS-T2.49).
  --    The FD CRC includes DYNAMIC stuff bits but NOT fixed stuff bits, with the
  --    ISO MSB-preset init. FdCrcDin is the region-aware DIN mux (raw data bits vs
  --    de-stuffed SOF + stuff-count), driven by the FSM (FD_CRC_DIN) where the
  --    state is known; INIT_MSB ties high (ISO FD). Boundary is silicon-anchored
  --    to clean edge-capture wire goldens (see can_frame_fsm header / golden test).
  U_Crc17 : entity tich.can_crc17
    port map (
      CLK => CLK,
      RST => RST,
      CLR => FdCrcClr,
      EN  => FdCrcEn,
      DIN => FdCrcDin,
      INIT_MSB => '1',                  -- ISO-FD init=0x10000 (register MSB preset)
      CRC => Crc17Computed);

  U_Crc21 : entity tich.can_crc21
    port map (
      CLK => CLK,
      RST => RST,
      CLR => FdCrcClr,
      EN  => FdCrcEn,
      DIN => FdCrcDin,
      INIT_MSB => '1',                  -- ISO-FD init=0x100000 (register MSB preset)
      CRC => Crc21Computed);

  -- ── Block 3: frame FSM — the brain. Drives STUFF_EN + the CRC window, owns
  --    the message FIFO and the crc_error verdict (compares CrcComputed).
  U_FrameFsm : entity tich.can_frame_fsm
    generic map (
      G_CAN_DEV_ID      => G_CAN_DEV_ID,
      G_TIMESTAMP_WIDTH => G_TIMESTAMP_WIDTH,
      G_ERROR_WIDTH     => G_ERROR_WIDTH,
      G_FIFO_DEPTH_LOG2 => G_FIFO_DEPTH_LOG2)
    port map (
      CLK            => CLK,
      RST            => RST,
      ENABLE         => Enable,
      BIT_VALID      => BitValid,
      BIT_VALUE      => BitValue,
      DS_VALID       => DsValid,
      DS_BIT         => DsBit,
      STUFF_ERROR    => StuffError,
      FORM_ERROR     => FormError,
      TIMESTAMP      => TIMESTAMP,
      STUFF_EN       => StuffEn,
      STUFF_MODE     => StuffMode,
      RATE_SEL       => RateSel,
      CRC_CLR        => CrcClr,
      CRC_EN         => CrcEn,
      CRC_COMPUTED   => CrcComputed,
      FD_CRC_CLR     => FdCrcClr,
      FD_CRC_EN      => FdCrcEn,
      FD_CRC_DIN     => FdCrcDin,
      CRC17_COMPUTED => Crc17Computed,
      CRC21_COMPUTED => Crc21Computed,
      CAN_MSG_OUT    => CAN_MSG_OUT,
      CAN_MSG_VLD    => CAN_MSG_VLD,
      CAN_MSG_RDY    => CAN_MSG_RDY,
      CAN_ERRORS     => CAN_ERRORS,
      DBG_STATE      => DbgState,      -- DS-T2.49 CRC-verdict tap (debug target)
      DBG_CRC_FIRED  => DbgCrcFired,
      DBG_CRC_CAP    => DbgCrcCap,
      DBG_CRC_COMP   => DbgCrcComp);

  -- DS-T2.49: pack the FD CRC-17 VERDICT trace for the top-level REA (debug target).
  -- Trigger the REA on bit 0 (DbgCrcFired=1) to catch the frame that throws a
  -- spurious crc_error, then read captured (Cap17) vs computed (CRC17_COMPUTED low 9).
  DBG_BUS <= DbgState                       -- [31:27] FSM state
             & DbgCrcComp(8 downto 0)       -- [26:18] computed CRC-17 low 9
             & DbgCrcCap                    -- [17:1]  captured Cap17 (17 b)
             & DbgCrcFired;                 -- [0]     verdict-mismatch pulse (TRIGGER)

end Rtl;
