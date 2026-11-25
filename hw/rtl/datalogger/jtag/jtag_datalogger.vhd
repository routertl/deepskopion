-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : jtag_datalogger  (DS-P3.56 / JTAG P1)
-- Project    : deepskopion
-- Standard   : VHDL-2008
-- Style      : ESA-aligned two-process (P_Comb / P_Seq, RegType record),
--              PascalCase internals; datalogger-contract UPPER_CASE ports (the
--              interface every datalogger shares with datalogger_gather).
-------------------------------------------------------------------------------
-- Description: PASSIVE IEEE-1149.1 (JTAG/TAP) datalogger. Watches TCK/TMS/TDI/TDO
-- as INPUTS ONLY (never drives the chain), mirrors the 16-state TAP FSM, and
-- captures the IR/DR shift streams (TDI host->device, TDO device->host) byte
-- by byte, emitting 32-bit zs_msg messages per
-- hw/rtl/datalogger/jtag/jtag_datalogger_protocol.yml. Domain = DOMAIN_JTAG (0x8,
-- system_ids.yml). Standard datalogger interface so it drops into datalogger_gather.
--
-- P1 SCOPE — CLK_SYS OVERSAMPLING (no second clock domain): TCK is an external,
-- async clock. P1 2-FF-synchronises TCK/TMS/TDI/TDO into clk_sys and advances
-- the TAP FSM on detected TCK rising edges (like the other deepskopion dataloggers
-- oversample their async buses), keeping the timestamp direct. CONSTRAINT: TCK
-- must be well below clk_sys (~<= clk_sys/4) for clean edge detection. A true
-- TCK-domain front-end for high-speed TCK is the P2 enhancement
-- (jtag_datalogger_protocol.yml open_points.tck_clock_domain).
--
-- BIT ORDER: JTAG shifts LSB-first; full TDI/TDO bytes are emitted LSB-first.
-- A sub-byte final shift byte carries its valid-bit count N in the *_shift_end
-- event low nibble; its N valid bits sit in the TOP N byte positions
-- (right-shift accumulator), so SW right-aligns the final byte by (8 - N).
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_pkg.all;       -- C_DATALOGGER_MSG_WIDTH (32)

entity jtag_datalogger is
  generic (
    G_JTAG_ID_WIDTH   : integer                        := 5;
    G_JTAG_DEV_ID     : natural range 0 to (2**5)-1     := 16;   -- system_ids.yml
    G_JTAG_PINS       : natural range 4 to 4            := 4;     -- TCK,TMS,TDI,TDO
    G_TIMESTAMP_WIDTH : natural range 16 to 32          := 16;
    G_CONFIG_WIDTH    : integer                        := 16;
    G_ERROR_WIDTH     : integer                        := 2;
    G_FIFO_DEPTH_LOG2 : integer                        := 4       -- 16-deep msg FIFO
    );
  port (
    CLK          : in  std_logic;
    RST          : in  std_logic;
    -- JTAG_PINS: [0]=TCK [1]=TMS [2]=TDI [3]=TDO (observed, never driven)
    JTAG_PINS    : in  std_logic_vector(G_JTAG_PINS-1 downto 0);
    CONFIG       : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
    TIMESTAMP    : in  std_logic_vector(G_TIMESTAMP_WIDTH-1 downto 0);
    JTAG_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
    JTAG_MSG_VLD : out std_logic;
    JTAG_MSG_RDY : in  std_logic;
    JTAG_ERRORS  : out std_logic_vector(G_ERROR_WIDTH-1 downto 0)
    );
end jtag_datalogger;

