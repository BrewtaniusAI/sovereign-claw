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
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Collection, Dict, Iterable, List, Mapping, Optional, Sequence

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
MAX_OPA_POLICY_DIR_ENTRIES = 1024
MAX_OPA_POLICY_TOTAL_ENTRIES = 4096
MAX_OPA_POLICY_DIRECTORIES = 2048
MAX_OPA_POLICY_PATH_DEPTH = 32
MAX_OPA_POLICY_PATH_BYTES = 1024
MAX_OPA_EVALUATOR_BYTES = 128 * 1024 * 1024
MAX_LOCAL_EVALUATOR_BYTES = 4 * 1024 * 1024
DEFAULT_OPA_TIMEOUT_MS = 750
DEFAULT_OPA_INPUT_MAX_BYTES = 128 * 1024
DEFAULT_OPA_STDOUT_MAX_BYTES = 64 * 1024
DEFAULT_OPA_STDERR_MAX_BYTES = 8 * 1024
MAX_LIST_ITEMS = 64
OPA_STDIN_WRITE_CHUNK_BYTES = 4096
MAX_POLICY_CONTEXT_FREEZE_DEPTH = 8
MAX_POLICY_CONTEXT_FREEZE_NODES = 4096
MAX_POLICY_INTEGER_VALUE = 10_000_000_000


def _bounded_text_value(value: Any) -> str:
    text = str(value).strip()
    if len(text.encode("utf-8")) <= MAX_POLICY_TEXT_BYTES:
        return text
    encoded = text.encode("utf-8")[:MAX_POLICY_TEXT_BYTES]
    return encoded.decode("utf-8", errors="ignore")


def _validate_policy_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = value.strip()
    if len(text.encode("utf-8")) > MAX_POLICY_TEXT_BYTES:
        raise ValueError(f"{field_name} exceeds {MAX_POLICY_TEXT_BYTES} bytes")
    return text


