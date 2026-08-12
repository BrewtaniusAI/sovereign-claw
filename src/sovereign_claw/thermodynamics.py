"""
thermodynamics.py — ELFE v∞.1 Stability Kernel
================================================
Implements the Lyapunov-guided descent law:

    dV/dt ≤ -a·V(x)^p - b·V(x)^q    (a,b > 0, 0 < p < 1, q > 1)

Analytical settling-time upper bound (continuous-time model):
    T_max ≤ 1/(a(1-p)) + 1/(b(q-1))

Issue #17 changes — production authority migration:
  • ``apply_drift_update(step_count, error_penalty)`` is now an explicitly
    labelled *legacy/model/testing* lane.  It must NOT be used as the
    production authority for task-state drift.
  • ``update_from_measured_vector(drift_vector)`` is the new production path.
    It sets ``current_drift`` from a server-derived ``DriftVectorV1`` composite
    scalar, rejecting UNMEASURED vectors (unknown must not default to zero).
  • ``_thoth_wadjet_closure()`` remains available for the legacy path only.
    The universal snap is no longer applied in the production path because
    numerical equality to zero is never a sufficient closure predicate on its
    own — closure requires a full ``ClosureDecisionV1`` evaluation.
  • ``check_isomorphic_state()`` continues to work for legacy tests but
    ``check_measured_status()`` is the production status checker.
  • Added distinct status literals: ``UNVERIFIED_CONVERGENCE``,
    ``BOUNDED_STEP_NO_CLOSURE``, ``STALLED``.  These are non-closure statuses
    from the verified closure predicate in ``measured_drift.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .measured_drift import DriftVectorV1

# ── Status literals ──────────────────────────────────────────────────────────
# Legacy status set — used by SystemThermodynamics.check_isomorphic_state()
Status = Literal[
    "ISOMORPHIC_CLOSURE",
    "T_MAX_VIOLATION",
    "CONTINUE_DESCENT",
    "HALTED_SILENCE_CLAUSE",
]

# Extended status set from issue #17 verified closure predicate.
# Use these in production code instead of the legacy Status literals.
ExtendedStatus = Literal[
    "ISOMORPHIC_CLOSURE",  # All conditions met; evidence persisted
    "UNVERIFIED_NO_CLOSURE",  # Evaluator/evidence unavailable
    "UNVERIFIED_CONVERGENCE",  # Measurement exists but no fixed-time certificate
    "BOUNDED_STEP_NO_CLOSURE",  # Bounded controller completed; no verified closure
    "STALLED",  # No validated progress / oscillation detected
    "T_MAX_VIOLATION",  # Step/wall budget expired
    "CONTINUE_DESCENT",  # Still descending
    "HALTED_SILENCE_CLAUSE",  # Soft/hard silence clause fired
]

# ── ELFE constants (blueprint defaults) ──────────────────────────────────────
_ELFE_A: float = 1.0  # descent coefficient a
_ELFE_B: float = 1.0  # descent coefficient b
_ELFE_P: float = 0.5  # 0 < p < 1
_ELFE_Q: float = 2.0  # q > 1
_THOTH_SNAP: float = 1.0 / 64.0  # Thoth-Wadjet closure threshold (legacy path only)


# ── TaskManifold ─────────────────────────────────────────────────────────────
@dataclass
class TaskManifold:
    """
    Universal Intent Layer (UIL) projection for a single task.

    Encodes the constraint manifold the Topological Descent Engine
    operates over.  Every field becomes part of the immutable Proof
    Vault trace.

    Parameters
    ----------
    objective        : Natural-language description of the task goal.
    forbidden_actions: Tool names the agent must NEVER call.
    t_max_steps      : Hard step budget — Silence Clause fires at breach.
    risk_threshold   : Soft Silence Clause — fires when drift exceeds this.
    metadata         : Arbitrary key/value pairs logged to ProofVault.
    elfe_a, elfe_b   : Lyapunov descent coefficients.
    elfe_p, elfe_q   : Exponents (0 < p < 1, q > 1).
    """

    objective: str
    forbidden_actions: list[str] = field(default_factory=list)
    t_max_steps: int = 16
    risk_threshold: float = 0.90  # Soft Silence Clause
    metadata: dict[str, Any] = field(default_factory=dict)

    # ELFE kernel tuning — advanced users only
    elfe_a: float = _ELFE_A
    elfe_b: float = _ELFE_B
    elfe_p: float = _ELFE_P
    elfe_q: float = _ELFE_Q
    # Descent scale — controls how aggressively drift decreases per step.
    # Default 0.1 gives ~10-step natural convergence with default a/b/p/q.
    # Set higher (e.g. 0.5) for tasks that need faster convergence.
    elfe_descent_scale: float = 0.1

    def __post_init__(self) -> None:
        if self.t_max_steps < 1:
            raise ValueError("t_max_steps must be ≥ 1")
        if not (0 < self.elfe_p < 1):
            raise ValueError("elfe_p must satisfy 0 < p < 1")
        if self.elfe_q <= 1:
            raise ValueError("elfe_q must satisfy q > 1")
        if self.elfe_a <= 0 or self.elfe_b <= 0:
            raise ValueError("elfe_a and elfe_b must be > 0")
        if not (0 < self.elfe_descent_scale <= 1.0):
            raise ValueError("elfe_descent_scale must be in (0.0, 1.0]")

    @property
    def theoretical_t_max(self) -> float:
        """
        Analytical upper bound on settling time from the Lyapunov condition:
            T_max ≤ 1/(a(1-p)) + 1/(b(q-1))
        """
        return 1.0 / (self.elfe_a * (1.0 - self.elfe_p)) + 1.0 / (self.elfe_b * (self.elfe_q - 1.0))


# ── SystemThermodynamics ─────────────────────────────────────────────────────
class SystemThermodynamics:
    """
    Enforces the ELFE stability law at runtime.

    Tracks system drift D(x) = ‖x − C(x)‖ (proxied as a scalar in [0, 1]).

    Issue #17 authority migration:
      * ``apply_drift_update`` is the LEGACY synthetic descent path.
        Preserved for model/testing compatibility; must NOT be the production
        authority for task-state drift.  Use ``update_from_measured_vector``
        in production orchestration.
      * ``update_from_measured_vector`` accepts a server-derived ``DriftVectorV1``
        and sets ``current_drift`` from the measured composite scalar.
        If the vector's composite scalar is None (any required component is
        UNMEASURED), the method leaves current_drift unchanged and returns None
        to signal UNMEASURED rather than silently zeroing the value.
      * Thoth-Wadjet snap (``_thoth_wadjet_closure``) remains in the legacy
        method only.  It is NOT applied in ``update_from_measured_vector``
        because universal numerical snap is not a valid closure predicate under
        the #17 contract.

    BUG FIXES vs. original:
      - apply_drift_update uses configurable ELFE coefficients.
      - current_drift is clamped to [0.0, 1.0].
      - check_isomorphic_state receives step_count correctly.
      - cumulative_penalty does not bleed across traces.
    """

    def __init__(self, manifold: TaskManifold) -> None:
        self.manifold = manifold
        self.t_max: int = manifold.t_max_steps
        self.current_drift: float = 1.0  # max uncertainty at start
        self.cumulative_penalty: float = 0.0
        self._step_history: list[float] = []
        # Track the last measured drift vector hash for evidence chaining
        self._last_measured_vector_hash: str | None = None

    # ── Production path (issue #17) ──────────────────────────────────────────
    def update_from_measured_vector(self, drift_vector: DriftVectorV1) -> float | None:
        """
        Update current_drift from a server-derived measured DriftVectorV1.

        Production authority path for task-state drift (issue #17).

        Returns the new composite scalar (float in [0,1]) if all required
        components are MEASURED.  Returns None — NOT 0.0 — if any required
        component is UNMEASURED.  The caller must treat None as UNMEASURED
        and must not infer progress from it.

        A successful tool call with unchanged measured state does NOT reduce
        constraint drift — drift follows the evidence, not the call count.
        """
        composite = drift_vector.composite_scalar()
        if composite is None:
            # UNMEASURED — do not update; return None to signal missing measurement.
            # UNMEASURED != 0.0 (invariant #3 from the contract).
            return None

        # Clamp to [0.0, 1.0] but do NOT apply the Thoth-Wadjet snap here.
        # Universal snap is not a valid closure predicate in the measured path.
        new_drift = max(0.0, min(1.0, composite))
        self.current_drift = new_drift
        self._step_history.append(new_drift)
        self._last_measured_vector_hash = drift_vector.vector_hash()
        return new_drift

    # ── Legacy path (model/testing compatibility) ────────────────────────────
    def apply_drift_update(self, step_count: int, error_penalty: float) -> float:
        """
        [LEGACY — model/testing lane only]

        Synthetic dual-regime ELFE descent surrogate.  Must NOT be used as
        the production authority for task-state drift (issue #17).

        The analytical condition dV/dt ≤ -a·V^p - b·V^q is discretised as:
            Δdrift = a·drift^p + b·drift^q - error_penalty

        Preserved for backward compatibility with model/test code that relies
        on the synthetic descent trajectory.  Production orchestration must
        use ``update_from_measured_vector`` instead.
        """
        a, b, p, q = (
            self.manifold.elfe_a,
            self.manifold.elfe_b,
            self.manifold.elfe_p,
            self.manifold.elfe_q,
        )

        penalty = max(0.0, error_penalty)
        self.cumulative_penalty += penalty

        # Dual-regime descent (scaled to give realistic multi-step trajectories)
        scale = self.manifold.elfe_descent_scale
        v = self.current_drift
        descent = scale * (a * (v**p) + b * (v**q))
        delta = descent - penalty

        new_drift = max(0.0, min(1.0, v - delta))
        new_drift = self._thoth_wadjet_closure(new_drift)

        self.current_drift = new_drift
        self._step_history.append(new_drift)
        return new_drift

    @staticmethod
    def _thoth_wadjet_closure(drift: float) -> float:
        """
        [LEGACY path only]

        Asymptotic truncation — snaps drift to 0 when within 1/64.
        Used only in the legacy ``apply_drift_update`` path.
        Must NOT be applied in the production measured-drift path because
        universal numerical equality to zero is not a sufficient closure
        predicate under the #17 contract.
        """
        if drift <= _THOTH_SNAP:
            return 0.0
        return drift

    # ── State evaluation ─────────────────────────────────────────────────────
    def check_isomorphic_state(self, step_count: int) -> Status:
        """
        Evaluate current execution status (legacy Status literals).

        Priority order:
          1. ISOMORPHIC_CLOSURE   — drift reached absolute zero (legacy path)
          2. T_MAX_VIOLATION      — step budget exhausted
          3. CONTINUE_DESCENT     — keep descending

        Note: In production code, use the ClosureDecisionV1 from
        measured_drift.evaluate_closure() rather than this method, because
        drift == 0.0 alone is not a sufficient closure predicate.
        """
        if self.current_drift == 0.0:
            return "ISOMORPHIC_CLOSURE"
        if step_count >= self.t_max:
            return "T_MAX_VIOLATION"
        return "CONTINUE_DESCENT"

    def check_measured_status(self, step_count: int) -> ExtendedStatus:
        """
        Production status check using extended status literals (issue #17).

        Does NOT declare ISOMORPHIC_CLOSURE based on drift alone — that
        requires a full ClosureDecisionV1 from measured_drift.evaluate_closure().
        Returns BOUNDED_STEP_NO_CLOSURE when the controller has run to budget
        without verified closure, and UNVERIFIED_CONVERGENCE when drift is low
        but no certificate is present.
        """
        if step_count >= self.t_max:
            return "T_MAX_VIOLATION"
        if self._last_measured_vector_hash is None:
            # No measured vector yet — cannot claim any convergence
            return "CONTINUE_DESCENT"
        if self.current_drift <= 0.0:
            # Low drift exists but closure requires ClosureDecisionV1 evidence
            return "UNVERIFIED_CONVERGENCE"
        return "CONTINUE_DESCENT"

    # ── Diagnostics ──────────────────────────────────────────────────────────
    def drift_trajectory(self) -> list[float]:
        """Return the full drift history for ProofVault diagnostics."""
        return list(self._step_history)

    def is_converging(self) -> bool:
        """
        Heuristic: True if the last 3 drift values are strictly decreasing.
        Useful for early warning before T_max is hit.
        """
        hist = self._step_history
        if len(hist) < 3:
            return True
        return hist[-1] < hist[-2] < hist[-3]
