"""
policy_engine.py — Authoritative policy evaluation boundary.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .tool_authority import canonical_json


class PolicyProfile(str, Enum):
    """Adaptive policy profiles controlling governance strictness."""

    STRICT = "strict"
    BALANCED = "balanced"
    EXPLORATORY = "exploratory"


class OpaMode(str, Enum):
    DISABLED = "disabled"
    AUTHORITATIVE = "authoritative"
    ADVISORY = "advisory"


class PolicyDecisionClass(str, Enum):
    ALLOW = "ALLOW"
    POLICY_DENY = "POLICY_DENY"
    POLICY_UNAVAILABLE = "POLICY_UNAVAILABLE"
    POLICY_INPUT_INVALID = "POLICY_INPUT_INVALID"
    POLICY_INFRA_FAILURE = "POLICY_INFRA_FAILURE"


PROFILE_DEFAULTS: Dict[PolicyProfile, Dict[str, Any]] = {
    PolicyProfile.STRICT: {
        "max_payload_bytes": 16384,
        "require_trace_id": True,
        "drift_threshold": 0.3,
        "allow_demo_backend": False,
        "max_tool_calls_per_step": 1,
    },
    PolicyProfile.BALANCED: {
        "max_payload_bytes": 32768,
        "require_trace_id": False,
        "drift_threshold": 0.7,
        "allow_demo_backend": True,
        "max_tool_calls_per_step": 5,
    },
    PolicyProfile.EXPLORATORY: {
        "max_payload_bytes": 65536,
        "require_trace_id": False,
        "drift_threshold": 0.9,
        "allow_demo_backend": True,
        "max_tool_calls_per_step": 10,
    },
}


POLICY_CONTEXT_VERSION = "1"
POLICY_EVALUATOR_VERSION = "policy-engine-v2"
# Guardrail BLOCK semantics are intentionally gated out of authoritative
# production composition in this issue #20 slice. This explicit identity is
# hashed into the bundle so authority verifiers can detect this mode.
POLICY_BUNDLE_GUARDRAIL_IDENTITY = "excluded.production_followup_required.issue_51"
MAX_POLICY_CONTEXT_BYTES = 256 * 1024
MAX_POLICY_REASON_COUNT = 32
MAX_POLICY_MATCHED_COUNT = 64
MAX_POLICY_TEXT_BYTES = 512
MAX_VIOLATION_RECORDS = 256
MAX_OPA_POLICY_FILES = 512
MAX_OPA_POLICY_FILE_BYTES = 2 * 1024 * 1024
MAX_OPA_POLICY_TOTAL_BYTES = 8 * 1024 * 1024
MAX_OPA_EVALUATOR_BYTES = 128 * 1024 * 1024
DEFAULT_OPA_TIMEOUT_MS = 750
DEFAULT_OPA_INPUT_MAX_BYTES = 128 * 1024
DEFAULT_OPA_STDOUT_MAX_BYTES = 64 * 1024
DEFAULT_OPA_STDERR_MAX_BYTES = 8 * 1024
MAX_LIST_ITEMS = 64
OPA_STDIN_WRITE_CHUNK_BYTES = 4096


@dataclass(frozen=True)
class PolicyExecutionContext:
    """Immutable, server-derived policy execution context."""

    context_version: str
    trace_id: str
    session_id: str
    correlation_id: str
    principal_identity: str
    principal_scopes: tuple[str, ...]
    policy_profile: str
    lane: str
    drift_value: float
    drift_components: Mapping[str, float]
    requested_tool: str
    tool_id: str
    tool_contract_hash: str
    tool_risk_class: str
    tool_capabilities: tuple[str, ...]
    config_identity_hash: str
    runtime_identity: str
    provider_identity: str
    fallback_identity: str
    budget_state: Mapping[str, Any]
    resource_state: Mapping[str, Any]
    execution_intent_id: str
    approval_correlation_id: str
    remaining_deadline_ms: int
    action_count: int
    step_index: int
    request_payload_bytes: int
    model_claims: Mapping[str, Any] = field(default_factory=dict)

    def to_authority_dict(self) -> dict[str, Any]:
        return {
            "context_version": self.context_version,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "principal_identity": self.principal_identity,
            "principal_scopes": list(self.principal_scopes),
            "policy_profile": self.policy_profile,
            "lane": self.lane,
            "drift": {
                "value": self.drift_value,
                "components": dict(sorted(self.drift_components.items())),
            },
            "tool": {
                "requested_tool": self.requested_tool,
                "tool_id": self.tool_id,
                "tool_contract_hash": self.tool_contract_hash,
                "risk_class": self.tool_risk_class,
                "capabilities": list(self.tool_capabilities),
            },
            "identity": {
                "config_identity_hash": self.config_identity_hash,
                "runtime_identity": self.runtime_identity,
                "provider_identity": self.provider_identity,
                "fallback_identity": self.fallback_identity,
            },
            "budget_state": dict(self.budget_state),
            "resource_state": dict(self.resource_state),
            "execution_intent_id": self.execution_intent_id,
            "approval_correlation_id": self.approval_correlation_id,
            "remaining_deadline_ms": self.remaining_deadline_ms,
            "action_count": self.action_count,
            "step_index": self.step_index,
            "request_payload_bytes": self.request_payload_bytes,
            "model_claims": dict(self.model_claims),
        }

    def canonical_bytes(self) -> bytes:
        payload = canonical_json(self.to_authority_dict())
        if len(payload) > MAX_POLICY_CONTEXT_BYTES:
            raise ValueError(f"Policy execution context exceeds {MAX_POLICY_CONTEXT_BYTES} bytes")
        return payload

    def context_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class PolicyBundleIdentity:
    evaluator_version: str
    profile: str
    local_rules_hash: str
    opa_mode: str
    opa_query: str
    opa_policy_digest: str
    opa_evaluator_identity: str
    guardrail_bundle_identity: str
    learned_signal_mode: str
    learned_signal_root: str

    def canonical_bytes(self) -> bytes:
        return canonical_json(
            {
                "evaluator_version": self.evaluator_version,
                "profile": self.profile,
                "local_rules_hash": self.local_rules_hash,
                "opa_mode": self.opa_mode,
                "opa_query": self.opa_query,
                "opa_policy_digest": self.opa_policy_digest,
                "opa_evaluator_identity": self.opa_evaluator_identity,
                "guardrail_bundle_identity": self.guardrail_bundle_identity,
                "learned_signal_mode": self.learned_signal_mode,
                "learned_signal_root": self.learned_signal_root,
            }
        )

    def bundle_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass
class PolicyDecision:
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    matched_policies: List[str] = field(default_factory=list)
    profile: str = "balanced"
    drift_at_evaluation: float = 0.0
    decision_class: str = PolicyDecisionClass.ALLOW.value
    context_hash: str = ""
    policy_bundle_hash: str = ""
    opa_mode: str = OpaMode.DISABLED.value
    opa_status: str = "disabled"
    evaluator_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ViolationRecord:
    """Record of a policy violation for learned signal tracking."""

    tool: str
    reason: str
    timestamp: float = 0.0
    count: int = 1


# Maximum violations before a tool is auto-denied by learned signals
MAX_VIOLATIONS_BEFORE_DENY = 3


@dataclass(frozen=True)
class _ExternalDecision:
    decision_class: PolicyDecisionClass
    reasons: List[str]
    matched: List[str]
    status: str
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class _BundleIdentityFailure:
    decision_class: PolicyDecisionClass
    code: str


@dataclass
class _PolicySnapshot:
    digest: str
    snapshot_root: Path
    cleanup_handle: tempfile.TemporaryDirectory[str] | None = None

    def cleanup(self) -> None:
        if self.cleanup_handle is None:
            return
        self.cleanup_handle.cleanup()
        self.cleanup_handle = None


@dataclass
class _PreparedBundle:
    bundle: PolicyBundleIdentity
    failure: _BundleIdentityFailure | None
    policy_snapshot: _PolicySnapshot | None
    opa_bin: Path | None
    opa_evaluator_identity: str

    def cleanup(self) -> None:
        if self.policy_snapshot is not None:
            self.policy_snapshot.cleanup()


@dataclass
class _StreamCapture:
    data: bytearray = field(default_factory=bytearray)
    overflowed: bool = False
    overflow_event: threading.Event = field(default_factory=threading.Event)


class PolicyEngine:
    """Deny-dominant local + optional OPA policy evaluator."""

    def __init__(
        self,
        forbidden_tools: Optional[Iterable[str]] = None,
        max_payload_bytes: int = 32768,
        require_trace_id: bool = False,
        rego_policy_dir: Optional[Path] = None,
        profile: PolicyProfile = PolicyProfile.BALANCED,
        *,
        opa_mode: OpaMode | str = OpaMode.DISABLED,
        opa_query: str = "data.sovereign_claw.execution",
        opa_timeout_ms: int = DEFAULT_OPA_TIMEOUT_MS,
        opa_max_input_bytes: int = DEFAULT_OPA_INPUT_MAX_BYTES,
        opa_max_stdout_bytes: int = DEFAULT_OPA_STDOUT_MAX_BYTES,
        opa_max_stderr_bytes: int = DEFAULT_OPA_STDERR_MAX_BYTES,
        evaluator_version: str = POLICY_EVALUATOR_VERSION,
        learned_signal_mode: str = "advisory",
        max_violation_records: int = MAX_VIOLATION_RECORDS,
        guardrail_bundle_identity: str = POLICY_BUNDLE_GUARDRAIL_IDENTITY,
    ) -> None:
        self.forbidden_tools = set(str(t) for t in (forbidden_tools or []))
        self.max_payload_bytes = int(max_payload_bytes)
        self.require_trace_id = require_trace_id
        self.rego_policy_dir = rego_policy_dir
        self._profile = profile
        self._violation_history: Dict[str, ViolationRecord] = {}
        self._learned_deny_tools: set[str] = set()
        self._current_drift: float = 0.0

        self.opa_mode = opa_mode if isinstance(opa_mode, OpaMode) else OpaMode(str(opa_mode))
        self.opa_query = opa_query
        self.opa_timeout_ms = max(1, int(opa_timeout_ms))
        self.opa_max_input_bytes = max(1, int(opa_max_input_bytes))
        self.opa_max_stdout_bytes = max(1, int(opa_max_stdout_bytes))
        self.opa_max_stderr_bytes = max(1, int(opa_max_stderr_bytes))
        self.evaluator_version = evaluator_version
        self.learned_signal_mode = self._normalize_learned_signal_mode(learned_signal_mode)
        self.max_violation_records = max(1, int(max_violation_records))
        self.guardrail_bundle_identity = self._bounded_text(guardrail_bundle_identity)

    def _normalize_learned_signal_mode(self, learned_signal_mode: str) -> str:
        mode = str(learned_signal_mode).strip().lower() or "advisory"
        if mode == "authoritative":
            # Process-local learned state is not persisted/versioned in this slice.
            # Downgrade to advisory so hidden mutable state cannot become authority.
            return "advisory"
        if mode not in {"advisory", "disabled"}:
            return "advisory"
        return mode

    @property
    def profile(self) -> PolicyProfile:
        return self._profile

    @property
    def current_drift(self) -> float:
        return self._current_drift

    def set_profile(self, profile: PolicyProfile) -> None:
        """Switch active policy profile."""
        self._profile = profile
        defaults = PROFILE_DEFAULTS[profile]
        self.max_payload_bytes = defaults["max_payload_bytes"]
        self.require_trace_id = defaults["require_trace_id"]

    def update_drift(self, drift: float) -> None:
        """Compatibility-only mutable drift cache (non-authoritative)."""
        self._current_drift = drift

    def policy_bundle_hash(self, profile: str | None = None) -> str:
        prepared = self._build_policy_bundle_identity(profile or self._profile.value)
        try:
            return prepared.bundle.bundle_hash()
        finally:
            prepared.cleanup()

    def build_execution_context(
        self,
        *,
        trace_id: str,
        session_id: str,
        correlation_id: str,
        principal_identity: str,
        principal_scopes: Sequence[str],
        policy_profile: str,
        lane: str,
        drift_value: float,
        drift_components: Mapping[str, float],
        requested_tool: str,
        tool_id: str,
        tool_contract_hash: str,
        tool_risk_class: str,
        tool_capabilities: Sequence[str],
        config_identity_hash: str,
        runtime_identity: str,
        provider_identity: str,
        fallback_identity: str,
        budget_state: Mapping[str, Any],
        resource_state: Mapping[str, Any],
        execution_intent_id: str,
        approval_correlation_id: str,
        remaining_deadline_ms: int,
        action_count: int,
        step_index: int,
        request_payload_bytes: int,
        model_claims: Mapping[str, Any] | None = None,
    ) -> PolicyExecutionContext:
        drift = float(drift_value)
        if not math.isfinite(drift):
            raise ValueError("drift_value must be finite")

        sanitized_components: dict[str, float] = {}
        for key, value in sorted(drift_components.items()):
            k = str(key).strip()
            if not k:
                continue
            fv = float(value)
            if not math.isfinite(fv):
                raise ValueError("drift_components values must be finite")
            sanitized_components[k] = fv

        if not sanitized_components:
            sanitized_components = {"scalar": drift}

        return PolicyExecutionContext(
            context_version=POLICY_CONTEXT_VERSION,
            trace_id=self._bounded_text(trace_id),
            session_id=self._bounded_text(session_id),
            correlation_id=self._bounded_text(correlation_id),
            principal_identity=self._bounded_text(principal_identity or "unset"),
            principal_scopes=tuple(
                sorted(self._bounded_text(s) for s in principal_scopes if str(s))
            ),
            policy_profile=self._bounded_text(policy_profile),
            lane=self._bounded_text(lane),
            drift_value=drift,
            drift_components=sanitized_components,
            requested_tool=self._bounded_text(requested_tool),
            tool_id=self._bounded_text(tool_id),
            tool_contract_hash=self._bounded_text(tool_contract_hash),
            tool_risk_class=self._bounded_text(tool_risk_class),
            tool_capabilities=tuple(
                sorted(self._bounded_text(c) for c in tool_capabilities if str(c).strip())
            ),
            config_identity_hash=self._bounded_text(config_identity_hash),
            runtime_identity=self._bounded_text(runtime_identity),
            provider_identity=self._bounded_text(provider_identity),
            fallback_identity=self._bounded_text(fallback_identity),
            budget_state=self._sanitize_json_map(dict(budget_state), max_depth=4),
            resource_state=self._sanitize_json_map(dict(resource_state), max_depth=4),
            execution_intent_id=self._bounded_text(execution_intent_id),
            approval_correlation_id=self._bounded_text(approval_correlation_id),
            remaining_deadline_ms=max(0, int(remaining_deadline_ms)),
            action_count=max(0, int(action_count)),
            step_index=max(0, int(step_index)),
            request_payload_bytes=max(0, int(request_payload_bytes)),
            model_claims=self._sanitize_json_map(dict(model_claims or {}), max_depth=4),
        )

    def evaluate(self, request: Dict[str, Any]) -> PolicyDecision:
        context = self._coerce_context(request)
        return self.evaluate_context(context)

    def evaluate_context(
        self,
        context: PolicyExecutionContext,
        *,
        bound_policy_bundle_hash: str | None = None,
    ) -> PolicyDecision:
        _ = context.canonical_bytes()
        context_hash = context.context_hash()
        prepared = self._build_policy_bundle_identity(context.policy_profile, for_evaluation=True)
        try:
            bundle = prepared.bundle
            bundle_failure = prepared.failure
            bundle_hash = bundle.bundle_hash()

            reasons: list[str] = []
            matched: list[str] = []
            profile = self._resolve_profile(context.policy_profile)
            profile_defaults = PROFILE_DEFAULTS[profile]

            tool = context.requested_tool
            if tool in self.forbidden_tools:
                reasons.append(f"tool '{tool}' is forbidden by local policy")
                matched.append("local.forbidden_tools")

            if self.learned_signal_mode == "authoritative" and tool in self._learned_deny_tools:
                reasons.append(f"tool '{tool}' is denied by learned violation signal")
                matched.append("learned.deny_tools")
            elif tool in self._learned_deny_tools:
                matched.append("learned.advisory_deny_tools")

            effective_max = int(profile_defaults.get("max_payload_bytes", self.max_payload_bytes))
            payload_size = context.request_payload_bytes
            if payload_size > effective_max:
                reasons.append(f"request payload size {payload_size} exceeds limit {effective_max}")
                matched.append("local.max_payload_bytes")

            effective_trace = bool(profile_defaults.get("require_trace_id", self.require_trace_id))
            if effective_trace and not context.trace_id:
                reasons.append("trace_id is required by policy")
                matched.append("local.require_trace_id")

            drift_threshold = float(profile_defaults.get("drift_threshold", 0.7))
            if context.drift_value > drift_threshold:
                if context.provider_identity == "demo_backend" and not profile_defaults.get(
                    "allow_demo_backend", True
                ):
                    reasons.append(f"demo backend not allowed under {profile.value} profile")
                    matched.append("contextual.drift_tightening")

                max_calls = int(profile_defaults.get("max_tool_calls_per_step", 5))
                if context.action_count > max_calls:
                    reasons.append(
                        f"tool call count {context.action_count} exceeds limit {max_calls} under {profile.value} profile"
                    )
                    matched.append("contextual.max_tool_calls")

            local_denied = bool(reasons)

            if (
                bound_policy_bundle_hash
                and not hmac.compare_digest(bound_policy_bundle_hash, bundle_hash)
                and self.opa_mode != OpaMode.DISABLED
            ):
                external = self._opa_failure(
                    PolicyDecisionClass.POLICY_INFRA_FAILURE,
                    "POLICY_BUNDLE_HASH_MISMATCH",
                )
            elif bundle_failure is not None and self.opa_mode != OpaMode.DISABLED:
                external = self._opa_failure(bundle_failure.decision_class, bundle_failure.code)
            else:
                external = self._evaluate_with_opa_context(
                    context,
                    policy_snapshot=prepared.policy_snapshot,
                    opa_bin=prepared.opa_bin,
                    expected_opa_evaluator_identity=prepared.opa_evaluator_identity,
                )
            reasons.extend(external.reasons)
            matched.extend(external.matched)

            if local_denied:
                final_allowed = False
                final_class = PolicyDecisionClass.POLICY_DENY
            elif self.opa_mode == OpaMode.AUTHORITATIVE:
                if external.decision_class == PolicyDecisionClass.ALLOW:
                    final_allowed = True
                    final_class = PolicyDecisionClass.ALLOW
                elif external.decision_class == PolicyDecisionClass.POLICY_DENY:
                    final_allowed = False
                    final_class = PolicyDecisionClass.POLICY_DENY
                elif external.decision_class == PolicyDecisionClass.POLICY_INPUT_INVALID:
                    final_allowed = False
                    final_class = PolicyDecisionClass.POLICY_INPUT_INVALID
                elif external.decision_class in (
                    PolicyDecisionClass.POLICY_UNAVAILABLE,
                    PolicyDecisionClass.POLICY_INFRA_FAILURE,
                ):
                    final_allowed = False
                    final_class = external.decision_class
                else:
                    final_allowed = False
                    final_class = PolicyDecisionClass.POLICY_UNAVAILABLE
            else:
                final_allowed = True
                final_class = PolicyDecisionClass.ALLOW

            bounded_reasons = self._sanitize_string_list(reasons, MAX_POLICY_REASON_COUNT)
            bounded_matched = self._sanitize_string_list(matched, MAX_POLICY_MATCHED_COUNT)

            # Infrastructure failures must not poison learned-deny history.
            if (
                not final_allowed
                and tool
                and final_class
                not in {
                    PolicyDecisionClass.POLICY_UNAVAILABLE,
                    PolicyDecisionClass.POLICY_INFRA_FAILURE,
                }
            ):
                self._record_violation(tool, "; ".join(bounded_reasons) or final_class.value)

            metadata = {
                "evaluator_version": self.evaluator_version,
                "bundle": {
                    "opa_mode": bundle.opa_mode,
                    "opa_query": bundle.opa_query,
                    "opa_policy_digest": bundle.opa_policy_digest,
                    "opa_evaluator_identity": bundle.opa_evaluator_identity,
                    "guardrail_bundle_identity": bundle.guardrail_bundle_identity,
                    "learned_signal_mode": bundle.learned_signal_mode,
                },
            }
            metadata.update(external.metadata)

            return PolicyDecision(
                allowed=final_allowed,
                reasons=bounded_reasons,
                matched_policies=bounded_matched,
                profile=profile.value,
                drift_at_evaluation=context.drift_value,
                decision_class=final_class.value,
                context_hash=context_hash,
                policy_bundle_hash=bundle_hash,
                opa_mode=self.opa_mode.value,
                opa_status=external.status,
                evaluator_metadata=metadata,
            )
        finally:
            prepared.cleanup()

    def _record_violation(self, tool: str, reason: str) -> None:
        """Record violation for learned signal tracking."""
        now = time.time()
        bounded_reason = self._bounded_text(reason)
        if tool in self._violation_history:
            record = self._violation_history[tool]
            record.count += 1
            record.reason = bounded_reason
            record.timestamp = now
        else:
            if len(self._violation_history) >= self.max_violation_records:
                oldest_tool = min(
                    self._violation_history, key=lambda k: self._violation_history[k].timestamp
                )
                self._violation_history.pop(oldest_tool, None)
                self._learned_deny_tools.discard(oldest_tool)
            self._violation_history[tool] = ViolationRecord(
                tool=tool,
                reason=bounded_reason,
                timestamp=now,
            )

        if self._violation_history[tool].count >= MAX_VIOLATIONS_BEFORE_DENY:
            self._learned_deny_tools.add(tool)

    def get_violation_history(self) -> Dict[str, ViolationRecord]:
        """Return a copy-safe full violation history."""
        return {k: copy.copy(v) for k, v in self._violation_history.items()}

    def clear_learned_denials(self) -> None:
        """Clear all learned denial patterns."""
        self._learned_deny_tools.clear()
        self._violation_history.clear()

    def test_policy(self, sample_request: Dict[str, Any]) -> PolicyDecision:
        """Test a policy evaluation against a sample request without side effects."""
        saved_violations = {k: copy.copy(v) for k, v in self._violation_history.items()}
        saved_denials = set(self._learned_deny_tools)

        result = self.evaluate(sample_request)

        self._violation_history = saved_violations
        self._learned_deny_tools = saved_denials
        return result

    def _evaluate_with_opa(self, request: Dict[str, Any]) -> Optional[PolicyDecision]:
        """Backward-compatible helper for tests that invoke _evaluate_with_opa directly."""
        context = self._coerce_context(request)
        external = self._evaluate_with_opa_context(context)
        if self.opa_mode == OpaMode.DISABLED:
            return None
        allowed = external.decision_class == PolicyDecisionClass.ALLOW
        return PolicyDecision(
            allowed=allowed,
            reasons=external.reasons,
            matched_policies=external.matched,
            profile=self._profile.value,
            drift_at_evaluation=context.drift_value,
            decision_class=external.decision_class.value,
            opa_mode=self.opa_mode.value,
            opa_status=external.status,
            evaluator_metadata=dict(external.metadata),
        )

    def _evaluate_with_opa_context(
        self,
        context: PolicyExecutionContext,
        *,
        policy_snapshot: _PolicySnapshot | None = None,
        opa_bin: Path | None = None,
        expected_opa_evaluator_identity: str | None = None,
    ) -> _ExternalDecision:
        if self.opa_mode == OpaMode.DISABLED:
            return _ExternalDecision(
                decision_class=PolicyDecisionClass.ALLOW,
                reasons=[],
                matched=[],
                status="disabled",
                metadata={"opa": {"mode": self.opa_mode.value}},
            )

        if self.rego_policy_dir is None:
            return self._opa_failure(
                PolicyDecisionClass.POLICY_UNAVAILABLE,
                "OPA_POLICY_DIR_MISSING",
            )

        owns_snapshot = False
        if policy_snapshot is None:
            try:
                policy_snapshot = self._snapshot_policy_dir(self.rego_policy_dir)
                owns_snapshot = True
            except Exception as exc:
                decision_class, code = self._classify_policy_dir_failure(exc)
                return self._opa_failure(decision_class, code)

        try:
            if opa_bin is None:
                opa_bin_str = shutil.which("opa")
                opa_bin = Path(opa_bin_str).resolve() if opa_bin_str else None
            if opa_bin is None:
                return self._opa_failure(
                    PolicyDecisionClass.POLICY_UNAVAILABLE,
                    "OPA_BINARY_MISSING",
                )
            if expected_opa_evaluator_identity:
                try:
                    current_identity = self._opa_evaluator_identity(opa_bin)
                except FileNotFoundError:
                    return self._opa_failure(
                        PolicyDecisionClass.POLICY_UNAVAILABLE,
                        "OPA_BINARY_MISSING",
                    )
                except PermissionError:
                    return self._opa_failure(
                        PolicyDecisionClass.POLICY_INFRA_FAILURE,
                        "OPA_BINARY_UNREADABLE",
                    )
                except Exception:
                    return self._opa_failure(
                        PolicyDecisionClass.POLICY_INFRA_FAILURE,
                        "OPA_EVALUATOR_IDENTITY_ERROR",
                    )
                if not hmac.compare_digest(current_identity, expected_opa_evaluator_identity):
                    return self._opa_failure(
                        PolicyDecisionClass.POLICY_INFRA_FAILURE,
                        "OPA_EVALUATOR_IDENTITY_DRIFT",
                    )

            request_obj = {
                "context": context.to_authority_dict(),
                "protocol_version": "1",
            }
            try:
                input_bytes = canonical_json(request_obj)
            except Exception:
                return self._opa_failure(
                    PolicyDecisionClass.POLICY_INPUT_INVALID,
                    "OPA_INPUT_INVALID",
                )

            if len(input_bytes) > self.opa_max_input_bytes:
                return self._opa_failure(
                    PolicyDecisionClass.POLICY_INPUT_INVALID,
                    "OPA_INPUT_OVERSIZE",
                )

            timeout_ms = min(
                self.opa_timeout_ms,
                max(1, int(context.remaining_deadline_ms or self.opa_timeout_ms)),
            )

            cmd = [
                str(opa_bin),
                "eval",
                "--format",
                "json",
                "--data",
                str(policy_snapshot.snapshot_root),
                "--input",
                "-",
                self.opa_query,
            ]

            try:
                run = self._run_bounded_subprocess(
                    cmd=cmd,
                    cwd=policy_snapshot.snapshot_root,
                    env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
                    stdin_data=input_bytes,
                    timeout_ms=timeout_ms,
                    max_stdout=self.opa_max_stdout_bytes,
                    max_stderr=self.opa_max_stderr_bytes,
                )
            except Exception:
                return self._opa_failure(
                    PolicyDecisionClass.POLICY_INFRA_FAILURE,
                    "OPA_SUBPROCESS_ERROR",
                )
        finally:
            if owns_snapshot and policy_snapshot is not None:
                policy_snapshot.cleanup()

        if run["timed_out"]:
            return self._opa_failure(
                PolicyDecisionClass.POLICY_UNAVAILABLE,
                "OPA_TIMEOUT",
            )
        if run["stdout_overflow"]:
            return self._opa_failure(
                PolicyDecisionClass.POLICY_UNAVAILABLE,
                "OPA_STDOUT_OVERSIZE",
            )
        if run["stderr_overflow"]:
            return self._opa_failure(
                PolicyDecisionClass.POLICY_UNAVAILABLE,
                "OPA_STDERR_OVERSIZE",
            )
        if run.get("stdin_error"):
            return self._opa_failure(
                PolicyDecisionClass.POLICY_INFRA_FAILURE,
                "OPA_STDIN_WRITE_FAILED",
            )
        if run["returncode"] != 0:
            return self._opa_failure(
                PolicyDecisionClass.POLICY_UNAVAILABLE,
                "OPA_NONZERO_EXIT",
            )

        try:
            payload = json.loads(run["stdout"].decode("utf-8"))
        except Exception:
            return self._opa_failure(
                PolicyDecisionClass.POLICY_UNAVAILABLE,
                "OPA_MALFORMED_JSON",
            )

        try:
            result_obj = self._extract_opa_result(payload)
        except ValueError as exc:
            return self._opa_failure(
                PolicyDecisionClass.POLICY_UNAVAILABLE,
                self._bounded_text(str(exc)),
            )

        allow = result_obj["allow"]
        deny = result_obj["deny"]
        matched = result_obj["matched"]

        if allow:
            return _ExternalDecision(
                decision_class=PolicyDecisionClass.ALLOW,
                reasons=[],
                matched=matched,
                status="allow",
                metadata={
                    "opa": {
                        "mode": self.opa_mode.value,
                        "timeout_ms": timeout_ms,
                        "stdout_bytes": len(run["stdout"]),
                    }
                },
            )

        deny_reasons = deny if deny else ["OPA_DENY_NO_REASON"]
        return _ExternalDecision(
            decision_class=PolicyDecisionClass.POLICY_DENY,
            reasons=deny_reasons,
            matched=matched,
            status="deny",
            metadata={
                "opa": {
                    "mode": self.opa_mode.value,
                    "timeout_ms": timeout_ms,
                    "stdout_bytes": len(run["stdout"]),
                }
            },
        )

    def _build_policy_bundle_identity(
        self,
        profile: str,
        *,
        for_evaluation: bool = False,
    ) -> _PreparedBundle:
        local_material = {
            "forbidden_tools": sorted(self.forbidden_tools),
            "max_payload_bytes": self.max_payload_bytes,
            "require_trace_id": self.require_trace_id,
            "profile": profile,
        }
        local_hash = hashlib.sha256(canonical_json(local_material)).hexdigest()

        opa_policy_digest = "disabled"
        opa_evaluator_identity = "disabled"
        failure: _BundleIdentityFailure | None = None
        policy_snapshot: _PolicySnapshot | None = None
        opa_bin: Path | None = None
        if self.opa_mode != OpaMode.DISABLED:
            if self.rego_policy_dir is None:
                opa_policy_digest = "missing"
            else:
                try:
                    if for_evaluation:
                        policy_snapshot = self._snapshot_policy_dir(self.rego_policy_dir)
                        opa_policy_digest = policy_snapshot.digest
                    else:
                        opa_policy_digest = self._digest_policy_dir(self.rego_policy_dir)
                except Exception as exc:
                    decision_class, code = self._classify_policy_dir_failure(exc)
                    failure = _BundleIdentityFailure(decision_class=decision_class, code=code)
                    opa_policy_digest = f"failure:{decision_class.value}:{code}"
            if failure is None:
                try:
                    opa_bin, opa_evaluator_identity = self._resolve_opa_evaluator_identity()
                except Exception as exc:
                    decision_class, code = self._classify_opa_evaluator_failure(exc)
                    failure = _BundleIdentityFailure(decision_class=decision_class, code=code)
                    opa_policy_digest = f"failure:{decision_class.value}:{code}"
                    opa_evaluator_identity = f"failure:{decision_class.value}:{code}"

        if failure is not None:
            opa_bin = None

        return _PreparedBundle(
            bundle=PolicyBundleIdentity(
                evaluator_version=self.evaluator_version,
                profile=profile,
                local_rules_hash=local_hash,
                opa_mode=self.opa_mode.value,
                opa_query=self.opa_query,
                opa_policy_digest=opa_policy_digest,
                opa_evaluator_identity=opa_evaluator_identity,
                guardrail_bundle_identity=self.guardrail_bundle_identity,
                learned_signal_mode=self.learned_signal_mode,
                learned_signal_root="none",
            ),
            failure=failure,
            policy_snapshot=policy_snapshot,
            opa_bin=opa_bin,
            opa_evaluator_identity=opa_evaluator_identity,
        )

    def _digest_policy_dir(self, root: Path) -> str:
        return self._scan_policy_dir(root)

    def _snapshot_policy_dir(self, root: Path) -> _PolicySnapshot:
        temp_dir = tempfile.TemporaryDirectory(prefix="sc-policy-")
        snapshot_root = Path(temp_dir.name)
        try:
            digest = self._scan_policy_dir(root, mirror_root=snapshot_root)
        except Exception:
            temp_dir.cleanup()
            raise
        return _PolicySnapshot(digest=digest, snapshot_root=snapshot_root, cleanup_handle=temp_dir)

    def _scan_policy_dir(self, root: Path, *, mirror_root: Path | None = None) -> str:
        if root.is_symlink():
            raise ValueError("policy directory root is symlink")
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise NotADirectoryError("policy directory is not a directory")

        records: list[dict[str, Any]] = []
        total_bytes = 0
        file_count = 0
        for path in sorted(resolved_root.rglob("*"), key=lambda p: p.as_posix()):
            if path.is_symlink():
                raise ValueError("policy directory contains symlink")
            if path.is_dir():
                continue
            entry_stat = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(entry_stat.st_mode):
                raise ValueError("policy directory contains non-regular file")
            file_count += 1
            if file_count > MAX_OPA_POLICY_FILES:
                raise ValueError("policy directory exceeds file count cap")

            real = path.resolve(strict=True)
            try:
                real.relative_to(resolved_root)
            except ValueError as exc:
                raise ValueError("policy directory path escape detected") from exc

            rel = str(path.relative_to(resolved_root)).replace(os.sep, "/")
            if entry_stat.st_size > MAX_OPA_POLICY_FILE_BYTES:
                raise ValueError("policy file exceeds max size")

            hasher = hashlib.sha256()
            read_bytes = 0
            mirror_file: Any = None
            if mirror_root is not None:
                mirror_path = mirror_root / rel
                mirror_path.parent.mkdir(parents=True, exist_ok=True)
                mirror_file = mirror_path.open("wb")
            with path.open("rb") as fh:
                try:
                    while True:
                        chunk = fh.read(64 * 1024)
                        if not chunk:
                            break
                        read_bytes += len(chunk)
                        if read_bytes > MAX_OPA_POLICY_FILE_BYTES:
                            raise ValueError("policy file exceeds max size")
                        hasher.update(chunk)
                        if mirror_file is not None:
                            mirror_file.write(chunk)
                finally:
                    if mirror_file is not None:
                        mirror_file.close()

            final_stat = path.stat(follow_symlinks=False)
            if (
                final_stat.st_dev != entry_stat.st_dev
                or final_stat.st_ino != entry_stat.st_ino
                or final_stat.st_size != entry_stat.st_size
                or read_bytes != final_stat.st_size
                or final_stat.st_mtime_ns != entry_stat.st_mtime_ns
            ):
                raise ValueError(f"policy file changed during digest: {rel}")

            total_bytes += read_bytes
            if total_bytes > MAX_OPA_POLICY_TOTAL_BYTES:
                raise ValueError("policy directory exceeds total size cap")

            records.append(
                {
                    "path": rel,
                    "sha256": hasher.hexdigest(),
                    "bytes": read_bytes,
                }
            )

        payload = canonical_json({"files": records})
        return hashlib.sha256(payload).hexdigest()

    def _resolve_opa_evaluator_identity(self) -> tuple[Path, str]:
        opa_bin = shutil.which("opa")
        if opa_bin is None:
            raise FileNotFoundError("opa binary not found")
        binary = Path(opa_bin).resolve(strict=True)
        return binary, self._opa_evaluator_identity(binary)

    def _opa_evaluator_identity(self, binary: Path) -> str:
        entry_stat = binary.stat()
        if not stat.S_ISREG(entry_stat.st_mode):
            raise ValueError("opa binary is not a regular file")
        if entry_stat.st_size > MAX_OPA_EVALUATOR_BYTES:
            raise ValueError("opa binary exceeds max size")
        hasher = hashlib.sha256()
        read_bytes = 0
        with binary.open("rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                read_bytes += len(chunk)
                if read_bytes > MAX_OPA_EVALUATOR_BYTES:
                    raise ValueError("opa binary exceeds max size")
                hasher.update(chunk)
        final_stat = binary.stat()
        if (
            final_stat.st_dev != entry_stat.st_dev
            or final_stat.st_ino != entry_stat.st_ino
            or final_stat.st_size != entry_stat.st_size
            or final_stat.st_mtime_ns != entry_stat.st_mtime_ns
        ):
            raise ValueError("opa binary changed during identity hash")
        return f"sha256:{hasher.hexdigest()}:bytes:{read_bytes}"

    def _classify_opa_evaluator_failure(self, exc: Exception) -> tuple[PolicyDecisionClass, str]:
        if isinstance(exc, FileNotFoundError):
            return (PolicyDecisionClass.POLICY_UNAVAILABLE, "OPA_BINARY_MISSING")
        if isinstance(exc, PermissionError):
            return (PolicyDecisionClass.POLICY_INFRA_FAILURE, "OPA_BINARY_UNREADABLE")
        if isinstance(exc, ValueError):
            return (
                PolicyDecisionClass.POLICY_INFRA_FAILURE,
                f"OPA_EVALUATOR_INVALID:{self._bounded_text(str(exc))}",
            )
        return (PolicyDecisionClass.POLICY_INFRA_FAILURE, "OPA_EVALUATOR_ERROR")

    def _classify_policy_dir_failure(self, exc: Exception) -> tuple[PolicyDecisionClass, str]:
        if isinstance(exc, FileNotFoundError):
            return (PolicyDecisionClass.POLICY_UNAVAILABLE, "OPA_POLICY_DIR_MISSING")
        if isinstance(exc, PermissionError):
            return (PolicyDecisionClass.POLICY_INFRA_FAILURE, "OPA_POLICY_DIR_UNREADABLE")
        if isinstance(exc, NotADirectoryError):
            return (PolicyDecisionClass.POLICY_INFRA_FAILURE, "OPA_POLICY_DIR_INVALID_TYPE")
        if isinstance(exc, ValueError):
            return (
                PolicyDecisionClass.POLICY_INFRA_FAILURE,
                f"OPA_POLICY_DIR_INVALID:{self._bounded_text(str(exc))}",
            )
        return (PolicyDecisionClass.POLICY_INFRA_FAILURE, "OPA_POLICY_DIR_ERROR")

    def _coerce_context(self, request: Dict[str, Any]) -> PolicyExecutionContext:
        if request.get("context_version") == POLICY_CONTEXT_VERSION and "requested_tool" in request:
            return self.build_execution_context(
                trace_id=str(request.get("trace_id", "")),
                session_id=str(request.get("session_id", "")),
                correlation_id=str(request.get("correlation_id", "")),
                principal_identity=str(request.get("principal_identity", "unset")),
                principal_scopes=list(request.get("principal_scopes", []) or []),
                policy_profile=str(request.get("policy_profile", self._profile.value)),
                lane=str(request.get("lane", "default")),
                drift_value=float(request.get("drift_value", 0.0)),
                drift_components=dict(request.get("drift_components", {"scalar": 0.0})),
                requested_tool=str(request.get("requested_tool", "")),
                tool_id=str(request.get("tool_id", "")),
                tool_contract_hash=str(request.get("tool_contract_hash", "")),
                tool_risk_class=str(request.get("tool_risk_class", "unknown")),
                tool_capabilities=list(request.get("tool_capabilities", []) or []),
                config_identity_hash=str(request.get("config_identity_hash", "")),
                runtime_identity=str(request.get("runtime_identity", "")),
                provider_identity=str(request.get("provider_identity", "")),
                fallback_identity=str(request.get("fallback_identity", "")),
                budget_state=dict(request.get("budget_state", {}) or {}),
                resource_state=dict(request.get("resource_state", {}) or {}),
                execution_intent_id=str(request.get("execution_intent_id", "")),
                approval_correlation_id=str(request.get("approval_correlation_id", "")),
                remaining_deadline_ms=int(
                    request.get("remaining_deadline_ms", self.opa_timeout_ms)
                ),
                action_count=int(request.get("action_count", 0)),
                step_index=int(request.get("step_index", 0)),
                request_payload_bytes=int(request.get("request_payload_bytes", 0)),
                model_claims=dict(request.get("model_claims", {}) or {}),
            )

        payload_size = len(canonical_json(request))
        drift = request.get("drift", self._current_drift)
        try:
            drift_value = float(drift)
        except Exception:
            drift_value = 0.0

        return self.build_execution_context(
            trace_id=str(request.get("trace_id", "")),
            session_id=str(request.get("session_id", "")),
            correlation_id=str(request.get("correlation_id", "")),
            principal_identity="legacy",
            principal_scopes=[],
            policy_profile=self._profile.value,
            lane="legacy",
            drift_value=drift_value if math.isfinite(drift_value) else 0.0,
            drift_components={"scalar": drift_value if math.isfinite(drift_value) else 0.0},
            requested_tool=str(request.get("tool", "")),
            tool_id=str(request.get("tool", "")),
            tool_contract_hash=str(request.get("tool_contract_hash", "")),
            tool_risk_class=str(request.get("tool_risk_class", "unknown")),
            tool_capabilities=list(request.get("tool_capabilities", []) or []),
            config_identity_hash=str(request.get("config_identity_hash", "legacy")),
            runtime_identity="legacy-runtime",
            provider_identity=str(request.get("agent_id", "")),
            fallback_identity="",
            budget_state={},
            resource_state={},
            execution_intent_id="",
            approval_correlation_id="",
            remaining_deadline_ms=self.opa_timeout_ms,
            action_count=int(request.get("tool_call_count", 0) or 0),
            step_index=0,
            request_payload_bytes=payload_size,
            model_claims={
                "caller_trace_id": request.get("trace_id"),
                "caller_correlation_id": request.get("correlation_id"),
            },
        )

    def _resolve_profile(self, profile: str) -> PolicyProfile:
        for candidate in PolicyProfile:
            if candidate.value == profile:
                return candidate
        return self._profile

    def _sanitize_string_list(self, values: Sequence[Any], max_items: int) -> List[str]:
        output: List[str] = []
        for item in values:
            if len(output) >= max_items:
                break
            if not isinstance(item, str):
                continue
            text = self._bounded_text(item)
            if text:
                output.append(text)
        return output

    def _bounded_text(self, value: Any) -> str:
        text = str(value).strip()
        if len(text.encode("utf-8")) <= MAX_POLICY_TEXT_BYTES:
            return text
        encoded = text.encode("utf-8")[:MAX_POLICY_TEXT_BYTES]
        return encoded.decode("utf-8", errors="ignore")

    def _sanitize_json_map(self, value: Mapping[str, Any], *, max_depth: int) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            if len(sanitized) >= MAX_LIST_ITEMS:
                break
            key = self._bounded_text(k)
            if not key:
                continue
            sanitized[key] = self._sanitize_json_value(v, depth=0, max_depth=max_depth)
        return sanitized

    def _sanitize_json_value(self, value: Any, *, depth: int, max_depth: int) -> Any:
        if depth > max_depth:
            raise ValueError("policy context exceeds max depth")
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("policy context requires finite numbers")
            return value
        if isinstance(value, str):
            return self._bounded_text(value)
        if isinstance(value, list):
            out_list: list[Any] = []
            for item in value[:MAX_LIST_ITEMS]:
                out_list.append(
                    self._sanitize_json_value(item, depth=depth + 1, max_depth=max_depth)
                )
            return out_list
        if isinstance(value, tuple):
            out_tuple_list: list[Any] = []
            for item in value[:MAX_LIST_ITEMS]:
                out_tuple_list.append(
                    self._sanitize_json_value(item, depth=depth + 1, max_depth=max_depth)
                )
            return out_tuple_list
        if isinstance(value, dict):
            out_dict: dict[str, Any] = {}
            for idx, (k, v) in enumerate(value.items()):
                if idx >= MAX_LIST_ITEMS:
                    break
                key = self._bounded_text(k)
                if not key:
                    continue
                out_dict[key] = self._sanitize_json_value(v, depth=depth + 1, max_depth=max_depth)
            return out_dict
        raise ValueError(f"unsupported policy context value type: {type(value).__name__}")

    def _extract_opa_result(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("OPA_RESULT_MALFORMED")
        result = payload.get("result")
        if not isinstance(result, list) or not result:
            raise ValueError("OPA_RESULT_EMPTY")
        first = result[0]
        if not isinstance(first, dict):
            raise ValueError("OPA_RESULT_MALFORMED")
        expressions = first.get("expressions")
        if not isinstance(expressions, list) or not expressions:
            raise ValueError("OPA_EXPRESSIONS_EMPTY")
        expression0 = expressions[0]
        if not isinstance(expression0, dict):
            raise ValueError("OPA_VALUE_MISSING")
        if "value" not in expression0:
            raise ValueError("OPA_VALUE_MISSING")
        value = expression0["value"]
        if not isinstance(value, dict):
            raise ValueError("OPA_VALUE_NOT_OBJECT")

        if "allow" not in value:
            raise ValueError("OPA_ALLOW_MISSING")
        allow = value["allow"]
        if not isinstance(allow, bool):
            raise ValueError("OPA_ALLOW_NOT_BOOL")

        deny = value.get("deny", [])
        matched = value.get("matched", [])
        if not isinstance(deny, list) or not isinstance(matched, list):
            raise ValueError("OPA_LIST_TYPE_INVALID")

        if any(not isinstance(item, str) for item in deny):
            raise ValueError("OPA_LIST_TYPE_INVALID")
        if any(not isinstance(item, str) for item in matched):
            raise ValueError("OPA_LIST_TYPE_INVALID")

        deny_sanitized = self._sanitize_string_list(deny, MAX_POLICY_REASON_COUNT)
        matched_sanitized = self._sanitize_string_list(matched, MAX_POLICY_MATCHED_COUNT)

        return {"allow": allow, "deny": deny_sanitized, "matched": matched_sanitized}

    def _opa_failure(
        self,
        decision_class: PolicyDecisionClass,
        code: str,
    ) -> _ExternalDecision:
        reason = self._bounded_text(code)
        if self.opa_mode == OpaMode.AUTHORITATIVE:
            return _ExternalDecision(
                decision_class=decision_class,
                reasons=[reason],
                matched=["opa.unavailable"],
                status="unavailable",
                metadata={"opa": {"mode": self.opa_mode.value, "failure": reason}},
            )
        return _ExternalDecision(
            decision_class=PolicyDecisionClass.ALLOW,
            reasons=[f"advisory:{reason}"],
            matched=["opa.advisory_unavailable"],
            status="advisory-unavailable",
            metadata={"opa": {"mode": self.opa_mode.value, "failure": reason}},
        )

    def _run_bounded_subprocess(
        self,
        *,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        stdin_data: bytes,
        timeout_ms: int,
        max_stdout: int,
        max_stderr: int,
    ) -> dict[str, Any]:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            cwd=str(cwd),
            env=env,
        )

        stdout_cap = _StreamCapture()
        stderr_cap = _StreamCapture()

        def _reader(stream: Any, cap: _StreamCapture, cap_bytes: int) -> None:
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    remaining = cap_bytes - len(cap.data)
                    if remaining <= 0:
                        cap.overflowed = True
                        cap.overflow_event.set()
                        continue
                    cap.data.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        cap.overflowed = True
                        cap.overflow_event.set()
                        continue
            finally:
                try:
                    stream.close()
                except Exception:
                    return

        out_t = threading.Thread(
            target=_reader, args=(proc.stdout, stdout_cap, max_stdout), daemon=True
        )
        err_t = threading.Thread(
            target=_reader, args=(proc.stderr, stderr_cap, max_stderr), daemon=True
        )
        out_t.start()
        err_t.start()

        timed_out = False
        stdin = proc.stdin
        if stdin is None:
            try:
                proc.kill()
            except Exception:
                pass
            return {
                "returncode": -1,
                "stdout": b"",
                "stderr": b"",
                "stdout_overflow": False,
                "stderr_overflow": False,
                "timed_out": False,
                "stdin_error": True,
            }
        stdin_error = threading.Event()

        def _writer() -> None:
            try:
                view = memoryview(stdin_data)
                offset = 0
                while offset < len(view):
                    written = stdin.write(view[offset : offset + OPA_STDIN_WRITE_CHUNK_BYTES])
                    if written is None or written <= 0:
                        raise BrokenPipeError("stdin write made no progress")
                    offset += written
                stdin.flush()
            except Exception:
                stdin_error.set()
            finally:
                try:
                    stdin.close()
                except Exception:
                    pass

        write_t = threading.Thread(target=_writer, daemon=True)
        write_t.start()

        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while proc.poll() is None:
            if stdout_cap.overflow_event.is_set() or stderr_cap.overflow_event.is_set():
                proc.kill()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                proc.kill()
                try:
                    stdin.close()
                except Exception:
                    pass
                break
            time.sleep(0.005)

        try:
            proc.wait(timeout=1)
        except Exception:
            proc.kill()

        out_t.join(timeout=1)
        err_t.join(timeout=1)
        write_t.join(timeout=0.05 if timed_out else min(1.0, max(0.2, timeout_ms / 1000.0)))
        if write_t.is_alive():
            if timed_out:
                stdin_error.set()
            else:
                try:
                    stdin.close()
                except Exception:
                    pass
                write_t.join(timeout=0.1)
                if write_t.is_alive():
                    stdin_error.set()

        return {
            "returncode": int(proc.returncode if proc.returncode is not None else -1),
            "stdout": bytes(stdout_cap.data),
            "stderr": bytes(stderr_cap.data),
            "stdout_overflow": stdout_cap.overflowed,
            "stderr_overflow": stderr_cap.overflowed,
            "timed_out": timed_out,
            "stdin_error": stdin_error.is_set(),
        }
