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

PREVIEW_TEXT_LIMIT = 512
PREVIEW_KEY_LIMIT = 64
PREVIEW_TOOL_LIMIT = 128
PREVIEW_COLLECTION_LIMIT = 32
PREVIEW_DEPTH_LIMIT = 4


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

    def _truncate_preview_text(self, value: str, limit: int = PREVIEW_TEXT_LIMIT) -> str:
        return value[:limit]

    def _sanitize_preview_value(self, value: Any, depth: int = 0) -> Any:
        if depth > PREVIEW_DEPTH_LIMIT:
            raise ValueError("preview payload exceeds maximum nesting depth")

        if value is None or isinstance(value, (bool, int, float)):
            return value

        if isinstance(value, str):
            return self._truncate_preview_text(value)

        if isinstance(value, (list, tuple)):
            if len(value) > PREVIEW_COLLECTION_LIMIT:
                raise ValueError("preview payload list exceeds maximum length")
            return [self._sanitize_preview_value(item, depth + 1) for item in value]

        if isinstance(value, dict):
            if len(value) > PREVIEW_COLLECTION_LIMIT:
                raise ValueError("preview payload mapping exceeds maximum size")
            sanitized: Dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("preview payload keys must be non-empty strings")
                sanitized[self._truncate_preview_text(key.strip(), PREVIEW_KEY_LIMIT)] = (
                    self._sanitize_preview_value(item, depth + 1)
                )
            return sanitized

        raise ValueError(f"preview payload type '{type(value).__name__}' is not supported")

    def _sanitize_preview_candidate(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(decision, dict):
            raise ValueError("preview proposal must be a mapping")

        tool_name = decision.get("tool")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("preview proposal must include a non-empty string tool")

        raw_kwargs = decision.get("kwargs", {})
        if raw_kwargs is None:
            raw_kwargs = {}
        if not isinstance(raw_kwargs, dict):
            raise ValueError("preview proposal kwargs must be a mapping")

        comment = decision.get("comment", "")
        if comment is None:
            comment = ""
        if not isinstance(comment, str):
            raise ValueError("preview proposal comment must be a string")

        provider = decision.get("provider", "runtime-local")
        if provider is None:
            provider = "runtime-local"
        if not isinstance(provider, str):
            raise ValueError("preview proposal provider must be a string")

        agent_id = decision.get("agent_id", "llm_backend")
        if agent_id is None:
            agent_id = "llm_backend"
        if not isinstance(agent_id, str):
            raise ValueError("preview proposal agent_id must be a string")

        provider_metadata = decision.get("provider_metadata", {})
        if provider_metadata is None:
            provider_metadata = {}
        if not isinstance(provider_metadata, dict):
            raise ValueError("preview proposal provider_metadata must be a mapping")

        return {
            "tool": self._truncate_preview_text(tool_name.strip(), PREVIEW_TOOL_LIMIT),
            "kwargs": self._sanitize_preview_value(raw_kwargs),
            "comment": self._truncate_preview_text(comment),
            "provider": self._truncate_preview_text(provider.strip() or "runtime-local", PREVIEW_TOOL_LIMIT),
            "agent_id": self._truncate_preview_text(agent_id.strip() or "llm_backend", PREVIEW_TOOL_LIMIT),
            "provider_metadata": self._sanitize_preview_value(provider_metadata),
        }

    def _preview_payload(
        self,
        *,
        status: str,
        supported: bool,
        proposal: Optional[Dict[str, Any]],
        policy_allowed: bool,
        policy_reasons: List[str],
        matched_rule_ids: List[str],
        policy_profile: str,
        predicted_drift: float,
        manifold: TaskManifold,
        expected_halt_reason: Optional[str],
        step_estimate: int,
    ) -> Dict[str, Any]:
        action = None
        diff_equivalent_proposal = None
        provider = "runtime-local"
        agent_id = "llm_backend"
        provider_metadata: Dict[str, Any] = {}

        if proposal is not None:
            action = {
                "tool": proposal["tool"],
                "kwargs": proposal["kwargs"],
                "comment": proposal["comment"],
            }
            diff_equivalent_proposal = {
                "tool": proposal["tool"],
                "kwargs": proposal["kwargs"],
            }
            provider = proposal["provider"]
            agent_id = proposal["agent_id"]
            provider_metadata = proposal["provider_metadata"]

        return {
            "status": status,
            "supported": supported,
            "preview": True,
            "trace_id": None,
            "action": action,
            "proposed_action": action,
            "diff_equivalent_proposal": diff_equivalent_proposal,
            "policy_status": "preview-supported" if supported else status,
            "policy_decision": {
                "allowed": policy_allowed,
                "reasons": list(policy_reasons),
                "matched_rule_ids": list(matched_rule_ids),
                "profile": policy_profile,
            },
            "matched_rule_ids": list(matched_rule_ids),
            "predicted_drift": predicted_drift,
            "projected_drift": predicted_drift,
            "projected_risk": predicted_drift,
            "risk_threshold": manifold.risk_threshold,
            "expected_halt_reason": expected_halt_reason,
            "step_estimate": step_estimate,
            "steps": step_estimate,
            "tool_calls": 0,
            "drift_trajectory": [predicted_drift] if step_estimate else [],
            "provider": provider,
            "agent_id": agent_id,
            "provider_metadata": provider_metadata,
            "source_status": status,
            "note": "Preview generated without tool execution.",
            "detail": expected_halt_reason,
        }

    def preview(self, manifold: TaskManifold) -> Dict[str, Any]:
        therm = SystemThermodynamics(manifold)

        try:
            raw_decision = self.llm.decide_next_action(
                objective=manifold.objective,
                history=[],
                forbidden_actions=manifold.forbidden_actions,
                drift=therm.current_drift,
            )
            proposal = self._sanitize_preview_candidate(raw_decision)
        except Exception as exc:
            return self._preview_payload(
                status="preview-malformed",
                supported=False,
                proposal=None,
                policy_allowed=False,
                policy_reasons=[f"Malformed model output: {type(exc).__name__}"],
                matched_rule_ids=[],
                policy_profile=getattr(self.policy_engine.profile, "value", "balanced"),
                predicted_drift=therm.current_drift,
                manifold=manifold,
                expected_halt_reason=str(exc),
                step_estimate=0,
            )

        tool_name = proposal["tool"]

        if tool_name == "HALT":
            reason = proposal["comment"] or "LLM issued HALT"
            return self._preview_payload(
                status="preview-halt",
                supported=False,
                proposal=proposal,
                policy_allowed=True,
                policy_reasons=[],
                matched_rule_ids=[],
                policy_profile=getattr(self.policy_engine.profile, "value", "balanced"),
                predicted_drift=therm.current_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=0,
            )

        if tool_name in manifold.forbidden_actions:
            reason = f"Forbidden action blocked: {tool_name}"
            return self._preview_payload(
                status="preview-forbidden",
                supported=False,
                proposal=proposal,
                policy_allowed=False,
                policy_reasons=[reason],
                matched_rule_ids=["manifold.forbidden_actions"],
                policy_profile=getattr(self.policy_engine.profile, "value", "balanced"),
                predicted_drift=therm.current_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=0,
            )

        if self.tools.get(tool_name) is None:
            reason = f"Unknown tool: {tool_name}"
            return self._preview_payload(
                status="preview-unknown-tool",
                supported=False,
                proposal=proposal,
                policy_allowed=False,
                policy_reasons=[reason],
                matched_rule_ids=["orchestrator.unknown_tool"],
                policy_profile=getattr(self.policy_engine.profile, "value", "balanced"),
                predicted_drift=therm.current_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=0,
            )

        policy_request = {
            "tool": tool_name,
            "kwargs": proposal["kwargs"],
            "comment": proposal["comment"],
            "agent_id": proposal["agent_id"],
        }
        previous_drift = self.policy_engine.current_drift
        try:
            self.policy_engine.update_drift(therm.current_drift)
            policy = self.policy_engine.test_policy(policy_request)
        except Exception as exc:
            reason = f"Policy engine failure: {type(exc).__name__}"
            return self._preview_payload(
                status="preview-policy-denied",
                supported=False,
                proposal=proposal,
                policy_allowed=False,
                policy_reasons=[reason],
                matched_rule_ids=["policy.runtime_error"],
                policy_profile=getattr(self.policy_engine.profile, "value", "balanced"),
                predicted_drift=therm.current_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=0,
            )
        finally:
            self.policy_engine.update_drift(previous_drift)

        matched_rule_ids = sorted(set(policy.matched_policies))
        if not policy.allowed:
            reason = "; ".join(policy.reasons) or "Policy denied proposed action"
            return self._preview_payload(
                status="preview-policy-denied",
                supported=False,
                proposal=proposal,
                policy_allowed=False,
                policy_reasons=list(policy.reasons),
                matched_rule_ids=matched_rule_ids,
                policy_profile=policy.profile,
                predicted_drift=therm.current_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=0,
            )

        projected_drift = therm.apply_drift_update(step_count=0, error_penalty=0.0)
        if projected_drift > manifold.risk_threshold:
            reason = (
                f"Soft Silence Clause: drift {projected_drift:.4f} "
                f"> risk_threshold {manifold.risk_threshold}"
            )
            return self._preview_payload(
                status="preview-risk-threshold",
                supported=True,
                proposal=proposal,
                policy_allowed=True,
                policy_reasons=[],
                matched_rule_ids=matched_rule_ids,
                policy_profile=policy.profile,
                predicted_drift=projected_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=1,
            )

        return self._preview_payload(
            status="preview",
            supported=True,
            proposal=proposal,
            policy_allowed=True,
            policy_reasons=[],
            matched_rule_ids=matched_rule_ids,
            policy_profile=policy.profile,
            predicted_drift=projected_drift,
            manifold=manifold,
            expected_halt_reason=None,
            step_estimate=1,
        )

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
