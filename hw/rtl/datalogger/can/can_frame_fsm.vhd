-------------------------------------------------------------------------------
-- can_frame_fsm.vhd  —  CAN / CAN-FD frame walker + message staging
--                       (DS-P3.100 v0.1 classic, DS-P2.64 v0.2 FD fork)
-------------------------------------------------------------------------------
-- The brain of the CAN datalogger. Consumes the DE-STUFFED field-bit stream from
-- can_destuff (Block 2) plus the RAW sampled stream from can_bit_timing (Block 1),
-- walks a CAN 2.0A/2.0B OR a CAN-FD frame, captures arbitration ID + control +
-- data + CRC, and emits 32-bit zs_msg messages per can_datalogger_protocol.yml.
--
-- CLASSIC (v0.1, silicon-validated): std (11-bit) + ext (29-bit) ID, 0..8 data
-- bytes, RTR, CRC-15. UNCHANGED by the FD fork below.
--
-- CAN-FD FORK (v0.2, DS-P2.64): the bit at the classic reserved position is FDF —
-- dominant ⇒ classic (untouched), recessive ⇒ FD. FDF sits at the S_R0 slot for a
-- STANDARD frame and the S_R1 slot for an EXTENDED frame; both FD paths converge
-- at S_FDRES. The FD chain captures res/BRS/ESI, decodes the non-linear DLC table
-- (0..64 bytes), reads the gray+even-parity stuff count, and selects CRC-17 (<=16
-- B) or CRC-21 (>16 B). It drives STUFF_MODE (fixed stuffing in stuff-count+CRC)
-- and RATE_SEL (data-phase bit rate when BRS=recessive).
--
--   FD CRC NOTE: unlike classic CAN, the FD CRC INCLUDES the dynamic stuff bits
--   (fed from the RAW/stuffed stream — FD_CRC_EN strobes BIT_VALID, not DS_VALID)
--   with a leading-'1' preset. The exact covered-bit boundary needs an external
--   golden FD frame to validate end-to-end (DS-P3.15, CTU CAN FD). At this FSM
--   level the crc_error COMPARE is tested by driving CRC17/21_COMPUTED directly,
--   exactly as the classic crc_error compare is (see test_can_frame_fsm).
--
-- DATA FLOW (top wires this) — see can_datalogger.vhd.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_pkg.all;       -- C_DATALOGGER_MSG_WIDTH (32)

entity can_frame_fsm is
  generic (
    G_CAN_DEV_ID      : natural range 0 to (2**5)-1 := 20;   -- system_ids.yml
    G_TIMESTAMP_WIDTH : natural range 16 to 32      := 16;
    G_ERROR_WIDTH     : integer                     := 2;
    G_FIFO_DEPTH_LOG2 : integer                     := 4      -- 16-deep msg FIFO
    );
  port (
    CLK         : in  std_logic;
    RST         : in  std_logic;
    ENABLE      : in  std_logic;                              -- per-device CONFIG enable
    -- raw sampled bit (from can_bit_timing) — used in IDLE for SOF arming AND to
    -- feed the FD CRC engines (FD CRC covers the stuffed stream)
    BIT_VALID   : in  std_logic;
    BIT_VALUE   : in  std_logic;
    -- de-stuffed field bit (from can_destuff) — the frame walk consumes these
    DS_VALID    : in  std_logic;
    DS_BIT      : in  std_logic;
    STUFF_ERROR : in  std_logic;                              -- 1-clk pulse from can_destuff (dynamic 6-run)
    FORM_ERROR  : in  std_logic := '0';                       -- 1-clk pulse from can_destuff (fixed-stuff form)
    TIMESTAMP   : in  std_logic_vector(G_TIMESTAMP_WIDTH-1 downto 0);
    STUFF_EN    : out std_logic;                              -- frames the stuffed region (to can_destuff)
    STUFF_MODE  : out std_logic;                              -- 1 = FD fixed stuffing (stuff-count + CRC)
    RATE_SEL    : out std_logic;                              -- 1 = FD data-phase bit rate (to can_bit_timing)
    -- classic CRC-15 engine window (can_crc15, de-stuffed feed)
    CRC_CLR      : out std_logic;
    CRC_EN       : out std_logic;
    CRC_COMPUTED : in  std_logic_vector(14 downto 0);
    -- FD CRC-17/21 engine window (can_crc17/can_crc21, RAW/stuffed feed) + results
    FD_CRC_CLR    : out std_logic;
    FD_CRC_EN     : out std_logic;
    FD_CRC_DIN    : out std_logic;                            -- region-muxed feed (raw data / de-stuffed SOF+stuffcnt)
    CRC17_COMPUTED : in std_logic_vector(16 downto 0) := (others => '0');
    CRC21_COMPUTED : in std_logic_vector(20 downto 0) := (others => '0');
    CAN_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
    CAN_MSG_VLD : out std_logic;
    CAN_MSG_RDY : in  std_logic;
    CAN_ERRORS  : out std_logic_vector(G_ERROR_WIDTH-1 downto 0);
    -- DS-P3.107/DS-T2.49 debug taps (Moore, from R) for the REA/ILA — DEBUG TARGET
    -- ONLY (canfd_demo). FSM state + the FD CRC-17 verdict: a 1-cycle DBG_CRC_FIRED
    -- pulse when crc_error fires, with the captured Cap17 and the engine's
    -- CRC17_COMPUTED latched at that instant so the analyzer reads captured-vs-
    -- computed. (This tap is what proved DS-T2.49 was a compute-side defect.)
    DBG_STATE     : out std_logic_vector(4 downto 0)  := (others => '0');
    DBG_CRC_FIRED : out std_logic := '0';
    DBG_CRC_CAP   : out std_logic_vector(16 downto 0) := (others => '0');
    DBG_CRC_COMP  : out std_logic_vector(16 downto 0) := (others => '0')
    );