def _validate_finite_real(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite real number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _validate_non_negative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if value > MAX_POLICY_INTEGER_VALUE:
        raise ValueError(f"{field_name} exceeds max value {MAX_POLICY_INTEGER_VALUE}")
    return value


def _deep_freeze_json(
    value: Any,
    *,
    max_depth: int = MAX_POLICY_CONTEXT_FREEZE_DEPTH,
    max_nodes: int = MAX_POLICY_CONTEXT_FREEZE_NODES,
    _depth: int = 0,
    _seen: set[int] | None = None,
    _state: dict[str, int] | None = None,
) -> Any:
    if _depth > max_depth:
        raise ValueError("policy context exceeds max depth")
    if _state is None:
        _state = {"nodes": 0}
    _state["nodes"] += 1
    if _state["nodes"] > max_nodes:
        raise ValueError("policy context exceeds max node count")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("policy context requires finite numbers")
        return value
    if _seen is None:
        _seen = set()
    if isinstance(value, tuple):
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError("policy context list exceeds maximum size")
        value_id = id(value)
        if value_id in _seen:
            raise ValueError("policy context contains cycle")
        _seen.add(value_id)
        try:
            return tuple(
                _deep_freeze_json(
                    item,
                    max_depth=max_depth,
                    max_nodes=max_nodes,
                    _depth=_depth + 1,
                    _seen=_seen,
                    _state=_state,
                )
                for item in value
            )
        finally:
            _seen.remove(value_id)
    if isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError("policy context list exceeds maximum size")
        value_id = id(value)
        if value_id in _seen:
            raise ValueError("policy context contains cycle")
        _seen.add(value_id)
        try:
            return tuple(
                _deep_freeze_json(
                    item,
                    max_depth=max_depth,
                    max_nodes=max_nodes,
                    _depth=_depth + 1,
                    _seen=_seen,
                    _state=_state,
                )
                for item in value
            )
        finally:
            _seen.remove(value_id)
    if isinstance(value, Mapping):
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError("policy context mapping exceeds maximum size")
        value_id = id(value)
        if value_id in _seen:
            raise ValueError("policy context contains cycle")
        _seen.add(value_id)
        frozen: dict[str, Any] = {}
        normalized_items: list[tuple[str, Any]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("policy context mapping keys must be strings")
            if len(key.encode("utf-8")) > MAX_POLICY_TEXT_BYTES:
                raise ValueError("policy context mapping key exceeds max length")
            normalized_items.append((key, item))
        try:
            for key, item in sorted(normalized_items, key=lambda kv: kv[0]):
                frozen[key] = _deep_freeze_json(
                    item,
                    max_depth=max_depth,
                    max_nodes=max_nodes,
                    _depth=_depth + 1,
                    _seen=_seen,
                    _state=_state,
                )
            return MappingProxyType(frozen)
        finally:
            _seen.remove(value_id)
    raise ValueError(f"unsupported policy context value type: {type(value).__name__}")


def _deep_thaw_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, tuple):
        return [_deep_thaw_json(item) for item in value]
    if isinstance(value, Mapping):
        return {str(k): _deep_thaw_json(v) for k, v in value.items()}
    raise ValueError(f"unsupported frozen policy context value type: {type(value).__name__}")


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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "drift_value",
            _validate_finite_real(self.drift_value, field_name="drift_value"),
        )

        for field_name in (
            "context_version",
            "trace_id",
            "session_id",
            "correlation_id",
            "principal_identity",
            "policy_profile",
            "lane",
            "requested_tool",
            "tool_id",
            "tool_contract_hash",
            "tool_risk_class",
            "config_identity_hash",
            "runtime_identity",
            "provider_identity",
            "fallback_identity",
            "execution_intent_id",
            "approval_correlation_id",
        ):
            value = getattr(self, field_name)
            object.__setattr__(
                self, field_name, _validate_policy_text(value, field_name=field_name)
            )

        if self.context_version != POLICY_CONTEXT_VERSION:
            raise ValueError(f"context_version must equal {POLICY_CONTEXT_VERSION}")
        if self.policy_profile not in {profile.value for profile in PolicyProfile}:
            raise ValueError("policy_profile must be one of strict, balanced, exploratory")

        if not isinstance(self.drift_components, Mapping):
            raise ValueError("drift_components must be a mapping")
        if len(self.drift_components) > MAX_LIST_ITEMS:
            raise ValueError("drift_components exceeds maximum size")
        normalized_components: dict[str, float] = {}
        for key, value in self.drift_components.items():
            if not isinstance(key, str):
                raise ValueError("drift_components keys must be strings")
            if len(key.encode("utf-8")) > MAX_POLICY_TEXT_BYTES:
                raise ValueError("drift_components key exceeds max length")
            fv = _validate_finite_real(value, field_name="drift_components values")
            normalized_components[key] = fv
        if not normalized_components:
            normalized_components = {"scalar": self.drift_value}

        if isinstance(self.principal_scopes, (str, bytes)) or not isinstance(
            self.principal_scopes, Collection
        ):
            raise ValueError("principal_scopes must be a collection")
        if len(self.principal_scopes) > MAX_LIST_ITEMS:
            raise ValueError("principal_scopes exceeds maximum size")
        normalized_scopes: list[str] = []
        for scope in self.principal_scopes:
            if not isinstance(scope, str):
                raise ValueError("principal_scopes values must be strings")
            text = _validate_policy_text(scope, field_name="principal_scopes value")
            if text:
                normalized_scopes.append(text)

        if isinstance(self.tool_capabilities, (str, bytes)) or not isinstance(
            self.tool_capabilities, Collection
        ):
            raise ValueError("tool_capabilities must be a collection")
        if len(self.tool_capabilities) > MAX_LIST_ITEMS:
            raise ValueError("tool_capabilities exceeds maximum size")
        normalized_capabilities: list[str] = []
        for capability in self.tool_capabilities:
            if not isinstance(capability, str):
                raise ValueError("tool_capabilities values must be strings")
            text = _validate_policy_text(capability, field_name="tool_capabilities value")
            if text:
                normalized_capabilities.append(text)

        object.__setattr__(
            self,
            "remaining_deadline_ms",
            _validate_non_negative_int(
                self.remaining_deadline_ms, field_name="remaining_deadline_ms"
            ),
        )
        object.__setattr__(
            self,
            "action_count",
            _validate_non_negative_int(self.action_count, field_name="action_count"),
        )
        object.__setattr__(
            self,
            "step_index",
            _validate_non_negative_int(self.step_index, field_name="step_index"),
        )
        object.__setattr__(
            self,
            "request_payload_bytes",
            _validate_non_negative_int(
                self.request_payload_bytes, field_name="request_payload_bytes"
            ),
        )

        object.__setattr__(
            self,
            "drift_components",
            MappingProxyType(dict(sorted(normalized_components.items()))),
        )
        object.__setattr__(self, "principal_scopes", tuple(sorted(set(normalized_scopes))))
        object.__setattr__(self, "tool_capabilities", tuple(sorted(set(normalized_capabilities))))
        object.__setattr__(self, "budget_state", _deep_freeze_json(self.budget_state))
        object.__setattr__(self, "resource_state", _deep_freeze_json(self.resource_state))
        object.__setattr__(self, "model_claims", _deep_freeze_json(self.model_claims))

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
            "budget_state": _deep_thaw_json(self.budget_state),
            "resource_state": _deep_thaw_json(self.resource_state),
            "execution_intent_id": self.execution_intent_id,
            "approval_correlation_id": self.approval_correlation_id,
            "remaining_deadline_ms": self.remaining_deadline_ms,
            "action_count": self.action_count,
            "step_index": self.step_index,
            "request_payload_bytes": self.request_payload_bytes,
            "model_claims": _deep_thaw_json(self.model_claims),
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
    opa_runner_config_identity: str
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
                "opa_runner_config_identity": self.opa_runner_config_identity,
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
    opa_evaluator_snapshot: "_EvaluatorSnapshot | None"
    opa_evaluator_identity: str

    def cleanup(self) -> None:
        if self.policy_snapshot is not None:
            self.policy_snapshot.cleanup()
        if self.opa_evaluator_snapshot is not None:
            self.opa_evaluator_snapshot.cleanup()