architecture Rtl of jtag_datalogger is

  -- ── Message format (32-bit): [31:27]=DevId [26:24]=Type [23:8]=Ts [7:0]=Payload
  constant C_TypeTdi      : std_logic_vector(2 downto 0) := "001";
  constant C_TypeTdo      : std_logic_vector(2 downto 0) := "010";
  constant C_TypeTapState : std_logic_vector(2 downto 0) := "011";  -- DS-T3.16 verbose_tap
  constant C_TypeEvent    : std_logic_vector(2 downto 0) := "100";

  -- Event codes (jtag_datalogger_protocol.yml constants.events)
  constant C_EvDisabled  : std_logic_vector(7 downto 0) := x"00";
  constant C_EvEnabled   : std_logic_vector(7 downto 0) := x"03";
  constant C_EvTapReset  : std_logic_vector(7 downto 0) := x"0C";
  constant C_EvIrBegin   : std_logic_vector(7 downto 0) := x"10";
  constant C_EvIrEnd     : std_logic_vector(7 downto 0) := x"20";   -- or valid_bits
  constant C_EvDrBegin   : std_logic_vector(7 downto 0) := x"30";
  constant C_EvDrEnd     : std_logic_vector(7 downto 0) := x"40";   -- or valid_bits

  constant C_FifoDepth : integer := 2**G_FIFO_DEPTH_LOG2;

  -- ── 16-state IEEE-1149.1 TAP FSM (encoding matches the protocol spec) ──────
  type StateType is (
    S_TEST_LOGIC_RESET, S_RUN_TEST_IDLE,
    S_SELECT_DR_SCAN, S_CAPTURE_DR, S_SHIFT_DR, S_EXIT1_DR, S_PAUSE_DR, S_EXIT2_DR, S_UPDATE_DR,
    S_SELECT_IR_SCAN, S_CAPTURE_IR, S_SHIFT_IR, S_EXIT1_IR, S_PAUSE_IR, S_EXIT2_IR, S_UPDATE_IR);

  function TapNext(S : StateType; Tms : std_logic) return StateType is
  begin
    case S is
      when S_TEST_LOGIC_RESET => if Tms = '1' then return S_TEST_LOGIC_RESET; else return S_RUN_TEST_IDLE; end if;
      when S_RUN_TEST_IDLE    => if Tms = '1' then return S_SELECT_DR_SCAN;   else return S_RUN_TEST_IDLE; end if;
      when S_SELECT_DR_SCAN   => if Tms = '1' then return S_SELECT_IR_SCAN;   else return S_CAPTURE_DR;    end if;
      when S_CAPTURE_DR       => if Tms = '1' then return S_EXIT1_DR;         else return S_SHIFT_DR;      end if;
      when S_SHIFT_DR         => if Tms = '1' then return S_EXIT1_DR;         else return S_SHIFT_DR;      end if;
      when S_EXIT1_DR         => if Tms = '1' then return S_UPDATE_DR;        else return S_PAUSE_DR;      end if;
      when S_PAUSE_DR         => if Tms = '1' then return S_EXIT2_DR;         else return S_PAUSE_DR;      end if;
      when S_EXIT2_DR         => if Tms = '1' then return S_UPDATE_DR;        else return S_SHIFT_DR;      end if;
      when S_UPDATE_DR        => if Tms = '1' then return S_SELECT_DR_SCAN;   else return S_RUN_TEST_IDLE; end if;
      when S_SELECT_IR_SCAN   => if Tms = '1' then return S_TEST_LOGIC_RESET; else return S_CAPTURE_IR;    end if;
      when S_CAPTURE_IR       => if Tms = '1' then return S_EXIT1_IR;         else return S_SHIFT_IR;      end if;
      when S_SHIFT_IR         => if Tms = '1' then return S_EXIT1_IR;         else return S_SHIFT_IR;      end if;
      when S_EXIT1_IR         => if Tms = '1' then return S_UPDATE_IR;        else return S_PAUSE_IR;      end if;
      when S_PAUSE_IR         => if Tms = '1' then return S_EXIT2_IR;         else return S_PAUSE_IR;      end if;
      when S_EXIT2_IR         => if Tms = '1' then return S_UPDATE_IR;        else return S_SHIFT_IR;      end if;
      when S_UPDATE_IR        => if Tms = '1' then return S_SELECT_DR_SCAN;   else return S_RUN_TEST_IDLE; end if;
      when others             => return S_TEST_LOGIC_RESET;
    end case;
  end function;

  -- Assemble a 32-bit datalogger message.
  function MkMsg(MType : std_logic_vector(2 downto 0);
                 Ts    : std_logic_vector(G_TIMESTAMP_WIDTH-1 downto 0);
                 Pl    : std_logic_vector(7 downto 0)) return std_logic_vector is
  begin
    return std_logic_vector(to_unsigned(G_JTAG_DEV_ID, 5)) & MType & Ts(15 downto 0) & Pl;
  end function;

  -- Up to FOUR messages can be produced on one TAP edge (partial TDI, partial
  -- TDO, end-event, and — in verbose_tap mode, DS-T3.16 — a tap_state for the
  -- transition); they are staged and pushed one-per-clk-cycle. TCK << clk_sys
  -- guarantees staging drains before the next edge.
  -- 0 to 4: up to 4 messages stage per edge; StgRd (unsigned 3b) reaches 4 after
  -- the 4th push and the default RamWrData read indexes Stg(StgRd) — size 5 keeps
  -- that read in range.
  type StageArray is array (0 to 4) of std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  type MsgRam     is array (0 to C_FifoDepth-1) of std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);

  -- ── Register record (ESA two-process) ─────────────────────────────────────
  type RegType is record
    TckM, TckS, TckQ : std_logic;                 -- TCK sync + delayed (edge detect)
    TmsM, TmsS       : std_logic;
    TdiM, TdiS       : std_logic;
    TdoM, TdoS       : std_logic;
    EnableQ          : std_logic;                 -- delayed enable (lifecycle edge)
    Tap              : StateType;
    TdiAcc, TdoAcc   : std_logic_vector(7 downto 0);
    BitCnt           : unsigned(3 downto 0);       -- 0..7
    Stg              : StageArray;
    StgCnt, StgRd    : unsigned(2 downto 0);    -- 0..4 (was 0..3; +tap_state, DS-T3.16)
    WrPtr, RdPtr     : unsigned(G_FIFO_DEPTH_LOG2 downto 0);
    OverflowCnt      : unsigned(G_ERROR_WIDTH-1 downto 0);
  end record;

  constant C_RegInit : RegType := (
    TckM => '0', TckS => '0', TckQ => '0', TmsM => '0', TmsS => '0',
    TdiM => '0', TdiS => '0', TdoM => '0', TdoS => '0', EnableQ => '0',
    Tap => S_TEST_LOGIC_RESET, TdiAcc => (others => '0'), TdoAcc => (others => '0'),
    BitCnt => (others => '0'), Stg => (others => (others => '0')),
    StgCnt => (others => '0'), StgRd => (others => '0'),
    WrPtr => (others => '0'), RdPtr => (others => '0'), OverflowCnt => (others => '0'));

  signal R, RNext         : RegType;
  signal Ram              : MsgRam;
  signal RamWe            : std_logic;
  signal RamWrAddr        : unsigned(G_FIFO_DEPTH_LOG2-1 downto 0);
  signal RamWrData        : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal ConfigEnable     : std_logic;
  signal VerboseTap       : std_logic;   -- CONFIG bit[2] (jtag_config.verbose_tap), DS-T3.16
  signal DeviceLoadConfig : std_logic_vector(G_CONFIG_WIDTH-G_JTAG_ID_WIDTH-1 downto 0);
  signal FifoFull, FifoEmpty : std_logic;