end can_frame_fsm;

architecture Rtl of can_frame_fsm is

  -- ── Message format (32-bit): [31:27]=DevId [26:24]=Type [23:8]=Ts [7:0]=Payload
  constant C_TypeId      : std_logic_vector(2 downto 0) := "001";
  constant C_TypeData    : std_logic_vector(2 downto 0) := "010";
  constant C_TypeControl : std_logic_vector(2 downto 0) := "011";
  constant C_TypeEvent   : std_logic_vector(2 downto 0) := "100";
  constant C_TypeCrc     : std_logic_vector(2 downto 0) := "101";
  constant C_TypeFdCtrl  : std_logic_vector(2 downto 0) := "110";   -- FD control: {IDE,BRS,ESI,0,DLC}
  constant C_TypeFdAux   : std_logic_vector(2 downto 0) := "111";   -- FD aux: {parity_ok,0000,stuffcnt_gray}

  -- Event codes (can_datalogger_protocol.yml constants.events)
  constant C_EvDisabled   : std_logic_vector(7 downto 0) := x"00";
  constant C_EvEnabled    : std_logic_vector(7 downto 0) := x"03";
  constant C_EvFrameBegin : std_logic_vector(7 downto 0) := x"10";
  constant C_EvFrameEnd   : std_logic_vector(7 downto 0) := x"20";
  constant C_EvAckObs     : std_logic_vector(7 downto 0) := x"30";
  constant C_EvStuffErr   : std_logic_vector(7 downto 0) := x"40";
  constant C_EvFormErr    : std_logic_vector(7 downto 0) := x"41";
  constant C_EvAckErr     : std_logic_vector(7 downto 0) := x"43";
  constant C_EvCrcErr     : std_logic_vector(7 downto 0) := x"42";   -- own CRC != captured CRC
  constant C_EvStuffCntErr: std_logic_vector(7 downto 0) := x"45";   -- FD stuff-count parity mismatch

  constant C_FifoDepth : integer := 2**G_FIFO_DEPTH_LOG2;

  -- ── Frame FSM states ────────────────────────────────────────────────────────
  type StateType is (
    S_IDLE,        -- wait for SOF on the raw line
    S_SOF,         -- swallow the de-stuffed SOF echo
    S_ARB,         -- collect 11 base-ID bits (MSB first)
    S_RTRSRR,      -- bit 12: RTR (std) or SRR (ext); RRS for FD
    S_IDE,         -- bit 13: IDE — forks std vs ext
    S_EXTID,       -- collect 18 extended-ID bits
    S_EXTRTR,      -- ext RTR bit (RRS for FD)
    S_R1,          -- ext reserved r1 — FDF position for EXTENDED frames
    S_R0,          -- reserved r0 — FDF position for STANDARD frames
    S_DLC,         -- 4 DLC bits → then emit control + id burst (classic)
    S_DATA,        -- DLC data bytes (8 bits each, MSB first)
    S_CRC,         -- 15 CRC bits (classic)
    -- ── CAN-FD chain (both forks converge at S_FDRES) ──
    S_FDRES,       -- res bit (after FDF)
    S_BRS,         -- bit-rate switch
    S_ESI,         -- error state indicator
    S_FDDLC,       -- 4 FD DLC bits → emit fd_control + id burst, decode 0..64 B
    S_FDDATA,      -- 0..64 data bytes
    S_STUFFCNT,    -- 3 gray + 1 even-parity stuff-count bits (fixed stuffing begins)
    S_FDCRC,       -- 17 or 21 CRC bits (fixed stuffing)
    -- ── shared fixed-form tail (always nominal rate) ──
    S_CRCDELIM,    -- fixed-form recessive delimiter
    S_ACK,         -- ACK slot (dominant = someone acked)
    S_ACKDELIM,    -- fixed-form recessive delimiter
    S_EOF);        -- 7 recessive bits

  -- Up to FIVE messages stage at once (ext: control + 4 id bytes); pushed one
  -- per clk. CAN bit time >> clk_sys, so staging drains well before the next bit.
  type StageArray is array (0 to 5) of std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  type MsgRam     is array (0 to C_FifoDepth-1) of std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);

  -- ── Register record (ESA two-process) ───────────────────────────────────────
  type RegType is record
    EnableQ      : std_logic;                       -- delayed enable (lifecycle edge)
    State        : StateType;
    BitIdx       : integer range 0 to 20;           -- bit counter within a field (FD CRC needs 0..20)
    BaseId       : std_logic_vector(10 downto 0);   -- 11-bit base identifier
    ExtId        : std_logic_vector(17 downto 0);   -- 18-bit extended remainder
    IdeFlag      : std_logic;
    RtrFlag      : std_logic;
    FdfFlag      : std_logic;                       -- 1 = CAN-FD frame
    BrsFlag      : std_logic;                       -- bit-rate switch captured
    EsiFlag      : std_logic;                       -- error state indicator captured
    CrcIs21      : std_logic;                       -- 0 = CRC-17 (<=16 B), 1 = CRC-21 (>16 B)
    DlcReg       : std_logic_vector(3 downto 0);
    StuffCnt     : std_logic_vector(3 downto 0);    -- {gray[2:0], parity} captured
    DataLen      : integer range 0 to 64;           -- bytes to expect (FD: 0..64)
    DataByteIdx  : integer range 0 to 64;
    ByteAcc      : std_logic_vector(7 downto 0);    -- 8-bit MSB-first accumulator
    CrcReg       : std_logic_vector(20 downto 0);   -- captured wire CRC (up to 21 bits)
    DbgCrcFired  : std_logic;                        -- DS-T2.49 FD CRC verdict tap (debug target)
    DbgCrcCap    : std_logic_vector(16 downto 0);
    DbgCrcComp   : std_logic_vector(16 downto 0);
    Stg          : StageArray;
    StgCnt, StgRd: unsigned(2 downto 0);            -- 0..5 staged messages
    WrPtr, RdPtr : unsigned(G_FIFO_DEPTH_LOG2 downto 0);
    OverflowCnt  : unsigned(G_ERROR_WIDTH-1 downto 0);
  end record;

  constant C_RegInit : RegType := (
    EnableQ => '0', State => S_IDLE, BitIdx => 0,
    BaseId => (others => '0'), ExtId => (others => '0'),
    IdeFlag => '0', RtrFlag => '0', FdfFlag => '0', BrsFlag => '0', EsiFlag => '0',
    CrcIs21 => '0', DlcReg => (others => '0'), StuffCnt => (others => '0'),
    DataLen => 0, DataByteIdx => 0, ByteAcc => (others => '0'),
    CrcReg => (others => '0'),
    DbgCrcFired => '0', DbgCrcCap => (others => '0'), DbgCrcComp => (others => '0'),
    Stg => (others => (others => '0')),
    StgCnt => (others => '0'), StgRd => (others => '0'),
    WrPtr => (others => '0'), RdPtr => (others => '0'), OverflowCnt => (others => '0'));

  signal R, RNext : RegType := C_RegInit;   -- defined power-on state (= reset state)
  signal Ram      : MsgRam;
  signal RamWe    : std_logic;
  signal RamWrAddr: unsigned(G_FIFO_DEPTH_LOG2-1 downto 0);
  signal RamWrData: std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal FifoFull, FifoEmpty : std_logic;

  -- Assemble a 32-bit datalogger message.
  function MkMsg(MType : std_logic_vector(2 downto 0);
                 Ts    : std_logic_vector(G_TIMESTAMP_WIDTH-1 downto 0);
                 Pl    : std_logic_vector(7 downto 0)) return std_logic_vector is
  begin
    return std_logic_vector(to_unsigned(G_CAN_DEV_ID, 5)) & MType & Ts(15 downto 0) & Pl;
  end function;

  -- FD DLC → data byte count (the non-linear table, ISO 11898-1:2015 / Bosch FD v1.0)
  function FdDlcToBytes(Dlc : std_logic_vector(3 downto 0)) return integer is
  begin
    case to_integer(unsigned(Dlc)) is
      when 0 to 8 => return to_integer(unsigned(Dlc));
      when 9      => return 12;
      when 10     => return 16;
      when 11     => return 20;
      when 12     => return 24;
      when 13     => return 32;
      when 14     => return 48;
      when others => return 64;   -- 15
    end case;
  end function;

  -- Classic dynamic-stuff region (SOF..CRC). FD extends through S_FDCRC.
  function StuffActive(S : StateType) return boolean is
  begin
    case S is
      when S_SOF | S_ARB | S_RTRSRR | S_IDE | S_EXTID | S_EXTRTR
         | S_R1 | S_R0 | S_DLC | S_DATA | S_CRC
         | S_FDRES | S_BRS | S_ESI | S_FDDLC | S_FDDATA | S_STUFFCNT | S_FDCRC
        => return true;
      when others => return false;
    end case;
  end function;

  -- FD fixed-stuffing region: stuff-count + CRC fields only.
  function FixedStuffRegion(S : StateType) return boolean is
  begin
    case S is
      when S_STUFFCNT | S_FDCRC => return true;
      when others => return false;
    end case;
  end function;

  -- FD data-phase (bit-rate-switched when BRS=recessive): ESI..FDCRC, PLUS the
  -- CRC DELIMITER. Per ISO 11898-1:2015 (CAN FD bit-rate switching), cross-checked
  -- vs the ctu-fee/ctucanfd_ip_core oracle (D.26), the data->nominal switch-back is
  -- AT the CRC delimiter's SAMPLE POINT — so the delimiter's leading phase is still
  -- DATA-rate. Keeping RATE_SEL='1' through S_CRCDELIM makes can_bit_timing sample
  -- the delimiter at the DATA sample point (correct, recessive); RATE_SEL then drops
  -- at the delimiter->ACK boundary and the recessive->dominant ACK edge hard-syncs
  -- the rest. Excluding S_CRCDELIM (the pre-fix behaviour) made the datalogger count a
  -- FULL NOMINAL delimiter and sample ~a-bit too late, into the ACK slot ->
  -- form_error+ack_error on real CTU BRS frames (DS-T2.48).
  function FdDataPhase(S : StateType) return boolean is
  begin
    case S is
      when S_ESI | S_FDDLC | S_FDDATA | S_STUFFCNT | S_FDCRC | S_CRCDELIM => return true;
      when others => return false;
    end case;
  end function;

  -- Classic CRC-15 covers SOF..end-of-data over the DE-STUFFED stream (excl. CRC bits).
  function CrcCovered(S : StateType) return boolean is
  begin
    case S is
      when S_SOF | S_ARB | S_RTRSRR | S_IDE | S_EXTID | S_EXTRTR
         | S_R1 | S_R0 | S_DLC | S_DATA => return true;
      when others => return false;
    end case;
  end function;

  -- FD CRC coverage (ISO 11898-1:2015, silicon-anchored DS-T2.49 2026-06-17):
  -- DYNAMIC stuff bits are INCLUDED, FIXED stuff bits are EXCLUDED. So the CRC
  -- spans SOF..end-of-stuff-count split across two feeds:
  --   * the dynamic region (ARB..end-of-DATA) over the RAW/STUFFED stream — every
  --     wire bit, dynamic stuff bits included — this function;
  --   * SOF and the stuff-count field over the DE-STUFFED stream (DS_VALID): SOF
  --     (the IDLE arming bit, no fresh BIT_VALID in S_SOF) and the 4 real
  --     stuff-count bits, with the fixed stuff bits dropped — gated in FD_CRC_EN.
  -- The CRC sequence itself is not covered. The earlier single RAW feed wrongly
  -- folded the fixed stuff bit(s) of the stuff-count into the CRC AND lacked the
  -- ISO MSB-preset init -> spurious crc_error on every FD frame (the datalogger
  -- captured the wire CRC fine but computed the wrong one). Anchored to clean
  -- edge-capture wire goldens: 0x456->0x02012, 0x111->0x1b647, 0x7AB->0x07288
  -- (CRC-17), 0x123->0x0bbd35 (CRC-21).
  function FdCrcDynRegion(S : StateType) return boolean is
  begin
    case S is
      when S_ARB | S_RTRSRR | S_IDE | S_EXTID | S_EXTRTR | S_R1 | S_R0
         | S_FDRES | S_BRS | S_ESI | S_FDDLC | S_FDDATA => return true;
      when others => return false;
    end case;
  end function;

