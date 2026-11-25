// rr unknown-llm (AI-generated)
// SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
// SPDX-FileCopyrightText: 2026 Daniel J. Mazure
//
// Top-level HDL wrapper for PolarFire SoC Discovery Kit MSS & SmartDesign design.
// DS-P2.142 (PF-H1): Staged Libero MSS/DDR design for Discovery Kit HSS DDR init.
//
// Exposes minimal fabric I/O to avoid I/O over-allocation while instantiating
// the generated SmartDesign MSS subsystem (disco_mss_sd).

`timescale 1ns / 1ps

module pf_mss_top_wrapper (
    input  wire       ref_clk_50mhz,
    output wire [3:0] led
);
    // Minimal fabric activity indicator
    assign led[0]   = ref_clk_50mhz;
    assign led[3:1] = 3'b000;

    // SmartDesign MSS subsystem instance.
    // Dedicated MSS pads (DDR4, MMUART0, etc.) are connected automatically
    // by Libero to silicon package balls.
    disco_mss_sd u_mss ();

endmodule
