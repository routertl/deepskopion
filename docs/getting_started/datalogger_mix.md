# Data logger combination

The combination of data loggers in a build is declared in a YAML file, which the target names in `system_config`. [`tools/datalogger-builder/configurator.py`](https://github.com/routertl/deepskopion/blob/main/tools/datalogger-builder/README.md) writes that file, checks the combination against the board's free pins and the limit of 31 data loggers, estimates resource use, and prints which board pin carries each signal.

```bash
# 1. Write the combination
python3 tools/datalogger-builder/configurator.py \
    --board zybo-z7-20 \
    --uart 2 --i2c 2 --spi 1 --edge 4 \
    -o tools/datalogger-builder/config/my_mix.yml

# 2. Make a target for it
rr target clone zybo_datalogger my_mix
#    In targets/my_mix.yml, name the combination file in both places that read it:
#    system_config: tools/datalogger-builder/config/my_mix.yml
#    and, in hooks.pre_build, the first argument of the datalogger-builder hook:
#      - tools/datalogger-builder/config/my_mix.yml

# 3. Select it, lock the project for it, and build as above
rr target set my_mix
rr lock --update
```

The build regenerates the gather RTL and its packages from the combination. `configurator.py --interactive` asks for the same values one at a time.

Four combinations ship as files and build the same way, from a clone of the board's datalogger target:

| Combination file (`tools/datalogger-builder/config/`) | Board target to clone | Dataloggers |
|---|---|---|
| `zybo_datalogger_config_5uart_2i2c.yml` | `zybo_datalogger` | 5 UART, 2 I2C |
| `zybo_datalogger_config_0uart_5i2c_1spi.yml` | `zybo_datalogger` | 5 I2C, 1 SPI |
| `de25_datalogger_config_5uart_2i2c.yml` | `de25_datalogger` | 5 UART, 2 I2C |
| `de25_datalogger_config_0uart_5i2c_1spi.yml` | `de25_datalogger` | 5 I2C, 1 SPI |

No combination file ships for the PolarFire SoC Discovery Kit. Its combinations are written with `configurator.py --board
polarfire-soc-discovery`, within the kit's 14-pin budget, and only from protocols with a measured resource cost there
(the configurator refuses QSPI, MDIO, I2S and 1-Wire for the kit).
