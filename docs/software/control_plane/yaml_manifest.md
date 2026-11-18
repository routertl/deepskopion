# YAML Manifest and Discovery

The register bank is self describing. 

## Discovery Mechanism

A manifest is stored in **BRAM** (Block RAM) within the PL and can be read by software at a fixed base address.

### Build-Time ID Generation
To manage complexity, **Datalogger Instance IDs** and **Message Type IDs** are assigned automatically at build time.

1.  **Input:** The Project YAML defines instances with `id: auto`.
    ```yaml
    dataloggers:
      uart:
        instances:
          - { name: uart0, id: auto }
          - { name: uart1, id: auto }
    ```
2.  **Generator:** A Python script runs before synthesis.
    *   Assigns contiguous IDs (0, 1, 2...).
    *   Generates **C Header** (`datalogger_ids.h`) for the Software.
    *   Generates **VHDL Package** (`datalogger_ids_pkg.vhd`) for the RTL.
3.  **Result:**
    *   **Software:** Uses `#define ZS_DATALOGGER_ID_UART0 0`.
    *   **Hardware:** Uses `constant C_DATALOGGER_ID_UART0 : natural := 0;`.

This ensures the manifest, software, and hardware remain perfectly synchronized.
