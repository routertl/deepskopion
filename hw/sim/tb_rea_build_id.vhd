-- rr unknown-llm (AI-generated)
-------------------------------------------------------------------------------
-- tb_rea_build_id.vhd  —  DS-T2.75 REA BUILD_ID (0xD4) design-identity readback
-------------------------------------------------------------------------------
-- Purpose: prove the on-chip mechanism that `rr hw bit-sha` depends on — that
-- rr_rea_regbank surfaces the build-flow-GENERATED design identity
-- (C_REA_BUILD_ID, from rr_rea_build_id_pkg) at the frozen JTAG register offset
-- 0xD4. This is the RTL half that the DS-T2.75 hook/sidecar/guard work assumed
-- was already present but was NOT: the deepskopion local rr_rea fork (v0.4
-- lineage) had no BUILD_ID register, so 0xD4 fell through the read mux's
-- `when others => (others => '0')` and read 0x00000000 (UNSTAMPED) — a value
-- that can NEVER reach `rr hw bit-sha` CONFIRMED.
--
-- The testbench instantiates the SAME regbank the DE25 datalogger synthesises
-- (u_rea → rr_rea_top → rr_rea_regbank; G_SAMPLE_W=32, G_DEPTH=4096) and drives
-- its combinational read port directly. It exposes, on ports the cocotb driver
-- can sample, both the DUT read data (rd_data_o) AND the two package constants
-- (C_REA_BUILD_ID, C_REA_VERSION) so the check is value-agnostic: it asserts
-- "0xD4 reads whatever design identity is in the package", not a hard-coded
-- number that would rot every commit.
-------------------------------------------------------------------------------
library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;

library rr_rea;
    use rr_rea.rr_rea_pkg.all;
    use rr_rea.rr_rea_build_id_pkg.all;

entity tb_rea_build_id is
    port (
        clk            : in  std_logic;
        rd_addr_i      : in  std_logic_vector(15 downto 0);
        rd_data_o      : out std_logic_vector(31 downto 0);
        pkg_build_id_o : out std_logic_vector(31 downto 0);
        pkg_version_o  : out std_logic_vector(31 downto 0)
    );
end entity tb_rea_build_id;

architecture str of tb_rea_build_id is

    -- Match the silicon instantiation (deepskopion_de25_top.vhd u_rea):
    --   G_SAMPLE_W => 32, G_DEPTH => 4096, G_TIMESTAMP_W => 0, G_NUM_CHAN => 1.
    constant C_SAMPLE_W : positive := 32;
    constant C_DEPTH    : positive := 4096;
    constant C_PTR_W    : positive := clog2(C_DEPTH);

    signal rd_data : std_logic_vector(31 downto 0);

begin

    -- Surface the two package constants so the cocotb driver can read the
    -- expected design identity and version without cocotb needing to reach a
    -- VHDL constant. rd_data_o mirrors the DUT read data for the driven address.
    pkg_build_id_o <= C_REA_BUILD_ID;
    pkg_version_o  <= C_REA_VERSION;
    rd_data_o      <= rd_data;

    u_regbank : entity rr_rea.rr_rea_regbank
        generic map (
            G_SAMPLE_W    => C_SAMPLE_W,
            G_DEPTH       => C_DEPTH,
            G_TIMESTAMP_W => 0,
            G_NUM_CHAN    => 1
        )
        port map (
            jtag_clk_i => clk,
            jtag_rst_i => '0',

            -- Write port unused for a read-only identity probe.
            wr_en_i    => '0',
            wr_addr_i  => (others => '0'),
            wr_data_i  => (others => '0'),

            -- The register under test.
            rd_addr_i  => rd_addr_i,
            rd_data_o  => rd_data,

            -- Status inputs from the (idle) sample-clock domain.
            armed_i     => '0',
            triggered_i => '0',
            done_i      => '0',
            overflow_i  => '0',
            start_ptr_i => (others => '0'),

            -- Config / comparator / pulse outputs are irrelevant here.
            pretrig_len_o  => open,
            posttrig_len_o => open,
            trig_value_o   => open,
            trig_mask_o    => open,
            trig_mode_o    => open,
            chan_sel_o     => open,
            decim_ratio_o  => open,
            cond_values_o  => open,
            cond_masks_o   => open,
            cond_ops_o     => open,
            cond_valid_o   => open,
            arm_toggle_o   => open,
            reset_toggle_o => open
        );

end architecture str;
