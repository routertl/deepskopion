# rr unknown-llm (AI-generated)
"""DS-P3.74: arbitrate the two writers of a datalogger's ``enable`` bit.

Two control sources can set a datalogger's ``enable`` (payload bit 0 of the
addressed ``DATALOGGER_CONFIG`` word, see datalogger_gather_contract.md
§"Enable arbitration"):

* the HOST, through the wave_stream CONTROL frame (DS-P3.72) — the user's
  stated intent, and
* a TRIGGER action that reconfigures the enable-mask adaptively (DS-P3.39).

Without a rule, the last writer wins, so a trigger could re-enable a datalogger
the user switched off (or a host write could silently cancel a trigger's
transient gate). The rule implemented here:

1. The host word is the BASELINE and the CEILING. Its ``enable`` bit is absolute
   when it is 0: a trigger can never enable a datalogger the host has disabled
   (or never configured — the ``config_control`` power-on latch is 0, i.e.
   disabled).
2. A trigger supplies a transient 32-bit ALLOW mask over device ids
   (bit ``i`` = id ``i``). The effective enable is
   ``host_enable(id) AND allow(id)``. A trigger can therefore only gate a
   host-enabled datalogger off, and restore it.
3. A host write during an active trigger mask updates the baseline at once.
   The effective word is recomputed against the active mask: a host disable
   wins immediately; a host enable on a trigger-gated id stays gated until the
   mask is released.
4. Releasing the mask (revert) restores the LATEST host baseline, not a snapshot
   taken when the trigger fired.
5. The trigger touches bit 0 only. Every other payload bit is the host's.
6. A CAN data-bank word (id ``CAN_DEV_ID`` with payload bit 10 set) is not an
   enable word — its bit 0 is ``data_cyc[0]`` (payload bit 9 clear) or bit 0 of a
   sample point (payload bit 9 set). It passes through unchanged and is never
   recorded as an enable baseline.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

ALL_ALLOWED = 0xFFFFFFFF          # no trigger override: every id allowed
_ENABLE_BIT = 0x1
CAN_DEV_ID = 20                    # CAN datalogger id; banked config (DS-P3.108b)
CAN_CFG_TYPE_DATA = 1 << 10        # payload bit 10 = CAN data-bank select


def is_enable_word(datalogger_id: int, cfg_word: int) -> bool:
    """True when payload bit 0 of this word is the datalogger ``enable``."""
    return not (datalogger_id == CAN_DEV_ID and cfg_word & CAN_CFG_TYPE_DATA)


def effective_cfg(host_cfg: int, datalogger_id: int, allow_mask: int) -> int:
    """The word to write: the host word with bit 0 ANDed with the trigger
    allow bit for this id. Pure; the whole precedence rule in one line."""
    allowed = (allow_mask >> datalogger_id) & 1
    return host_cfg & ~_ENABLE_BIT | (host_cfg & _ENABLE_BIT & allowed)


class EnableArbiter:
    """Single owner of the per-datalogger enable for one regbank.

    ``write`` is the low-level applier: ``write(datalogger_id, cfg_word)``
    returning None or a reject reason (``apply_datalogger_config`` bound to a
    regbank). Every CONFIG write — host or trigger — goes through here.
    """

    def __init__(self, write: Callable[[int, int], Optional[str]]) -> None:
        self._write = write
        self._host: Dict[int, int] = {}      # id -> last accepted host enable-word
        self._written: Dict[int, int] = {}   # id -> last enable-word written
        self._allow = ALL_ALLOWED

    @property
    def allow_mask(self) -> int:
        return self._allow

    def host_cfg(self, datalogger_id: int) -> Optional[int]:
        return self._host.get(datalogger_id)

    def apply_host(self, datalogger_id: int, cfg_word: int) -> Optional[str]:
        """Apply a host CONFIG word (rule 1, 3, 5, 6)."""
        if not is_enable_word(datalogger_id, cfg_word):
            return self._write(datalogger_id, cfg_word)
        word = effective_cfg(cfg_word, datalogger_id, self._allow)
        err = self._write(datalogger_id, word)
        if err is None:
            self._host[datalogger_id] = cfg_word
            self._written[datalogger_id] = word
        return err

    def apply_trigger_mask(self, allow_mask: int) -> Optional[str]:
        """Install a trigger ALLOW mask (rule 2). Rewrites only the ids whose
        effective word changes. An id the host never configured is left alone:
        its power-on latch is disabled and a trigger cannot raise it."""
        if not 0 <= allow_mask <= ALL_ALLOWED:
            return f"allow_mask 0x{allow_mask:x} is not a 32-bit id mask"
        self._allow = allow_mask
        for dl_id, host_cfg in sorted(self._host.items()):
            word = effective_cfg(host_cfg, dl_id, self._allow)
            if word != self._written.get(dl_id):
                err = self._write(dl_id, word)
                if err is not None:
                    return err
                self._written[dl_id] = word
        return None

    def release_trigger_mask(self) -> Optional[str]:
        """Revert the trigger override: the latest host baseline stands (rule 4)."""
        return self.apply_trigger_mask(ALL_ALLOWED)