begin

  FifoFull  <= '1' when (R.WrPtr(G_FIFO_DEPTH_LOG2-1 downto 0) = R.RdPtr(G_FIFO_DEPTH_LOG2-1 downto 0))
                        and (R.WrPtr(G_FIFO_DEPTH_LOG2) /= R.RdPtr(G_FIFO_DEPTH_LOG2)) else '0';
  FifoEmpty <= '1' when R.WrPtr = R.RdPtr else '0';

  -- STUFF_EN: stuffed fields, PLUS combinationally on the first raw dominant in
  -- IDLE so destuff seeds the stuff-run on SOF (the seam, see classic header).
  STUFF_EN <= '1' when StuffActive(R.State)
                       or (R.State = S_IDLE and BIT_VALID = '1' and BIT_VALUE = '0')
                  else '0';

  -- STUFF_MODE: FD fixed stuffing only in the stuff-count + CRC fields.
  STUFF_MODE <= '1' when FixedStuffRegion(R.State) else '0';

  -- RATE_SEL: FD data-phase bit rate, gated by the captured BRS bit (Moore level).
  RATE_SEL <= '1' when (R.FdfFlag = '1' and R.BrsFlag = '1' and FdDataPhase(R.State))
                  else '0';

  -- classic CRC-15 window (de-stuffed feed)
  CRC_CLR <= '1' when R.State = S_IDLE else '0';
  CRC_EN  <= '1' when (DS_VALID = '1' and CrcCovered(R.State)) else '0';

  -- FD CRC-17/21 window (DS-T2.49). Coverage = DYNAMIC stuff bits IN, FIXED stuff
  -- bits OUT, ISO MSB-preset init (the engines' INIT_MSB tie-high). Two feeds:
  --   * dynamic region (ARB..DATA): the RAW/stuffed stream (BIT_VALID/BIT_VALUE) —
  --     dynamic stuff bits covered;
  --   * SOF and the stuff-count field: the DE-STUFFED stream (DS_VALID/DS_BIT) —
  --     SOF (the IDLE arming bit, no fresh BIT_VALID in S_SOF) and the 4 real
  --     stuff-count bits, with the fixed stuff bits excluded.
  -- CRC field: not covered. FD_CRC_DIN is the matching DIN mux (driven here, where
  -- the state is known) — can_datalogger wires it straight to the engines.
  FD_CRC_CLR <= '1' when R.State = S_IDLE else '0';
  FD_CRC_EN  <= '1' when (BIT_VALID = '1' and FdCrcDynRegion(R.State)) else
                '1' when (DS_VALID  = '1' and (R.State = S_SOF or R.State = S_STUFFCNT))
                else '0';
  FD_CRC_DIN <= DS_BIT when (R.State = S_SOF or R.State = S_STUFFCNT) else BIT_VALUE;

  -- DS-P3.107/DS-T2.49 debug taps (Moore, from the registered record R). Debug target.
  DBG_STATE     <= std_logic_vector(to_unsigned(StateType'pos(R.State), 5));
  DBG_CRC_FIRED <= R.DbgCrcFired;
  DBG_CRC_CAP   <= R.DbgCrcCap;
  DBG_CRC_COMP  <= R.DbgCrcComp;

  -----------------------------------------------------------------------------
  -- P_Comb: lifecycle edges, frame walk, message staging, FIFO push + read.
  -----------------------------------------------------------------------------
  P_Comb : process(R, ENABLE, BIT_VALID, BIT_VALUE, DS_VALID, DS_BIT, STUFF_ERROR,
                   FORM_ERROR, TIMESTAMP, CAN_MSG_RDY, FifoFull, FifoEmpty,
                   CRC_COMPUTED, CRC17_COMPUTED, CRC21_COMPUTED)
    variable V        : RegType;
    variable N        : integer range 0 to 6;
    variable Parts    : StageArray;
    variable FullId   : std_logic_vector(28 downto 0);
    variable NewByte  : std_logic_vector(7 downto 0);
    variable Dlc4     : std_logic_vector(3 downto 0);
    variable Dlen     : integer range 0 to 64;
    variable C15      : std_logic_vector(14 downto 0);
    variable Cap17    : std_logic_vector(16 downto 0);
    variable Cap21    : std_logic_vector(20 downto 0);
    variable ParOk    : std_logic;
  begin
    V := R;

    V.EnableQ := ENABLE;
    V.DbgCrcFired := '0';                        -- DS-T2.49: default low (1-cycle verdict pulse)

    -- default RAM write controls
    RamWe     <= '0';
    RamWrAddr <= R.WrPtr(G_FIFO_DEPTH_LOG2-1 downto 0);
    RamWrData <= R.Stg(to_integer(R.StgRd));

    if R.StgCnt /= 0 then
      -- PUSH: drain one staged message into the FIFO
      if FifoFull = '0' then
        RamWe   <= '1';
        V.WrPtr := R.WrPtr + 1;
      elsif R.OverflowCnt /= (R.OverflowCnt'range => '1') then
        V.OverflowCnt := R.OverflowCnt + 1;
      end if;
      V.StgRd  := R.StgRd + 1;
      V.StgCnt := R.StgCnt - 1;
    else
      -- CAPTURE: stage new work (only when drained)
      N := 0; Parts := (others => (others => '0'));

      if ENABLE = '1' and R.EnableQ = '0' then
        Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvEnabled); N := 1;
      elsif ENABLE = '0' and R.EnableQ = '1' then
        Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvDisabled); N := 1;
        V.State  := S_IDLE;                          -- abort any in-flight frame
      elsif ENABLE = '1' and STUFF_ERROR = '1' and R.State /= S_IDLE then
        Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvStuffErr); N := 1;
        V.State  := S_IDLE;                          -- stuff violation → abort frame
      elsif ENABLE = '1' and FORM_ERROR = '1' and R.State /= S_IDLE then
        Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvFormErr); N := 1;
        V.State  := S_IDLE;                          -- FD fixed-stuff form fault → abort
      elsif ENABLE = '1' then
        case R.State is

          when S_IDLE =>
            -- SOF = first raw dominant after idle (raw, so STUFF_EN covers it).
            if BIT_VALID = '1' and BIT_VALUE = '0' then
              V.BaseId    := (others => '0');
              V.ExtId     := (others => '0');
              V.CrcReg    := (others => '0');
              V.ByteAcc   := (others => '0');
              V.DataByteIdx := 0;
              V.FdfFlag   := '0';
              V.BrsFlag   := '0';
              V.EsiFlag   := '0';
              V.CrcIs21   := '0';
              V.State     := S_SOF;
            end if;

          when S_SOF =>
            -- swallow the de-stuffed SOF echo AND emit frame_begin now.
            if DS_VALID = '1' then
              Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvFrameBegin); N := 1;
              V.BitIdx := 0;
              V.State  := S_ARB;
            end if;

          when S_ARB =>
            if DS_VALID = '1' then
              V.BaseId := R.BaseId(9 downto 0) & DS_BIT;   -- MSB-first
              if R.BitIdx = 10 then
                V.State := S_RTRSRR;
              else
                V.BitIdx := R.BitIdx + 1;
              end if;
            end if;

          when S_RTRSRR =>
            if DS_VALID = '1' then
              V.RtrFlag := DS_BIT;                          -- provisional (RTR if std)
              V.State   := S_IDE;
            end if;

          when S_IDE =>
            if DS_VALID = '1' then
              V.IdeFlag := DS_BIT;
              if DS_BIT = '0' then
                V.State := S_R0;                            -- standard: FDF/r0 next
              else
                V.BitIdx := 0;                              -- extended: 18 more ID bits
                V.State  := S_EXTID;
              end if;
            end if;

          when S_EXTID =>
            if DS_VALID = '1' then
              V.ExtId := R.ExtId(16 downto 0) & DS_BIT;     -- MSB-first
              if R.BitIdx = 17 then
                V.State := S_EXTRTR;
              else
                V.BitIdx := R.BitIdx + 1;
              end if;
            end if;

          when S_EXTRTR =>
            if DS_VALID = '1' then
              V.RtrFlag := DS_BIT;                          -- real RTR (ext classic); RRS for FD
              V.State   := S_R1;
            end if;

          when S_R1 =>
            -- EXTENDED frames: this bit is FDF. recessive ⇒ FD, dominant ⇒ classic r0.
            if DS_VALID = '1' then
              if DS_BIT = '1' then
                V.FdfFlag := '1';
                V.State   := S_FDRES;                       -- extended FD
              else
                V.State   := S_R0;                          -- extended classic (r0 next)
              end if;
            end if;

          when S_R0 =>
            -- STANDARD frames: this bit is FDF (IdeFlag=0). recessive ⇒ FD.
            -- EXTENDED classic reaches here as plain r0 (FDF already 0 at S_R1).
            if DS_VALID = '1' then
              if R.IdeFlag = '0' and DS_BIT = '1' then
                V.FdfFlag := '1';
                V.State   := S_FDRES;                       -- standard FD
              else
                V.BitIdx := 0;
                V.State  := S_DLC;                          -- classic DLC
              end if;
            end if;

          when S_DLC =>
            if DS_VALID = '1' then
              V.DlcReg := R.DlcReg(2 downto 0) & DS_BIT;    -- MSB-first
              if R.BitIdx = 3 then
                Dlc4 := R.DlcReg(2 downto 0) & DS_BIT;
                if R.RtrFlag = '1' then
                  Dlen := 0;
                elsif unsigned(Dlc4) > 8 then
                  Dlen := 8;
                else
                  Dlen := to_integer(unsigned(Dlc4));
                end if;
                V.DataLen := Dlen;

                -- control byte: {IDE, RTR, 00, DLC[3:0]}
                Parts(0) := MkMsg(C_TypeControl, TIMESTAMP,
                                  R.IdeFlag & R.RtrFlag & "00" & Dlc4);
                if R.IdeFlag = '0' then
                  Parts(1) := MkMsg(C_TypeId, TIMESTAMP, "00000" & R.BaseId(10 downto 8));
                  Parts(2) := MkMsg(C_TypeId, TIMESTAMP, R.BaseId(7 downto 0));
                  N := 3;
                else
                  FullId := R.BaseId & R.ExtId;
                  Parts(1) := MkMsg(C_TypeId, TIMESTAMP, "000" & FullId(28 downto 24));
                  Parts(2) := MkMsg(C_TypeId, TIMESTAMP, FullId(23 downto 16));
                  Parts(3) := MkMsg(C_TypeId, TIMESTAMP, FullId(15 downto 8));
                  Parts(4) := MkMsg(C_TypeId, TIMESTAMP, FullId(7 downto 0));
                  N := 5;
                end if;

                V.DataByteIdx := 0;
                V.BitIdx      := 0;
                V.ByteAcc     := (others => '0');
                if Dlen = 0 then
                  V.State := S_CRC;
                else
                  V.State := S_DATA;
                end if;
              else
                V.BitIdx := R.BitIdx + 1;
              end if;
            end if;

          when S_DATA =>
            if DS_VALID = '1' then
              NewByte := R.ByteAcc(6 downto 0) & DS_BIT;    -- MSB-first
              if R.BitIdx = 7 then
                Parts(0) := MkMsg(C_TypeData, TIMESTAMP, NewByte); N := 1;
                V.ByteAcc := (others => '0');
                V.BitIdx  := 0;
                if R.DataByteIdx = R.DataLen - 1 then
                  V.State := S_CRC;
                else
                  V.DataByteIdx := R.DataByteIdx + 1;
                end if;
              else
                V.ByteAcc := NewByte;
                V.BitIdx  := R.BitIdx + 1;
              end if;
            end if;

          when S_CRC =>
            if DS_VALID = '1' then
              V.CrcReg := R.CrcReg(19 downto 0) & DS_BIT;   -- MSB-first (low 15 used)
              if R.BitIdx = 14 then
                C15 := R.CrcReg(13 downto 0) & DS_BIT;       -- captured wire CRC-15
                Parts(0) := MkMsg(C_TypeCrc, TIMESTAMP, C15(14 downto 7));
                Parts(1) := MkMsg(C_TypeCrc, TIMESTAMP, C15(6 downto 0) & '0');
                N := 2;
                if C15 /= CRC_COMPUTED then
                  Parts(2) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvCrcErr);
                  N := 3;
                end if;
                V.State := S_CRCDELIM;                       -- STUFF_EN drops here
              else
                V.BitIdx := R.BitIdx + 1;
              end if;
            end if;

          -- ───────────────────────── CAN-FD chain ─────────────────────────
          when S_FDRES =>
            if DS_VALID = '1' then
              V.State := S_BRS;                             -- res bit (reserved, ignored)
            end if;

          when S_BRS =>
            if DS_VALID = '1' then
              V.BrsFlag := DS_BIT;                          -- 1 = switch to data rate
              V.State   := S_ESI;
            end if;

          when S_ESI =>
            if DS_VALID = '1' then
              V.EsiFlag := DS_BIT;                          -- 1 = error-passive transmitter
              V.BitIdx  := 0;
              V.State   := S_FDDLC;
            end if;

          when S_FDDLC =>
            if DS_VALID = '1' then
              V.DlcReg := R.DlcReg(2 downto 0) & DS_BIT;    -- MSB-first
              if R.BitIdx = 3 then
                Dlc4 := R.DlcReg(2 downto 0) & DS_BIT;
                Dlen := FdDlcToBytes(Dlc4);                 -- non-linear 0..64
                V.DataLen := Dlen;
                if Dlen <= 16 then V.CrcIs21 := '0'; else V.CrcIs21 := '1'; end if;

                -- fd_control byte: {IDE, BRS, ESI, 0, DLC[3:0]}  (FDF implied by type)
                Parts(0) := MkMsg(C_TypeFdCtrl, TIMESTAMP,
                                  R.IdeFlag & R.BrsFlag & R.EsiFlag & '0' & Dlc4);
                if R.IdeFlag = '0' then
                  Parts(1) := MkMsg(C_TypeId, TIMESTAMP, "00000" & R.BaseId(10 downto 8));
                  Parts(2) := MkMsg(C_TypeId, TIMESTAMP, R.BaseId(7 downto 0));
                  N := 3;
                else
                  FullId := R.BaseId & R.ExtId;
                  Parts(1) := MkMsg(C_TypeId, TIMESTAMP, "000" & FullId(28 downto 24));
                  Parts(2) := MkMsg(C_TypeId, TIMESTAMP, FullId(23 downto 16));
                  Parts(3) := MkMsg(C_TypeId, TIMESTAMP, FullId(15 downto 8));
                  Parts(4) := MkMsg(C_TypeId, TIMESTAMP, FullId(7 downto 0));
                  N := 5;
                end if;

                V.DataByteIdx := 0;
                V.BitIdx      := 0;
                V.ByteAcc     := (others => '0');
                if Dlen = 0 then
                  V.State := S_STUFFCNT;
                else
                  V.State := S_FDDATA;
                end if;
              else
                V.BitIdx := R.BitIdx + 1;
              end if;
            end if;

          when S_FDDATA =>
            if DS_VALID = '1' then
              NewByte := R.ByteAcc(6 downto 0) & DS_BIT;    -- MSB-first
              if R.BitIdx = 7 then
                Parts(0) := MkMsg(C_TypeData, TIMESTAMP, NewByte); N := 1;
                V.ByteAcc := (others => '0');
                V.BitIdx  := 0;
                if R.DataByteIdx = R.DataLen - 1 then
                  V.State := S_STUFFCNT;
                else
                  V.DataByteIdx := R.DataByteIdx + 1;
                end if;
              else
                V.ByteAcc := NewByte;
                V.BitIdx  := R.BitIdx + 1;
              end if;
            end if;

          when S_STUFFCNT =>
            -- 3 gray bits (MSB-first) + 1 even-parity bit. STUFF_MODE high here.
            if DS_VALID = '1' then
              V.StuffCnt := R.StuffCnt(2 downto 0) & DS_BIT;
              if R.BitIdx = 3 then
                -- even parity over all 4 bits ⇒ XOR = 0
                ParOk := not (R.StuffCnt(2) xor R.StuffCnt(1) xor R.StuffCnt(0) xor DS_BIT);
                -- fd_aux: {parity_ok, 0000, stuff_count_gray[2:0]}
                Parts(0) := MkMsg(C_TypeFdAux, TIMESTAMP,
                                  ParOk & "0000" & R.StuffCnt(2 downto 0));
                N := 1;
                if ParOk = '0' then
                  Parts(1) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvStuffCntErr);
                  N := 2;
                end if;
                V.BitIdx := 0;
                V.State  := S_FDCRC;
              else
                V.BitIdx := R.BitIdx + 1;
              end if;
            end if;

          when S_FDCRC =>
            -- 17 (CrcIs21=0) or 21 (CrcIs21=1) CRC bits. STUFF_MODE high.
            if DS_VALID = '1' then
              V.CrcReg := R.CrcReg(19 downto 0) & DS_BIT;   -- MSB-first
              if (R.CrcIs21 = '0' and R.BitIdx = 16) or (R.CrcIs21 = '1' and R.BitIdx = 20) then
                if R.CrcIs21 = '0' then
                  Cap17 := R.CrcReg(15 downto 0) & DS_BIT;   -- captured 17-bit CRC
                  -- 3 crc bytes, big-endian zero-extended to 24 bits
                  Parts(0) := MkMsg(C_TypeCrc, TIMESTAMP, "0000000" & Cap17(16));
                  Parts(1) := MkMsg(C_TypeCrc, TIMESTAMP, Cap17(15 downto 8));
                  Parts(2) := MkMsg(C_TypeCrc, TIMESTAMP, Cap17(7 downto 0));
                  N := 3;
                  -- DS-T2.49 REA tap (debug target): latch captured + computed.
                  V.DbgCrcCap  := Cap17;
                  V.DbgCrcComp := CRC17_COMPUTED;
                  if Cap17 /= CRC17_COMPUTED then
                    V.DbgCrcFired := '1';
                    Parts(3) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvCrcErr);
                    N := 4;
                  end if;
                else
                  Cap21 := R.CrcReg(19 downto 0) & DS_BIT;   -- captured 21-bit CRC
                  Parts(0) := MkMsg(C_TypeCrc, TIMESTAMP, "000" & Cap21(20 downto 16));
                  Parts(1) := MkMsg(C_TypeCrc, TIMESTAMP, Cap21(15 downto 8));
                  Parts(2) := MkMsg(C_TypeCrc, TIMESTAMP, Cap21(7 downto 0));
                  N := 3;
                  if Cap21 /= CRC21_COMPUTED then
                    Parts(3) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvCrcErr);
                    N := 4;
                  end if;
                end if;
                V.State := S_CRCDELIM;                       -- STUFF_EN/STUFF_MODE drop
              else
                V.BitIdx := R.BitIdx + 1;
              end if;
            end if;

          -- ──────────────────────── shared tail ────────────────────────
          when S_CRCDELIM =>
            if DS_VALID = '1' then
              if DS_BIT /= '1' then                          -- expect recessive
                Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvFormErr); N := 1;
              end if;
              V.State := S_ACK;
            end if;

          when S_ACK =>
            if DS_VALID = '1' then
              if DS_BIT = '0' then
                Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvAckObs);  -- someone acked
              else
                Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvAckErr);  -- nobody acked
              end if;
              N := 1;
              V.State := S_ACKDELIM;
            end if;

          when S_ACKDELIM =>
            if DS_VALID = '1' then
              if DS_BIT /= '1' then
                Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvFormErr); N := 1;
              end if;
              V.BitIdx := 0;
              V.State  := S_EOF;
            end if;

          when S_EOF =>
            if DS_VALID = '1' then
              if R.BitIdx = 6 then
                Parts(0) := MkMsg(C_TypeEvent, TIMESTAMP, C_EvFrameEnd); N := 1;
                V.State := S_IDLE;
              else
                V.BitIdx := R.BitIdx + 1;
              end if;
            end if;

        end case;
      end if;

      if N /= 0 then
        V.Stg := Parts; V.StgCnt := to_unsigned(N, 3); V.StgRd := (others => '0');
      end if;
    end if;

    -- read-side handshake
    if FifoEmpty = '0' and CAN_MSG_RDY = '1' then
      V.RdPtr := R.RdPtr + 1;
    end if;

    RNext <= V;
  end process;

  -----------------------------------------------------------------------------
  -- P_Seq: register the record (sync reset) + the inferred message RAM.
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

  CAN_MSG_OUT <= Ram(to_integer(R.RdPtr(G_FIFO_DEPTH_LOG2-1 downto 0)));
  CAN_MSG_VLD <= not FifoEmpty;
  CAN_ERRORS  <= std_logic_vector(R.OverflowCnt);

end Rtl;
