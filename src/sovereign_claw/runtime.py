"""
runtime.py — Sovereign Claw Runtime Entrypoint
=============================================
Public interface for executing governed AI workflows.
Supports governed execution and side-effect-free preview mode.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .orchestrator import Orchestrator
from .thermodynamics import TaskManifold


class SovereignRuntime:
    """
    High-level execution interface for Sovereign Claw.

    Supports:
      - full Orchestrator execution via execute(...)
      - side-effect-free preview via preview(...)
      - lightweight test orchestrators exposing run(...)
    """

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def run(
        self,
        objective: str,
        *,
        forbidden_actions: Optional[list[str]] = None,
        t_max_steps: int = 8,
        risk_threshold: float = 0.9,
    ) -> Dict[str, Any]:
        """
        Governed execution path.
        This is the normal runtime path and may perform real execution.
        """
        return self._dispatch(
            objective=objective,
            forbidden_actions=forbidden_actions,
            t_max_steps=t_max_steps,
            risk_threshold=risk_threshold,
            preview=False,
        )

    def preview(
        self,
        objective: str,
        *,
        forbidden_actions: Optional[list[str]] = None,
        t_max_steps: int = 8,
        risk_threshold: float = 0.9,
    ) -> Dict[str, Any]:
        """
        Side-effect-free preview path.

        This computes predicted governed outcome without committing execution.
        It preserves deterministic governance semantics and returns a JSON shape
        compatible with the current bridge and UI.
        """
        return self._dispatch(
            objective=objective,
            forbidden_actions=forbidden_actions,
            t_max_steps=t_max_steps,
            risk_threshold=risk_threshold,
            preview=True,
        )

    def _dispatch(
        self,
        *,
        objective: str,
        forbidden_actions: Optional[list[str]],
        t_max_steps: int,
        risk_threshold: float,
        preview: bool,
    ) -> Dict[str, Any]:
        # ── PATH 1: Full Orchestrator (real system) ─────────────────────────
        if hasattr(self.orchestrator, "execute"):
            manifold = TaskManifold(
                objective=objective,
                forbidden_actions=forbidden_actions or [],
                t_max_steps=t_max_steps,
                risk_threshold=risk_threshold,
            )

            receipt = self._execute_or_preview(manifold=manifold, preview=preview)
            return self._normalize_receipt(receipt=receipt, preview=preview)

        # ── PATH 2: Simple/Test Orchestrator (legacy support) ───────────────
        if hasattr(self.orchestrator, "run"):
            try:
                result = self.orchestrator.run(objective)
            except Exception as exc:
                return {
                    "status": "preview-error" if preview else "error",
                    "reason": f"Orchestrator failure: {type(exc).__name__}",
                    "preview": preview,
                }

            if not isinstance(result, dict):
                return {
                    "status": "preview-error" if preview else "error",
                    "reason": "Invalid orchestrator response",
                    "raw": result,
                    "preview": preview,
                }

            return self._normalize_legacy_result(result=result, preview=preview)

        # ── Unsupported orchestrator ────────────────────────────────────────
        return {
            "status": "preview-error" if preview else "error",
            "reason": "Unsupported orchestrator interface",
            "preview": preview,
        }

    def _execute_or_preview(self, *, manifold: TaskManifold, preview: bool) -> Any:
        """
        Execute through the richest supported orchestrator interface.

        Preference order:
          1. orchestrator.preview(manifold)
          2. orchestrator.execute(manifold, preview=True)
          3. orchestrator.execute(manifold)

        The last fallback preserves compatibility with existing systems that
        compute deterministically and may already be side-effect-gated. If your
        orchestrator is side-effectful by default, add native preview support
        there next.
        """
        if preview and hasattr(self.orchestrator, "preview"):
            return self.orchestrator.preview(manifold)

        if preview:
            try:
                return self.orchestrator.execute(manifold, preview=True)
            except TypeError:
                # Older orchestrators may not accept preview kwarg.
                pass

        return self.orchestrator.execute(manifold)

    def _normalize_receipt(self, *, receipt: Any, preview: bool) -> Dict[str, Any]:
        status = getattr(receipt, "status", None)
        trace_id = getattr(receipt, "trace_id", None)
        steps = getattr(receipt, "steps", None)
        final_drift = getattr(receipt, "final_drift", None)
        drift_trajectory = getattr(receipt, "drift_trajectory", None)
        halt_reason = getattr(receipt, "halt_reason", None)

        provider = getattr(receipt, "provider", "runtime-local")
        policy_status = getattr(receipt, "policy_status", "constraint-gated")

        base = {
            "trace_id": trace_id,
            "steps": steps,
            "final_drift": final_drift,
            "drift_trajectory": drift_trajectory,
            "provider": provider,
            "policy_status": policy_status,
            "preview": preview,
        }

        if preview:
            return {
                "status": "preview",
                "reason": halt_reason or status or "Preview computed",
                **base,
            }

        if status == "ISOMORPHIC_CLOSURE":
            return {
                "status": "executed",
                **base,
            }

        return {
            "status": "halted",
            "reason": halt_reason or status,
            **base,
        }

    def _normalize_legacy_result(
        self,
        *,
        result: Dict[str, Any],
        preview: bool,
    ) -> Dict[str, Any]:
        tool = result.get("tool")

        if preview:
            if tool == "HALT":
                return {
                    "status": "preview",
                    "reason": result.get("comment", ""),
                    "trace_id": result.get("trace_id"),
                    "steps": result.get("steps"),
                    "final_drift": result.get("final_drift"),
                    "drift_trajectory": result.get("drift_trajectory", []),
                    "provider": result.get("provider", "runtime-local"),
                    "policy_status": result.get("policy_status", "constraint-gated"),
                    "preview": True,
                    "agent": result.get("agent_id"),
                }

            return {
                "status": "preview",
                "reason": result.get("comment", "Preview computed"),
                "trace_id": result.get("trace_id"),
                "steps": result.get("steps"),
                "final_drift": result.get("final_drift"),
                "drift_trajectory": result.get("drift_trajectory", []),
                "provider": result.get("provider", "runtime-local"),
                "policy_status": result.get("policy_status", "constraint-gated"),
                "preview": True,
                "action": result,
            }

        if tool == "HALT":
            return {
                "status": "halted",
                "reason": result.get("comment", ""),
                "agent": result.get("agent_id"),
                "trace_id": result.get("trace_id"),
                "steps": result.get("steps"),
                "final_drift": result.get("final_drift"),
                "drift_trajectory": result.get("drift_trajectory", []),
                "provider": result.get("provider", "runtime-local"),
                "policy_status": result.get("policy_status", "constraint-gated"),
                "preview": False,
            }

        return {
            "status": "executed",
            "action": result,
            "trace_id": result.get("trace_id"),
            "steps": result.get("steps"),
            "final_drift": result.get("final_drift"),
            "drift_trajectory": result.get("drift_trajectory", []),
            "provider": result.get("provider", "runtime-local"),
            "policy_status": result.get("policy_status", "constraint-gated"),
            "preview": False,
        }


__all__ = ["SovereignRuntime", "Orchestrator"]
