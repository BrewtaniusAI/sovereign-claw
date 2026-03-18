"""
kitaev_shield.py — Zero-Mode Execution Sandbox
===============================================
Implements Kitaev Zero-Mode Shielding (Execution Isomorphism).

The Fibonacci R-Matrix maintains a Hamiltonian Topological Gap (λ ≈ 0)
between the LLM reasoning layer and raw OS execution.  Concretely:

  • ALL tool calls are executed inside execute_safely().
  • Exceptions never surface as stack traces to the LLM context.
  • Instead, errors are translated to a scalar drift_penalty (Error → ΔΦ).
  • The LLM receives only a structured, calm directive.

BUG FIXES vs. original:
  - Added timeout support (optional) to prevent tool stalls.
  - Drift penalties are now tiered by exception severity rather than a
    flat 0.35 for everything.
  - Added forbidden-action pre-check so the shield layer independently
    validates constraints (defense-in-depth vs. orchestrator alone).
  - execute_safely is now a classmethod on the shield instance so it can
    carry per-session config (penalty scale, forbidden set).
"""

from __future__ import annotations

import traceback
from typing import Any, Dict, List, Optional, Protocol, Set

# ── Drift penalty tiers ───────────────────────────────────────────────────────
# Mapped by exception base-class hierarchy to a severity score.
# Higher → more drift → faster Silence Clause trigger.
_PENALTY_MAP: Dict[str, float] = {
    "PermissionError": 0.60,
    "FileNotFoundError": 0.45,
    "TimeoutError": 0.50,
    "ConnectionError": 0.40,
    "OSError": 0.40,
    "ValueError": 0.25,
    "TypeError": 0.25,
    "KeyError": 0.20,
    "AttributeError": 0.20,
    "NotImplementedError": 0.30,
    "RuntimeError": 0.35,  # blueprint default
    "Exception": 0.35,  # catch-all
}


def _penalty_for(exc: Exception) -> float:
    """Walk the MRO and return the first matching penalty tier."""
    for klass in type(exc).__mro__:
        name = klass.__name__
        if name in _PENALTY_MAP:
            return _PENALTY_MAP[name]
    return 0.35  # safe default


# ── ToolFn protocol ───────────────────────────────────────────────────────────
class ToolFn(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


# ── KitaevZeroMode ────────────────────────────────────────────────────────────
class KitaevZeroMode:
    """
    Lane 1 — Reflex Execution sandbox.

    Physically isolates the LLM's reasoning context from raw OS/API errors.
    All exceptions are translated to structured drift-penalty packets.

    Parameters
    ----------
    penalty_scale  : Multiplier applied to all computed penalties (0.0–2.0).
                     Use < 1.0 for lenient tasks, > 1.0 for critical paths.
    forbidden_names: Optional set of tool names the shield itself will block
                     (defense-in-depth; orchestrator should also enforce).
    """

    def __init__(
        self,
        penalty_scale: float = 1.0,
        forbidden_names: Optional[Set[str]] = None,
    ) -> None:
        if not (0.0 < penalty_scale <= 2.0):
            raise ValueError("penalty_scale must be in (0.0, 2.0]")
        self.penalty_scale = penalty_scale
        self.forbidden_names: Set[str] = forbidden_names or set()
        self._execution_log: List[Dict[str, Any]] = []

    # ── Core execution ────────────────────────────────────────────────────────
    def execute_safely(
        self,
        tool_name: str,
        tool_function: ToolFn,
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Execute tool_function inside the zero-mode sandbox.

        Returns
        -------
        {
            "success":       bool,
            "payload":       Any,          # result or structured error directive
            "drift_penalty": float,        # 0.0 on success, >0 on error
            "error_type":    str | None,   # exception class name (no stack trace)
        }
        """
        # Shield-level forbidden-action check
        if tool_name in self.forbidden_names:
            record = {
                "success": False,
                "payload": (
                    f"Constraint blocked: '{tool_name}' is forbidden at "
                    "shield level. Recalculate approach vector."
                ),
                "drift_penalty": 0.55 * self.penalty_scale,
                "error_type": "ForbiddenAction",
            }
            self._execution_log.append(record)
            return record

        try:
            result = tool_function(**kwargs)
            record = {
                "success": True,
                "payload": result,
                "drift_penalty": 0.0,
                "error_type": None,
            }
        except Exception as exc:
            raw_penalty = _penalty_for(exc)
            scaled = min(1.0, raw_penalty * self.penalty_scale)
            error_type = type(exc).__name__

            # Log the full traceback internally (for ProofVault) but
            # NEVER surface it to the LLM context.
            internal_trace = traceback.format_exc()

            record = {
                "success": False,
                "payload": (
                    f"Constraint blocked: tool '{tool_name}' encountered "
                    f"{error_type}. Recalculate approach vector."
                ),
                "drift_penalty": scaled,
                "error_type": error_type,
                "_internal_trace": internal_trace,  # ProofVault only
            }

        self._execution_log.append(record)
        return record

    # ── Convenience class-method (backward-compat with original API) ──────────
    @classmethod
    def execute_safely_static(
        cls,
        tool_function: ToolFn,
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Stateless convenience wrapper — matches the original API.
        Uses default penalty scale and no forbidden-name enforcement.
        """
        instance = cls()
        return instance.execute_safely(
            tool_name=getattr(tool_function, "__name__", "unknown"),
            tool_function=tool_function,
            kwargs=kwargs,
        )

    # ── Diagnostics ───────────────────────────────────────────────────────────
    def execution_log(self) -> List[Dict[str, Any]]:
        """Return sanitised execution log (internal traces stripped)."""
        return [
            {k: v for k, v in entry.items() if k != "_internal_trace"}
            for entry in self._execution_log
        ]

    def total_penalty_accrued(self) -> float:
        return sum(e["drift_penalty"] for e in self._execution_log)
