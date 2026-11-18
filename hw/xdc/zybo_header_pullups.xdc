# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: CERN-OHL-W-2.0
# DS-P2.309 interim (RTL-P2.2402): remove when rr ships the per-target pull key (DS-P3.269).
# Every Pmod JA/JB header input idles HIGH (UART RX, SPI CS and I2C all idle high):
# an undriven input with no pull reads low and the I2C logger reports a stretch storm.
set_property PULLUP true [get_ports {ja[0]}]
set_property PULLUP true [get_ports {ja[1]}]
set_property PULLUP true [get_ports {ja[2]}]
set_property PULLUP true [get_ports {ja[3]}]
set_property PULLUP true [get_ports {ja[4]}]
set_property PULLUP true [get_ports {ja[5]}]
set_property PULLUP true [get_ports {ja[6]}]
set_property PULLUP true [get_ports {ja[7]}]
set_property PULLUP true [get_ports {jb[0]}]
set_property PULLUP true [get_ports {jb[1]}]
set_property PULLUP true [get_ports {jb[2]}]
set_property PULLUP true [get_ports {jb[3]}]
set_property PULLUP true [get_ports {jb[4]}]
set_property PULLUP true [get_ports {jb[5]}]
set_property PULLUP true [get_ports {jb[6]}]
set_property PULLUP true [get_ports {jb[7]}]
