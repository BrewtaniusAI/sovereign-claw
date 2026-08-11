"""
orchestrator.py — Giles Node / Topological Descent Engine
==========================================================
Central governance node. Implements:

  • Topological Descent loop with strict T_max enforcement
  • Bounded-worker dispatch for governed non-trusted tools
  • Kitaev Zero-Mode delegation for trusted/development in-process tools
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
from typing import Any, Literal, Protocol

from .execution_boundary import (
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MAX_STDERR_BYTES,
    DEFAULT_MAX_STDOUT_BYTES,
    SUBPROCESS_WORKER_BUILD_IDENTITY,
    WORKER_SUCCESS_STATUS,
    WorkerProtocolError,
    WorkerRequestV1,
    canonical_json_digest_bounded,
    probe_hardened_container_seccomp_v1_capabilities,
    run_subprocess_bounded_v1,
    validate_worker_response_authority,
)
from .ip_shield import seal_with_build_fingerprint
from .kitaev_shield import KitaevZeroMode
from .policy_engine import PolicyEngine
from .proof_vault import ProofVault, StepRecord
from .thermodynamics import SystemThermodynamics, TaskManifold
from .tool_authority import (
    InputSchemaInvalidError,
    MissingPostconditionValidatorError,
    OutputSchemaInvalidError,
    PostconditionValidatorRegistry,
    ToolAuthorityError,
    ToolRegistry,
    canonical_json,
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
        history: list[dict[str, Any]],
        forbidden_actions: list[str],
        drift: float,
    ) -> dict[str, Any]:
        pass


# ── ExecutionReceipt ──────────────────────────────────────────────────────────
@dataclass
class ExecutionReceipt:
    trace_id: str
    status: Status
    steps: int
    final_drift: float
    drift_trajectory: list[float] = field(default_factory=list)
    halt_reason: str | None = None
    required_action: str | None = None
    policy_profile: str | None = None
    provider: str | None = None


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
        tools: dict[str, Any] | None = None,
        vault: ProofVault | None = None,
        shield: KitaevZeroMode | None = None,
        policy_engine: PolicyEngine | None = None,
        tool_registry: ToolRegistry | None = None,
        postcondition_validator_registry: PostconditionValidatorRegistry | None = None,
    ) -> None:
        self.llm = llm_backend
        self.tools: dict[str, Any] = tools or {}
        self.vault = vault or ProofVault()
        self.shield = shield or KitaevZeroMode()
        self.policy_engine = policy_engine or PolicyEngine()
        self.tool_registry = tool_registry
        self.postcondition_validator_registry = postcondition_validator_registry
        # Immutable server-owned governed handler bindings (keyed by worker_handler_id).
        # Populated via register_governed_handler(); never overridable after binding.
        self._governed_handlers: dict[str, Any] = {}

    @property
    def _governed(self) -> bool:
        """True when a ToolRegistry is configured (governed mode)."""
        return self.tool_registry is not None

    def _governed_policy_bundle_hash(self) -> str:
        """Stable hash of the policy bundle identity in effect."""
        profile = getattr(self.policy_engine.profile, "value", "balanced")
        material = json.dumps({"profile": profile}, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
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

    def _governed_principal_identity(self, manifold: TaskManifold) -> str:
        """
        Derive a canonical principal-context identity from principal ID + sorted scopes.

        Scope drift (adding or removing scopes) produces a different identity and
        therefore invalidates any previously computed action digest.
        """
        principal_id = str(manifold.metadata.get("principal_identity", "")).strip() or "unset"
        raw_scopes = manifold.metadata.get("principal_scopes", [])
        if isinstance(raw_scopes, list):
            scopes = sorted(str(s) for s in raw_scopes if isinstance(s, str))
        else:
            scopes = []
        material = canonical_json({"principal_id": principal_id, "principal_scopes": scopes})
        return hashlib.sha256(material).hexdigest()

    def _check_principal_scopes(
        self,
        governed_entry: Any,
        manifold: TaskManifold,
    ) -> str | None:
        """
        Check that the manifold's principal_scopes satisfy the ToolSpec's
        required_principal_scopes.

        Returns an error string if scopes are missing, else None.
        """
        required = governed_entry.spec.required_principal_scopes
        if not required:
            return None
        raw_scopes = manifold.metadata.get("principal_scopes", [])
        if isinstance(raw_scopes, list):
            granted = {str(s) for s in raw_scopes if isinstance(s, str)}
        else:
            granted = set()
        missing = [s for s in required if s not in granted]
        if missing:
            return f"Missing required principal scopes: {missing!r}"
        return None

    # ── Tool registry ─────────────────────────────────────────────────────────
    def register_tool(self, name: str, fn: Any) -> None:
        self.tools[name] = fn

    def register_governed_handler(self, handler_id: str, fn: Any) -> None:
        """
        Bind an immutable governed handler for *handler_id* (a ``worker_handler_id`` value
        from a :class:`ToolSpecV1`).

        Once bound, the handler cannot be substituted (prevents post-approval
        callable replacement).  Re-registering the *same* callable is idempotent.
        Raises ValueError if a different callable is supplied for an already-bound
        handler_id.
        """
        existing = self._governed_handlers.get(handler_id)
        if existing is not None:
            if existing is not fn:
                raise ValueError(
                    f"Governed handler for {handler_id!r} is already bound; substitution rejected"
                )
            return  # idempotent
        self._governed_handlers[handler_id] = fn

    def unregister_tool(self, name: str) -> None:
        self.tools.pop(name, None)

    def reset(self) -> None:
        """
        Reset runtime-local state for reuse across multiple runs.

        Current implementation is intentionally lightweight because
        Orchestrator keeps almost all state per-execution.
        """
        return

    # ── Constraint helpers ────────────────────────────────────────────────────
    def _project_to_constraint(
        self,
        decision: dict[str, Any],
        *,
        drift: float,
        trace_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
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
        proposed: dict[str, Any],
        projected: dict[str, Any],
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
            sanitized: dict[str, Any] = {}
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
            sanitized: dict[str, Any] = {}
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

    @staticmethod
    def _sanitized_failure_record(error_class: str, raw_diagnostic: str) -> dict[str, Any]:
        """Return a bounded, privacy-safe failure descriptor.

        Stores only the stable error class/code, a SHA-256 digest of the raw
        diagnostic, and its byte length.  The raw diagnostic body is never
        persisted so that output values, file paths, or secret-bearing exception
        text cannot reach ProofVault or model history.
        """
        encoded = raw_diagnostic.encode("utf-8", errors="replace")
        return {
            "error_class": error_class,
            "diagnostic_digest": hashlib.sha256(encoded).hexdigest(),
            "diagnostic_bytes": len(encoded),
        }

    def _validate_tool_kwargs(self, tool_name: str, tool_fn: Any, kwargs: dict[str, Any]) -> str:
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
        kwargs: dict[str, Any],
        policy_profile: str,
        tool_fn: Any,
    ) -> dict[str, Any]:
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
        kwargs: dict[str, Any],
        policy_profile: str,
        tool_fn: Any,
    ) -> tuple[str, dict[str, Any]]:
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
        decision: dict[str, Any],
        trace_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
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

    def _build_worker_request(
        self,
        *,
        trace_id: str,
        step_idx: int,
        correlation_id: str,
        action_digest: str,
        tool_kwargs: dict[str, Any],
        governed_entry: Any,
        registry_snapshot_hash: str,
        policy_bundle_hash: str,
        principal_identity: str,
        manifold: TaskManifold,
    ) -> WorkerRequestV1:
        raw_scopes = manifold.metadata.get("principal_scopes", [])
        principal_scopes = (
            tuple(sorted(str(s) for s in raw_scopes if isinstance(s, str)))
            if isinstance(raw_scopes, list)
            else ()
        )
        return WorkerRequestV1(
            schema_version="1",
            request_id=f"{trace_id}:{step_idx}",
            trace_id=trace_id,
            correlation_id=correlation_id,
            tool_id=governed_entry.spec.tool_id,
            tool_contract_hash=governed_entry.tool_contract_hash,
            registry_snapshot_hash=registry_snapshot_hash,
            worker_handler_id=governed_entry.worker_handler_id,
            worker_build_identity=governed_entry.spec.worker_build_identity,
            isolation_profile=governed_entry.spec.isolation_profile,
            action_digest=action_digest,
            policy_identity=policy_bundle_hash,
            principal_identity=principal_identity,
            principal_scopes=principal_scopes,
            capabilities=tuple(sorted(str(c) for c in governed_entry.spec.capabilities)),
            args=tool_kwargs,
            deadline_ms=governed_entry.spec.default_deadline_ms,
            cpu_budget_ms=None,
            memory_bytes=None,
            max_processes=None,
            max_request_bytes=DEFAULT_MAX_REQUEST_BYTES,
            max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
            max_stdout_bytes=DEFAULT_MAX_STDOUT_BYTES,
            max_stderr_bytes=DEFAULT_MAX_STDERR_BYTES,
            max_output_bytes=governed_entry.spec.max_output_bytes,
            postcondition_validator_id=governed_entry.spec.postcondition_validator_id or "__none__",
            postcondition_validator_version=governed_entry.spec.postcondition_validator_version
            or "__none__",
            evidence_policy=governed_entry.spec.evidence_policy,
            redaction_policy=governed_entry.spec.redaction_policy,
        )

    def _sanitize_preview_candidate(self, decision: dict[str, Any]) -> dict[str, Any]:
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
        proposal: dict[str, Any] | None,
        policy_allowed: bool,
        policy_reasons: list[str],
        matched_rule_ids: list[str],
        policy_profile: str,
        predicted_drift: float,
        manifold: TaskManifold,
        expected_halt_reason: str | None,
        step_estimate: int,
        action_digest: str | None = None,
        authority_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = None
        diff_equivalent_proposal = None
        provider = "runtime-local"
        agent_id = "llm_backend"
        provider_metadata: dict[str, Any] = {}

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

        payload: dict[str, Any] = {
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

    def preview(self, manifold: TaskManifold) -> dict[str, Any]:
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

        # In ungoverned mode only: check if the tool callable is registered.
        # In governed mode the registry check above is authoritative.
        if not self._governed and self.tools.get(tool_name) is None:
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

        action_digest: str | None = None
        authority_metadata: dict[str, Any] | None = None

        if governed_entry is not None:
            # ── Governed action digest: no inspect.signature() ──────────────
            assert self.tool_registry is not None
            # Scope check: reject missing required_principal_scopes before approval
            scope_error = self._check_principal_scopes(governed_entry, manifold)
            if scope_error is not None:
                return self._preview_payload(
                    status="preview-missing-scopes",
                    supported=True,
                    approvable=False,
                    proposal=proposal,
                    policy_allowed=False,
                    policy_reasons=[scope_error],
                    matched_rule_ids=["orchestrator.principal_scopes"],
                    policy_profile=policy_profile,
                    predicted_drift=therm.current_drift,
                    manifold=manifold,
                    expected_halt_reason=scope_error,
                    step_estimate=0,
                )
            try:
                registry_snapshot_hash = self.tool_registry.snapshot_hash()
                principal_identity = self._governed_principal_identity(manifold)
                canonical_args_bytes = canonicalize_args(
                    proposal["kwargs"],
                    governed_entry.spec.input_schema,
                    governed_entry.spec.max_input_bytes,
                )
                policy_bundle_hash = self._governed_policy_bundle_hash()
                config_identity_hash = self._governed_config_identity_hash(registry_snapshot_hash)
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

        history: list[dict[str, Any]] = []
        step_idx = 0
        final_status: Status = "HALTED_SILENCE_CLAUSE"
        halt_reason: str | None = None
        required_action: str | None = None
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

            # ── Unknown / governed tool lookup ────────────────────────────────
            if self._governed:
                # In governed mode, resolve the registry entry first to get the exact
                # worker_handler_id, then look up the immutable handler binding keyed by
                # that ID.  Never fall back to mutable self.tools — that would allow
                # callable substitution after preview while the digest remains unchanged.
                if self.tool_registry is None:
                    # Invariant violation: _governed is True iff tool_registry is set.
                    final_status = "HALTED_SILENCE_CLAUSE"
                    halt_reason = "GOVERNED_REGISTRY_MISSING"
                    self._log_step(
                        trace_id=trace_id,
                        step_index=step_idx,
                        node="orchestrator",
                        action="GOVERNED_REGISTRY_MISSING",
                        drift=therm.current_drift,
                        status=final_status,
                        payload={"tool": tool_name},
                    )
                    break
                try:
                    _pre_entry = self.tool_registry.get(tool_name)
                    _handler_id = _pre_entry.worker_handler_id
                except Exception:
                    final_status = "HALTED_SILENCE_CLAUSE"
                    halt_reason = "TOOL_CONTRACT_CHANGED"
                    self._log_step(
                        trace_id=trace_id,
                        step_index=step_idx,
                        node="orchestrator",
                        action="TOOL_CONTRACT_CHANGED",
                        drift=therm.current_drift,
                        status=final_status,
                        payload={"tool": tool_name},
                    )
                    break
                tool_fn = self._governed_handlers.get(_handler_id)
                if tool_fn is None:
                    final_status = "HALTED_SILENCE_CLAUSE"
                    halt_reason = f"GOVERNED_HANDLER_NOT_FOUND: {_handler_id!r}"
                    self._log_step(
                        trace_id=trace_id,
                        step_index=step_idx,
                        node="orchestrator",
                        action="GOVERNED_HANDLER_NOT_FOUND",
                        drift=therm.current_drift,
                        status=final_status,
                        payload={"tool": tool_name, "handler_id": _handler_id},
                    )
                    break
            else:
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
                # Scope check: reject missing required_principal_scopes with zero calls
                scope_error = self._check_principal_scopes(governed_exec_entry, manifold)
                if scope_error is not None:
                    final_status = "HALTED_SILENCE_CLAUSE"
                    halt_reason = "MISSING_PRINCIPAL_SCOPES"
                    self._log_step(
                        trace_id=trace_id,
                        step_index=step_idx,
                        node="orchestrator",
                        action="MISSING_PRINCIPAL_SCOPES",
                        drift=therm.current_drift,
                        status=final_status,
                        payload={"tool": tool_name, "reason": scope_error},
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
                    principal_identity = self._governed_principal_identity(manifold)
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
                    actual_action: dict[str, Any] = {
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

            # ── Governed: re-verify handler binding by exact worker_handler_id ──
            # Overwrite tool_fn with the definitively re-verified callable keyed by
            # the worker_handler_id from the re-validated registry entry.  This
            # prevents any handler drift between the early lookup and dispatch.
            if governed_exec_entry is not None:
                final_handler_id = governed_exec_entry.worker_handler_id
                tool_fn = self._governed_handlers.get(final_handler_id)
                if tool_fn is None:
                    final_status = "HALTED_SILENCE_CLAUSE"
                    halt_reason = f"GOVERNED_HANDLER_NOT_FOUND: {final_handler_id!r}"
                    self._log_step(
                        trace_id=trace_id,
                        step_index=step_idx,
                        node="orchestrator",
                        action="GOVERNED_HANDLER_NOT_FOUND",
                        drift=therm.current_drift,
                        status=final_status,
                        payload={"tool": tool_name, "handler_id": final_handler_id},
                    )
                    break

            # ── Execute governed action (trusted in-process or bounded worker) ─
            shielded: dict[str, Any]
            worker_effective_identity: dict[str, Any] = {}
            if governed_exec_entry is not None and not governed_exec_entry.trusted_execution_class:
                if governed_exec_entry.spec.isolation_profile == "hardened_container_seccomp_v1":
                    sandbox_caps = probe_hardened_container_seccomp_v1_capabilities()
                    if not sandbox_caps.available:
                        shielded = {
                            "success": False,
                            "payload": "Isolation profile unavailable",
                            "drift_penalty": 0.55,
                            "error_type": "ISOLATION_UNAVAILABLE",
                        }
                    else:
                        shielded = {
                            "success": False,
                            "payload": "hardened_container_seccomp_v1 launch not implemented",
                            "drift_penalty": 0.55,
                            "error_type": "UNSUPPORTED_ISOLATION",
                        }
                elif governed_exec_entry.spec.isolation_profile != "subprocess_bounded_v1":
                    shielded = {
                        "success": False,
                        "payload": (
                            "Unsupported isolation profile for governed worker: "
                            f"{governed_exec_entry.spec.isolation_profile}"
                        ),
                        "drift_penalty": 0.55,
                        "error_type": "UNSUPPORTED_ISOLATION",
                    }
                else:
                    assert self.tool_registry is not None
                    try:
                        dispatch_entry = self.tool_registry.get(tool_name)
                    except Exception:
                        final_status = "HALTED_SILENCE_CLAUSE"
                        halt_reason = "TOOL_CONTRACT_CHANGED"
                        self._log_step(
                            trace_id=trace_id,
                            step_index=step_idx,
                            node="orchestrator",
                            action="TOOL_CONTRACT_CHANGED",
                            drift=therm.current_drift,
                            status=final_status,
                            payload={"tool": tool_name},
                        )
                        break
                    if (
                        dispatch_entry.tool_contract_hash != governed_exec_entry.tool_contract_hash
                        or dispatch_entry.worker_handler_id != governed_exec_entry.worker_handler_id
                        or dispatch_entry.spec.worker_build_identity
                        != governed_exec_entry.spec.worker_build_identity
                        or dispatch_entry.spec.isolation_profile
                        != governed_exec_entry.spec.isolation_profile
                    ):
                        final_status = "HALTED_SILENCE_CLAUSE"
                        halt_reason = "TOOL_CONTRACT_CHANGED"
                        self._log_step(
                            trace_id=trace_id,
                            step_index=step_idx,
                            node="orchestrator",
                            action="TOOL_CONTRACT_CHANGED",
                            drift=therm.current_drift,
                            status=final_status,
                            payload={"tool": tool_name},
                        )
                        break
                    try:
                        dispatch_canonical_args = canonicalize_args(
                            tool_kwargs,
                            dispatch_entry.spec.input_schema,
                            dispatch_entry.spec.max_input_bytes,
                        )
                    except Exception:
                        final_status = "HALTED_SILENCE_CLAUSE"
                        halt_reason = "INPUT_SCHEMA_INVALID"
                        self._log_step(
                            trace_id=trace_id,
                            step_index=step_idx,
                            node="orchestrator",
                            action="INVALID_TOOL_KWARGS",
                            drift=therm.current_drift,
                            status=final_status,
                            payload={"tool": tool_name, "reason": halt_reason},
                        )
                        break
                    if governed_exec_canonical_args is None or not hmac.compare_digest(
                        hashlib.sha256(governed_exec_canonical_args).digest(),
                        hashlib.sha256(dispatch_canonical_args).digest(),
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
                            payload={"tool": tool_name},
                        )
                        break
                    worker_request = self._build_worker_request(
                        trace_id=trace_id,
                        step_idx=step_idx,
                        correlation_id=execution_correlation_id,
                        action_digest=actual_action_digest,
                        tool_kwargs=tool_kwargs,
                        governed_entry=dispatch_entry,
                        registry_snapshot_hash=registry_snapshot_hash,
                        policy_bundle_hash=policy_bundle_hash,
                        principal_identity=principal_identity,
                        manifold=manifold,
                    )
                    if worker_request.worker_build_identity != SUBPROCESS_WORKER_BUILD_IDENTITY:
                        final_status = "HALTED_SILENCE_CLAUSE"
                        halt_reason = "TOOL_CONTRACT_CHANGED"
                        self._log_step(
                            trace_id=trace_id,
                            step_index=step_idx,
                            node="orchestrator",
                            action="TOOL_CONTRACT_CHANGED",
                            drift=therm.current_drift,
                            status=final_status,
                            payload={
                                "tool": tool_name,
                                "reason": "worker_build_identity mismatch for subprocess_bounded_v1",
                            },
                        )
                        break
                    request_bytes = worker_request.canonical_bytes()
                    request_digest = hashlib.sha256(request_bytes).hexdigest()
                    try:
                        self.vault.append_authority_event(
                            "tool.dispatch.launch",
                            trace_id,
                            {
                                "request_id": worker_request.request_id,
                                "request_digest": request_digest,
                                "request_size_bytes": len(request_bytes),
                                "tool_id": dispatch_entry.spec.tool_id,
                                "tool_contract_hash": dispatch_entry.tool_contract_hash,
                                "registry_snapshot_hash": registry_snapshot_hash,
                                "worker_handler_id": dispatch_entry.worker_handler_id,
                                "worker_build_identity": worker_request.worker_build_identity,
                                "isolation_profile": worker_request.isolation_profile,
                                "action_digest": actual_action_digest,
                                "principal_identity": principal_identity,
                                "policy_identity": policy_bundle_hash,
                                "max_output_bytes": dispatch_entry.spec.max_output_bytes,
                                "max_response_bytes": worker_request.max_response_bytes,
                                "max_stdout_bytes": worker_request.max_stdout_bytes,
                                "max_stderr_bytes": worker_request.max_stderr_bytes,
                                "deadline_ms": worker_request.deadline_ms,
                            },
                        )
                    except Exception:
                        final_status = "HALTED_SILENCE_CLAUSE"
                        halt_reason = "EVIDENCE_PERSISTENCE_FAILED"
                        self._log_step(
                            trace_id=trace_id,
                            step_index=step_idx,
                            node="orchestrator",
                            action="EVIDENCE_PERSISTENCE_FAILED",
                            drift=therm.current_drift,
                            status=final_status,
                            payload={
                                "tool": tool_name,
                                "reason": "launch authority event persistence failed",
                            },
                        )
                        break
                    worker_response = run_subprocess_bounded_v1(worker_request)
                    worker_effective_identity = (
                        worker_response.side_effect_evidence
                        if isinstance(worker_response.side_effect_evidence, dict)
                        else {}
                    )
                    terminal_status = worker_response.status
                    terminal_diagnostic_class = worker_response.diagnostic_class
                    terminal_validation_class = ""
                    terminal_result_digest = ""
                    terminal_result_size = 0
                    validation_error: Exception | None = None
                    try:
                        terminal_result_digest, terminal_result_size = canonical_json_digest_bounded(
                            worker_response.result,
                            max_bytes=dispatch_entry.spec.max_output_bytes,
                        )
                    except WorkerProtocolError as exc:
                        terminal_status = exc.code
                        terminal_diagnostic_class = exc.code
                        terminal_validation_class = exc.code

                    try:
                        validate_worker_response_authority(
                            worker_request,
                            worker_response,
                            dispatch_entry,
                        )
                    except (WorkerProtocolError, OutputSchemaInvalidError) as exc:
                        validation_error = exc
                        terminal_validation_class = getattr(exc, "code", type(exc).__name__)
                        terminal_status = getattr(exc, "code", "PROTOCOL_ERROR")
                        terminal_diagnostic_class = terminal_validation_class

                    try:
                        self.vault.append_authority_event(
                            "tool.dispatch.terminal",
                            trace_id,
                            {
                                "request_id": worker_request.request_id,
                                "status": terminal_status,
                                "duration_ms": worker_response.duration_ms,
                                "result_digest": terminal_result_digest,
                                "result_size_bytes": terminal_result_size,
                                "response_result_sha256": worker_response.result_sha256,
                                "response_result_size_bytes": worker_response.result_size_bytes,
                                "effective_worker_build_identity": worker_effective_identity.get(
                                    "effective_worker_build_identity",
                                    "",
                                ),
                                "effective_profile_id": worker_effective_identity.get(
                                    "effective_profile_id",
                                    "",
                                ),
                                "effective_capability_matrix_hash": worker_effective_identity.get(
                                    "effective_capability_matrix_hash",
                                    "",
                                ),
                                "diagnostic_class": terminal_diagnostic_class,
                                "validation_class": terminal_validation_class,
                            },
                        )
                    except Exception:
                        final_status = "HALTED_SILENCE_CLAUSE"
                        halt_reason = "EVIDENCE_PERSISTENCE_FAILED"
                        self._log_step(
                            trace_id=trace_id,
                            step_index=step_idx,
                            node="orchestrator",
                            action="EVIDENCE_PERSISTENCE_FAILED",
                            drift=therm.current_drift,
                            status=final_status,
                            payload={
                                "tool": tool_name,
                                "reason": "terminal authority event persistence failed",
                            },
                        )
                        break
                    if validation_error is not None:
                        shielded = {
                            "success": False,
                            "payload": "Bounded worker protocol validation failed",
                            "drift_penalty": 0.55,
                            "error_type": terminal_validation_class,
                        }
                    else:
                        worker_ok = worker_response.status == WORKER_SUCCESS_STATUS
                        worker_error = "" if worker_ok else worker_response.status
                        shielded = {
                            "success": worker_ok,
                            "payload": worker_response.result,
                            "drift_penalty": 0.0 if worker_ok else 0.55,
                            "error_type": worker_error,
                        }
            else:
                # trusted/development in-process execution class
                shielded = self.shield.execute_safely(
                    tool_name=tool_name,
                    tool_function=tool_fn,
                    kwargs=tool_kwargs,
                )

            # ── Governed output schema validation ────────────────────────────
            # _raw_output_schema_error: transient only; never written to vault.
            # output_schema_failure: sanitized form (class+digest+bytes) for evidence.
            _raw_output_schema_error: str | None = None
            output_schema_failure: dict[str, Any] | None = None
            output_size_bytes: int = 0
            output_digest_hex: str = ""
            if governed_exec_entry is not None and shielded["success"]:
                try:
                    validate_output(
                        shielded["payload"],
                        governed_exec_entry.spec.output_schema,
                    )
                except (OutputSchemaInvalidError, Exception) as exc:
                    _raw_output_schema_error = str(exc)
                    output_schema_failure = self._sanitized_failure_record(
                        "OUTPUT_SCHEMA_INVALID", _raw_output_schema_error
                    )

                # Enforce max_output_bytes before reporting success
                if _raw_output_schema_error is None:
                    try:
                        output_encoded = canonical_json(shielded["payload"])
                    except Exception:
                        output_encoded = str(shielded["payload"]).encode("utf-8", errors="replace")
                    output_size_bytes = len(output_encoded)
                    output_digest_hex = hashlib.sha256(output_encoded).hexdigest()
                    if output_size_bytes > governed_exec_entry.spec.max_output_bytes:
                        _raw_output_schema_error = (
                            f"Output size {output_size_bytes} bytes exceeds "
                            f"max_output_bytes {governed_exec_entry.spec.max_output_bytes}"
                        )
                        output_schema_failure = self._sanitized_failure_record(
                            "OUTPUT_SIZE_EXCEEDED", _raw_output_schema_error
                        )

            # Unified check flag for downstream gating (mirrors old output_schema_error)
            output_schema_error: str | None = _raw_output_schema_error

            # ── Postcondition validation ──────────────────────────────────────
            # _raw_postcondition_error: transient only; never written to vault.
            # postcondition_failure: sanitized form (class+digest+bytes) for evidence.
            _raw_postcondition_error: str | None = None
            postcondition_failure: dict[str, Any] | None = None
            postcondition_error: str | None = None
            if (
                governed_exec_entry is not None
                and shielded["success"]
                and output_schema_error is None
                and governed_exec_entry.spec.postcondition_validator_id
            ):
                validator_id = governed_exec_entry.spec.postcondition_validator_id
                validator_version = governed_exec_entry.spec.postcondition_validator_version
                pv_registry = self.postcondition_validator_registry
                if pv_registry is None:
                    _raw_postcondition_error = (
                        f"No postcondition validator registry configured; "
                        f"required validator {validator_id!r}"
                    )
                else:
                    try:
                        pv_registry.validate(
                            validator_id,
                            validator_version,
                            tool_kwargs,
                            shielded["payload"],
                            {"trace_id": trace_id, "step_index": step_idx},
                        )
                    except MissingPostconditionValidatorError as exc:
                        _raw_postcondition_error = str(exc)
                    except Exception as exc:
                        _raw_postcondition_error = f"POSTCONDITION_FAILED: {exc}"
                if _raw_postcondition_error is not None:
                    postcondition_failure = self._sanitized_failure_record(
                        "POSTCONDITION_FAILED", _raw_postcondition_error
                    )
                    postcondition_error = _raw_postcondition_error

            new_drift = therm.apply_drift_update(
                step_count=step_idx,
                error_penalty=shielded["drift_penalty"] + drift_delta,
            )

            # Update Byzantine reputation for this agent
            self.vault.update_agent_reputation(agent_id, shielded["drift_penalty"])

            # ── Privacy-safe step payload ─────────────────────────────────────
            # In governed mode: log only bounded metadata; no raw kwargs/results.
            # In ungoverned/legacy mode: log the full payload as before.
            step_success = (
                shielded["success"] and output_schema_error is None and postcondition_error is None
            )

            # ── Governed: ProofVault authority event (before step record) ────
            # Evidence persistence must succeed before any record can assert
            # success=True. Attempt it first; on failure, demote step_success so
            # the subsequently written step record never asserts terminal success
            # without persisted evidence.
            # Sanitized failure records (class+digest+bytes) are used in all
            # governed evidence so that raw diagnostics never reach the vault.
            evidence_persistence_failed = False
            if governed_exec_entry is not None:
                try:
                    authority_event_payload = {
                        "tool_id": governed_exec_entry.spec.tool_id,
                        "tool_contract_hash": governed_exec_entry.tool_contract_hash,
                        "action_digest": actual_action_digest,
                        "registry_snapshot_hash": (
                            self.tool_registry.snapshot_hash()
                            if self.tool_registry is not None
                            else ""
                        ),
                        "success": step_success,
                        "error_class": shielded.get("error_type"),
                        "output_schema_failure": output_schema_failure,
                        "postcondition_failure": postcondition_failure,
                        "output_digest": output_digest_hex,
                        "output_size_bytes": output_size_bytes,
                        "isolation_profile": governed_exec_entry.spec.isolation_profile,
                        "effective_worker_build_identity": worker_effective_identity.get(
                            "effective_worker_build_identity",
                            "",
                        ),
                        "effective_profile_id": worker_effective_identity.get(
                            "effective_profile_id",
                            "",
                        ),
                        "effective_capability_matrix_hash": worker_effective_identity.get(
                            "effective_capability_matrix_hash",
                            "",
                        ),
                    }
                    self.vault.append_authority_event(
                        "tool.execution",
                        trace_id,
                        authority_event_payload,
                    )
                except Exception:
                    # If the tool already actuated and evidence cannot be persisted,
                    # we must not report success — an actuation without evidence is
                    # an uncertain outcome.
                    if step_success:
                        evidence_persistence_failed = True
                        step_success = False

            if governed_exec_entry is not None:
                _canonical_args_log = governed_exec_canonical_args or b""
                payload = {
                    "tool_id": governed_exec_entry.spec.tool_id,
                    "tool_contract_hash": governed_exec_entry.tool_contract_hash,
                    "action_digest": actual_action_digest,
                    "registry_snapshot_hash": (
                        self.tool_registry.snapshot_hash() if self.tool_registry is not None else ""
                    ),
                    "canonical_args_digest": hashlib.sha256(_canonical_args_log).hexdigest(),
                    "canonical_args_size_bytes": len(_canonical_args_log),
                    "success": step_success,
                    "error_class": shielded.get("error_type"),
                    "output_digest": output_digest_hex,
                    "output_size_bytes": output_size_bytes,
                    "output_schema_failure": output_schema_failure,
                    "postcondition_failure": postcondition_failure,
                    "isolation_profile": governed_exec_entry.spec.isolation_profile,
                    "drift_penalty": shielded["drift_penalty"],
                    "constraint_drift_delta": drift_delta,
                }
            else:
                payload = {
                    "decision_comment": comment,
                    "tool": tool_name,
                    "tool_kwargs": tool_kwargs,
                    "tool_result": shielded["payload"],
                    "success": step_success,
                    "error_type": shielded.get("error_type"),
                    "drift_penalty": shielded["drift_penalty"],
                    "constraint_drift_delta": drift_delta,
                }
            if output_schema_error is not None and governed_exec_entry is None:
                payload["output_schema_error"] = output_schema_error
            if postcondition_error is not None and governed_exec_entry is None:
                payload["postcondition_error"] = postcondition_error

            self._log_step(
                trace_id=trace_id,
                step_index=step_idx,
                node=agent_id,
                action=f"TOOL:{tool_name}",
                drift=new_drift,
                status="CONTINUE_DESCENT",
                payload=payload,
            )

            # ── Evidence persistence failure halts (actuation without evidence) ─
            if evidence_persistence_failed:
                final_status = "HALTED_SILENCE_CLAUSE"
                halt_reason = "EVIDENCE_PERSISTENCE_FAILED"
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx + 1,
                    node="orchestrator",
                    action="EVIDENCE_PERSISTENCE_FAILED",
                    drift=new_drift,
                    status=final_status,
                    payload={"tool": tool_name, "reason": halt_reason},
                )
                break

            # ── Postcondition failure halts ───────────────────────────────────
            if postcondition_error is not None:
                final_status = "HALTED_SILENCE_CLAUSE"
                # halt_reason uses stable class + digest — no raw diagnostic body
                halt_reason = (
                    f"POSTCONDITION_FAILED: class={postcondition_failure['error_class']} "  # type: ignore[index]
                    f"digest={postcondition_failure['diagnostic_digest'][:16]}"  # type: ignore[index]
                )
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx + 1,
                    node="orchestrator",
                    action="POSTCONDITION_FAILED",
                    drift=new_drift,
                    status=final_status,
                    payload={
                        "tool": tool_name,
                        "reason": halt_reason,
                        "failure": postcondition_failure,
                    },
                )
                break

            # ── Output schema failure halts ───────────────────────────────────
            if output_schema_error is not None:
                final_status = "HALTED_SILENCE_CLAUSE"
                # halt_reason uses stable class + digest — no raw diagnostic body
                halt_reason = (
                    f"OUTPUT_SCHEMA_INVALID: class={output_schema_failure['error_class']} "  # type: ignore[index]
                    f"digest={output_schema_failure['diagnostic_digest'][:16]}"  # type: ignore[index]
                )
                self._log_step(
                    trace_id=trace_id,
                    step_index=step_idx + 1,
                    node="orchestrator",
                    action="OUTPUT_SCHEMA_INVALID",
                    drift=new_drift,
                    status=final_status,
                    payload={
                        "tool": tool_name,
                        "reason": halt_reason,
                        "failure": output_schema_failure,
                    },
                )
                break

            history.append(
                {
                    "step": step_idx,
                    "tool": tool_name,
                    "success": step_success,
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
        payload: dict[str, Any],
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
