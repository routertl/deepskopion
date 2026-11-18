-- SPDX-FileCopyrightText: 2024-2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT

-------------------------------------------------------------------------------
-- Title      : Phase Accumulator
-- Project    :
-------------------------------------------------------------------------------
-- File       : phase_accumulator.vhd
-- Author     : Daniel  dasjimaz@gmail.com
-- Company    : TICH (c)
-- Created    : 2025-02-07
-- Last update: 2025-02-07
-- Platform   :
-- Standard   : VHDL 2008
-------------------------------------------------------------------------------
-- Description: Implements a numerically controlled oscillator (NCO) using
--              a phase accumulator. The output phase value is used to drive
--              waveform synthesis such as a sine LUT.
-------------------------------------------------------------------------------
-- Copyright (c) 2025
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2025-02-07  1.1      dmazure Improved reset & initialization
-------------------------------------------------------------------------------

library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;

entity phase_accumulator is
  generic (
    G_ACC_WIDTH : integer := 32
  );
  port (
    CLK          : in std_logic;
    RST          : in std_logic;
    PHASE_IN     : in std_logic_vector(G_ACC_WIDTH - 1 downto 0);
    PHASE_OUT    : out std_logic_vector(G_ACC_WIDTH - 1 downto 0)
  );
end entity phase_accumulator;

architecture RTL of phase_accumulator is

    signal phaseAccumulator_r : unsigned(G_ACC_WIDTH - 1 downto 0) := (others => '0');

begin

  -------------------------------------------------------------------------------
  -- Phase Accumulator Tuning Word Calculation for DDS
  -------------------------------------------------------------------------------
  -- Overview:
  -- The tuning word determines the step size added to the phase accumulator on 
  -- each clock cycle, which defines the output frequency of the Direct Digital 
  -- Synthesizer (DDS). The tuning word is computed using the following formula:
  --
  --              Tuning_Word = (f_out * 2^N) / f_clk
  --
  -- Where:
  --   - f_out  = Desired output frequency (Hz)
  --   - f_clk  = System clock frequency driving the phase accumulator (Hz)
  --   - N      = Width of the phase accumulator (bits)
  --   - 2^N    = Full-scale range of the accumulator (phase resolution)
  --
  -- The tuning word controls how fast the phase accumulator overflows, which 
  -- determines the frequency of the generated waveform.

    process(CLK)
    begin
        if rising_edge(CLK) then
            phaseAccumulator_r <= phaseAccumulator_r + unsigned(PHASE_IN);
            if RST = '1' then
                phaseAccumulator_r <= (others => '0');
            end if;
        end if;
    end process;

    -- PHASE_OUT <= std_logic(phaseAccumulator_r(G_ACC_WIDTH-1));
    PHASE_OUT <= std_logic_vector(phaseAccumulator_r);

end RTL;