@dataclass
class _StreamCapture:
    data: bytearray = field(default_factory=bytearray)
    overflowed: bool = False
    overflow_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class _EvaluatorSnapshot:
    binary_path: Path
    identity: str
    cleanup_handle: tempfile.TemporaryDirectory[str] | None = None

    def cleanup(self) -> None:
        if self.cleanup_handle is None:
            return
        self.cleanup_handle.cleanup()
        self.cleanup_handle = None


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
            raise ValueError(
                "LEARNED_SIGNAL_MODE_UNSUPPORTED: authoritative requires persisted root"
            )
        if mode not in {"advisory", "disabled"}:
            raise ValueError(f"LEARNED_SIGNAL_MODE_INVALID:{self._bounded_text(mode)}")
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
        drift = _validate_finite_real(drift_value, field_name="drift_value")

        sanitized_components: dict[str, float] = {}
        for key, value in sorted(drift_components.items()):
            if not isinstance(key, str):
                raise ValueError("drift_components keys must be strings")
            k = key.strip()
            if not k:
                continue
            if len(k.encode("utf-8")) > MAX_POLICY_TEXT_BYTES:
                raise ValueError("drift_components key exceeds max length")
            fv = _validate_finite_real(value, field_name="drift_components values")
            sanitized_components[k] = fv

        if not sanitized_components:
            sanitized_components = {"scalar": drift}

        normalized_scopes: list[str] = []
        for scope in principal_scopes:
            if not isinstance(scope, str):
                raise ValueError("principal_scopes values must be strings")
            normalized_scope = _validate_policy_text(scope, field_name="principal_scopes value")
            if normalized_scope:
                normalized_scopes.append(normalized_scope)

        normalized_capabilities: list[str] = []
        for capability in tool_capabilities:
            if not isinstance(capability, str):
                raise ValueError("tool_capabilities values must be strings")
            normalized_capability = _validate_policy_text(
                capability, field_name="tool_capabilities value"
            )
            if normalized_capability:
                normalized_capabilities.append(normalized_capability)

        return PolicyExecutionContext(
            context_version=POLICY_CONTEXT_VERSION,
            trace_id=self._authority_text(trace_id, field_name="trace_id"),
            session_id=self._authority_text(session_id, field_name="session_id"),
            correlation_id=self._authority_text(correlation_id, field_name="correlation_id"),
            principal_identity=self._authority_text(
                principal_identity or "unset", field_name="principal_identity"
            ),
            principal_scopes=tuple(sorted(normalized_scopes)),
            policy_profile=self._authority_text(policy_profile, field_name="policy_profile"),
            lane=self._authority_text(lane, field_name="lane"),
            drift_value=drift,
            drift_components=sanitized_components,
            requested_tool=self._authority_text(requested_tool, field_name="requested_tool"),
            tool_id=self._authority_text(tool_id, field_name="tool_id"),
            tool_contract_hash=self._authority_text(
                tool_contract_hash, field_name="tool_contract_hash"
            ),
            tool_risk_class=self._authority_text(tool_risk_class, field_name="tool_risk_class"),
            tool_capabilities=tuple(sorted(normalized_capabilities)),
            config_identity_hash=self._authority_text(
                config_identity_hash, field_name="config_identity_hash"
            ),
            runtime_identity=self._authority_text(runtime_identity, field_name="runtime_identity"),
            provider_identity=self._authority_text(
                provider_identity, field_name="provider_identity"
            ),
            fallback_identity=self._authority_text(
                fallback_identity, field_name="fallback_identity"
            ),
            budget_state=self._sanitize_json_map(dict(budget_state), max_depth=4),
            resource_state=self._sanitize_json_map(dict(resource_state), max_depth=4),
            execution_intent_id=self._authority_text(
                execution_intent_id, field_name="execution_intent_id"
            ),
            approval_correlation_id=self._authority_text(
                approval_correlation_id, field_name="approval_correlation_id"
            ),
            remaining_deadline_ms=_validate_non_negative_int(
                remaining_deadline_ms, field_name="remaining_deadline_ms"
            ),
            action_count=_validate_non_negative_int(action_count, field_name="action_count"),
            step_index=_validate_non_negative_int(step_index, field_name="step_index"),
            request_payload_bytes=_validate_non_negative_int(
                request_payload_bytes, field_name="request_payload_bytes"
            ),
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

            bundle_guard_failure = bool(bound_policy_bundle_hash) and not hmac.compare_digest(
                bound_policy_bundle_hash or "", bundle_hash
            )
            bundle_failure_fatal = False
            if bundle_guard_failure:
                external = _ExternalDecision(
                    decision_class=PolicyDecisionClass.POLICY_INFRA_FAILURE,
                    reasons=["POLICY_BUNDLE_HASH_MISMATCH"],
                    matched=["policy.bundle_drift"],
                    status="unavailable",
                    metadata={
                        "policy": {
                            "bundle_failure": "POLICY_BUNDLE_HASH_MISMATCH",
                            "expected_policy_bundle_hash": bound_policy_bundle_hash,
                            "evaluated_policy_bundle_hash": bundle_hash,
                        }
                    },
                )
            elif bundle_failure is not None:
                bundle_failure_fatal = (
                    self.opa_mode == OpaMode.DISABLED
                    or bundle_failure.code.startswith("LOCAL_POLICY_IDENTITY_ERROR:")
                )
                if bundle_failure_fatal:
                    external = _ExternalDecision(
                        decision_class=PolicyDecisionClass.POLICY_INFRA_FAILURE,
                        reasons=[bundle_failure.code],
                        matched=["policy.bundle_identity"],
                        status="unavailable",
                        metadata={"policy": {"bundle_failure": bundle_failure.code}},
                    )
                else:
                    external = self._opa_failure(bundle_failure.decision_class, bundle_failure.code)
            else:
                external = self._evaluate_with_opa_context(
                    context,
                    policy_snapshot=prepared.policy_snapshot,
                    opa_evaluator_snapshot=prepared.opa_evaluator_snapshot,
                    expected_opa_evaluator_identity=prepared.opa_evaluator_identity,
                )
            reasons.extend(external.reasons)
            matched.extend(external.matched)

            if bundle_guard_failure or bundle_failure_fatal:
                final_allowed = False
                final_class = external.decision_class
            elif local_denied:
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
                    "profile": bundle.profile,
                    "local_rules_hash": bundle.local_rules_hash,
                    "opa_mode": bundle.opa_mode,
                    "opa_query": bundle.opa_query,
                    "opa_policy_digest": bundle.opa_policy_digest,
                    "opa_evaluator_identity": bundle.opa_evaluator_identity,
                    "opa_runner_config_identity": bundle.opa_runner_config_identity,
                    "guardrail_bundle_identity": bundle.guardrail_bundle_identity,
                    "learned_signal_mode": bundle.learned_signal_mode,
                    "learned_signal_root": bundle.learned_signal_root,
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
        opa_evaluator_snapshot: _EvaluatorSnapshot | None = None,
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
        owns_evaluator_snapshot = False
        if policy_snapshot is None:
            try:
                policy_snapshot = self._snapshot_policy_dir(self.rego_policy_dir)
                owns_snapshot = True
            except Exception as exc:
                decision_class, code = self._classify_policy_dir_failure(exc)
                return self._opa_failure(decision_class, code)

        try:
            if opa_evaluator_snapshot is None:
                try:
                    opa_evaluator_snapshot = self._snapshot_opa_evaluator()
                    owns_evaluator_snapshot = True
                except Exception as exc:
                    decision_class, code = self._classify_opa_evaluator_failure(exc)
                    return self._opa_failure(decision_class, code)
            if expected_opa_evaluator_identity:
                if not hmac.compare_digest(
                    opa_evaluator_snapshot.identity, expected_opa_evaluator_identity
                ):
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
            if context.remaining_deadline_ms <= 0:
                return self._opa_failure(
                    PolicyDecisionClass.POLICY_UNAVAILABLE,
                    "OPA_DEADLINE_EXHAUSTED",
                )

            timeout_ms = min(self.opa_timeout_ms, context.remaining_deadline_ms)
            assert opa_evaluator_snapshot is not None
            started_at = time.monotonic()

            cmd = [
                str(opa_evaluator_snapshot.binary_path),
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
            duration_ms = max(0, int((time.monotonic() - started_at) * 1000))
        finally:
            if owns_snapshot and policy_snapshot is not None:
                policy_snapshot.cleanup()
            if owns_evaluator_snapshot and opa_evaluator_snapshot is not None:
                opa_evaluator_snapshot.cleanup()

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
        if run.get("reader_error"):
            return self._opa_failure(
                PolicyDecisionClass.POLICY_INFRA_FAILURE,
                "OPA_SUBPROCESS_ERROR",
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
        opa_metadata = {
            "mode": self.opa_mode.value,
            "timeout_ms": timeout_ms,
            "stdout_bytes": len(run["stdout"]),
            "stderr_bytes": len(run["stderr"]),
            "duration_ms": duration_ms,
            "stdout_sha256": hashlib.sha256(run["stdout"]).hexdigest(),
            "stderr_sha256": hashlib.sha256(run["stderr"]).hexdigest(),
        }

        if allow:
            return _ExternalDecision(
                decision_class=PolicyDecisionClass.ALLOW,
                reasons=[],
                matched=matched,
                status="allow",
                metadata={"opa": opa_metadata},
            )

        deny_reasons = deny if deny else ["OPA_DENY_NO_REASON"]
        return _ExternalDecision(
            decision_class=PolicyDecisionClass.POLICY_DENY,
            reasons=deny_reasons,
            matched=matched,
            status="deny",
            metadata={"opa": opa_metadata},
        )

    def _build_policy_bundle_identity(
        self,
        profile: str,
        *,
        for_evaluation: bool = False,
    ) -> _PreparedBundle:
        failure: _BundleIdentityFailure | None = None
        local_hash = ""
        try:
            local_hash = self._local_rules_hash(profile)
        except Exception as exc:
            failure = _BundleIdentityFailure(
                decision_class=PolicyDecisionClass.POLICY_INFRA_FAILURE,
                code=f"LOCAL_POLICY_IDENTITY_ERROR:{self._bounded_text(str(exc))}",
            )
            local_hash = f"failure:{failure.decision_class.value}:{failure.code}"

        opa_policy_digest = "disabled"
        opa_evaluator_identity = "disabled"
        opa_runner_config_identity = "disabled"
        policy_snapshot: _PolicySnapshot | None = None
        opa_evaluator_snapshot: _EvaluatorSnapshot | None = None
        if self.opa_mode != OpaMode.DISABLED:
            opa_runner_config_identity = self._opa_runner_config_identity()
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
                    if for_evaluation:
                        opa_evaluator_snapshot = self._snapshot_opa_evaluator()
                        opa_evaluator_identity = opa_evaluator_snapshot.identity
                    else:
                        _, opa_evaluator_identity = self._resolve_opa_evaluator_identity()
                except Exception as exc:
                    decision_class, code = self._classify_opa_evaluator_failure(exc)
                    failure = _BundleIdentityFailure(decision_class=decision_class, code=code)
                    opa_policy_digest = f"failure:{decision_class.value}:{code}"
                    opa_evaluator_identity = f"failure:{decision_class.value}:{code}"
                    opa_runner_config_identity = f"failure:{decision_class.value}:{code}"

        if failure is not None:
            if opa_evaluator_snapshot is not None:
                opa_evaluator_snapshot.cleanup()
            opa_evaluator_snapshot = None

        return _PreparedBundle(
            bundle=PolicyBundleIdentity(
                evaluator_version=self.evaluator_version,
                profile=profile,
                local_rules_hash=local_hash,
                opa_mode=self.opa_mode.value,
                opa_query=self.opa_query,
                opa_policy_digest=opa_policy_digest,
                opa_evaluator_identity=opa_evaluator_identity,
                opa_runner_config_identity=opa_runner_config_identity,
                guardrail_bundle_identity=self.guardrail_bundle_identity,
                learned_signal_mode=self.learned_signal_mode,
                learned_signal_root="none",
            ),
            failure=failure,
            policy_snapshot=policy_snapshot,
            opa_evaluator_snapshot=opa_evaluator_snapshot,
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
        total_entries = 0
        directory_count = 1
        pending_dirs: deque[Path] = deque([resolved_root])
        while pending_dirs:
            current_dir = pending_dirs.popleft()
            child_paths: list[Path] = []
            with os.scandir(current_dir) as it:
                for entry in it:
                    total_entries += 1
                    if total_entries > MAX_OPA_POLICY_TOTAL_ENTRIES:
                        raise ValueError("policy directory exceeds entry cap")
                    child_paths.append(Path(entry.path))
                    if len(child_paths) > MAX_OPA_POLICY_DIR_ENTRIES:
                        raise ValueError("policy directory exceeds per-directory entry cap")

            child_paths.sort(key=lambda p: p.as_posix())
            for path in child_paths:
                entry_stat = path.stat(follow_symlinks=False)
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise ValueError("policy directory contains symlink")

                real = path.resolve(strict=True)
                try:
                    real.relative_to(resolved_root)
                except ValueError as exc:
                    raise ValueError("policy directory path escape detected") from exc

                rel = str(path.relative_to(resolved_root)).replace(os.sep, "/")
                if len(rel.encode("utf-8")) > MAX_OPA_POLICY_PATH_BYTES:
                    raise ValueError("policy directory path exceeds byte cap")
                depth = len(path.relative_to(resolved_root).parts)
                if depth > MAX_OPA_POLICY_PATH_DEPTH:
                    raise ValueError("policy directory path exceeds depth cap")

                if stat.S_ISDIR(entry_stat.st_mode):
                    directory_count += 1
                    if directory_count > MAX_OPA_POLICY_DIRECTORIES:
                        raise ValueError("policy directory exceeds directory cap")
                    pending_dirs.append(path)
                    continue

                if not stat.S_ISREG(entry_stat.st_mode):
                    raise ValueError("policy directory contains non-regular file")
                file_count += 1
                if file_count > MAX_OPA_POLICY_FILES:
                    raise ValueError("policy directory exceeds file count cap")

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

    def _local_evaluator_identity(self) -> str:
        module_path = Path(__file__).resolve(strict=True)
        entry_stat = module_path.stat()
        if not stat.S_ISREG(entry_stat.st_mode):
            raise ValueError("policy engine module is not a regular file")
        if entry_stat.st_size > MAX_LOCAL_EVALUATOR_BYTES:
            raise ValueError("policy engine module exceeds max size")
        hasher = hashlib.sha256()
        read_bytes = 0
        with module_path.open("rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                read_bytes += len(chunk)
                if read_bytes > MAX_LOCAL_EVALUATOR_BYTES:
                    raise ValueError("policy engine module exceeds max size")
                hasher.update(chunk)
        final_stat = module_path.stat()
        if (
            final_stat.st_dev != entry_stat.st_dev
            or final_stat.st_ino != entry_stat.st_ino
            or final_stat.st_size != entry_stat.st_size
            or final_stat.st_mtime_ns != entry_stat.st_mtime_ns
        ):
            raise ValueError("policy engine module changed during identity hash")
        return f"sha256:{hasher.hexdigest()}:bytes:{read_bytes}"

    def _local_rules_hash(self, profile: str) -> str:
        resolved_profile = self._resolve_profile(profile)
        profile_defaults = PROFILE_DEFAULTS[resolved_profile]
        local_material = {
            "forbidden_tools": sorted(self.forbidden_tools),
            "profile": resolved_profile.value,
            "profile_defaults": profile_defaults,
            "max_payload_bytes": self.max_payload_bytes,
            "require_trace_id": self.require_trace_id,
            "learned_signal_mode": self.learned_signal_mode,
            "local_evaluator_identity": self._local_evaluator_identity(),
        }
        return hashlib.sha256(canonical_json(local_material)).hexdigest()

    def _snapshot_opa_evaluator(self) -> _EvaluatorSnapshot:
        binary, _ = self._resolve_opa_evaluator_identity()
        temp_dir = tempfile.TemporaryDirectory(prefix="sc-opa-bin-")
        snapshot_root = Path(temp_dir.name)
        snapshot_binary = snapshot_root / "opa"
        entry_stat = binary.stat()
        if not stat.S_ISREG(entry_stat.st_mode):
            temp_dir.cleanup()
            raise ValueError("opa binary is not a regular file")
        if entry_stat.st_size > MAX_OPA_EVALUATOR_BYTES:
            temp_dir.cleanup()
            raise ValueError("opa binary exceeds max size")

        hasher = hashlib.sha256()
        read_bytes = 0
        try:
            with binary.open("rb") as src, snapshot_binary.open("wb") as dst:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    read_bytes += len(chunk)
                    if read_bytes > MAX_OPA_EVALUATOR_BYTES:
                        raise ValueError("opa binary exceeds max size")
                    hasher.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            os.chmod(snapshot_binary, 0o500)
            final_stat = binary.stat()
            if (
                final_stat.st_dev != entry_stat.st_dev
                or final_stat.st_ino != entry_stat.st_ino
                or final_stat.st_size != entry_stat.st_size
                or final_stat.st_mtime_ns != entry_stat.st_mtime_ns
            ):
                raise ValueError("opa binary changed during identity hash")
            snapshot_stat = snapshot_binary.stat()
            if snapshot_stat.st_size != read_bytes:
                raise ValueError("opa snapshot copy incomplete")
            identity = f"sha256:{hasher.hexdigest()}:bytes:{read_bytes}"
            return _EvaluatorSnapshot(
                binary_path=snapshot_binary,
                identity=identity,
                cleanup_handle=temp_dir,
            )
        except Exception:
            temp_dir.cleanup()
            raise

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
            raise ValueError(
                "authoritative policy context dictionaries are not accepted by evaluate(); use evaluate_context()"
            )

        try:
            payload_size = self.bounded_payload_size(request)
        except Exception:
            payload_size = MAX_POLICY_CONTEXT_BYTES + 1
        drift = request.get("drift", self._current_drift)
        if isinstance(drift, bool) or not isinstance(drift, (int, float)):
            drift_value = self._current_drift
        else:
            drift_value = float(drift)
            if not math.isfinite(drift_value):
                drift_value = self._current_drift
        if not math.isfinite(drift_value):
            drift_value = 0.0

        requested_tool = request.get("tool")
        if not isinstance(requested_tool, str):
            requested_tool = ""

        tool_call_count = request.get("tool_call_count", 0)
        if isinstance(tool_call_count, bool) or not isinstance(tool_call_count, int):
            action_count = 0
        else:
            action_count = max(0, min(tool_call_count, MAX_POLICY_INTEGER_VALUE))

        return self.build_execution_context(
            trace_id="",
            session_id="",
            correlation_id="",
            principal_identity="legacy",
            principal_scopes=[],
            policy_profile=self._profile.value,
            lane="legacy",
            drift_value=drift_value if math.isfinite(drift_value) else 0.0,
            drift_components={"scalar": drift_value if math.isfinite(drift_value) else 0.0},
            requested_tool=requested_tool,
            tool_id=requested_tool,
            tool_contract_hash="legacy-unbound",
            tool_risk_class="unknown",
            tool_capabilities=[],
            config_identity_hash="legacy",
            runtime_identity="legacy-runtime",
            provider_identity="legacy-provider",
            fallback_identity="",
            budget_state={},
            resource_state={},
            execution_intent_id="",
            approval_correlation_id="",
            remaining_deadline_ms=self.opa_timeout_ms,
            action_count=action_count,
            step_index=0,
            request_payload_bytes=payload_size,
            model_claims={
                "caller_trace_id": request.get("trace_id"),
                "caller_session_id": request.get("session_id"),
                "caller_correlation_id": request.get("correlation_id"),
                "caller_drift": request.get("drift"),
                "caller_tool_contract_hash": request.get("tool_contract_hash"),
                "caller_tool_risk_class": request.get("tool_risk_class"),
                "caller_tool_capabilities": request.get("tool_capabilities"),
                "caller_config_identity_hash": request.get("config_identity_hash"),
                "caller_provider_identity": request.get("provider_identity"),
                "caller_fallback_identity": request.get("fallback_identity"),
                "caller_agent_id": request.get("agent_id"),
                "caller_action_count": request.get("tool_call_count"),
            },
        )

    def _resolve_profile(self, profile: str) -> PolicyProfile:
        for candidate in PolicyProfile:
            if candidate.value == profile:
                return candidate
        raise ValueError(f"unsupported policy profile: {self._bounded_text(profile)}")

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
        return _bounded_text_value(value)

    def _authority_text(self, value: Any, *, field_name: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        return _validate_policy_text(value, field_name=field_name)

    def bounded_payload_size(
        self, payload: Any, *, max_depth: int = 8, max_nodes: int = MAX_POLICY_CONTEXT_FREEZE_NODES
    ) -> int:
        def _preflight(value: Any, depth: int, seen: set[int], state: dict[str, int]) -> None:
            if depth > max_depth:
                raise ValueError("policy payload exceeds max depth")
            state["nodes"] += 1
            if state["nodes"] > max_nodes:
                raise ValueError("policy payload exceeds max node count")
            if value is None or isinstance(value, (bool, int)):
                return
            if isinstance(value, float):
                if not math.isfinite(value):
                    raise ValueError("policy payload requires finite numbers")
                return
            if isinstance(value, str):
                if len(value.encode("utf-8")) > MAX_POLICY_CONTEXT_BYTES:
                    raise ValueError("policy payload string exceeds max length")
                return
            if isinstance(value, (list, tuple)):
                if len(value) > MAX_LIST_ITEMS:
                    raise ValueError("policy payload list exceeds max length")
                value_id = id(value)
                if value_id in seen:
                    raise ValueError("policy payload contains cycle")
                seen.add(value_id)
                try:
                    for item in value:
                        _preflight(item, depth + 1, seen, state)
                finally:
                    seen.remove(value_id)
                return
            if isinstance(value, Mapping):
                if len(value) > MAX_LIST_ITEMS:
                    raise ValueError("policy payload mapping exceeds maximum size")
                value_id = id(value)
                if value_id in seen:
                    raise ValueError("policy payload contains cycle")
                seen.add(value_id)
                normalized_keys: set[str] = set()
                try:
                    for key, item in value.items():
                        if not isinstance(key, str):
                            raise ValueError("policy payload keys must be strings")
                        if len(key.encode("utf-8")) > MAX_POLICY_CONTEXT_BYTES:
                            raise ValueError("policy payload key exceeds max length")
                        normalized_key = key.strip()
                        if not normalized_key:
                            raise ValueError("policy payload keys must be non-empty strings")
                        if normalized_key in normalized_keys:
                            raise ValueError("policy payload keys collide after normalization")
                        normalized_keys.add(normalized_key)
                        _preflight(item, depth + 1, seen, state)
                finally:
                    seen.remove(value_id)
                return
            raise ValueError(f"unsupported policy payload value type: {type(value).__name__}")

        _preflight(payload, 0, set(), {"nodes": 0})

        def _bounded(value: Any, depth: int) -> Any:
            if depth > max_depth:
                raise ValueError("policy payload exceeds max depth")
            if value is None or isinstance(value, (bool, int)):
                return value
            if isinstance(value, float):
                if not math.isfinite(value):
                    raise ValueError("policy payload requires finite numbers")
                return value
            if isinstance(value, str):
                if len(value.encode("utf-8")) > MAX_POLICY_CONTEXT_BYTES:
                    raise ValueError("policy payload string exceeds max length")
                return value
            if isinstance(value, (list, tuple)):
                if len(value) > MAX_LIST_ITEMS:
                    raise ValueError("policy payload list exceeds max length")
                return [_bounded(item, depth + 1) for item in value]
            if isinstance(value, dict):
                if len(value) > MAX_LIST_ITEMS:
                    raise ValueError("policy payload mapping exceeds maximum size")
                out: dict[str, Any] = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError("policy payload keys must be strings")
                    if len(key.encode("utf-8")) > MAX_POLICY_CONTEXT_BYTES:
                        raise ValueError("policy payload key exceeds max length")
                    normalized_key = key.strip()
                    if not normalized_key:
                        raise ValueError("policy payload keys must be non-empty strings")
                    if normalized_key in out:
                        raise ValueError("policy payload keys collide after normalization")
                    out[normalized_key] = _bounded(item, depth + 1)
                return out
            raise ValueError(f"unsupported policy payload value type: {type(value).__name__}")

        return len(canonical_json(_bounded(payload, 0)))

    def _opa_runner_config_identity(self) -> str:
        material = canonical_json(
            {
                "timeout_ms": self.opa_timeout_ms,
                "max_input_bytes": self.opa_max_input_bytes,
                "max_stdout_bytes": self.opa_max_stdout_bytes,
                "max_stderr_bytes": self.opa_max_stderr_bytes,
                "stdin_chunk_bytes": OPA_STDIN_WRITE_CHUNK_BYTES,
                "mode": self.opa_mode.value,
                "shell": False,
                "env_keys": ["PATH", "LANG", "LC_ALL"],
            }
        )
        return f"sha256:{hashlib.sha256(material).hexdigest()}"

    def _sanitize_json_map(self, value: Mapping[str, Any], *, max_depth: int) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            if len(sanitized) >= MAX_LIST_ITEMS:
                break
            if not isinstance(k, str):
                raise ValueError("policy context mapping keys must be strings")
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
                if not isinstance(k, str):
                    raise ValueError("policy context mapping keys must be strings")
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
        if len(result) != 1:
            raise ValueError("OPA_RESULT_AMBIGUOUS")
        first = result[0]
        if not isinstance(first, dict):
            raise ValueError("OPA_RESULT_MALFORMED")
        expressions = first.get("expressions")
        if not isinstance(expressions, list) or not expressions:
            raise ValueError("OPA_EXPRESSIONS_EMPTY")
        if len(expressions) != 1:
            raise ValueError("OPA_EXPRESSIONS_AMBIGUOUS")
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
        extra_keys = set(value.keys()) - {"allow", "deny", "matched"}
        if extra_keys:
            raise ValueError("OPA_DECISION_UNKNOWN_FIELDS")

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
        reader_error = threading.Event()

        def _close_stream(stream: Any, *, mark_reader_error: bool = False) -> None:
            try:
                stream.close()
            except Exception:
                if mark_reader_error:
                    reader_error.set()

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
            except Exception:
                reader_error.set()
            finally:
                _close_stream(stream, mark_reader_error=True)

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
            kill_error = False
            try:
                proc.kill()
            except Exception:
                kill_error = True
            return {
                "returncode": -1,
                "stdout": b"",
                "stderr": b"",
                "stdout_overflow": False,
                "stderr_overflow": False,
                "timed_out": False,
                "stdin_error": True,
                "reader_error": reader_error.is_set(),
                "kill_error": kill_error,
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
                    stdin_error.set()

        write_t = threading.Thread(target=_writer, daemon=True)
        write_t.start()

        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while proc.poll() is None:
            if reader_error.is_set():
                proc.kill()
                break
            if stdout_cap.overflow_event.is_set() or stderr_cap.overflow_event.is_set():
                proc.kill()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                proc.kill()
                try:
                    stdin.close()
                except Exception:
                    stdin_error.set()
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
                    stdin_error.set()
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
            "reader_error": reader_error.is_set(),
        }
