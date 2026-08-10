"""
policy_engine.py — Adaptive Governance Engine
==============================================
Evaluates execution requests against deterministic local rules,
adaptive policy profiles, contextual drift-aware rules, learned
violation signals, and optional external OPA/Rego evaluation.

Profiles:
  - strict:       maximum governance, minimal tool access
  - balanced:     standard governance (default)
  - exploratory:  relaxed governance for development/testing
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class PolicyProfile(str, Enum):
    """Adaptive policy profiles controlling governance strictness."""

    STRICT = "strict"
    BALANCED = "balanced"
    EXPLORATORY = "exploratory"


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


@dataclass
class PolicyDecision:
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    matched_policies: List[str] = field(default_factory=list)
    profile: str = "balanced"
    drift_at_evaluation: float = 0.0


@dataclass
class ViolationRecord:
    """Record of a policy violation for learned signal tracking."""

    tool: str
    reason: str
    timestamp: float = 0.0
    count: int = 1


# Maximum violations before a tool is auto-denied by learned signals
MAX_VIOLATIONS_BEFORE_DENY = 3


class PolicyEngine:
    """
    Adaptive governance engine with profile-aware evaluation,
    contextual drift rules, and learned violation signals.

    Features:
      - Three governance profiles (strict/balanced/exploratory)
      - Contextual rules that tighten permissions when drift rises
      - Learned violation signals that feed deny patterns
      - Optional OPA/Rego external policy evaluation
    """

    def __init__(
        self,
        forbidden_tools: Optional[Iterable[str]] = None,
        max_payload_bytes: int = 32768,
        require_trace_id: bool = False,
        rego_policy_dir: Optional[Path] = None,
        profile: PolicyProfile = PolicyProfile.BALANCED,
    ) -> None:
        self.forbidden_tools = set(forbidden_tools or [])
        self.max_payload_bytes = max_payload_bytes
        self.require_trace_id = require_trace_id
        self.rego_policy_dir = rego_policy_dir
        self._profile = profile
        self._violation_history: Dict[str, ViolationRecord] = {}
        self._learned_deny_tools: set[str] = set()
        self._current_drift: float = 0.0

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
        """Update current drift for contextual rule evaluation."""
        self._current_drift = drift

    def evaluate(self, request: Dict[str, Any]) -> PolicyDecision:
        reasons: List[str] = []
        matched: List[str] = []

        profile_defaults = PROFILE_DEFAULTS[self._profile]

        # Forbidden tools check
        tool = str(request.get("tool", ""))
        if tool in self.forbidden_tools:
            reasons.append(f"tool '{tool}' is forbidden by local policy")
            matched.append("local.forbidden_tools")

        # Learned deny check
        if tool in self._learned_deny_tools:
            reasons.append(f"tool '{tool}' is denied by learned violation signal")
            matched.append("learned.deny_tools")

        # Payload size check (profile-aware)
        effective_max = profile_defaults.get("max_payload_bytes", self.max_payload_bytes)
        payload_size = len(json.dumps(request, sort_keys=True).encode("utf-8"))
        if payload_size > effective_max:
            reasons.append(f"request payload size {payload_size} exceeds limit {effective_max}")
            matched.append("local.max_payload_bytes")

        # Trace ID requirement (profile-aware)
        effective_trace = profile_defaults.get("require_trace_id", self.require_trace_id)
        if effective_trace and not request.get("trace_id"):
            reasons.append("trace_id is required by policy")
            matched.append("local.require_trace_id")

        # Contextual drift-aware rules
        drift_threshold = profile_defaults.get("drift_threshold", 0.7)
        if self._current_drift > drift_threshold:
            # High drift → tighten permissions
            agent_id = str(request.get("agent_id", ""))
            if agent_id == "demo_backend" and not profile_defaults.get("allow_demo_backend", True):
                reasons.append(f"demo backend not allowed under {self._profile.value} profile")
                matched.append("contextual.drift_tightening")

            # Under high drift in strict mode, limit tool calls
            max_calls = profile_defaults.get("max_tool_calls_per_step", 5)
            tool_call_count = request.get("tool_call_count", 0)
            if tool_call_count > max_calls:
                reasons.append(
                    f"tool call count {tool_call_count} exceeds "
                    f"limit {max_calls} under {self._profile.value} profile"
                )
                matched.append("contextual.max_tool_calls")

        # OPA/Rego evaluation
        opa_decision = self._evaluate_with_opa(request)
        if opa_decision is not None:
            matched.extend(opa_decision.matched_policies)
            reasons.extend(opa_decision.reasons)

        # Record violation if denied
        if reasons and tool:
            self._record_violation(tool, "; ".join(reasons))

        return PolicyDecision(
            allowed=not reasons,
            reasons=reasons,
            matched_policies=matched,
            profile=self._profile.value,
            drift_at_evaluation=self._current_drift,
        )

    def _record_violation(self, tool: str, reason: str) -> None:
        """Record violation for learned signal tracking."""
        if tool in self._violation_history:
            self._violation_history[tool].count += 1
        else:
            self._violation_history[tool] = ViolationRecord(tool=tool, reason=reason)

        # Auto-deny tool after repeated violations
        if self._violation_history[tool].count >= MAX_VIOLATIONS_BEFORE_DENY:
            self._learned_deny_tools.add(tool)

    def get_violation_history(self) -> Dict[str, ViolationRecord]:
        """Return the full violation history."""
        return dict(self._violation_history)

    def clear_learned_denials(self) -> None:
        """Clear all learned denial patterns."""
        self._learned_deny_tools.clear()
        self._violation_history.clear()

    def test_policy(self, sample_request: Dict[str, Any]) -> PolicyDecision:
        """Test a policy evaluation against a sample request without side effects."""
        # Deep copy violation history to prevent ViolationRecord mutation leakage
        saved_violations = {k: copy.copy(v) for k, v in self._violation_history.items()}
        saved_denials = set(self._learned_deny_tools)

        result = self.evaluate(sample_request)

        # Restore state (no side effects)
        self._violation_history = saved_violations
        self._learned_deny_tools = saved_denials
        return result

    def _evaluate_with_opa(self, request: Dict[str, Any]) -> Optional[PolicyDecision]:
        if not self.rego_policy_dir:
            return None
        opa_bin = shutil.which("opa")
        if opa_bin is None:
            return None

        cmd = [
            opa_bin,
            "eval",
            "--format",
            "json",
            "--data",
            str(self.rego_policy_dir),
            "--input",
            "-",
            "data.sovereign_claw.execution",
        ]
        proc = subprocess.run(
            cmd,
            input=json.dumps(request).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            return PolicyDecision(
                allowed=False,
                reasons=[f"opa evaluation failed: {proc.stderr.decode('utf-8', 'ignore').strip()}"],
                matched_policies=["opa.runtime_error"],
            )

        payload = json.loads(proc.stdout.decode("utf-8"))
        results = payload.get("result", [])
        if not results:
            return None

        expressions = results[0].get("expressions", [])
        if not expressions:
            return None

        value = expressions[0].get("value") or {}
        return PolicyDecision(
            allowed=bool(value.get("allow", True)),
            reasons=list(value.get("deny", [])),
            matched_policies=list(value.get("matched", [])),
        )
