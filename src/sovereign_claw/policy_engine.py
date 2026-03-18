"""
policy_engine.py — optional OPA/Rego-compatible policy guardrails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class PolicyDecision:
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    matched_policies: List[str] = field(default_factory=list)


class PolicyEngine:
    """
    Evaluate execution requests against a deterministic local rule set and,
    when available, an external OPA CLI using bundled Rego policies.
    """

    def __init__(
        self,
        forbidden_tools: Optional[Iterable[str]] = None,
        max_payload_bytes: int = 32768,
        require_trace_id: bool = False,
        rego_policy_dir: Optional[Path] = None,
    ) -> None:
        self.forbidden_tools = set(forbidden_tools or [])
        self.max_payload_bytes = max_payload_bytes
        self.require_trace_id = require_trace_id
        self.rego_policy_dir = rego_policy_dir

    def evaluate(self, request: Dict[str, Any]) -> PolicyDecision:
        reasons: List[str] = []
        matched: List[str] = []

        tool = str(request.get("tool", ""))
        if tool in self.forbidden_tools:
            reasons.append(f"tool '{tool}' is forbidden by local policy")
            matched.append("local.forbidden_tools")

        payload_size = len(json.dumps(request, sort_keys=True).encode("utf-8"))
        if payload_size > self.max_payload_bytes:
            reasons.append(
                f"request payload size {payload_size} exceeds limit {self.max_payload_bytes}"
            )
            matched.append("local.max_payload_bytes")

        if self.require_trace_id and not request.get("trace_id"):
            reasons.append("trace_id is required by policy")
            matched.append("local.require_trace_id")

        opa_decision = self._evaluate_with_opa(request)
        if opa_decision is not None:
            matched.extend(opa_decision.matched_policies)
            reasons.extend(opa_decision.reasons)

        return PolicyDecision(
            allowed=not reasons,
            reasons=reasons,
            matched_policies=matched,
        )

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