begin

  -- ── config_control: latch the per-device enable from the CONFIG broadcast ──
  U_ConfigControl : entity tich.config_control
    generic map (
      G_DEV_ID       => G_JTAG_DEV_ID,
      G_CONFIG_WIDTH => G_CONFIG_WIDTH)
    port map (
      CLK        => CLK,
      RST        => RST,
      CONFIG     => CONFIG,
      CONFIG_OUT => DeviceLoadConfig,
      CAPTURE_RST  => open);  -- DS-T2.31: per-datalogger reset not yet adopted here
  ConfigEnable <= DeviceLoadConfig(0);
  VerboseTap   <= DeviceLoadConfig(2);   -- [15:11]=id stripped by config_control; [2]=verbose_tap

  FifoFull  <= '1' when (R.WrPtr(G_FIFO_DEPTH_LOG2-1 downto 0) = R.RdPtr(G_FIFO_DEPTH_LOG2-1 downto 0))
                        and (R.WrPtr(G_FIFO_DEPTH_LOG2) /= R.RdPtr(G_FIFO_DEPTH_LOG2)) else '0';
  FifoEmpty <= '1' when R.WrPtr = R.RdPtr else '0';

  -----------------------------------------------------------------------------
  -- P_Comb: input sync, TAP FSM advance, IR/DR shift capture + message staging,
  -- staging->FIFO push, and the read-side handshake. All next-state in V.
  -----------------------------------------------------------------------------
  P_Comb : process(R, JTAG_PINS, ConfigEnable, VerboseTap, TIMESTAMP, JTAG_MSG_RDY, FifoFull, FifoEmpty)
    variable V       : RegType;
    variable N       : integer range 0 to 4;
    variable Parts   : StageArray;
    variable Bc      : unsigned(3 downto 0);
    variable AccI    : std_logic_vector(7 downto 0);
    variable AccO    : std_logic_vector(7 downto 0);
    variable Full    : boolean;
    variable TckRise : std_logic;
    variable TapN    : StateType;
  begin
    V := R;

    -- 2-FF input synchronisers + TCK delayed copy for edge detect
    V.TckM := JTAG_PINS(0); V.TckS := R.TckM; V.TckQ := R.TckS;
    V.TmsM := JTAG_PINS(1); V.TmsS := R.TmsM;
    V.TdiM := JTAG_PINS(2); V.TdiS := R.TdiM;
    V.TdoM := JTAG_PINS(3); V.TdoS := R.TdoM;
    if R.TckS = '1' and R.TckQ = '0' then TckRise := '1'; else TckRise := '0'; end if;

    V.EnableQ := ConfigEnable;

    -- default RAM write controls
    RamWe     <= '0';
    RamWrAddr <= R.WrPtr(G_FIFO_DEPTH_LOG2-1 downto 0);
    RamWrData <= R.Stg(to_integer(R.StgRd));

    if R.StgCnt /= 0 then
      -- PUSH: drain one staged message into the FIFO
      if FifoFull = '0' then
        RamWe  <= '1';
        V.WrPtr := R.WrPtr + 1;
      elsif R.OverflowCnt /= (R.OverflowCnt'range => '1') then
        V.OverflowCnt := R.OverflowCnt + 1;
      end if;
      V.StgRd  := R.StgRd + 1;
      V.StgCnt := R.StgCnt - 1;
    else
      -- CAPTURE: stage new work (only when drained)
      N := 0; Parts := (others => (others => '0'));
      Bc := R.BitCnt; AccI := R.TdiAcc; AccO := R.TdoAcc; Full := false;

      if ConfigEnable = '1' and R.EnableQ = '0' then
        Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvEnabled); N := 1;
      elsif ConfigEnable = '0' and R.EnableQ = '1' then
        Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvDisabled); N := 1;
      elsif ConfigEnable = '1' and TckRise = '1' then
        TapN := TapNext(R.Tap, R.TmsS);

        -- 1) shift capture in the SHIFT states (LSB-first, right-shift)
        if R.Tap = S_SHIFT_DR or R.Tap = S_SHIFT_IR then
          AccI := R.TdiS & AccI(7 downto 1);
          AccO := R.TdoS & AccO(7 downto 1);
          if Bc = 7 then
            Parts(0) := MkMsg(C_TypeTdi, TIMESTAMP, AccI);
            Parts(1) := MkMsg(C_TypeTdo, TIMESTAMP, AccO);
            N := 2; Bc := (others => '0'); Full := true;
          else
            Bc := Bc + 1;
          end if;
        end if;

        -- 2) edge-triggered TAP events
        if TapN = S_SHIFT_IR and R.Tap = S_CAPTURE_IR then
          Parts(N) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvIrBegin); N := N + 1; Bc := (others => '0');
        elsif R.Tap = S_SHIFT_IR and TapN = S_EXIT1_IR then
          if (not Full) and Bc /= 0 then
            Parts(N) := MkMsg(C_TypeTdi, TIMESTAMP, AccI); N := N + 1;
            Parts(N) := MkMsg(C_TypeTdo, TIMESTAMP, AccO); N := N + 1;
          end if;
          Parts(N) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvIrEnd or std_logic_vector(resize(Bc, 8)));
          N := N + 1; Bc := (others => '0');
        elsif TapN = S_SHIFT_DR and R.Tap = S_CAPTURE_DR then
          Parts(N) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvDrBegin); N := N + 1; Bc := (others => '0');
        elsif R.Tap = S_SHIFT_DR and TapN = S_EXIT1_DR then
          if (not Full) and Bc /= 0 then
            Parts(N) := MkMsg(C_TypeTdi, TIMESTAMP, AccI); N := N + 1;
            Parts(N) := MkMsg(C_TypeTdo, TIMESTAMP, AccO); N := N + 1;
          end if;
          Parts(N) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvDrEnd or std_logic_vector(resize(Bc, 8)));
          N := N + 1; Bc := (others => '0');
        elsif TapN = S_TEST_LOGIC_RESET and R.Tap /= S_TEST_LOGIC_RESET then
          Parts(N) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvTapReset); N := N + 1;
        end if;

        -- DS-T3.16: verbose_tap — emit a tap_state message on every FSM
        -- transition (not self-loops). Payload[3:0] = the 4-bit code of the
        -- state just entered; StateType'pos matches the yml tap_states ordering
        -- (test_logic_reset=0x0 .. update_ir=0xF). May be the 4th message this
        -- edge (after TDI+TDO+end), hence the widened staging.
        if VerboseTap = '1' and TapN /= R.Tap then
          Parts(N) := MkMsg(C_TypeTapState, TIMESTAMP,
                            x"0" & std_logic_vector(to_unsigned(StateType'pos(TapN), 4)));
          N := N + 1;
        end if;

        V.Tap := TapN;
      end if;

      V.BitCnt := Bc; V.TdiAcc := AccI; V.TdoAcc := AccO;
      if N /= 0 then
        V.Stg := Parts; V.StgCnt := to_unsigned(N, 3); V.StgRd := (others => '0');
      end if;
    end if;

    -- read-side handshake
    if FifoEmpty = '0' and JTAG_MSG_RDY = '1' then
      V.RdPtr := R.RdPtr + 1;
    end if;

    RNext <= V;
  end process;

  -----------------------------------------------------------------------------
  -- P_Seq: register the record (synchronous reset) + the inferred message RAM.
  -----------------------------------------------------------------------------
  P_Seq : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then
        R <= C_RegInit;
      else
        R <= RNext;
      end if;
      if RamWe = '1' then
        Ram(to_integer(RamWrAddr)) <= RamWrData;
      end if;
    end if;
  end process;

  -- ── Outputs ────────────────────────────────────────────────────────────────
  JTAG_MSG_OUT <= Ram(to_integer(R.RdPtr(G_FIFO_DEPTH_LOG2-1 downto 0)));
  JTAG_MSG_VLD <= not FifoEmpty;
  JTAG_ERRORS  <= std_logic_vector(R.OverflowCnt);

end Rtl;
