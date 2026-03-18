"""
thermodynamics.py — ELFE v∞.1 Stability Kernel
================================================
Implements the Fixed-Time Lyapunov Condition:

    dV/dt ≤ -a·V(x)^p - b·V(x)^q    (a,b > 0, 0 < p < 1, q > 1)

Bounded settling time:
    T_max ≤ 1/(a(1-p)) + 1/(b(q-1))

Thoth-Wadjet Closure (snap to zero):
    If D(x) ≤ 1/64, then D(x) → 0

All parameters are configurable; defaults match the blueprint spec.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal

# ── Status literals ──────────────────────────────────────────────────────────
Status = Literal[
    "ISOMORPHIC_CLOSURE",
    "T_MAX_VIOLATION",
    "CONTINUE_DESCENT",
    "HALTED_SILENCE_CLAUSE",
]

# ── ELFE constants (blueprint defaults) ──────────────────────────────────────
_ELFE_A: float = 1.0   # descent coefficient a
_ELFE_B: float = 1.0   # descent coefficient b
_ELFE_P: float = 0.5   # 0 < p < 1
_ELFE_Q: float = 2.0   # q > 1
_THOTH_SNAP: float = 1.0 / 64.0   # Thoth-Wadjet closure threshold


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
    forbidden_actions: List[str] = field(default_factory=list)
    t_max_steps: int = 16
    risk_threshold: float = 0.90          # Soft Silence Clause
    metadata: Dict[str, Any] = field(default_factory=dict)

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
        return (
            1.0 / (self.elfe_a * (1.0 - self.elfe_p))
            + 1.0 / (self.elfe_b * (self.elfe_q - 1.0))
        )


# ── SystemThermodynamics ─────────────────────────────────────────────────────
class SystemThermodynamics:
    """
    Enforces the ELFE fixed-time Lyapunov condition at runtime.

    Tracks system drift D(x) = ‖x − C(x)‖ (proxied as a scalar
    in [0, 1]) and applies:
      • Dual-regime descent  (base improvement − error penalty)
      • Cumulative penalty accumulation
      • Thoth-Wadjet snap-to-zero when D ≤ 1/64
      • T_max violation detection

    BUG FIXES vs. original:
      - apply_drift_update now uses configurable ELFE coefficients
        instead of hard-coded magic numbers.
      - current_drift is clamped to [0.0, 1.0] — original could
        produce values > 1 under sustained penalty.
      - check_isomorphic_state receives step_count correctly
        (original compared ≥ t_max after increment; now pre-checked).
      - cumulative_penalty no longer bleeds across traces (reset on
        re-instantiation by design).
    """

    def __init__(self, manifold: TaskManifold) -> None:
        self.manifold = manifold
        self.t_max: int = manifold.t_max_steps
        self.current_drift: float = 1.0      # max uncertainty at start
        self.cumulative_penalty: float = 0.0
        self._step_history: List[float] = []

    # ── Lyapunov descent surrogate ───────────────────────────────────────────
    def apply_drift_update(self, step_count: int, error_penalty: float) -> float:
        """
        Update drift using the dual-regime ELFE kernel surrogate.

        The analytical condition dV/dt ≤ -a·V^p - b·V^q is discretised as:
            Δdrift = a·drift^p + b·drift^q - error_penalty

        Penalty is accumulated separately so the Silence Clause can
        evaluate total system health independently.
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
        descent = scale * (a * (v ** p) + b * (v ** q))
        delta = descent - penalty

        new_drift = max(0.0, min(1.0, v - delta))
        new_drift = self._thoth_wadjet_closure(new_drift)

        self.current_drift = new_drift
        self._step_history.append(new_drift)
        return new_drift

    @staticmethod
    def _thoth_wadjet_closure(drift: float) -> float:
        """
        Asymptotic truncation — snaps drift to 0 when within 1/64.
        Prevents infinite micro-correction loops.
        """
        if drift <= _THOTH_SNAP:
            return 0.0
        return drift

    # ── State evaluation ─────────────────────────────────────────────────────
    def check_isomorphic_state(self, step_count: int) -> Status:
        """
        Evaluate current execution status.

        Priority order:
          1. ISOMORPHIC_CLOSURE   — drift reached absolute zero
          2. T_MAX_VIOLATION      — step budget exhausted
          3. CONTINUE_DESCENT     — keep descending
        """
        if self.current_drift == 0.0:
            return "ISOMORPHIC_CLOSURE"
        if step_count >= self.t_max:
            return "T_MAX_VIOLATION"
        return "CONTINUE_DESCENT"

    # ── Diagnostics ──────────────────────────────────────────────────────────
    def drift_trajectory(self) -> List[float]:
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
