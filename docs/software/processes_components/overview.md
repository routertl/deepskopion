# Linux Stack and Software Layer

The Linux layer runs on the ARM Cortex-A9 cores of the Zynq. It serves as the "Server" side of the Deepskopion system.

## Components

### 1. Kernel Drivers
*   **DMA Management:** Allocates the contiguous DDR memory regions for the **Circular Buffers**.
*   **Descriptor Control:** Manages the AXI DMA Buffer Descriptors (BDs). The driver or userspace resets these descriptors once data is consumed, allowing the FPGA to overwrite old data in the ring.
*   **Interrupts:** Catches DMA interrupts to notify user space of new available data.
*   **UIO / mmap:** Exposes the physical ring buffer memory to the `dataloggerd` daemon for zero-copy access.

### 2. The Datalogger Daemon (`dataloggerd`)
The core userspace application.
*   **Buffer Reader:** Reads valid messages from the Circular Buffers by checking the DMA descriptors.
*   **Protocol Decoding (Level 1):** Performs fundamental unpacking of the FPGA message format.
*   **Network Server:** Listens for connections from the PC GUI.
*   **Configuration Manager:** Receives commands from the PC (e.g., "Start SPI Datalogger", "Set Voltage to 1.8V") and writes registers in the FPGA to apply them.

### 3. Communication Protocol
The Daemon sends data to the PC.
*   **Transport:** TCP/IP (reliable control/status) + potentially UDP or TCP for high-bandwidth data stream.
*   **Data Format:** A structural representation of the captured logic data, optimized for parsing by the C++ GUI.
