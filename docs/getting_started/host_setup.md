# Host setup

The build host needs these packages and fixes before the board guides. The versions of every tool are in the Requirements table of [the README](https://github.com/routertl/deepskopion/blob/main/README.md).

The steps assume a host with `python3`, `python3-venv`, `pip`, `git`, `sudo` and the `en_US.UTF-8` locale. On
Ubuntu and Debian (a minimal or container image runs these as root, without the `sudo` prefix, and adds `sudo` to
the list):

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git locales curl libopengl0
sudo locale-gen en_US.UTF-8
```

Vivado 2024.1 stops with exit code 134 (`locale::facet::_S_create_c_locale name not valid`) on a host without the
`en_US.UTF-8` locale, as a minimal or container image is.

On Ubuntu 24.04, Vivado 2024.1 stops at start with `couldn't load file librdi_commontasks.so: libtinfo.so.5: cannot
open shared object file`: Ubuntu 24.04 ships only `libtinfo.so.6`. This link gives Vivado the name it loads:

```bash
sudo ln -s /usr/lib/x86_64-linux-gnu/libtinfo.so.6 /usr/lib/x86_64-linux-gnu/libtinfo.so.5
```

## Python 3.11 on Ubuntu 22.04

RouteRTL needs Python 3.11 or newer. On Ubuntu 24.04, `python3` is 3.12 and the quick starts run as written. On
Ubuntu 22.04, `python3` is 3.10 and the distribution has no released Python 3.11 package, so these three commands
replace each quick start's `python3 -m venv .venv` line. They install a user-local Python 3.11 through uv and need no
sudo:

```bash
python3 -m venv .uv && .uv/bin/pip install uv
.uv/bin/uv python install 3.11
.uv/bin/uv venv --seed --python 3.11 .venv
```

`--seed` puts pip into the environment, so the following `pip install` lines install into Python 3.11.

## Install RouteRTL and clone the repository

Each board's build below starts from this, in the same shell:

```bash
python3 -m venv .venv          # Ubuntu 22.04: the three commands of "Python 3.11 on Ubuntu 22.04" instead
source .venv/bin/activate
pip install routertl
git clone https://github.com/routertl/deepskopion.git deepskopion
cd deepskopion
```

With an unpacked archive instead of the clone, `cd` into its folder in place of the last two lines. The simulations
need the clone. A board build from the archive runs, but its bitstream carries no build stamp, so `rr hw bit-sha`
cannot confirm that build on the board.
