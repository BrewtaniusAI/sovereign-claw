"""
guardrails.py — Autonomous Guardrails Engine
=============================================
Programmable safety constraints between AI reasoning and execution.

Guardrails act as an enforcement layer — sitting between the agent's
probabilistic reasoning and the deterministic execution layer — that
prevents privilege escalation, detects infinite loops, blocks
destructive actions, and gates cost overruns.

Unlike prompt engineering or post-generation filters, these guardrails
are deterministic, machine-readable policies that integrate with the
PolicyEngine and multi-agent A2A environments.

Reference: NVIDIA NeMo Guardrails pattern, EU AI Act Article 50
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class GuardrailSeverity(str, Enum):
    """Severity level for guardrail violations."""

    INFO = "info"
    WARNING = "warning"
    BLOCK = "block"


@dataclass
class GuardrailResult:
    """Result of a guardrail check against an action."""

    rule_name: str
    passed: bool
    severity: GuardrailSeverity = GuardrailSeverity.INFO
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GuardrailDecision:
    """Aggregate decision from all guardrail checks."""

    allowed: bool
    results: List[GuardrailResult] = field(default_factory=list)
    blocked_by: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    evaluation_time_ms: float = 0.0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


class GuardrailRule:
    """
    Base guardrail rule with a check function.

    Each rule evaluates an action context dict and returns a GuardrailResult.
    Rules are composable and can be added to a GuardrailEngine.
    """

    def __init__(
        self,
        name: str,
        severity: GuardrailSeverity = GuardrailSeverity.BLOCK,
        description: str = "",
        check_fn: Optional[Callable[[Dict[str, Any]], GuardrailResult]] = None,
    ) -> None:
        self.name = name
        self.severity = severity
        self.description = description
        self._check_fn = check_fn

    def check(self, context: Dict[str, Any]) -> GuardrailResult:
        """Evaluate this rule against the given action context."""
        if self._check_fn is not None:
            return self._check_fn(context)
        return GuardrailResult(
            rule_name=self.name,
            passed=True,
            severity=self.severity,
            message="No check function defined (pass-through)",
        )


# ── Built-in guardrail rules ─────────────────────────────────────────────────

# Privileged tools that require explicit authorization
PRIVILEGED_TOOLS = frozenset(
    {
        "shell_exec",
        "file_delete",
        "sudo",
        "rm_rf",
        "drop_table",
        "system_shutdown",
        "credential_access",
        "network_config",
        "firewall_modify",
    }
)

# Maximum consecutive identical actions before loop detection triggers
MAX_CONSECUTIVE_REPEATS = 5

# Default cost limit per execution (in USD)
DEFAULT_COST_LIMIT = 10.0

# Default token limit per execution
DEFAULT_TOKEN_LIMIT = 100_000


def _check_privilege_escalation(context: Dict[str, Any]) -> GuardrailResult:
    """Block actions attempting to use privileged tools without authorization."""
    tool = context.get("tool", "")
    authorized_tools = set(context.get("authorized_privileged_tools", []))

    if tool in PRIVILEGED_TOOLS and tool not in authorized_tools:
        return GuardrailResult(
            rule_name="privilege_escalation",
            passed=False,
            severity=GuardrailSeverity.BLOCK,
            message=f"Privileged tool '{tool}' requires explicit authorization",
            metadata={"tool": tool, "privileged_tools": sorted(PRIVILEGED_TOOLS)},
        )
    return GuardrailResult(
        rule_name="privilege_escalation",
        passed=True,
        severity=GuardrailSeverity.BLOCK,
    )


def _check_loop_detection(context: Dict[str, Any]) -> GuardrailResult:
    """Detect and block infinite agent loops via consecutive identical actions."""
    action_history: List[str] = context.get("action_history", [])
    threshold = context.get("loop_threshold", MAX_CONSECUTIVE_REPEATS)

    if len(action_history) >= threshold:
        recent = action_history[-threshold:]
        if len(set(recent)) == 1:
            return GuardrailResult(
                rule_name="loop_detection",
                passed=False,
                severity=GuardrailSeverity.BLOCK,
                message=(
                    f"Loop detected: action '{recent[0]}' repeated {threshold} consecutive times"
                ),
                metadata={"repeated_action": recent[0], "count": threshold},
            )
    return GuardrailResult(
        rule_name="loop_detection",
        passed=True,
        severity=GuardrailSeverity.BLOCK,
    )


def _check_destructive_action(context: Dict[str, Any]) -> GuardrailResult:
    """Gate destructive actions that require human confirmation."""
    tool = context.get("tool", "")
    destructive_tools = set(
        context.get(
            "destructive_tools",
            [
                "file_delete",
                "rm_rf",
                "drop_table",
                "truncate_table",
                "format_disk",
                "system_shutdown",
            ],
        )
    )
    human_approved = context.get("human_approved", False)

    if tool in destructive_tools and not human_approved:
        return GuardrailResult(
            rule_name="destructive_action",
            passed=False,
            severity=GuardrailSeverity.BLOCK,
            message=f"Destructive action '{tool}' requires human approval",
            metadata={"tool": tool, "requires": "human_approved=True"},
        )
    return GuardrailResult(
        rule_name="destructive_action",
        passed=True,
        severity=GuardrailSeverity.BLOCK,
    )


def _check_cost_limit(context: Dict[str, Any]) -> GuardrailResult:
    """Prevent exceeding cost budgets."""
    current_cost = context.get("current_cost_usd", 0.0)
    estimated_cost = context.get("estimated_action_cost_usd", 0.0)
    limit = context.get("cost_limit_usd", DEFAULT_COST_LIMIT)

    projected = current_cost + estimated_cost
    if projected > limit:
        return GuardrailResult(
            rule_name="cost_limit",
            passed=False,
            severity=GuardrailSeverity.BLOCK,
            message=(f"Action would exceed cost limit: ${projected:.2f} > ${limit:.2f} limit"),
            metadata={
                "current_cost": current_cost,
                "estimated_cost": estimated_cost,
                "limit": limit,
            },
        )
    # Warn when approaching 80% of limit
    if projected > limit * 0.8:
        return GuardrailResult(
            rule_name="cost_limit",
            passed=True,
            severity=GuardrailSeverity.WARNING,
            message=f"Approaching cost limit: ${projected:.2f} / ${limit:.2f}",
            metadata={"current_cost": current_cost, "projected": projected},
        )
    return GuardrailResult(
        rule_name="cost_limit",
        passed=True,
        severity=GuardrailSeverity.INFO,
    )


def _check_token_limit(context: Dict[str, Any]) -> GuardrailResult:
    """Prevent exceeding token limits per execution."""
    tokens_used = context.get("tokens_used", 0)
    estimated_tokens = context.get("estimated_tokens", 0)
    limit = context.get("token_limit", DEFAULT_TOKEN_LIMIT)

    projected = tokens_used + estimated_tokens
    if projected > limit:
        return GuardrailResult(
            rule_name="token_limit",
            passed=False,
            severity=GuardrailSeverity.BLOCK,
            message=(f"Action would exceed token limit: {projected:,} > {limit:,} limit"),
            metadata={"tokens_used": tokens_used, "estimated": estimated_tokens},
        )
    return GuardrailResult(
        rule_name="token_limit",
        passed=True,
        severity=GuardrailSeverity.INFO,
    )


# Pre-built rule instances
PRIVILEGE_ESCALATION_GUARD = GuardrailRule(
    name="privilege_escalation",
    severity=GuardrailSeverity.BLOCK,
    description="Blocks privileged tool access without authorization",
    check_fn=_check_privilege_escalation,
)

LOOP_DETECTION_GUARD = GuardrailRule(
    name="loop_detection",
    severity=GuardrailSeverity.BLOCK,
    description="Detects and blocks infinite agent loops",
    check_fn=_check_loop_detection,
)

DESTRUCTIVE_ACTION_GUARD = GuardrailRule(
    name="destructive_action",
    severity=GuardrailSeverity.BLOCK,
    description="Gates destructive operations requiring human approval",
    check_fn=_check_destructive_action,
)

COST_LIMIT_GUARD = GuardrailRule(
    name="cost_limit",
    severity=GuardrailSeverity.BLOCK,
    description="Prevents exceeding execution cost budgets",
    check_fn=_check_cost_limit,
)

TOKEN_LIMIT_GUARD = GuardrailRule(
    name="token_limit",
    severity=GuardrailSeverity.BLOCK,
    description="Prevents exceeding token limits per execution",
    check_fn=_check_token_limit,
)

# Default rule set — all built-in guards
DEFAULT_RULES: List[GuardrailRule] = [
    PRIVILEGE_ESCALATION_GUARD,
    LOOP_DETECTION_GUARD,
    DESTRUCTIVE_ACTION_GUARD,
    COST_LIMIT_GUARD,
    TOKEN_LIMIT_GUARD,
]


class GuardrailEngine:
    """
    Evaluates a set of guardrail rules against an action context.

    Sits between AI reasoning and execution to enforce safety invariants.
    All evaluations are logged for audit compliance.
    """

    def __init__(self, rules: Optional[List[GuardrailRule]] = None) -> None:
        self._rules: List[GuardrailRule] = list(rules or DEFAULT_RULES)
        self._evaluation_count: int = 0
        self._block_count: int = 0
        self._warning_count: int = 0

    @property
    def rules(self) -> List[GuardrailRule]:
        return list(self._rules)

    def add_rule(self, rule: GuardrailRule) -> None:
        """Add a custom guardrail rule."""
        self._rules.append(rule)

    def remove_rule(self, name: str) -> bool:
        """Remove a rule by name. Returns True if removed."""
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.name != name]
        return len(self._rules) < before

    def evaluate(self, context: Dict[str, Any]) -> GuardrailDecision:
        """
        Evaluate all guardrail rules against the given action context.

        Returns a GuardrailDecision indicating whether the action is allowed.
        An action is blocked if ANY rule with BLOCK severity fails.
        """
        start = time.monotonic()
        results: List[GuardrailResult] = []
        blocked_by: List[str] = []
        warnings: List[str] = []

        for rule in self._rules:
            result = rule.check(context)
            results.append(result)

            if not result.passed:
                if result.severity == GuardrailSeverity.BLOCK:
                    blocked_by.append(result.rule_name)
                    self._block_count += 1
                    logger.warning("Guardrail BLOCKED: %s — %s", result.rule_name, result.message)
                elif result.severity == GuardrailSeverity.WARNING:
                    warnings.append(result.message)
                    self._warning_count += 1
                    logger.info("Guardrail WARNING: %s — %s", result.rule_name, result.message)

            if result.passed and result.severity == GuardrailSeverity.WARNING:
                warnings.append(result.message)
                self._warning_count += 1

        elapsed = (time.monotonic() - start) * 1000
        self._evaluation_count += 1

        return GuardrailDecision(
            allowed=len(blocked_by) == 0,
            results=results,
            blocked_by=blocked_by,
            warnings=warnings,
            evaluation_time_ms=elapsed,
        )

    def stats(self) -> Dict[str, int]:
        """Return guardrail evaluation statistics."""
        return {
            "evaluations": self._evaluation_count,
            "blocks": self._block_count,
            "warnings": self._warning_count,
            "rules_count": len(self._rules),
        }
