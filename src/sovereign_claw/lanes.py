"""
lanes.py — Tri-Temporal Execution Governor
==========================================
Defines the three execution lanes required by the Sovereign Claw spec:

    Lane 1 — REFLEX       : KitaevZeroMode tool sandbox (sub-second)
    Lane 2 — DELIBERATE   : Rabbit/Cypher reasoning loop (seconds)
    Lane 3 — AUTHORITATIVE: Giles sealed closure (minutes, cryptographic)

Issue #17 changes:
  • ``advance(approved, drift)`` is now an explicitly labelled *legacy/
    model/testing* path.  Caller-supplied ``drift == 0.0`` no longer causes
    REFLEX → AUTHORITATIVE in the production path because:
      - A fabricated or NaN/negative scalar cannot confer lane authority.
      - AUTHORITATIVE output is a lane/state, not proof of closure.
      - Final closure comes from ``ClosureDecisionV1``, not a numeric snap.
  • ``advance_from_evidence(evidence)`` is the new production path.
    It accepts server-derived ``LaneTransitionEvidenceV1`` and enforces that:
      - No scalar-zero or caller-approved transition skips the deliberate gate.
      - Only a verified ``ISOMORPHIC_CLOSURE`` in the evidence permits
        REFLEX → AUTHORITATIVE shortcut.
      - AUTHORITATIVE completion sets final_status to the evidence
        closure_status (which may be non-closure).
  • ``advance(approved, drift)`` retains its semantics for backward compat
    but is annotated as non-authoritative legacy.
"""

from __future__ import annotations

from enum import Enum

from .measured_drift import LaneTransitionEvidenceV1


class Lane(str, Enum):
    REFLEX = "REFLEX"  # Lane 1 — fast, isolated tool execution
    DELIBERATE = "DELIBERATE"  # Lane 2 — Rabbit/Cypher reasoning
    AUTHORITATIVE = "AUTHORITATIVE"  # Lane 3 — Giles sealed output
    STALL = "STALL"  # Hard stop — loop guard triggered


# ── Human-readable descriptions ───────────────────────────────────────────────
LANE_DESCRIPTIONS: dict[Lane, str] = {
    Lane.REFLEX: "Kitaev Zero-Mode sandboxed tool execution",
    Lane.DELIBERATE: "Rabbit draft → Cypher audit reasoning loop",
    Lane.AUTHORITATIVE: "Giles sealed authoritative closure + ProofVault write",
    Lane.STALL: "Loop guard exceeded — hard stop, state sealed",
}

# ── Exported constants (backward-compat with original lanes.py stub) ──────────
LANE_REFLEX = Lane.REFLEX
LANE_DELIBERATE = Lane.DELIBERATE
LANE_AUTHORITATIVE = Lane.AUTHORITATIVE


# ── LaneRouter ────────────────────────────────────────────────────────────────
class LaneRouter:
    """
    Stateful tri-temporal router.

    Production usage (issue #17):
        Use ``advance_from_evidence`` with server-derived
        ``LaneTransitionEvidenceV1`` for authoritative lane transitions.

    Legacy/test usage:
        ``advance(approved, drift)`` is preserved for model/test compatibility
        but is non-authoritative.  Caller-supplied ``drift == 0.0`` or
        ``approved=True`` cannot substitute for measured evidence.
    """

    def __init__(self, max_deliberate_loops: int = 2) -> None:
        if max_deliberate_loops < 1:
            raise ValueError("max_deliberate_loops must be ≥ 1")
        self.max_deliberate_loops = max_deliberate_loops
        self._current: Lane = Lane.REFLEX
        self._deliberate_loops: int = 0
        self._done: bool = False
        self._final_status: str | None = None

    @property
    def current(self) -> Lane:
        return self._current

    @property
    def done(self) -> bool:
        return self._done

    @property
    def final_status(self) -> str | None:
        return self._final_status

    def advance_from_evidence(self, evidence: LaneTransitionEvidenceV1) -> Lane:
        """
        [PRODUCTION PATH — issue #17]

        Transition lanes using server-derived ``LaneTransitionEvidenceV1``.

        Rules enforced:
          - Only ``ISOMORPHIC_CLOSURE`` closure_status permits
            REFLEX → AUTHORITATIVE shortcut (not any scalar-zero value, and
            not UNVERIFIED_CONVERGENCE).
          - ``POLICY_DENIED``, ``EXECUTION_FAILURE``, or ``T_MAX_VIOLATION``
            always route to STALL.
          - AUTHORITATIVE completion records the evidence closure_status as
            final_status (which may be a non-closure status).
          - No caller-controlled drift scalar can substitute for evidence.
          - The evidence ``prior_lane`` must match the router's current lane
            (fabricated public LaneTransitionEvidenceV1 for a wrong lane is
            rejected; the router stays in current lane and routes to STALL).
        """
        if self._done:
            return self._current

        # Terminal failures always route to STALL regardless of lane
        if evidence.closure_status in ("POLICY_DENIED", "EXECUTION_FAILURE", "T_MAX_VIOLATION"):
            self._current = Lane.STALL
            self._done = True
            self._final_status = evidence.closure_status
            return self._current

        # Validate that evidence.prior_lane matches the router's current lane.
        # A fabricated LaneTransitionEvidenceV1 for a different lane is rejected.
        if evidence.prior_lane != self._current.value:
            self._current = Lane.STALL
            self._done = True
            self._final_status = "EVIDENCE_FAILURE"  # prior_lane mismatch — evidence rejected
            return self._current

        # Only verified ISOMORPHIC_CLOSURE may shortcut to AUTHORITATIVE
        if evidence.closure_status == "ISOMORPHIC_CLOSURE" and self._current == Lane.REFLEX:
            self._current = Lane.AUTHORITATIVE
            return self._current

        if self._current == Lane.REFLEX:
            self._current = Lane.DELIBERATE

        elif self._current == Lane.DELIBERATE:
            self._deliberate_loops += 1
            # Only ISOMORPHIC_CLOSURE grants entry to AUTHORITATIVE;
            # UNVERIFIED_CONVERGENCE does NOT constitute approval.
            approved = evidence.closure_status == "ISOMORPHIC_CLOSURE"
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
            self._final_status = evidence.closure_status

        return self._current

    def advance(self, approved: bool, drift: float = 1.0) -> Lane:
        """
        [LEGACY — model/testing lane only]

        Transition lanes given approval and a caller-supplied drift scalar.

        Non-authoritative: caller-supplied ``drift == 0.0`` or
        ``approved=True`` may transition lanes for model/test use but must
        NOT be treated as evidence of closure in production execution.
        Final closure requires a ``ClosureDecisionV1`` from
        ``measured_drift.evaluate_closure()``, not this method.

        Preserved for backward compatibility with existing tests and model code.
        """
        if self._done:
            return self._current

        if drift == 0.0:
            # Legacy early-closure shortcut — non-authoritative.
            # Production code must use advance_from_evidence() instead.
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
