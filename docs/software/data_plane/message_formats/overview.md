# Message Formats Overview

This page defines the binary format of messages written by the FPGA logic into DDR. All messages share a common header, followed by a protocol-specific payload.

Messages are byte-aligned and can be parsed sequentially from a DMA buffer without any memory allocations. The format is stable and versioned.

## High Level Concepts
A message represents one protocol-level event:
*   a **UART** character
*   an **I2C** frame
*   an **SPI** transfer
*   an **I2S** sample (stream)
*   a **QSPI** nibble or word
*   an **Edge Event** from the Edge Detector

## Common Structure
All messages include:
1.  **Header:** Meta-data (ID, Type, Length).
2.  **Timestamp:** 64-bit Global Time.
3.  **Payload:** Variable length data.

## Endianness
All fields are stored in **Little Endian** format (matching the ARM Cortex-A9 and Zynq architecture).

## Burst vs Stream
*   **Burst Messages:** Variable sized payloads.
*   **Stream Messages (I2S):** Fixed width payload, sampled continuously.

The header format is identical for both; only the payload interpretation differs.
