-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- Title      : level_datalogger
-- Project    : datalogger
-------------------------------------------------------------------------------
-- File       : level_datalogger.vhd
-- Author     : Daniel  <djmazure@routertl.com>
-- Company    :
-- Created    : 2025-06-10
-- Last update: 2025-06-10
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: Level Datalogger. Creates a datalogger message when the injector pins
--              are toggled. The message contains the timestamp, the pin state
-------------------------------------------------------------------------------
-- Copyright (c) 2025 RouteRTL
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2025-02-11  1.0      dmazure Created
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use IEEE.MATH_REAL.all;
use ieee.std_logic_misc.all;

library tich;
use tich.datalogger_pkg.all;

entity level_datalogger is
    generic (
      G_LEVEL_ID_WIDTH  : integer                                    := 5;
      G_LEVEL_DEV_ID    : natural range 0 to (2**G_LEVEL_ID_WIDTH)-1 := 24;
      G_TIMESTAMP_WIDTH : natural range 16 to 32                     := 16;
      G_CONFIG_WIDTH    : integer                                    := 16;
      G_ERROR_WIDTH     : integer                                    := 2;
      G_SIM_EN          : integer                                    := 0; -- 1 for simulation, 0 for synthesis
      G_ILA_EN          : integer                                    := 1
      );
    port (
      CLK         : in  std_logic;
      RST         : in  std_logic;
      LEVEL_PINS  : in  std_logic_vector(C_LEVEL_INJ_PINS-1 downto 0);
      CONFIG      : in  std_logic_vector(G_CONFIG_WIDTH-1 downto 0);
      TIMESTAMP   : in  std_logic_vector(G_TIMESTAMP_WIDTH-1 downto 0);
      -- LEVEL
      LEVEL_MSG_OUT : out std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0');
      LEVEL_MSG_VLD : out std_logic;
      LEVEL_MSG_RDY : in  std_logic;
      --
      LEVEL_ERRORS : out std_logic_vector(G_ERROR_WIDTH-1 downto 0)
      );
end level_datalogger;


architecture rtl of level_datalogger is

  constant C_MSG_INJ_CODE         : std_logic_vector(2 downto 0) := "000";
  constant C_TIMESTAMP_SLICE_HIGH : integer := 16;

  signal deviceLoadConfig : std_logic_vector(G_CONFIG_WIDTH-G_LEVEL_ID_WIDTH-1 downto 0);
  signal configEnable     : std_logic;
  -- DS-P3.90 Phase 2: per-datalogger reset (global RST OR this device disabled).
  signal dataloggerRst       : std_logic;

  signal levelPins_r : std_logic_vector(C_LEVEL_INJ_PINS-1 downto 0) := (others => '0');
  signal msgVld_r    : std_logic := '0';
  signal msg         : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal msgOut      : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0) := (others => '0'); -- not a register, but there are some bits that might not be used thus need to be initialized
  signal pinChange_r : std_logic := '0';
  -- DS-T3.11: the (timestamp, pins) tuple latched at the CHANGE instant, so the
  -- frame reports WHEN the level changed — not when the arbiter finally accepted
  -- it. Reading TIMESTAMP/LEVEL_PINS combinationally at the (RDY-stallable)
  -- delivery lagged the real change by the whole backpressure window.
  signal tsCaptured_r   : std_logic_vector(C_TIMESTAMP_SLICE_HIGH-1 downto 0) := (others => '0');
  signal pinsCaptured_r : std_logic_vector(C_LEVEL_INJ_PINS-1 downto 0) := (others => '0');

  -- DS-P3.90 Phase 1: uniform symmetric ENABLED/DISABLED lifecycle frames.
  constant C_LEVEL_EVENT_TYPE     : std_logic_vector(2 downto 0) := "100";
  constant C_LEVEL_EVENT_ENABLED  : std_logic_vector(7 downto 0) := x"03";
  constant C_LEVEL_EVENT_DISABLED : std_logic_vector(7 downto 0) := x"00";
  signal lifecycleMsg : std_logic_vector(C_DATALOGGER_MSG_WIDTH-1 downto 0);
  signal lifecycleVld : std_logic;

