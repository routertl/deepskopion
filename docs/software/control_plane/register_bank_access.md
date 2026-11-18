# Register Bank Access

The FPGA exposes a memory mapped register bank derived from a YAML schema. Software uses this register bank to configure the dataloggers and to read back status.

## Access Mechanism

The register bank is exposed to the Processing System (PS) as a memory mapped region. Depending on the chosen implementation, access is done via:

*   **mmap on /dev/uioX:** Using the Userspace I/O (UIO) driver framework.
*   **Kernel Driver IO:** An IO region exposed by a custom kernel driver.

The Software HAL (Hardware Abstraction Layer) provides helper functions to abstract the underlying access method:
```c
uint32_t dsk_reg_read(uint32_t index);
void     dsk_reg_write(uint32_t index, uint32_t value);
```

## Error Handling

The access library is responsible for:

*   **Schema Validation:** Handling missing or unexpected registers (schema mismatch).
*   **Protection:** Protecting read-only registers from writes.
*   **Feedback:** Providing useful error messages back to user space when access violations occur.
