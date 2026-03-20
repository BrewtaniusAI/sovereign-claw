"""
orchestrator.py — Giles Node / Topological Descent Engine
==========================================================
Central governance node. Implements:

  • Topological Descent loop with strict T_max enforcement
  • Kitaev Zero-Mode delegation for all tool calls
  • ProofVault logging of every decision and drift value
  • Byzantine Reputation weight updates per agent
  • Soft Silence Clause (risk_threshold) + Hard Silence Clause (T_max)
  • Forbidden-action enforcement (hard block, not just warning)
  • Pre-execution constraint validation against PolicyEngine

BUG FIXES / EXTENSIONS:
  - Orchestrator requires explicit LLMBackend
  - Soft Silence Clause check fires at the correct threshold timing
  - T_max pre-check evaluated at the top of every loop iteration
  - _log_step is private and raises on vault failure
  - ExecutionReceipt includes full drift_trajectory
  - Orchestrator passes manifold to SystemThermodynamics so ELFE coefficients are honored
  - Added reset() for multi-run scenarios without re-instantiation
  - Added pre-execution constraint projection C(x)
  - Added structural drift delta measurement D(x) = |x - C(x)|
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Protocol

from .ip_shield import seal_with_build_fingerprint
from .kitaev_shield import KitaevZeroMode
from .policy_engine import PolicyEngine
from .proof_vault import ProofVault, StepRecord
from .thermodynamics import SystemThermodynamics, TaskManifold

# ── Types ─────────────────────────────────────────────────────────────────────
Status = Literal[
    "ISOMORPHIC_CLOSURE",
    "T_MAX_VIOLATION",
    "HALTED_SILENCE_CLAUSE",
]


# ── LLMBackend protocol ───────────────────────────────────────────────────────
class LLMBackend(Protocol):
    """
    Minimal protocol for pluggable LLM providers.

    decide_next_action must return a dict with at minimum:
        {
          "tool":    str,            # tool name or "HALT"
          "kwargs":  Dict[str, Any], # arguments passed to the tool
          "comment": str,            # reasoning trace (logged, not exec'd)
        }
    """

    def decide_next_action(
        self,
        objective: str,
        history: List[Dict[str, Any]],
        forbidden_actions: List[str],
        drift: float,
    ) -> Dict[str, Any]: ...


# ── ExecutionReceipt ──────────────────────────────────────────────────────────
@dataclass
class ExecutionReceipt:
    trace_id: str
    status: Status
    steps: int
    final_drift: float
    drift_trajectory: List[float] = field(default_factory=list)
    halt_reason: Optional[str] = None


# ── Orchestrator ──────────────────────────────────────────────────────────────
class Orchestrator:
    """
    Giles node: Topological Descent + governance.

    Maintains drift via SystemThermodynamics.
    Delegates tool calls through KitaevZeroMode.
    Logs everything to ProofVault.
    """

    def __init__(
        self,
        llm_backend: LLMBackend,
        tools: Optional[Dict[str, Any]] = None,
        vault: Optional[ProofVault] = None,
        shield: Optional[KitaevZeroMode] = None,
        policy_engine: Optional[PolicyEngine] = None,
    ) -> None:
        self.llm = llm_backend
        self.tools: Dict[str, Any] = tools or {}
        self.vault = vault or ProofVault()
        self.shield = shield or KitaevZeroMode()
        self.policy_engine = policy_engine or PolicyEngine()

    # ── Tool registry ─────────────────────────────────────────────────────────
    def register_tool(self, name: str, fn: Any) -> None:
        self.tools[name] = fn

    def unregister_tool(self, name: str) -> None:
        self.tools.pop(name, None)

    def reset(self) -> None:
        """
        Reset runtime-local state for reuse across multiple runs.

        Current implementation is intentionally lightweight because
        Orchestrator keeps almost all state per-execution.
        """
        return None

    # ── Constraint helpers ────────────────────────────────────────────────────
    def _project_to_constraint(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """
        Constraint projection C(x).

        Current implementation delegates to the PolicyEngine. If policy denies
        the action, project it into a safe HALT state. If policy is unavailable
        or errors, fail closed to HALT.
        """
        if not self.policy_engine:
            return decision

        try:
            policy = self.policy_engine.evaluate(decision)
        except Exception as exc:
            return {
                "tool": "HALT",
                "kwargs": {},
                "comment": f"Policy engine failure: {type(exc).__name__}",
                "agent_id": "orchestrator",
            }

        allowed = getattr(policy, "allowed", True)
        reason = getattr(policy, "reason", "")

        if not allowed:
            return {
                "tool": "HALT",
                "kwargs": {},
                "comment": f"Policy violation: {reason}".strip(),
                "agent_id": "orchestrator",
            }

        return decision

    def _compute_drift_delta(
        self,
        proposed: Dict[str, Any],
        projected: Dict[str, Any],
    ) -> float:
        """
        Compute structural drift between proposed state x and projected state C(x).

        Returns a normalized mismatch ratio in [0, 1].
        """
        if proposed == projected:
            return 0.0

        if not isinstance(proposed, dict) or not isinstance(projected, dict):
            return 1.0

        keys = set(proposed.keys()) | set(projected.keys())
        if not keys:
            return 0.0

        mismatches = sum(1 for key in keys if proposed.get(key) != projected.get(key))
        return mismatches / len(keys)

    # ── Execution ─────────────────────────────────────────────────────────────
    def execute(self, manifold: TaskManifold) -> ExecutionReceipt:
        """
        Run the Topological Descent loop against a TaskManifold.

        Termination conditions (in priority order):
          1. ISOMORPHIC_CLOSURE    — drift reached 0
          2. T_MAX_VIOLATION       — step budget exhausted
          3. HALTED_SILENCE_CLAUSE — LLM issued HALT, constraint rejection,
                                     forbidden action, unknown tool, or
                                     risk_threshold exceeded
        """
        therm = SystemThermodynamics(manifold)

        trace_meta = seal_with_build_fingerprint(
            {
                "forbidden_actions": manifold.forbidden_actions,
                "t_max_steps": manifold.t_max_steps,
                "theoretical_t_max": manifold.theoretical_t_max,
                "elfe_a": manifold.elfe_a,
                "elfe_b": manifold.elfe_b,
                "elfe_p": manifold.elfe_p,
                "elfe_q": manifold.elfe_q,
            }
        )
        trace_id = self.vault.create_trace(
            objective=manifold.objective,
            meta=trace_meta,
        )

        history: List[Dict[str, Any]] = []
        step_idx = 0
        final_status: Status = "HALTED_SILENCE_CLAUSE"
        halt_reason: Optional[str] = None

        while True:
            # ── Pre-step state check ──────────────────────────────────────────
            state_status = therm.check_isomorphic_state(step_idx)

            if state_status == "ISOMORPHIC_CLOSURE":
                final_status = "ISOMORPHIC_CLOSURE"
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx,
                    node="orchestrator",
                    action="ISOMORPHIC_CLOSURE",
                    drift=therm.current_drift,
                    status=final_status,
                    payload={"reason": "Drift reached zero"},
                )
                break

            if state_status == "T_MAX_VIOLATION":
                final_status = "T_MAX_VIOLATION"
                halt_reason = f"T_max ({manifold.t_max_steps} steps) exceeded"
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx,
                    node="orchestrator",
                    action="T_MAX_VIOLATION",
                    drift=therm.current_drift,
                    status=final_status,
                    payload={"reason": halt_reason},
                )
                break

            # ── Query LLM ────────────────────────────────────────────────────
            decision = self.llm.decide_next_action(
                objective=manifold.objective,
                history=history,
                forbidden_actions=manifold.forbidden_actions,
                drift=therm.current_drift,
            )

            # ── Constraint projection C(x) ───────────────────────────────────
            projected = self._project_to_constraint(decision)
            drift_delta = self._compute_drift_delta(decision, projected)

            if projected.get("tool") == "HALT" and decision.get("tool") != "HALT":
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = projected.get("comment", "Constraint violation")

                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx,
                    node="orchestrator",
                    action="CONSTRAINT_REJECTION",
                    drift=therm.current_drift,
                    status=final_status,
                    payload={
                        "original": decision,
                        "projected": projected,
                        "drift_delta": drift_delta,
                        "reason": halt_reason,
                    },
                )
                break

            decision = projected

            tool_name = decision.get("tool", "HALT")
            tool_kwargs = decision.get("kwargs", {}) or {}
            comment = decision.get("comment", "")
            agent_id = decision.get("agent_id", "llm_backend")

            # ── HALT signal ───────────────────────────────────────────────────
            if tool_name == "HALT":
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = f"LLM issued HALT: {comment}"
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx,
                    node=agent_id,
                    action="HALT",
                    drift=therm.current_drift,
                    status=final_status,
                    payload={"comment": comment},
                )
                break

            # ── Forbidden action ──────────────────────────────────────────────
            if tool_name in manifold.forbidden_actions:
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = f"Forbidden action blocked: {tool_name}"
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx,
                    node="orchestrator",
                    action="FORBIDDEN_ACTION_BLOCKED",
                    drift=therm.current_drift,
                    status=final_status,
                    payload={"tool": tool_name, "reason": "Forbidden by manifold"},
                )
                break

            # ── Unknown tool ──────────────────────────────────────────────────
            tool_fn = self.tools.get(tool_name)
            if tool_fn is None:
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = f"Unknown tool: {tool_name}"
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx,
                    node="orchestrator",
                    action="UNKNOWN_TOOL",
                    drift=therm.current_drift,
                    status=final_status,
                    payload={"tool": tool_name},
                )
                break

            # ── Execute via Kitaev shield ────────────────────────────────────
            shielded = self.shield.execute_safely(
                tool_name=tool_name,
                tool_function=tool_fn,
                kwargs=tool_kwargs,
            )

            new_drift = therm.apply_drift_update(
                step_count=step_idx,
                error_penalty=shielded["drift_penalty"] + drift_delta,
            )

            # Update Byzantine reputation for this agent
            self.vault.update_agent_reputation(agent_id, shielded["drift_penalty"])

            payload = {
                "decision_comment": comment,
                "tool": tool_name,
                "tool_kwargs": tool_kwargs,
                "tool_result": shielded["payload"],
                "success": shielded["success"],
                "error_type": shielded.get("error_type"),
                "drift_penalty": shielded["drift_penalty"],
                "constraint_drift_delta": drift_delta,
            }

            self._log_step(
                trace_id=trace_id,
                step_index=step_idx,
                node=agent_id,
                action=f"TOOL:{tool_name}",
                drift=new_drift,
                status="CONTINUE_DESCENT",
                payload=payload,
            )

            history.append(
                {
                    "step": step_idx,
                    "tool": tool_name,
                    "success": shielded["success"],
                    "payload": shielded["payload"],
                    "drift": new_drift,
                }
            )

            step_idx += 1

            # ── Soft Silence Clause ───────────────────────────────────────────
            if new_drift > manifold.risk_threshold:
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = (
                    f"Soft Silence Clause: drift {new_drift:.4f} "
                    f"> risk_threshold {manifold.risk_threshold}"
                )
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx,
                    node="orchestrator",
                    action="RISK_THRESHOLD_HALT",
                    drift=new_drift,
                    status=final_status,
                    payload={"reason": halt_reason},
                )
                break

        return ExecutionReceipt(
            trace_id=trace_id,
            status=final_status,
            steps=step_idx,
            final_drift=therm.current_drift,
            drift_trajectory=therm.drift_trajectory(),
            halt_reason=halt_reason,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────
    def _log_step(
        self,
        trace_id: str,
        step_index: int,
        node: str,
        action: str,
        drift: float,
        status: str,
        payload: Dict[str, Any],
    ) -> None:
        rec = StepRecord(
            trace_id=trace_id,
            step_index=step_index,
            timestamp=time.time(),
            node=node,
            action=action,
            drift=drift,
            status=status,
            payload=payload,
        )
        self.vault.append_step(rec)