begin

    assert C_DATALOGGER_MSG_WIDTH - G_LEVEL_ID_WIDTH - C_TIMESTAMP_SLICE_HIGH - C_MSG_INJ_CODE'length >= C_LEVEL_INJ_PINS;

    GEN_INFO: if G_SIM_EN = 1 generate
      assert false report "Level Datalogger: Simulation mode enabled" severity note;
      assert false report "Level Datalogger: Device ID: " & integer'image(G_LEVEL_DEV_ID) severity note;
      assert false report "Level Datalogger: Config Width: " & integer'image(G_CONFIG_WIDTH) severity note;
      assert false report "Level Datalogger: Timestamp Width: " & integer'image(G_TIMESTAMP_WIDTH) severity note;
    end generate GEN_INFO;

  -- Register the configuration values
  i_config_control : entity tich.config_control
  generic map (
    G_DEV_ID_WIDTH => G_LEVEL_ID_WIDTH,
    G_DEV_ID       => G_LEVEL_DEV_ID,
    G_CONFIG_WIDTH => G_CONFIG_WIDTH
    )
  port map (
    CLK        => CLK,
    RST        => RST,
    CONFIG     => CONFIG,
    CONFIG_OUT => deviceLoadConfig,
      CAPTURE_RST  => dataloggerRst  -- DS-P3.90 Phase 2: resets the decode regs on disable
    );

    --
    configEnable <= deviceLoadConfig(0);

    -- DS-P3.90 Phase 1: uniform ENABLED/DISABLED lifecycle, in the always-on
    -- (global RST) domain so it survives a future per-datalogger CAPTURE_RST (H.96).
    i_lifecycle : entity tich.datalogger_lifecycle
      generic map (
        G_DEV_ID        => G_LEVEL_DEV_ID,
        G_DEV_ID_WIDTH  => G_LEVEL_ID_WIDTH,
        G_TYPE          => C_LEVEL_EVENT_TYPE,
        G_TS_WIDTH      => C_TIMESTAMP_SLICE_HIGH,
        G_CODE_ENABLED  => C_LEVEL_EVENT_ENABLED,
        G_CODE_DISABLED => C_LEVEL_EVENT_DISABLED
        )
      port map (
        CLK       => CLK,
        RST       => RST,
        CONFIG_EN => configEnable,
        TIMESTAMP => TIMESTAMP(C_TIMESTAMP_SLICE_HIGH-1 downto 0),
        MSG_OUT   => lifecycleMsg,
        MSG_VLD   => lifecycleVld
        );

    -- Main logic

    pr_capture: process(CLK)
    begin
      if rising_edge(CLK) then
        if configEnable = '1' then
          if LEVEL_MSG_RDY = '1' then -- 1 cycle pulse if the arbiter is ready, otherwise, hold.
            levelPins_r <= LEVEL_PINS;
            pinChange_r <= '0';
            msgVld_r    <= pinChange_r;
          end if;
          if levelPins_r /= LEVEL_PINS then
            -- DS-T3.42: in the cycle a pending change is delivered (RDY=1,
            -- pinChange_r=1) the RDY branch above has just cleared pinChange_r
            -- and taken LEVEL_PINS as the new baseline, but levelPins_r still
            -- holds the OLD value here, so this test sees the very change that
            -- is being delivered and re-armed it: a second identical frame.
            -- A change that is the pending one (LEVEL_PINS = pinsCaptured_r)
            -- is not a new change; anything else is, and is flagged as before.
            if not (LEVEL_MSG_RDY = '1' and pinChange_r = '1'
                    and LEVEL_PINS = pinsCaptured_r) then
              pinChange_r <= '1';
            end if;
            -- DS-T3.11: freeze (timestamp, pins) on the FIRST pending change
            -- (pinChange_r still '0'); hold them through any backpressure stall
            -- until delivered, so the frame carries the change-instant values.
            if pinChange_r = '0' then
              tsCaptured_r   <= TIMESTAMP(C_TIMESTAMP_SLICE_HIGH-1 downto 0);
              pinsCaptured_r <= LEVEL_PINS(C_LEVEL_INJ_PINS-1 downto 0);
            end if;
          end if;
        end if;
        -- DS-P3.90 Phase 2: per-datalogger reset. dataloggerRst = global RST OR this
        -- device disabled (config ENABLE=0), so ENABLE=0 flushes ALL decode
        -- state — the last captured level, the pending-change flag and the
        -- output-valid — and a re-enable starts a clean capture (it re-emits the
        -- current level because levelPins_r is cleared). Lifecycle + config
        -- stay on global RST (H.96); only the decode regs take dataloggerRst.
        if dataloggerRst = '1' then
          levelPins_r    <= (others => '0');
          pinChange_r    <= '0';
          msgVld_r       <= '0';
          tsCaptured_r   <= (others => '0');  -- DS-T3.11
          pinsCaptured_r <= (others => '0');  -- DS-T3.11
        end if;
      end if;
    end process;


    -- DS-T3.11: build the frame from the capture-instant latches, not live TIMESTAMP/LEVEL_PINS
    msg(C_DATALOGGER_MSG_WIDTH-1 downto C_LEVEL_INJ_PINS) <= std_logic_vector(to_unsigned(G_LEVEL_DEV_ID, G_LEVEL_ID_WIDTH)) & C_MSG_INJ_CODE & tsCaptured_r;
    msg(C_LEVEL_INJ_PINS -1 downto 0)                  <= pinsCaptured_r;

    -- Lifecycle frame (ENABLED/DISABLED) takes top priority over the level
    -- payload so an enable/disable marker is never masked (DS-P3.90 Phase 1).
    msgOut <= lifecycleMsg when lifecycleVld = '1' else
              msg          when msgVld_r = '1' else
              (others => '0');

    LEVEL_MSG_OUT <= msgOut;
    LEVEL_MSG_VLD <= lifecycleVld or msgVld_r;

    LEVEL_ERRORS  <= (others => '0'); -- No errors


end rtl;