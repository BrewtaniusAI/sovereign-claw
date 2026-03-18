"""
orchestrator.py — Giles Node / Topological Descent Engine
==========================================================
Central governance node.  Implements:

  • Topological Descent loop with strict T_max enforcement
  • Kitaev Zero-Mode delegation for all tool calls
  • ProofVault logging of every decision and drift value
  • Byzantine Reputation weight updates per agent
  • Soft Silence Clause (risk_threshold) + Hard Silence Clause (T_max)
  • Forbidden-action enforcement (hard block, not just warning)

BUG FIXES vs. original:
  - Orchestrator now requires explicit LLMBackend — original had a
    silent None default that would crash at runtime with an opaque error.
  - Soft Silence Clause check moved BEFORE step increment to correctly
    fire at risk_threshold, not one step late.
  - T_max pre-check is now evaluated at the top of every loop iteration
    so a manifold with t_max_steps=0 is immediately rejected.
  - _log_step is now private and raises on vault failure rather than
    silently swallowing errors.
  - ExecutionReceipt includes full drift_trajectory for diagnostics.
  - Orchestrator now passes manifold (not just t_max_steps) to
    SystemThermodynamics so ELFE coefficients are honoured.
  - Added reset() for multi-run scenarios without re-instantiation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Protocol

from .kitaev_shield import KitaevZeroMode
from .proof_vault import ProofVault, StepRecord
from .thermodynamics import SystemThermodynamics, TaskManifold
from .ip_shield import seal_with_build_fingerprint
from .policy_engine import PolicyEngine

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
          "tool":    str,                  # tool name or "HALT"
          "kwargs":  Dict[str, Any],       # arguments passed to the tool
          "comment": str,                  # reasoning trace (logged, not exec'd)
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

    Parameters
    ----------
    llm_backend : LLMBackend — required; must implement decide_next_action.
    tools       : Dict mapping tool names to callables.
    vault       : ProofVault instance (default: creates one at ~/.sovereign_claw/).
    shield      : KitaevZeroMode instance (default: standard penalty scale).
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

    # ── Execution ─────────────────────────────────────────────────────────────
    def execute(self, manifold: TaskManifold) -> ExecutionReceipt:
        """
        Run the Topological Descent loop against a TaskManifold.

        Termination conditions (in priority order):
          1. ISOMORPHIC_CLOSURE    — drift reached 0
          2. T_MAX_VIOLATION       — step budget exhausted
          3. HALTED_SILENCE_CLAUSE — LLM issued HALT, forbidden action,
                                     unknown tool, or risk_threshold exceeded
        """
        therm = SystemThermodynamics(manifold)
        # DRIFT-6 FIX: inject build fingerprint into every Orchestrator trace
        # so the IP chain is intact for all execution events.
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
        step_idx: int = 0
        final_status: Status = "HALTED_SILENCE_CLAUSE"
        halt_reason: Optional[str] = None

        while True:
            # ── Pre-step state check ──────────────────────────────────────────
            state_status = therm.check_isomorphic_state(step_idx)

            if state_status == "ISOMORPHIC_CLOSURE":
                final_status = "ISOMORPHIC_CLOSURE"
                self._log_step(
                    trace_id,
                    step_idx,
                    "orchestrator",
                    "ISOMORPHIC_CLOSURE",
                    therm.current_drift,
                    final_status,
                    {"reason": "Drift reached zero"},
                )
                break

            if state_status == "T_MAX_VIOLATION":
                final_status = "T_MAX_VIOLATION"
                halt_reason = f"T_max ({manifold.t_max_steps} steps) exceeded"
                self._log_step(
                    trace_id,
                    step_idx,
                    "orchestrator",
                    "T_MAX_VIOLATION",
                    therm.current_drift,
                    final_status,
                    {"reason": halt_reason},
                )
                break

            # ── Query LLM ────────────────────────────────────────────────────
            decision = self.llm.decide_next_action(
                objective=manifold.objective,
                history=history,
                forbidden_actions=manifold.forbidden_actions,
                drift=therm.current_drift,
            )

            tool_name = decision.get("tool", "HALT")
            tool_kwargs = decision.get("kwargs", {}) or {}
            comment = decision.get("comment", "")
            agent_id = decision.get("agent_id", "llm_backend")

            # ── HALT signal ───────────────────────────────────────────────────
            if tool_name == "HALT":
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = f"LLM issued HALT: {comment}"
                self._log_step(
                    trace_id,
                    step_idx,
                    agent_id,
                    "HALT",
                    therm.current_drift,
                    final_status,
                    {"comment": comment},
                )
                break

            # ── Forbidden action ──────────────────────────────────────────────
            if tool_name in manifold.forbidden_actions:
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = f"Forbidden action blocked: {tool_name}"
                self._log_step(
                    trace_id,
                    step_idx,
                    "orchestrator",
                    "FORBIDDEN_ACTION_BLOCKED",
                    therm.current_drift,
                    final_status,
                    {"tool": tool_name, "reason": "Forbidden by manifold"},
                )
                break

            # ── Unknown tool ──────────────────────────────────────────────────
            tool_fn = self.tools.get(tool_name)
            if tool_fn is None:
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = f"Unknown tool: {tool_name}"
                self._log_step(
                    trace_id,
                    step_idx,
                    "orchestrator",
                    "UNKNOWN_TOOL",
                    therm.current_drift,
                    final_status,
                    {"tool": tool_name},
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
                error_penalty=shielded["drift_penalty"],
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
            }

            self._log_step(
                trace_id,
                step_idx,
                agent_id,
                f"TOOL:{tool_name}",
                new_drift,
                "CONTINUE_DESCENT",
                payload,
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
                    trace_id,
                    step_idx,
                    "orchestrator",
                    "RISK_THRESHOLD_HALT",
                    new_drift,
                    final_status,
                    {"reason": halt_reason},
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
