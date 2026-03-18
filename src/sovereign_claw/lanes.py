"""
lanes.py — Tri-Temporal Execution Governor
==========================================
Defines the three execution lanes required by the Sovereign Claw spec:

    Lane 1 — REFLEX       : KitaevZeroMode tool sandbox (sub-second)
    Lane 2 — DELIBERATE   : Rabbit/Cypher reasoning loop (seconds)
    Lane 3 — AUTHORITATIVE: Giles sealed closure (minutes, cryptographic)

The no-skip invariant is enforced by LaneRouter:
  • Every task must pass through REFLEX → DELIBERATE → AUTHORITATIVE
    in order.
  • Short-circuit to AUTHORITATIVE is only permitted if drift == 0.0
    (ISOMORPHIC_CLOSURE already achieved in a lower lane).
  • Stall guard fires when deliberate_loops >= max_deliberate_loops.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional


class Lane(str, Enum):
    REFLEX        = "REFLEX"        # Lane 1 — fast, isolated tool execution
    DELIBERATE    = "DELIBERATE"    # Lane 2 — Rabbit/Cypher reasoning
    AUTHORITATIVE = "AUTHORITATIVE" # Lane 3 — Giles sealed output
    STALL         = "STALL"         # Hard stop — loop guard triggered


# ── Human-readable descriptions ───────────────────────────────────────────────
LANE_DESCRIPTIONS: Dict[Lane, str] = {
    Lane.REFLEX:        "Kitaev Zero-Mode sandboxed tool execution",
    Lane.DELIBERATE:    "Rabbit draft → Cypher audit reasoning loop",
    Lane.AUTHORITATIVE: "Giles sealed authoritative closure + ProofVault write",
    Lane.STALL:         "Loop guard exceeded — hard stop, state sealed",
}

# ── Exported constants (backward-compat with original lanes.py stub) ──────────
LANE_REFLEX        = Lane.REFLEX
LANE_DELIBERATE    = Lane.DELIBERATE
LANE_AUTHORITATIVE = Lane.AUTHORITATIVE


# ── LaneRouter ────────────────────────────────────────────────────────────────
class LaneRouter:
    """
    Stateful tri-temporal router.

    Usage
    -----
    router = LaneRouter(max_deliberate_loops=2)
    while not router.done:
        current_lane = router.current
        # ... run the node for current_lane ...
        router.advance(approved=True/False, drift=0.3)
    print(router.final_status)
    """

    def __init__(self, max_deliberate_loops: int = 2) -> None:
        if max_deliberate_loops < 1:
            raise ValueError("max_deliberate_loops must be ≥ 1")
        self.max_deliberate_loops = max_deliberate_loops
        self._current: Lane = Lane.REFLEX
        self._deliberate_loops: int = 0
        self._done: bool = False
        self._final_status: Optional[str] = None

    @property
    def current(self) -> Lane:
        return self._current

    @property
    def done(self) -> bool:
        return self._done

    @property
    def final_status(self) -> Optional[str]:
        return self._final_status

    def advance(self, approved: bool, drift: float = 1.0) -> Lane:
        """
        Transition to the next lane given approval and current drift.

        Parameters
        ----------
        approved : Whether the deliberate layer signed off on the output.
        drift    : Current system drift (0.0 triggers early AUTHORITATIVE).

        Returns
        -------
        The new current lane.
        """
        if self._done:
            return self._current

        if drift == 0.0:
            # Early isomorphic closure — skip ahead
            self._current = Lane.AUTHORITATIVE
            return self._current

        if self._current == Lane.REFLEX:
            self._current = Lane.DELIBERATE

        elif self._current == Lane.DELIBERATE:
            self._deliberate_loops += 1
            if approved:
                self._current = Lane.AUTHORITATIVE
            elif self._deliberate_loops >= self.max_deliberate_loops:
                self._current = Lane.STALL
                self._done = True
                self._final_status = "T_MAX_VIOLATION_STALL"
            else:
                self._current = Lane.DELIBERATE  # re-enter loop

        elif self._current == Lane.AUTHORITATIVE:
            self._done = True
            self._final_status = "ISOMORPHIC_CLOSURE"

        return self._current

    def reset(self) -> None:
        self._current = Lane.REFLEX
        self._deliberate_loops = 0
        self._done = False
        self._final_status = None
