"""
drift.py — Decomposed Drift Tracking (Historical/Diagnostic)
=============================================================
Breaks drift into actionable components for historical diagnostics:

  D_total = D_tool + D_constraint + D_provider + D_policy

Each component is tracked independently per execution step,
enabling targeted debugging and governance optimization.

Historical/diagnostic role:
    DriftTracker / DriftReport / DriftBreakdown / DriftComponent are for
    historical diagnostics, reputation weighting, and trend analysis ONLY.
    They are NOT the production authority path for task-state drift or closure.

Production authority (issue #17):
    Instantaneous measured drift is now represented by DriftVectorV1 from
    measured_drift.py, populated by a server-owned ConstraintEvaluator from
    authoritative before/after StateObservationV1 observations.
    Accumulated historical penalties may not substitute for current measured
    task-state distance.

Re-exported for convenience:
    DriftVectorV1, DriftMetricIdentity, ComponentMeasurement, MeasurementState
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from . import measured_drift as _measured_drift

# Explicit aliases keep the historical import surface while making the
# re-export relationship visible to static/security analyzers.
ComponentMeasurement = _measured_drift.ComponentMeasurement
DriftMetricIdentity = _measured_drift.DriftMetricIdentity
DriftVectorV1 = _measured_drift.DriftVectorV1
MeasurementState = _measured_drift.MeasurementState

DriftSource = Literal["tool", "constraint", "provider", "policy"]


@dataclass
class DriftComponent:
    """A single drift contribution from a specific source."""

    source: DriftSource
    value: float
    step_index: int
    detail: str = ""


@dataclass
class DriftBreakdown:
    """Complete drift breakdown for a single execution step."""

    step_index: int
    d_tool: float = 0.0
    d_constraint: float = 0.0
    d_provider: float = 0.0
    d_policy: float = 0.0

    @property
    def d_total(self) -> float:
        return self.d_tool + self.d_constraint + self.d_provider + self.d_policy

    def to_dict(self) -> dict[str, float]:
        return {
            "step_index": self.step_index,
            "d_tool": self.d_tool,
            "d_constraint": self.d_constraint,
            "d_provider": self.d_provider,
            "d_policy": self.d_policy,
            "d_total": self.d_total,
        }


@dataclass
class DriftReport:
    """Aggregated drift report for an entire execution."""

    trace_id: str
    steps: list[DriftBreakdown] = field(default_factory=list)
    components: list[DriftComponent] = field(default_factory=list)

    @property
    def total_d_tool(self) -> float:
        return sum(s.d_tool for s in self.steps)

    @property
    def total_d_constraint(self) -> float:
        return sum(s.d_constraint for s in self.steps)

    @property
    def total_d_provider(self) -> float:
        return sum(s.d_provider for s in self.steps)

    @property
    def total_d_policy(self) -> float:
        return sum(s.d_policy for s in self.steps)

    @property
    def total_drift(self) -> float:
        return (
            self.total_d_tool
            + self.total_d_constraint
            + self.total_d_provider
            + self.total_d_policy
        )

    def summary(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "total_steps": len(self.steps),
            "total_drift": self.total_drift,
            "breakdown": {
                "d_tool": self.total_d_tool,
                "d_constraint": self.total_d_constraint,
                "d_provider": self.total_d_provider,
                "d_policy": self.total_d_policy,
            },
            "dominant_source": self.dominant_source,
        }

    @property
    def dominant_source(self) -> str:
        sources = {
            "tool": self.total_d_tool,
            "constraint": self.total_d_constraint,
            "provider": self.total_d_provider,
            "policy": self.total_d_policy,
        }
        return max(sources, key=sources.get)  # type: ignore[arg-type]


class DriftTracker:
    """
    Tracks decomposed drift across execution steps.

    Usage:
        tracker = DriftTracker("trace-123")
        tracker.record_tool_drift(step=0, value=0.1, detail="echo_text error")
        tracker.record_constraint_drift(step=0, value=0.05)
        report = tracker.report()
    """

    def __init__(self, trace_id: str) -> None:
        self._trace_id = trace_id
        self._steps: dict[int, DriftBreakdown] = {}
        self._components: list[DriftComponent] = []

    def _ensure_step(self, step_index: int) -> DriftBreakdown:
        if step_index not in self._steps:
            self._steps[step_index] = DriftBreakdown(step_index=step_index)
        return self._steps[step_index]

    def record_tool_drift(self, step: int, value: float, detail: str = "") -> None:
        """Record drift from tool execution error/penalty."""
        breakdown = self._ensure_step(step)
        breakdown.d_tool += value
        self._components.append(
            DriftComponent(source="tool", value=value, step_index=step, detail=detail)
        )

    def record_constraint_drift(self, step: int, value: float, detail: str = "") -> None:
        """Record drift from constraint projection mismatch."""
        breakdown = self._ensure_step(step)
        breakdown.d_constraint += value
        self._components.append(
            DriftComponent(source="constraint", value=value, step_index=step, detail=detail)
        )

    def record_provider_drift(self, step: int, value: float, detail: str = "") -> None:
        """Record drift from provider failure/latency."""
        breakdown = self._ensure_step(step)
        breakdown.d_provider += value
        self._components.append(
            DriftComponent(source="provider", value=value, step_index=step, detail=detail)
        )

    def record_policy_drift(self, step: int, value: float, detail: str = "") -> None:
        """Record drift from policy violation or tightening."""
        breakdown = self._ensure_step(step)
        breakdown.d_policy += value
        self._components.append(
            DriftComponent(source="policy", value=value, step_index=step, detail=detail)
        )

    def get_step(self, step_index: int) -> DriftBreakdown:
        """Get drift breakdown for a specific step."""
        return self._steps.get(step_index, DriftBreakdown(step_index=step_index))

    def report(self) -> DriftReport:
        """Generate complete drift report."""
        sorted_steps = sorted(self._steps.values(), key=lambda s: s.step_index)
        return DriftReport(
            trace_id=self._trace_id,
            steps=sorted_steps,
            components=list(self._components),
        )
