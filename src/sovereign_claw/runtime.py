"""
runtime.py — Sovereign Claw Runtime Entrypoint
=============================================
Public interface for executing governed AI workflows.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .orchestrator import Orchestrator
from .thermodynamics import TaskManifold


class SovereignRuntime:
    """
    High-level execution interface for Sovereign Claw.
    Supports both:
      - full Orchestrator (execute)
      - lightweight test orchestrators (run)
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

        # ── PATH 1: Full Orchestrator (real system) ─────────────────────────
        if hasattr(self.orchestrator, "execute"):
            manifold = TaskManifold(
                objective=objective,
                forbidden_actions=forbidden_actions or [],
                t_max_steps=t_max_steps,
                risk_threshold=risk_threshold,
            )

            receipt = self.orchestrator.execute(manifold)

            if receipt.status == "ISOMORPHIC_CLOSURE":
                return {
                    "status": "executed",
                    "trace_id": receipt.trace_id,
                    "steps": receipt.steps,
                    "final_drift": receipt.final_drift,
                    "drift_trajectory": receipt.drift_trajectory,
                }

            return {
                "status": "halted",
                "trace_id": receipt.trace_id,
                "reason": receipt.halt_reason or receipt.status,
                "steps": receipt.steps,
                "final_drift": receipt.final_drift,
                "drift_trajectory": receipt.drift_trajectory,
            }

        # ── PATH 2: Simple/Test Orchestrator (legacy support) ───────────────
        if hasattr(self.orchestrator, "run"):
            try:
                result = self.orchestrator.run(objective)
            except Exception as exc:
                return {
                    "status": "error",
                    "reason": f"Orchestrator failure: {type(exc).__name__}",
                }

            if not isinstance(result, dict):
                return {
                    "status": "error",
                    "reason": "Invalid orchestrator response",
                    "raw": result,
                }

            tool = result.get("tool")

            if tool == "HALT":
                return {
                    "status": "halted",
                    "reason": result.get("comment", ""),
                    "agent": result.get("agent_id"),
                }

            return {
                "status": "executed",
                "action": result,
            }

        # ── Unsupported orchestrator ────────────────────────────────────────
        return {
            "status": "error",
            "reason": "Unsupported orchestrator interface",
        }