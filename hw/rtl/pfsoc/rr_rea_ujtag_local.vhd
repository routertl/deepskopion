-- rr unknown-llm (AI-generated)
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
--
-- rr_rea_ujtag_local — Microchip PolarFire / PolarFire SoC UJTAG wrapper for
-- routertl/rea's rr_rea_top. DS-P2.132 (PF-T10 first-silicon witness).
--
-- WHY THIS FILE EXISTS: it binds rr_rea_top to Microchip's UJTAG primitive with
-- the same rr_rea_top port shape as rr_rea_jtag_intel.vhd (sample_clk / probe /
-- trigger / arst / tck / tdi / tdo / capture / shift_en / update / sel), so the
-- on-chip REA register map is identical to the intel/xilinx legs and `rr ila`
-- (OpenOcdFlashProTransport) talks to it unchanged. rr_rea_top now comes from
-- the routertl/rea package (DS-P1.73); the vendored libs/local/rr_rea copy this
-- header used to describe no longer exists (DS-P2.326).
--
-- DS-P2.326: G_QUAL_CONDS (storage-qualifier slots) and ext_trigger (an
-- external fire input) pass straight to rr_rea_top. Their defaults are
-- rr_rea_top's own (0 and '0'), so an instance that does not set them
-- elaborates the same core as before.
--
-- UJTAG port mapping (per the SDK wrapper header): UDRCK->tck, UTDI->tdi,
-- UTDO<-tdo, UDRCAP->capture, UDRSH->shift_en, UDRUPD->update, UIREG->8-bit IR,
-- URSTB->arst (inverted). sel is asserted when UIREG matches the user opcode.
-- G_CTRL_CHAIN maps to the UJTAG user IR opcode: 1->0x55, 2->0x56, 3->0x57,
-- 4->0x58 (== IR_TABLE_MICROCHIP_UJTAG in sdk/cli/rea/transport.py), or a raw
-- opcode 16..127 passed directly.

library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;
-- rr_rea_top comes from the routertl/rea package, compiled into library rr_rea. This wrapper is compiled into
-- work, so a top's component declaration binds to it by default binding.
library rr_rea;

entity rr_rea_ujtag_local is
    generic (
        G_SAMPLE_W    : positive := 12;
        G_DEPTH       : positive := 4096;
        G_TIMESTAMP_W : natural  := 32;
        G_NUM_CHAN    : positive := 1;
        G_CTRL_CHAIN  : integer  := 1;
        G_QUAL_CONDS  : natural  := 0
    );
    port (
        sample_clk  : in  std_logic;
        sample_rst  : in  std_logic;
        probe_in    : in  std_logic_vector(G_SAMPLE_W - 1 downto 0);
        trigger_out : out std_logic;
        ext_trigger : in  std_logic := '0';
        -- Dedicated JTAG TAP pins, brought to the TOP so Libero connects the
        -- UJTAG macro to the hardwired TAP. They must reach real top-level ports
        -- (a net with fanout 1) — not constants — or Libero Compile fails
        -- CMPPF_073. They do NOT consume user package pins (dedicated bank).
        jtag_tck    : in  std_logic;
        jtag_tdi    : in  std_logic;
        jtag_tms    : in  std_logic;
        jtag_trstb  : in  std_logic;
        jtag_tdo    : out std_logic
    );
end entity rr_rea_ujtag_local;

architecture rtl of rr_rea_ujtag_local is

    -- UJTAG is a hard macro whose TCK/TDI/TDO/TMS/TRSTB pins connect to the
    -- device's DEDICATED JTAG TAP pads — hardwired by Libero, NOT user I/O.
    -- Leaving them unconnected (open) or driving them with constants makes
    -- Libero's Compile fail CMPPF_073 ("IO port tied off to VCC/GND"); they
    -- must be wired to real top-level ports (jtag_*) to reach the dedicated TAP.
    component UJTAG is
        port (
            UTDO   : in  std_logic;
            UDRCAP : out std_logic;
            UDRSH  : out std_logic;
            UDRUPD : out std_logic;
            UIREG  : out std_logic_vector(7 downto 0);
            URSTB  : out std_logic;
            UTDI   : out std_logic;
            TCK    : in  std_logic := '0';
            TRSTB  : in  std_logic := '1';
            TDI    : in  std_logic := '0';
            TDO    : out std_logic;
            TMS    : in  std_logic := '0';
            UDRCK  : out std_logic
        );
    end component UJTAG;

    function resolve_opcode(chain : integer) return std_logic_vector is
    begin
        if chain >= 16 and chain <= 127 then
            return std_logic_vector(to_unsigned(chain, 8));
        elsif chain = 1 then
            return x"55";
        elsif chain = 2 then
            return x"56";
        elsif chain = 3 then
            return x"57";
        elsif chain = 4 then
            return x"58";
        else
            return x"55";
        end if;
    end function;

    constant C_USER_OPCODE : std_logic_vector(7 downto 0) := resolve_opcode(G_CTRL_CHAIN);

    signal tck      : std_logic;
    signal tdi      : std_logic;
    signal tdo      : std_logic;
    signal capture  : std_logic;
    signal shift_en : std_logic;
    signal update   : std_logic;
    signal sel      : std_logic;
    signal arst     : std_logic;
    signal uireg    : std_logic_vector(7 downto 0);

    attribute syn_noprune : boolean;
    attribute syn_noprune of u_ujtag : label is true;

begin

    -- Dedicated JTAG TAP pins (TCK/TDI/TDO/TMS/TRSTB) are wired to the top-level
    -- dedicated TAP ports (jtag_*) so Libero connects the UJTAG macro to the
    -- hardwired on-chip TAP (D-6a) — zero user package pins.
    u_ujtag : UJTAG
        port map (
            UTDO   => tdo,
            UDRCAP => capture,
            UDRSH  => shift_en,
            UDRUPD => update,
            UIREG  => uireg,
            URSTB  => open,
            UTDI   => tdi,
            TCK    => jtag_tck,
            TRSTB  => jtag_trstb,
            TDI    => jtag_tdi,
            TDO    => jtag_tdo,
            TMS    => jtag_tms,
            UDRCK  => tck
        );

    sel  <= '1' when (uireg = C_USER_OPCODE) else '0';
    -- arst is tied inactive (mirrors rr_rea_jtag_intel): the capture FSM relies
    -- on its natural init + the JTAG CTRL arm/reset path. Driving arst from
    -- UJTAG's URSTB (asserted on every Test-Logic-Reset) would clear the capture
    -- buffer each time OpenOCD cycles the TAP — observed DS-P2.132 as an
    -- all-zeros readback.
    arst <= '0';

    u_top : entity rr_rea.rr_rea_top
        generic map (
            G_SAMPLE_W    => G_SAMPLE_W,
            G_DEPTH       => G_DEPTH,
            G_TIMESTAMP_W => G_TIMESTAMP_W,
            G_NUM_CHAN    => G_NUM_CHAN,
            G_QUAL_CONDS  => G_QUAL_CONDS
        )
        port map (
            sample_clk_i => sample_clk,
            sample_rst_i => sample_rst,
            probe_i      => probe_in,
            trigger_o    => trigger_out,
            ext_trigger_i => ext_trigger,
            arst_i       => arst,
            tck_i        => tck,
            tdi_i        => tdi,
            tdo_o        => tdo,
            capture_i    => capture,
            shift_en_i   => shift_en,
            update_i     => update,
            sel_i        => sel
        );

end architecture rtl;
