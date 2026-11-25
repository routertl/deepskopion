-- rr unknown-llm (AI-generated)
-- SPDX-License-Identifier: MIT
-- Stub for CTU CAN FD can_top_apb entity when CTU CAN FD core is omitted (DS-P2.121).
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity can_top_apb is
    generic(
        rx_buffer_size          : natural range 32 to 4096  := 32;
        txt_buffer_count        : natural range 2 to 8      := 2;
        sup_filtA               : boolean                   := false;
        sup_filtB               : boolean                   := false;
        sup_filtC               : boolean                   := false;
        sup_range               : boolean                   := false;
        sup_test_registers      : boolean                   := true;
        sup_traffic_ctrs        : boolean                   := false;
        sup_parity              : boolean                   := false;
        active_timestamp_bits   : natural range 0 to 63     := 63;
        reset_buffer_rams       : boolean                   := false;
        target_technology       : natural                   := 0
    );
    port(
        aclk             : in  std_logic;
        arstn            : in  std_logic;
        scan_enable      : in  std_logic := '0';
        res_n_out        : out std_logic;
        irq              : out std_logic;
        CAN_tx           : out std_logic;
        CAN_rx           : in  std_logic;
        timestamp        : in  std_logic_vector(63 downto 0);
        s_apb_paddr      : in  std_logic_vector(31 downto 0);
        s_apb_penable    : in  std_logic;
        s_apb_pprot      : in  std_logic_vector(2 downto 0) := "000";
        s_apb_prdata     : out std_logic_vector(31 downto 0);
        s_apb_pready     : out std_logic;
        s_apb_psel       : in  std_logic;
        s_apb_pslverr    : out std_logic;
        s_apb_pstrb      : in  std_logic_vector(3 downto 0) := "1111";
        s_apb_pwdata     : in  std_logic_vector(31 downto 0);
        s_apb_pwrite     : in  std_logic
    );
end entity can_top_apb;

architecture stub of can_top_apb is
begin
    res_n_out     <= '1';
    irq           <= '0';
    CAN_tx        <= '1';
    s_apb_prdata  <= (others => '0');
    s_apb_pready  <= '1';
    s_apb_pslverr <= '0';
end architecture stub;
