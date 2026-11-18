-- Auto-generated AXI-Lite register file
-----------------------------------------------------------------------------
-- File        : system_regbank.vhd
-- Description : AXI4-Lite register file with REGS_IN / REGS_OUT record interface
-- Standard    : VHDL-2008
-- SPDX-License-Identifier: NOASSERTION
-- Source      : regbank_schema.yml  •  Generator: RouteRTL SDK
-- DO NOT EDIT — regenerate with `rr regbank axilite`
-----------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library tich;
use tich.system_regbank_pkg.all;

entity system_regbank is
  generic (
    G_ADDR_WIDTH : integer := 10;
    G_DATA_WIDTH : integer := 32
  );
  port (
    CLK           : in  std_logic;
    RST           : in  std_logic;

    -- AXI4-Lite Slave Interface
    S_AXI_AWADDR  : in  std_logic_vector(G_ADDR_WIDTH-1 downto 0);
    S_AXI_AWPROT  : in  std_logic_vector(2 downto 0);
    S_AXI_AWVALID : in  std_logic;
    S_AXI_AWREADY : out std_logic;

    S_AXI_WDATA   : in  std_logic_vector(G_DATA_WIDTH-1 downto 0);
    S_AXI_WSTRB   : in  std_logic_vector(G_DATA_WIDTH/8-1 downto 0);
    S_AXI_WVALID  : in  std_logic;
    S_AXI_WREADY  : out std_logic;

    S_AXI_BRESP   : out std_logic_vector(1 downto 0);
    S_AXI_BVALID  : out std_logic;
    S_AXI_BREADY  : in  std_logic;

    S_AXI_ARADDR  : in  std_logic_vector(G_ADDR_WIDTH-1 downto 0);
    S_AXI_ARPROT  : in  std_logic_vector(2 downto 0);
    S_AXI_ARVALID : in  std_logic;
    S_AXI_ARREADY : out std_logic;

    S_AXI_RDATA   : out std_logic_vector(G_DATA_WIDTH-1 downto 0);
    S_AXI_RRESP   : out std_logic_vector(1 downto 0);
    S_AXI_RVALID  : out std_logic;
    S_AXI_RREADY  : in  std_logic;

    -- User logic record interface (register-bank pattern)
    REGS_IN       : in  t_memArrayReg;
    REGS_OUT      : out t_memArrayReg
  );
end entity system_regbank;

architecture rtl of system_regbank is

  constant C_ADDR_LSB  : integer := 2;  -- byte-to-word conversion
  constant C_DEPTH     : integer := C_REGBANK_DEPTH;

  -- Write-1-to-clear and write/read mask vectors
  constant C_W1C_MASK : t_memArray := (
    others => (others => '0')
  );
  constant C_WMASK : t_memArray := (
    others => (others => '1')
  );
  constant C_RMASK : t_memArray := (
    others => (others => '1')
  );

  -- Pipelined reset (high-fanout mitigation)
  constant C_RST_PIPE_STAGES : integer := 4;
  signal rst_pipe : std_logic_vector(C_RST_PIPE_STAGES-1 downto 0) := (others => '1');

  -- Register storage
  signal reg_bank : t_memArray := C_INIT_REGBANK;

  -- AXI handshake state
  signal aw_ready_r : std_logic := '0';
  signal w_ready_r  : std_logic := '0';
  signal b_valid_r  : std_logic := '0';
  signal ar_ready_r : std_logic := '0';
  signal r_valid_r  : std_logic := '0';
  signal r_data_r   : std_logic_vector(G_DATA_WIDTH-1 downto 0) := (others => '0');

  -- Latched write address / read address
  signal aw_addr_r  : unsigned(G_ADDR_WIDTH-1 downto 0) := (others => '0');
  signal ar_addr_r  : unsigned(G_ADDR_WIDTH-1 downto 0) := (others => '0');

  -- Valid propagation (register-bank pattern)
  signal valid_r  : std_logic_vector(2**C_DEPTH-1 downto 0) := (others => '0');
  signal valid_r2 : std_logic_vector(2**C_DEPTH-1 downto 0) := (others => '0');

