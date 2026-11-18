-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: CERN-OHL-W-2.0
-- AXI4-Lite slave to APB3 master.  AW and W are accepted independently;
-- the APB setup phase cannot begin until both halves are present.
library ieee;
use ieee.std_logic_1164.all;

entity axil_apb_shim is
  port (
    clk_i, rst_i : in std_logic;
    s_awaddr_i : in std_logic_vector(31 downto 0);
    s_awvalid_i : in std_logic; s_awready_o : out std_logic;
    s_wdata_i : in std_logic_vector(31 downto 0);
    s_wstrb_i : in std_logic_vector(3 downto 0);
    s_wvalid_i : in std_logic; s_wready_o : out std_logic;
    s_bresp_o : out std_logic_vector(1 downto 0);
    s_bvalid_o : out std_logic; s_bready_i : in std_logic;
    s_araddr_i : in std_logic_vector(31 downto 0);
    s_arvalid_i : in std_logic; s_arready_o : out std_logic;
    s_rdata_o : out std_logic_vector(31 downto 0);
    s_rresp_o : out std_logic_vector(1 downto 0);
    s_rvalid_o : out std_logic; s_rready_i : in std_logic;
    paddr_o : out std_logic_vector(31 downto 0);
    psel_o, penable_o, pwrite_o : out std_logic;
    pwdata_o : out std_logic_vector(31 downto 0);
    prdata_i : in std_logic_vector(31 downto 0);
    pready_i, pslverr_i : in std_logic
  );
end entity;

architecture rtl of axil_apb_shim is
  type state_t is (IDLE, APB_SETUP, APB_ACCESS, AXI_WRITE_RESP, AXI_READ_RESP);
  signal state_r : state_t := IDLE;
  signal aw_hold_r, w_hold_r : std_logic := '0';
  signal awaddr_r, araddr_r, wdata_r : std_logic_vector(31 downto 0) := (others => '0');
  signal rdata_r : std_logic_vector(31 downto 0) := (others => '0');
  signal write_r, error_r : std_logic := '0';
begin
  -- CTU APB3 has no byte-strobe input. Partial AXI writes are refused instead
  -- of silently widening them into a destructive full-word write.
  s_awready_o <= '1' when state_r = IDLE and aw_hold_r = '0' else '0';
  s_wready_o  <= '1' when state_r = IDLE and w_hold_r = '0' else '0';
  s_arready_o <= '1' when state_r = IDLE and aw_hold_r = '0' and
                            w_hold_r = '0' and s_awvalid_i = '0' and
                            s_wvalid_i = '0' else '0';
  s_bvalid_o <= '1' when state_r = AXI_WRITE_RESP else '0';
  s_bresp_o  <= "10" when error_r = '1' else "00";
  s_rvalid_o <= '1' when state_r = AXI_READ_RESP else '0';
  s_rresp_o  <= "10" when error_r = '1' else "00";
  s_rdata_o  <= rdata_r;

  paddr_o   <= awaddr_r when write_r = '1' else araddr_r;
  pwdata_o  <= wdata_r;
  pwrite_o  <= write_r;
  psel_o    <= '1' when state_r = APB_SETUP or state_r = APB_ACCESS else '0';
  penable_o <= '1' when state_r = APB_ACCESS else '0';

  process(clk_i)
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        state_r <= IDLE; aw_hold_r <= '0'; w_hold_r <= '0'; error_r <= '0';
      else
        case state_r is
          when IDLE =>
            if s_awvalid_i = '1' and aw_hold_r = '0' then
              awaddr_r <= s_awaddr_i; aw_hold_r <= '1';
            end if;
            if s_wvalid_i = '1' and w_hold_r = '0' then
              wdata_r <= s_wdata_i; w_hold_r <= '1';
              if s_wstrb_i /= "1111" then error_r <= '1'; end if;
            end if;
            if aw_hold_r = '1' and w_hold_r = '1' then
              write_r <= '1'; aw_hold_r <= '0'; w_hold_r <= '0';
              if error_r = '1' then state_r <= AXI_WRITE_RESP;
              else state_r <= APB_SETUP; end if;
            elsif s_arvalid_i = '1' and s_arready_o = '1' then
              araddr_r <= s_araddr_i; write_r <= '0'; state_r <= APB_SETUP;
            end if;
          when APB_SETUP => state_r <= APB_ACCESS;
          when APB_ACCESS =>
            if pready_i = '1' then
              error_r <= pslverr_i;
              if write_r = '1' then state_r <= AXI_WRITE_RESP;
              else rdata_r <= prdata_i; state_r <= AXI_READ_RESP; end if;
            end if;
          when AXI_WRITE_RESP =>
            if s_bready_i = '1' then state_r <= IDLE; error_r <= '0'; end if;
          when AXI_READ_RESP =>
            if s_rready_i = '1' then state_r <= IDLE; error_r <= '0'; end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
