-------------------------------------------------------------------------------
-- Standard   : VHDL-2008
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- File       : zs_msg_pkg.vhd
-- Description: Constants + helpers for the zs_msg_header_t framing emitted
--              by the PL into the rr-ddr-stream DDR ring. Handwritten,
--              shared by zs_msg_framer.vhd and any future protocol-
--              decoder block that needs to assemble (severity | domain |
--              event_id) msg_type words.
--
--              Spec: docs/software/data_plane/message_formats/common_header.md
--                    docs/software/data_plane/message_formats/event_and_datalogger_id_spec.md
--                    sw/stream/zs_msg.py (PS-side parser — mirror of these
--                    constants).
--
-- DS-P3.28 v1.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;

package zs_msg_pkg is

  -- ── Magic / version / header geometry ───────────────────────────────────
  -- Magic value as the LE-uint16 view of bytes 'Z'(0x5A), 'S'(0x53).
  -- On the PS, struct.unpack('<H', bytes[0:2]) → 0x5A53. In memory the
  -- bytes are [0x53, 0x5A] (LE storage of 0x5A53). zs_msg_framer emits
  -- the LE byte order; the PS parser ZS_MAGIC constant matches this.
  constant C_ZS_MAGIC            : std_logic_vector(15 downto 0) := X"5A53";

  constant C_ZS_VERSION          : std_logic_vector(7 downto 0)  := X"01";

  -- Header length in 32-bit words (5 words = 20 bytes).
  constant C_ZS_HEADER_LEN_WORDS : std_logic_vector(7 downto 0)  := X"05";

  -- Header count as a natural — used to size the framer's emission FSM.
  constant C_ZS_NUM_HDR_WORDS    : natural := 5;

  -- ── msg_class enum (byte at offset 5) ───────────────────────────────────
  constant C_ZS_CLASS_DATA    : std_logic_vector(7 downto 0) := X"00";
  constant C_ZS_CLASS_STATUS  : std_logic_vector(7 downto 0) := X"01";
  constant C_ZS_CLASS_TRIGGER : std_logic_vector(7 downto 0) := X"02";

  -- ── Severity nibble (msg_type bits [15:12]) ─────────────────────────────
  -- >>> GENERATED severity from system_ids.yml by id_gen.py (DS-P3.70) — DO NOT EDIT >>>
  -- Severity nibble (msg_type[15:12]).
  constant C_ZS_SEV_INFO    : std_logic_vector(3 downto 0) := X"0";
  constant C_ZS_SEV_WARN    : std_logic_vector(3 downto 0) := X"1";
  constant C_ZS_SEV_ERROR   : std_logic_vector(3 downto 0) := X"2";
  constant C_ZS_SEV_DEBUG   : std_logic_vector(3 downto 0) := X"3";
  constant C_ZS_SEV_CONTROL : std_logic_vector(3 downto 0) := X"4";
  -- <<< END GENERATED severity <<<

  -- ── Error-event provenance codes (event_id[7:0], reserved 0xF0-0xFF) ─────
  -- >>> GENERATED error_events from system_ids.yml by id_gen.py (DS-P3.70) — DO NOT EDIT >>>
  -- Cross-domain error-provenance codes (event_id[7:0], reserved 0xF0-0xFF).
  constant C_ZS_ERR_EVT_INTERNAL_FIFO_OVERFLOW : std_logic_vector(7 downto 0) := X"F0";
  constant C_ZS_ERR_EVT_ARBITER_DROP           : std_logic_vector(7 downto 0) := X"F1";
  constant C_ZS_ERR_EVT_RING_FULL              : std_logic_vector(7 downto 0) := X"F2";
  constant C_ZS_ERR_EVT_DECODE_ERROR           : std_logic_vector(7 downto 0) := X"F3";
  -- <<< END GENERATED error_events <<<

  -- ── Domain nibble (msg_type bits [11:8]) ────────────────────────────────
  -- >>> GENERATED domains from system_ids.yml by id_gen.py (DS-P3.57) — DO NOT EDIT >>>
  constant C_ZS_DOMAIN_SYS     : std_logic_vector(3 downto 0) := X"0";
  constant C_ZS_DOMAIN_UART    : std_logic_vector(3 downto 0) := X"1";
  constant C_ZS_DOMAIN_I2C     : std_logic_vector(3 downto 0) := X"2";
  constant C_ZS_DOMAIN_SPI     : std_logic_vector(3 downto 0) := X"3";
  constant C_ZS_DOMAIN_I2S     : std_logic_vector(3 downto 0) := X"4";
  constant C_ZS_DOMAIN_QSPI    : std_logic_vector(3 downto 0) := X"5";
  constant C_ZS_DOMAIN_EDGE    : std_logic_vector(3 downto 0) := X"6";
  constant C_ZS_DOMAIN_TRIG    : std_logic_vector(3 downto 0) := X"7";
  constant C_ZS_DOMAIN_JTAG    : std_logic_vector(3 downto 0) := X"8";
  constant C_ZS_DOMAIN_MDIO    : std_logic_vector(3 downto 0) := X"9";
  constant C_ZS_DOMAIN_CAN     : std_logic_vector(3 downto 0) := X"A";
  constant C_ZS_DOMAIN_ONEWIRE : std_logic_vector(3 downto 0) := X"B";
  constant C_ZS_DOMAIN_LIN     : std_logic_vector(3 downto 0) := X"C";
  constant C_ZS_DOMAIN_EYE     : std_logic_vector(3 downto 0) := X"D";
  -- <<< END GENERATED domains <<<

  -- ── Per-domain event_id codes (event_id[7:0], range 0x00-0xEF) ──────────
  -- NOT id_gen-managed (only domains + the 0xF0+ error_events are). Most
  -- per-event codes are stamped locally where they are produced (e.g. the edge
  -- aggregator hardcodes msg_type X"0601" = EVT_EDGE_BATCH). EVT_EDGE_MAXFREQ
  -- (DS-P3.117) is the in-band max-frequency carrier under DOMAIN_EDGE; named
  -- here so the maxfreq emitter and the SW side (zs_msg.py EVT_EDGE_MAXFREQ)
  -- share one literal. Mirrors docs/.../event_and_datalogger_id_spec.md (0x0602).
  constant C_ZS_EVT_EDGE_MAXFREQ : std_logic_vector(7 downto 0) := X"02";
  -- DS-T2.84: the edge_datalogger ENABLED/DISABLED lifecycle frame (DS-T2.43),
  -- carried as its OWN single-word zs_msg (msg_type 0x0608), never as an
  -- EDGE_BATCH entry. A batch entry keeps only S/F/E, and the lifecycle type
  -- "100" is bit-identical to an S=1/E=0 level readback, so on EDGE_EVENTS the
  -- marker read as "line is low" and its ENABLED/DISABLED code was lost. The
  -- gather tags edge data words with event_id = "00000" & S/F/E (0x00-0x07);
  -- 0x08 sits outside that range, so no data word can take the lifecycle route.
  constant C_ZS_EVT_EDGE_LIFECYCLE : std_logic_vector(7 downto 0) := X"08";
  -- DS-P2.16: one edge_datalogger Stream-RLE word (SYNC / PACK / LONG / IDLE),
  -- carried as its OWN single-word zs_msg (msg_type 0x0609). Its [26:24] is an
  -- RLE word type, so as an EDGE_BATCH entry it would read as S/F/E flags.
  constant C_ZS_EVT_EDGE_RLE : std_logic_vector(7 downto 0) := X"09";

  -- ── Helpers ────────────────────────────────────────────────────────────
  function make_msg_type(sev    : std_logic_vector(3 downto 0);
                         dom    : std_logic_vector(3 downto 0);
                         evt_id : std_logic_vector(7 downto 0))
    return std_logic_vector;

  -- DS-T2.84: '1' when a gather-tagged word belongs on the edge_batcher_aggregator
  -- (EDGE_BATCH → EDGE_EVENTS) path; '0' sends it to the framer as its own frame.
  -- Shared by deepskopion_core and tb_datalogger_to_ddr so the bench routes
  -- exactly as the fabric does.
  function is_edge_batch_word(msg_type : std_logic_vector(15 downto 0))
    return std_logic;

end package zs_msg_pkg;


package body zs_msg_pkg is

  function make_msg_type(sev    : std_logic_vector(3 downto 0);
                         dom    : std_logic_vector(3 downto 0);
                         evt_id : std_logic_vector(7 downto 0))
    return std_logic_vector is
  begin
    -- bits[15:12] = severity, bits[11:8] = domain, bits[7:0] = event_id
    return sev & dom & evt_id;
  end function;

  function is_edge_batch_word(msg_type : std_logic_vector(15 downto 0))
    return std_logic is
  begin
    -- Only the S/F/E tags 0x00-0x07 are batch entries. Every other edge
    -- event_id (lifecycle 0x08, Stream-RLE 0x09) is framed on its own.
    if msg_type(11 downto 8) = C_ZS_DOMAIN_EDGE
       and msg_type(7 downto 3) = "00000" then
      return '1';
    end if;
    return '0';
  end function;

end package body zs_msg_pkg;