begin

  -- Reset pipeline
  rst_pipe <= rst_pipe(C_RST_PIPE_STAGES-2 downto 0) & RST when rising_edge(CLK);

  -- Write address channel
  pr_aw : process(CLK)
  begin
    if rising_edge(CLK) then
      if rst_pipe(C_RST_PIPE_STAGES-1) = '1' then
        aw_ready_r <= '0';
      elsif aw_ready_r = '0' and S_AXI_AWVALID = '1' and S_AXI_WVALID = '1'
            and b_valid_r = '0' then  -- one handshake per write
        aw_ready_r <= '1';
        aw_addr_r  <= unsigned(S_AXI_AWADDR);
      else
        aw_ready_r <= '0';
      end if;
    end if;
  end process pr_aw;
  S_AXI_AWREADY <= aw_ready_r;

  -- Write data channel
  pr_w : process(CLK)
  begin
    if rising_edge(CLK) then
      if rst_pipe(C_RST_PIPE_STAGES-1) = '1' then
        w_ready_r <= '0';
      elsif w_ready_r = '0' and S_AXI_AWVALID = '1' and S_AXI_WVALID = '1'
            and b_valid_r = '0' then  -- one handshake per write
        w_ready_r <= '1';
      else
        w_ready_r <= '0';
      end if;
    end if;
  end process pr_w;
  S_AXI_WREADY <= w_ready_r;

  -- Register write logic (respects C_READ_ONLY mask)
  pr_reg_wr : process(CLK)
    variable v_index : integer;
  begin
    if rising_edge(CLK) then
      valid_r  <= (others => '0');
      valid_r2 <= valid_r;
      if aw_ready_r = '1' and w_ready_r = '1' then
        v_index := to_integer(aw_addr_r(G_ADDR_WIDTH-1 downto C_ADDR_LSB));
        if v_index < C_NUM_USER_REGS and C_READ_ONLY(v_index) = '0' then
          -- Byte-lane strobing
          for byte_i in 0 to G_DATA_WIDTH/8-1 loop
            if S_AXI_WSTRB(byte_i) = '1' then
              for b in byte_i*8 to byte_i*8+7 loop
                if C_W1C_MASK(v_index)(b) = '1' then
                  if S_AXI_WDATA(b) = '1' then
                    reg_bank(v_index)(b) <= '0';
                  end if;
                elsif C_WMASK(v_index)(b) = '1' then
                  reg_bank(v_index)(b) <= S_AXI_WDATA(b);
                end if;
              end loop;
            end if;
          end loop;
          valid_r(v_index) <= '1';
        end if;
      end if;
      -- FPGA-side writes (REGS_IN)
      for i in 0 to C_NUM_USER_REGS-1 loop
        if REGS_IN(i).valid = '1' and C_ROM_REGS(i) = '0' then
          reg_bank(i) <= REGS_IN(i).data;
          valid_r(i)  <= '1';
        end if;
      end loop;
      -- Reset (pipelined)
      if rst_pipe(C_RST_PIPE_STAGES-1) = '1' then
        for i in 0 to C_NUM_USER_REGS-1 loop
          if C_ROM_REGS(i) = '0' then
            reg_bank(i) <= C_INIT_REGBANK(i);
          end if;
        end loop;
      end if;
    end if;
  end process pr_reg_wr;

  -- Write response
  pr_b : process(CLK)
  begin
    if rising_edge(CLK) then
      if RST = '1' then  -- VALID clears on RAW reset, not rst_pipe
        b_valid_r <= '0';
      elsif aw_ready_r = '1' and w_ready_r = '1' and b_valid_r = '0' then
        b_valid_r <= '1';
      elsif S_AXI_BREADY = '1' and b_valid_r = '1' then
        b_valid_r <= '0';
      end if;
    end if;
  end process pr_b;
  S_AXI_BVALID <= b_valid_r and not RST;  -- VALID forced low throughout reset
  S_AXI_BRESP  <= "00";  -- OKAY

  -- Read address channel
  pr_ar : process(CLK)
  begin
    if rising_edge(CLK) then
      if rst_pipe(C_RST_PIPE_STAGES-1) = '1' then
        ar_ready_r <= '0';
      elsif ar_ready_r = '0' and S_AXI_ARVALID = '1'
            and r_valid_r = '0' then  -- one handshake per read
        ar_ready_r <= '1';
        ar_addr_r  <= unsigned(S_AXI_ARADDR);
      else
        ar_ready_r <= '0';
      end if;
    end if;
  end process pr_ar;
  S_AXI_ARREADY <= ar_ready_r;

  -- Read data channel
  pr_rd : process(CLK)
    variable v_index : integer;
  begin
    if rising_edge(CLK) then
      if RST = '1' then  -- VALID clears on RAW reset, not rst_pipe
        r_valid_r <= '0';
        r_data_r  <= (others => '0');
      elsif ar_ready_r = '1' and S_AXI_ARVALID = '1' and r_valid_r = '0' then
        r_valid_r <= '1';
        v_index := to_integer(ar_addr_r(G_ADDR_WIDTH-1 downto C_ADDR_LSB));
        if v_index < C_NUM_USER_REGS then
          r_data_r <= reg_bank(v_index) and C_RMASK(v_index);
        else
          r_data_r <= (others => '0');
        end if;
      elsif S_AXI_RREADY = '1' and r_valid_r = '1' then
        r_valid_r <= '0';
      end if;
    end if;
  end process pr_rd;
  S_AXI_RVALID <= r_valid_r and not RST;  -- VALID forced low throughout reset
  S_AXI_RDATA  <= r_data_r;
  S_AXI_RRESP  <= "00";  -- OKAY

  -- Expose registers to user logic
  GEN_REGS_OUT : for i in 0 to C_NUM_USER_REGS-1 generate
    REGS_OUT(i).data  <= reg_bank(i);
    REGS_OUT(i).valid <= valid_r2(i);
  end generate GEN_REGS_OUT;

end architecture rtl;
