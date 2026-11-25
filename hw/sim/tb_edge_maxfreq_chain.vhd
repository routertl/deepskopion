-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- tb_edge_maxfreq_chain.vhd  —  DS-P3.117 gather→maxfreq_event_bank integration
-------------------------------------------------------------------------------
-- Proves the LIVE in-band max-frequency datapath end-to-end at the gather level:
--
--   cocotb pulse-gen ─ EDGE0..2_IN ─→ CAPTURE_PINS ─→ datalogger_gather
--        (real edges)                                   │
--                                  EDGE_MAX_IVALS / EDGE_MAX_NEW (per channel)
--                                                       ▼
--                                              maxfreq_event_bank
--                                                       │  FR_* (framer input)
--                                                       ▼
--                                   (the cocotb test reconstructs the zs_msg
--                                    the framer would pack and asserts it is a
--                                    byte-exact EVT_EDGE_MAXFREQ frame, then
--                                    feeds the bytes through the daemon decoder)
--
-- This is the gather→bank half of deepskopion_top's 4th framer input. The bank
-- is wired EXACTLY as the top wires it (same generics + CONFIG-driven RE_ARM),
-- so a pass here proves the same RTL the top elaborates. The 4:1 framer mux
-- itself is covered by the top's batch/error/edge regression tests staying green.
--
-- EDGE is a passive single-line monitor: no slave/responder BFM.
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library tich;
use tich.datalogger_config_pkg.all;
use tich.datalogger_pkg.all;
use tich.datalogger_records_pkg.all;
use tich.system_regbank_pkg.all;
use tich.zs_msg_pkg.all;

entity tb_edge_maxfreq_chain is
  generic (
    G_SYS_CLK : natural := 100_000_000
  );
  port (
    CLK             : in  std_logic;
    RST             : in  std_logic;
    -- cocotb pulse-gen edge lines (one per EDGE channel)
    EDGE0_IN        : in  std_logic;
    EDGE1_IN        : in  std_logic;
    EDGE2_IN        : in  std_logic;
    -- Datalogger control
    CONFIG          : in  std_logic_vector(C_DATALOGGER_CONFIG_WIDTH-1 downto 0);
    TIMESTAMP       : in  std_logic_vector(C_TIMESTAMP_WIDTH-1 downto 0);
    -- maxfreq_event_bank framer-input handshake (the unit under test's output)
    MF_FR_VALID      : out std_logic;
    MF_FR_READY      : in  std_logic;
    MF_FR_DATALOGGER_ID : out std_logic_vector(7 downto 0);
    MF_FR_MSG_CLASS  : out std_logic_vector(7 downto 0);
    MF_FR_MSG_TYPE   : out std_logic_vector(15 downto 0);
    MF_FR_FLAGS      : out std_logic_vector(15 downto 0);
    MF_FR_TIMESTAMP  : out std_logic_vector(63 downto 0);
    MF_FR_PAYLOAD    : out std_logic_vector(63 downto 0);
    MF_FR_WORDS      : out std_logic_vector(7 downto 0)
  );
end entity tb_edge_maxfreq_chain;

architecture str of tb_edge_maxfreq_chain is
  signal capture_pins   : std_logic_vector(C_NUM_DATALOGGER_PINS-1 downto 0) := (others => '1');
  signal timestamp64  : std_logic_vector(63 downto 0);

  -- gather → bank per-channel max-frequency taps
  signal edge_max_ivals : std_logic_vector(C_EDGE_DATALOGGERS*C_EDGE_MAXFREQ_WIDTH-1 downto 0);
  signal edge_max_new   : std_logic_vector(C_EDGE_DATALOGGERS-1 downto 0);

  -- Pack the per-EDGE-channel device ids into the flat generic the bank expects
  -- (channel k id → bits [(k+1)*8-1 : k*8]) — same construction as deepskopion_top.
  function f_edge_ids_flat return std_logic_vector is
    variable v : std_logic_vector(C_EDGE_DATALOGGERS*8-1 downto 0) := (others => '0');
  begin
    for k in 0 to C_EDGE_DATALOGGERS-1 loop
      v((k+1)*8-1 downto k*8) := std_logic_vector(to_unsigned(C_EDGE_IDS(k), 8));
    end loop;
    return v;
  end function f_edge_ids_flat;
  constant C_EDGE_IDS_FLAT : std_logic_vector(C_EDGE_DATALOGGERS*8-1 downto 0)
                           := f_edge_ids_flat;
begin

  timestamp64 <= std_logic_vector(resize(unsigned(TIMESTAMP), 64));

  -- Map the three pulse-gen lines onto the EDGE0..2 datalogger pins; all other pins
  -- idle high so only the enabled EDGE instances can emit. SINGLE driver.
  p_pins : process(EDGE0_IN, EDGE1_IN, EDGE2_IN)
  begin
    capture_pins                <= (others => '1');
    capture_pins(C_EDGE0_LINE)  <= EDGE0_IN;
    capture_pins(C_EDGE1_LINE)  <= EDGE1_IN;
    capture_pins(C_EDGE2_LINE)  <= EDGE2_IN;
  end process p_pins;

  u_gather : entity tich.datalogger_gather
    generic map (
      G_SYS_CLK => G_SYS_CLK
    )
    port map (
      RST             => RST,
      CLK             => CLK,
      CAPTURE_PINS      => capture_pins,
      CONFIG          => CONFIG,
      TIMESTAMP       => TIMESTAMP,
      DOUT_PAYLOAD    => open,
      DOUT_DATALOGGER_ID => open,
      DOUT_MSG_TYPE   => open,
      DOUT_VALID      => open,
      DOUT_REQ        => '1',          -- always accept the burst path (not under test)
      FIFO_CSR        => open,
      PROTO_COUNTERS  => open,
      EDGE_ERR_CNTS   => open,
      EDGE_MAX_IVALS  => edge_max_ivals,
      EDGE_MAX_NEW    => edge_max_new
    );

  u_maxfreq_bank : entity tich.maxfreq_event_bank
    generic map (
      G_NUM_CH      => C_EDGE_DATALOGGERS,
      G_DOMAIN      => C_ZS_DOMAIN_EDGE,
      G_EVENT_ID    => C_ZS_EVT_EDGE_MAXFREQ,
      G_DATALOGGER_IDS => C_EDGE_IDS_FLAT,
      G_IVAL_WIDTH  => C_EDGE_MAXFREQ_WIDTH,
      G_REARM_BIT   => 2
    )
    port map (
      CLK           => CLK,
      RST           => RST,
      MAX_IVALS     => edge_max_ivals,
      MAX_NEW       => edge_max_new,
      CONFIG        => CONFIG,
      TIMESTAMP     => timestamp64,
      FR_VALID      => MF_FR_VALID,
      FR_READY      => MF_FR_READY,
      FR_DATALOGGER_ID => MF_FR_DATALOGGER_ID,
      FR_MSG_CLASS  => MF_FR_MSG_CLASS,
      FR_MSG_TYPE   => MF_FR_MSG_TYPE,
      FR_FLAGS      => MF_FR_FLAGS,
      FR_TIMESTAMP  => MF_FR_TIMESTAMP,
      FR_PAYLOAD    => MF_FR_PAYLOAD,
      FR_WORDS      => MF_FR_WORDS
    );

end architecture str;
