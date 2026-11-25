# rr unknown-llm (AI-generated)
"""Production DE-C0 register and wire stimulus constants (DS-P1.58).

Moved to ``sw.stream.stimulus_contract`` (DS-P2.136) because the daemon arms the
generator from it and the DE25 rootfs strips this testbench package. Re-exported
here so the witness tooling keeps its import path. Patch hooks such as
``_open_mmuart`` on ``sw.stream.stimulus_contract``, not on this module.
"""

from sw.stream.stimulus_contract import *  # noqa: F401,F403
from sw.stream.stimulus_contract import (  # noqa: F401  (private names * skips)
    _MSS_DRIVABLE, _open_mmuart, _set_115200_8n1,
)
