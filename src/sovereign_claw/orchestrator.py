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

import copy
import hashlib
import hmac
import inspect
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Protocol

from .ip_shield import seal_with_build_fingerprint
from .kitaev_shield import KitaevZeroMode
from .policy_engine import PolicyEngine
from .proof_vault import ProofVault, StepRecord
from .thermodynamics import SystemThermodynamics, TaskManifold
from .tool_authority import (
    ApprovedActionMismatchError,
    InputSchemaInvalidError,
    OutputSchemaInvalidError,
    ToolAuthorityError,
    ToolContractChangedError,
    ToolRegistry,
    canonicalize_args,
    compute_action_digest,
    validate_output,
)

# ── Types ─────────────────────────────────────────────────────────────────────
Status = Literal[
    "ISOMORPHIC_CLOSURE",
    "T_MAX_VIOLATION",
    "HALTED_SILENCE_CLAUSE",
]

ACTION_DIGEST_VERSION = "sovereign.action.v1"
PREVIEW_COMMENT_LIMIT = 512
PREVIEW_DISPLAY_TEXT_LIMIT = 512
PREVIEW_AUTHORITY_TEXT_LIMIT = 1024
PREVIEW_KEY_LIMIT = 64
PREVIEW_TOOL_LIMIT = 128
PREVIEW_COLLECTION_LIMIT = 32
PREVIEW_DEPTH_LIMIT = 4
ACTION_DIGEST_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


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
    required_action: Optional[str] = None
    policy_profile: Optional[str] = None
    provider: Optional[str] = None


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
        tool_registry: Optional[ToolRegistry] = None,
    ) -> None:
        self.llm = llm_backend
        self.tools: Dict[str, Any] = tools or {}
        self.vault = vault or ProofVault()
        self.shield = shield or KitaevZeroMode()
        self.policy_engine = policy_engine or PolicyEngine()
        self.tool_registry = tool_registry

    @property
    def _governed(self) -> bool:
        """True when a ToolRegistry is configured (governed mode)."""
        return self.tool_registry is not None

    def _governed_policy_bundle_hash(self) -> str:
        """Stable hash of the policy bundle identity in effect."""
        profile = getattr(self.policy_engine.profile, "value", "balanced")
        material = json.dumps(
            {"profile": profile}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _governed_config_identity_hash(self, registry_snapshot_hash: str) -> str:
        """Stable hash binding Orchestrator config + registry state."""
        material = json.dumps(
            {
                "policy_profile": getattr(self.policy_engine.profile, "value", "balanced"),
                "registry_snapshot_hash": registry_snapshot_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

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
    def _project_to_constraint(
        self,
        decision: Dict[str, Any],
        *,
        drift: float,
        trace_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Constraint projection C(x).

        Current implementation delegates to the PolicyEngine. If policy denies
        the action, project it into a safe HALT state. If policy is unavailable
        or errors, fail closed to HALT.
        """
        if not self.policy_engine:
            return decision

        try:
            policy_request = self._build_policy_request(
                decision=decision,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            self.policy_engine.update_drift(drift)
            policy = self.policy_engine.evaluate(policy_request)
        except Exception as exc:
            return {
                "tool": "HALT",
                "kwargs": {},
                "comment": f"Policy engine failure: {type(exc).__name__}",
                "agent_id": "orchestrator",
            }

        allowed = getattr(policy, "allowed", True)
        reason = "; ".join(getattr(policy, "reasons", [])) or getattr(policy, "reason", "")

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

    def _truncate_preview_text(self, value: str, limit: int = PREVIEW_DISPLAY_TEXT_LIMIT) -> str:
        return value[:limit]

    def _normalize_preview_key(self, key: str) -> str:
        normalized = key.strip()
        if not normalized:
            raise ValueError("preview payload keys must be non-empty strings")
        if len(normalized) > PREVIEW_KEY_LIMIT:
            raise ValueError("preview payload key exceeds maximum length")
        return normalized

    def _sanitize_preview_value(self, value: Any, depth: int = 0) -> Any:
        if depth > PREVIEW_DEPTH_LIMIT:
            raise ValueError("preview payload exceeds maximum nesting depth")

        if value is None or isinstance(value, (bool, int)):
            return value

        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("preview payload numbers must be finite JSON values")
            return value

        if isinstance(value, str):
            if len(value) > PREVIEW_AUTHORITY_TEXT_LIMIT:
                raise ValueError("preview payload string exceeds maximum length")
            return value

        if isinstance(value, (list, tuple)):
            if len(value) > PREVIEW_COLLECTION_LIMIT:
                raise ValueError("preview payload list exceeds maximum length")
            return [self._sanitize_preview_value(item, depth + 1) for item in value]

        if isinstance(value, dict):
            if len(value) > PREVIEW_COLLECTION_LIMIT:
                raise ValueError("preview payload mapping exceeds maximum size")
            sanitized: Dict[str, Any] = {}
            normalized_keys: set[str] = set()
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("preview payload keys must be non-empty strings")
                normalized_key = self._normalize_preview_key(key)
                if normalized_key in normalized_keys:
                    raise ValueError("preview payload contains duplicate keys after normalization")
                normalized_keys.add(normalized_key)
                sanitized[normalized_key] = self._sanitize_preview_value(item, depth + 1)
            return sanitized

        raise ValueError(f"preview payload type '{type(value).__name__}' is not supported")

    def _sanitize_display_value(self, value: Any, depth: int = 0) -> Any:
        if depth > PREVIEW_DEPTH_LIMIT:
            raise ValueError("preview display payload exceeds maximum nesting depth")

        if value is None or isinstance(value, (bool, int)):
            return value

        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("preview display payload numbers must be finite JSON values")
            return value

        if isinstance(value, str):
            return self._truncate_preview_text(value, PREVIEW_DISPLAY_TEXT_LIMIT)

        if isinstance(value, (list, tuple)):
            if len(value) > PREVIEW_COLLECTION_LIMIT:
                raise ValueError("preview display payload list exceeds maximum length")
            return [self._sanitize_display_value(item, depth + 1) for item in value]

        if isinstance(value, dict):
            if len(value) > PREVIEW_COLLECTION_LIMIT:
                raise ValueError("preview display payload mapping exceeds maximum size")
            sanitized: Dict[str, Any] = {}
            normalized_keys: set[str] = set()
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("preview display payload keys must be non-empty strings")
                normalized_key = self._normalize_preview_key(key)
                if normalized_key in normalized_keys:
                    raise ValueError(
                        "preview display payload contains duplicate keys after normalization"
                    )
                normalized_keys.add(normalized_key)
                sanitized[normalized_key] = self._sanitize_display_value(item, depth + 1)
            return sanitized

        raise ValueError(f"preview display payload type '{type(value).__name__}' is not supported")

    def _validate_tool_kwargs(self, tool_name: str, tool_fn: Any, kwargs: Dict[str, Any]) -> str:
        try:
            signature = inspect.signature(tool_fn)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"preview kwargs cannot be validated for '{tool_name}' because the tool schema is opaque"
            ) from exc

        try:
            signature.bind(**kwargs)
        except TypeError as exc:
            raise ValueError(
                f"preview kwargs do not match tool schema for '{tool_name}': {exc}"
            ) from exc

        return str(signature)

    def _canonicalize_action(
        self,
        *,
        tool_name: str,
        kwargs: Dict[str, Any],
        policy_profile: str,
        tool_fn: Any,
    ) -> Dict[str, Any]:
        normalized_tool = tool_name.strip()
        if not normalized_tool:
            raise ValueError("preview proposal must include a non-empty string tool")
        if len(normalized_tool) > PREVIEW_TOOL_LIMIT:
            raise ValueError("preview proposal tool exceeds maximum length")

        normalized_kwargs = self._sanitize_preview_value(kwargs)
        if not isinstance(normalized_kwargs, dict):
            raise ValueError("preview proposal kwargs must be a mapping")
        tool_schema = self._validate_tool_kwargs(normalized_tool, tool_fn, normalized_kwargs)
        return {
            "version": ACTION_DIGEST_VERSION,
            "policy_profile": policy_profile,
            "tool": normalized_tool,
            "kwargs": normalized_kwargs,
            "tool_schema": tool_schema,
        }

    def _action_digest(
        self,
        *,
        tool_name: str,
        kwargs: Dict[str, Any],
        policy_profile: str,
        tool_fn: Any,
    ) -> tuple[str, Dict[str, Any]]:
        canonical = self._canonicalize_action(
            tool_name=tool_name,
            kwargs=kwargs,
            policy_profile=policy_profile,
            tool_fn=tool_fn,
        )
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()
        return digest, canonical

    def _build_policy_request(
        self,
        *,
        decision: Dict[str, Any],
        trace_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        request = {
            "tool": decision.get("tool", ""),
            "kwargs": decision.get("kwargs", {}) or {},
            "comment": decision.get("comment", ""),
            "agent_id": decision.get("agent_id", ""),
        }
        if trace_id:
            request["trace_id"] = trace_id
        if correlation_id:
            request["correlation_id"] = correlation_id
        return request

    def _preview_context_id(
        self,
        manifold: TaskManifold,
        policy_profile: str,
        kind: str,
    ) -> str:
        material = {
            "objective": manifold.objective,
            "forbidden_actions": manifold.forbidden_actions,
            "t_max_steps": manifold.t_max_steps,
            "risk_threshold": manifold.risk_threshold,
            "policy_profile": policy_profile,
            "kind": kind,
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()
        return f"preview-{kind}-{digest[:20]}"

    def _execution_correlation_id(self, trace_id: str) -> str:
        """Derive a bounded execution correlation ID from the authoritative trace ID."""
        digest = hashlib.sha256(trace_id.encode("utf-8")).hexdigest()
        return f"exec-corr-{digest[:20]}"

    def _sanitize_preview_candidate(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(decision, dict):
            raise ValueError("preview proposal must be a mapping")

        tool_name = decision.get("tool")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("preview proposal must include a non-empty string tool")
        normalized_tool = tool_name.strip()
        if len(normalized_tool) > PREVIEW_TOOL_LIMIT:
            raise ValueError("preview proposal tool exceeds maximum length")

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
            "tool": normalized_tool,
            "kwargs": self._sanitize_preview_value(raw_kwargs),
            "comment": self._truncate_preview_text(comment, PREVIEW_COMMENT_LIMIT),
            "provider": self._truncate_preview_text(
                provider.strip() or "runtime-local", PREVIEW_DISPLAY_TEXT_LIMIT
            ),
            "agent_id": self._truncate_preview_text(
                agent_id.strip() or "llm_backend", PREVIEW_DISPLAY_TEXT_LIMIT
            ),
            "provider_metadata": self._sanitize_display_value(provider_metadata),
        }

    def _preview_payload(
        self,
        *,
        status: str,
        supported: bool,
        approvable: bool,
        proposal: Optional[Dict[str, Any]],
        policy_allowed: bool,
        policy_reasons: List[str],
        matched_rule_ids: List[str],
        policy_profile: str,
        predicted_drift: float,
        manifold: TaskManifold,
        expected_halt_reason: Optional[str],
        step_estimate: int,
        action_digest: Optional[str] = None,
        authority_metadata: Optional[Dict[str, Any]] = None,
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

        payload: Dict[str, Any] = {
            "status": status,
            "supported": supported,
            "approvable": approvable,
            "preview": True,
            "policy_profile": policy_profile,
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
            "action_digest": action_digest,
            "action_digest_version": ACTION_DIGEST_VERSION if action_digest else None,
            "provider": provider,
            "agent_id": agent_id,
            "provider_metadata": provider_metadata,
            "source_status": status,
            "note": "Preview generated without tool execution.",
            "detail": expected_halt_reason,
            "governed": self._governed,
        }
        if authority_metadata:
            payload["authority_metadata"] = authority_metadata
        return payload

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
                approvable=False,
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
        policy_profile = getattr(self.policy_engine.profile, "value", "balanced")
        preview_trace_id = self._preview_context_id(manifold, policy_profile, "trace")
        preview_correlation_id = self._preview_context_id(manifold, policy_profile, "corr")

        if tool_name == "HALT":
            reason = proposal["comment"] or "LLM issued HALT"
            return self._preview_payload(
                status="preview-halt",
                supported=True,
                approvable=False,
                proposal=proposal,
                policy_allowed=True,
                policy_reasons=[],
                matched_rule_ids=[],
                policy_profile=policy_profile,
                predicted_drift=therm.current_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=0,
            )

        if tool_name in manifold.forbidden_actions:
            reason = f"Forbidden action blocked: {tool_name}"
            return self._preview_payload(
                status="preview-forbidden",
                supported=True,
                approvable=False,
                proposal=proposal,
                policy_allowed=False,
                policy_reasons=[reason],
                matched_rule_ids=["manifold.forbidden_actions"],
                policy_profile=policy_profile,
                predicted_drift=therm.current_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=0,
            )

        # ── Governed path: reject raw callables not in registry ──────────────
        if self._governed:
            assert self.tool_registry is not None
            try:
                governed_entry = self.tool_registry.get(tool_name)
            except Exception:
                governed_entry = None
            if governed_entry is None:
                reason = (
                    f"Tool {tool_name!r} not in governed registry; "
                    "raw callables are not approvable in governed mode"
                )
                return self._preview_payload(
                    status="preview-unknown-tool",
                    supported=True,
                    approvable=False,
                    proposal=proposal,
                    policy_allowed=False,
                    policy_reasons=[reason],
                    matched_rule_ids=["registry.unregistered_tool"],
                    policy_profile=policy_profile,
                    predicted_drift=therm.current_drift,
                    manifold=manifold,
                    expected_halt_reason=reason,
                    step_estimate=0,
                )
        else:
            governed_entry = None

        if self.tools.get(tool_name) is None:
            reason = f"Unknown tool: {tool_name}"
            return self._preview_payload(
                status="preview-unknown-tool",
                supported=True,
                approvable=False,
                proposal=proposal,
                policy_allowed=False,
                policy_reasons=[reason],
                matched_rule_ids=["orchestrator.unknown_tool"],
                policy_profile=policy_profile,
                predicted_drift=therm.current_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=0,
            )

        action_digest: Optional[str] = None
        authority_metadata: Optional[Dict[str, Any]] = None

        if governed_entry is not None:
            # ── Governed action digest: no inspect.signature() ──────────────
            assert self.tool_registry is not None
            try:
                registry_snapshot_hash = self.tool_registry.snapshot_hash()
                principal_identity = str(
                    manifold.metadata.get("principal_identity", "")
                ).strip() or "unset"
                canonical_args_bytes = canonicalize_args(
                    proposal["kwargs"],
                    governed_entry.spec.input_schema,
                    governed_entry.spec.max_input_bytes,
                )
                policy_bundle_hash = self._governed_policy_bundle_hash()
                config_identity_hash = self._governed_config_identity_hash(
                    registry_snapshot_hash
                )
                action_digest = compute_action_digest(
                    tool_id=governed_entry.spec.tool_id,
                    tool_contract_hash=governed_entry.tool_contract_hash,
                    canonical_args_bytes=canonical_args_bytes,
                    policy_bundle_hash=policy_bundle_hash,
                    config_identity_hash=config_identity_hash,
                    principal_identity=principal_identity,
                )
                authority_metadata = {
                    "tool_id": governed_entry.spec.tool_id,
                    "tool_contract_hash": governed_entry.tool_contract_hash,
                    "registry_snapshot_hash": registry_snapshot_hash,
                    "worker_handler_id": governed_entry.worker_handler_id,
                    "isolation_profile": governed_entry.spec.isolation_profile,
                    "risk_class": governed_entry.spec.risk_class,
                }
            except (ToolAuthorityError, InputSchemaInvalidError, ValueError) as exc:
                return self._preview_payload(
                    status="preview-malformed",
                    supported=True,
                    approvable=False,
                    proposal=proposal,
                    policy_allowed=False,
                    policy_reasons=[str(exc)],
                    matched_rule_ids=["orchestrator.tool_schema"],
                    policy_profile=policy_profile,
                    predicted_drift=therm.current_drift,
                    manifold=manifold,
                    expected_halt_reason=str(exc),
                    step_estimate=0,
                )
        else:
            # ── Ungoverned legacy digest (development lane) ──────────────────
            try:
                action_digest, _ = self._action_digest(
                    tool_name=tool_name,
                    kwargs=proposal["kwargs"],
                    policy_profile=policy_profile,
                    tool_fn=self.tools[tool_name],
                )
            except ValueError as exc:
                return self._preview_payload(
                    status="preview-malformed",
                    supported=True,
                    approvable=False,
                    proposal=proposal,
                    policy_allowed=False,
                    policy_reasons=[str(exc)],
                    matched_rule_ids=["orchestrator.tool_schema"],
                    policy_profile=policy_profile,
                    predicted_drift=therm.current_drift,
                    manifold=manifold,
                    expected_halt_reason=str(exc),
                    step_estimate=0,
                )

        policy_request = self._build_policy_request(
            decision=proposal,
            trace_id=preview_trace_id,
            correlation_id=preview_correlation_id,
        )
        try:
            preview_policy_engine = copy.deepcopy(self.policy_engine)
            preview_policy_engine.update_drift(therm.current_drift)
            policy = preview_policy_engine.evaluate(policy_request)
        except Exception as exc:
            reason = f"Policy engine failure: {type(exc).__name__}"
            return self._preview_payload(
                status="preview-policy-denied",
                supported=True,
                approvable=False,
                proposal=proposal,
                policy_allowed=False,
                policy_reasons=[reason],
                matched_rule_ids=["policy.runtime_error"],
                policy_profile=policy_profile,
                predicted_drift=therm.current_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=0,
                action_digest=action_digest,
                authority_metadata=authority_metadata,
            )

        matched_rule_ids = sorted(set(policy.matched_policies))
        if not policy.allowed:
            reason = "; ".join(policy.reasons) or "Policy denied proposed action"
            return self._preview_payload(
                status="preview-policy-denied",
                supported=True,
                approvable=False,
                proposal=proposal,
                policy_allowed=False,
                policy_reasons=list(policy.reasons),
                matched_rule_ids=matched_rule_ids,
                policy_profile=policy.profile,
                predicted_drift=therm.current_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=0,
                action_digest=action_digest,
                authority_metadata=authority_metadata,
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
                approvable=False,
                proposal=proposal,
                policy_allowed=True,
                policy_reasons=[],
                matched_rule_ids=matched_rule_ids,
                policy_profile=policy.profile,
                predicted_drift=projected_drift,
                manifold=manifold,
                expected_halt_reason=reason,
                step_estimate=1,
                action_digest=action_digest,
                authority_metadata=authority_metadata,
            )

        return self._preview_payload(
            status="preview",
            supported=True,
            approvable=True,
            proposal=proposal,
            policy_allowed=True,
            policy_reasons=[],
            matched_rule_ids=matched_rule_ids,
            policy_profile=policy.profile,
            predicted_drift=projected_drift,
            manifold=manifold,
            expected_halt_reason=None,
            step_estimate=1,
            action_digest=action_digest,
            authority_metadata=authority_metadata,
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
        approved_action_digest_raw = str(
            manifold.metadata.get("approved_action_digest") or ""
        ).strip()
        approved_action_digest = approved_action_digest_raw.lower()

        trace_meta = seal_with_build_fingerprint(
            {
                "forbidden_actions": manifold.forbidden_actions,
                "t_max_steps": manifold.t_max_steps,
                "theoretical_t_max": manifold.theoretical_t_max,
                "elfe_a": manifold.elfe_a,
                "elfe_b": manifold.elfe_b,
                "elfe_p": manifold.elfe_p,
                "elfe_q": manifold.elfe_q,
                "approved_action_digest": approved_action_digest or None,
                "approved_action_digest_version": ACTION_DIGEST_VERSION
                if approved_action_digest
                else None,
            }
        )
        trace_id = self.vault.create_trace(
            objective=manifold.objective,
            meta=trace_meta,
        )
        execution_correlation_id = self._execution_correlation_id(trace_id)

        history: List[Dict[str, Any]] = []
        step_idx = 0
        final_status: Status = "HALTED_SILENCE_CLAUSE"
        halt_reason: Optional[str] = None
        required_action: Optional[str] = None
        active_policy_profile = getattr(self.policy_engine.profile, "value", "balanced")
        actual_provider = "runtime-local"

        if approved_action_digest_raw and not ACTION_DIGEST_HEX_RE.fullmatch(
            approved_action_digest
        ):
            halt_reason = "INVALID_APPROVED_ACTION_DIGEST"
            self._log_step(
                trace_id=trace_id,
                step_index=step_idx,
                node="orchestrator",
                action="INVALID_APPROVED_ACTION_DIGEST",
                drift=therm.current_drift,
                status=final_status,
                payload={"approved_action_digest": approved_action_digest_raw},
            )
            return ExecutionReceipt(
                trace_id=trace_id,
                status=final_status,
                steps=step_idx,
                final_drift=therm.current_drift,
                drift_trajectory=therm.drift_trajectory(),
                halt_reason=halt_reason,
                required_action=required_action,
                policy_profile=active_policy_profile,
            )

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

            if approved_action_digest and step_idx > 0:
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = "APPROVAL_SCOPE_EXHAUSTED"
                required_action = "REPREVIEW_REQUIRED"
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx,
                    node="orchestrator",
                    action="APPROVAL_SCOPE_EXHAUSTED",
                    drift=therm.current_drift,
                    status=final_status,
                    payload={
                        "approved_action_digest": approved_action_digest,
                        "required_action": required_action,
                    },
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
            projected = self._project_to_constraint(
                decision,
                drift=therm.current_drift,
                trace_id=trace_id,
                correlation_id=execution_correlation_id,
            )
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
            decision_provider = decision.get("provider")
            if isinstance(decision_provider, str) and decision_provider.strip():
                actual_provider = decision_provider.strip()

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

            # ── Governed mode: registry resolution + contract re-check ────────
            governed_exec_entry = None
            governed_exec_canonical_args: bytes | None = None
            if self._governed:
                assert self.tool_registry is not None
                try:
                    governed_exec_entry = self.tool_registry.get(tool_name)
                except Exception as exc:
                    final_status = "HALTED_SILENCE_CLAUSE"
                    halt_reason = "TOOL_CONTRACT_CHANGED"
                    self._log_step(
                        trace_id=trace_id,
                        step_index=step_idx,
                        node="orchestrator",
                        action="TOOL_CONTRACT_CHANGED",
                        drift=therm.current_drift,
                        status=final_status,
                        payload={"tool": tool_name, "reason": str(exc)},
                    )
                    break
                try:
                    governed_exec_canonical_args = canonicalize_args(
                        tool_kwargs,
                        governed_exec_entry.spec.input_schema,
                        governed_exec_entry.spec.max_input_bytes,
                    )
                except (InputSchemaInvalidError, ToolAuthorityError, ValueError) as exc:
                    final_status = "HALTED_SILENCE_CLAUSE"
                    halt_reason = "INPUT_SCHEMA_INVALID"
                    self._log_step(
                        trace_id=trace_id,
                        step_index=step_idx,
                        node="orchestrator",
                        action="INVALID_TOOL_KWARGS",
                        drift=therm.current_drift,
                        status=final_status,
                        payload={"tool": tool_name, "reason": halt_reason, "detail": str(exc)},
                    )
                    break

            try:
                if governed_exec_entry is not None and governed_exec_canonical_args is not None:
                    # Governed action digest (no inspect.signature)
                    assert self.tool_registry is not None
                    registry_snapshot_hash = self.tool_registry.snapshot_hash()
                    principal_identity = str(
                        manifold.metadata.get("principal_identity", "")
                    ).strip() or "unset"
                    policy_bundle_hash = self._governed_policy_bundle_hash()
                    config_identity_hash = self._governed_config_identity_hash(
                        registry_snapshot_hash
                    )
                    actual_action_digest = compute_action_digest(
                        tool_id=governed_exec_entry.spec.tool_id,
                        tool_contract_hash=governed_exec_entry.tool_contract_hash,
                        canonical_args_bytes=governed_exec_canonical_args,
                        policy_bundle_hash=policy_bundle_hash,
                        config_identity_hash=config_identity_hash,
                        principal_identity=principal_identity,
                    ).lower()
                    actual_action: Dict[str, Any] = {
                        "tool_id": governed_exec_entry.spec.tool_id,
                        "tool_contract_hash": governed_exec_entry.tool_contract_hash,
                    }
                else:
                    # Ungoverned legacy digest (development lane)
                    actual_action_digest, actual_action = self._action_digest(
                        tool_name=tool_name,
                        kwargs=tool_kwargs,
                        policy_profile=active_policy_profile,
                        tool_fn=tool_fn,
                    )
                    actual_action_digest = actual_action_digest.lower()
            except ValueError as exc:
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = "INVALID_TOOL_KWARGS"
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx,
                    node="orchestrator",
                    action="INVALID_TOOL_KWARGS",
                    drift=therm.current_drift,
                    status=final_status,
                    payload={"tool": tool_name, "reason": halt_reason, "detail": str(exc)},
                )
                break

            if (
                step_idx == 0
                and approved_action_digest
                and not hmac.compare_digest(
                    approved_action_digest.encode("ascii"),
                    actual_action_digest.encode("ascii"),
                )
            ):
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = "APPROVED_ACTION_MISMATCH"
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx,
                    node="orchestrator",
                    action="APPROVED_ACTION_MISMATCH",
                    drift=therm.current_drift,
                    status=final_status,
                    payload={
                        "tool": tool_name,
                        "approved_action_digest": approved_action_digest,
                        "actual_action_digest": actual_action_digest,
                        "actual_action": actual_action,
                    },
                )
                break

            # ── Execute via Kitaev shield ────────────────────────────────────
            shielded = self.shield.execute_safely(
                tool_name=tool_name,
                tool_function=tool_fn,
                kwargs=tool_kwargs,
            )

            # ── Governed output schema validation ────────────────────────────
            output_schema_error: Optional[str] = None
            if governed_exec_entry is not None and shielded["success"]:
                try:
                    validate_output(
                        shielded["payload"],
                        governed_exec_entry.spec.output_schema,
                    )
                except (OutputSchemaInvalidError, Exception) as exc:
                    output_schema_error = str(exc)

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
                "success": shielded["success"] and output_schema_error is None,
                "error_type": shielded.get("error_type"),
                "drift_penalty": shielded["drift_penalty"],
                "constraint_drift_delta": drift_delta,
            }
            if output_schema_error is not None:
                payload["output_schema_error"] = output_schema_error

            self._log_step(
                trace_id=trace_id,
                step_index=step_idx,
                node=agent_id,
                action=f"TOOL:{tool_name}",
                drift=new_drift,
                status="CONTINUE_DESCENT",
                payload=payload,
            )

            # ── Governed: ProofVault authority event ─────────────────────────
            if governed_exec_entry is not None:
                try:
                    output_raw = shielded["payload"]
                    output_bytes = len(
                        str(output_raw).encode("utf-8", errors="replace")
                        if not isinstance(output_raw, bytes)
                        else output_raw
                    )
                    output_digest = hashlib.sha256(
                        (
                            output_raw.encode("utf-8", errors="replace")
                            if isinstance(output_raw, str)
                            else str(output_raw).encode("utf-8", errors="replace")
                        )
                    ).hexdigest()
                    authority_event_payload = {
                        "tool_id": governed_exec_entry.spec.tool_id,
                        "tool_contract_hash": governed_exec_entry.tool_contract_hash,
                        "action_digest": actual_action_digest,
                        "registry_snapshot_hash": (
                            self.tool_registry.snapshot_hash()
                            if self.tool_registry is not None
                            else ""
                        ),
                        "success": shielded["success"] and output_schema_error is None,
                        "error_type": shielded.get("error_type"),
                        "output_schema_error": output_schema_error,
                        "output_digest": output_digest,
                        "output_size_bytes": output_bytes,
                        "isolation_profile": governed_exec_entry.spec.isolation_profile,
                    }
                    self.vault.append_authority_event(
                        "tool.execution",
                        trace_id,
                        authority_event_payload,
                    )
                except Exception:
                    pass  # authority event failure must not affect tool result

            # ── Output schema failure halts ───────────────────────────────────
            if output_schema_error is not None:
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = f"OUTPUT_SCHEMA_INVALID: {output_schema_error}"
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx + 1,
                    node="orchestrator",
                    action="OUTPUT_SCHEMA_INVALID",
                    drift=new_drift,
                    status=final_status,
                    payload={"tool": tool_name, "reason": halt_reason},
                )
                break

            history.append(
                {
                    "step": step_idx,
                    "tool": tool_name,
                    "success": shielded["success"] and output_schema_error is None,
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
            required_action=required_action,
            policy_profile=active_policy_profile,
            provider=actual_provider,
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
