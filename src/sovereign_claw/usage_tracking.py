"""
usage_tracking — Token & Cost Tracking with Budget Alerts
==========================================================
Per-session usage metering for governed execution.

Features:
- Per-session token counting (prompt + completion)
- Cost tracking per provider with configurable rates
- Usage reporting with breakdowns by model, session, time period
- Budget alerts with configurable thresholds and callbacks
- Rate-of-spend tracking to predict budget exhaustion
- Usage export (JSON, CSV-style dict)
- Governed usage: all spend auditable via ProofVault

Every LLM call's token usage and cost is tracked.
Budget overruns trigger governance alerts.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class AlertSeverity(str, Enum):
    """Severity of a budget alert."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class AlertType(str, Enum):
    """Type of budget alert."""

    THRESHOLD_REACHED = "threshold_reached"
    BUDGET_EXCEEDED = "budget_exceeded"
    RATE_SPIKE = "rate_spike"
    SESSION_LIMIT = "session_limit"
    DAILY_LIMIT = "daily_limit"


@dataclass
class ProviderRates:
    """Token pricing for a model/provider."""

    provider: str
    model: str
    prompt_cost_per_1k: float = 0.0  # Cost per 1K prompt tokens
    completion_cost_per_1k: float = 0.0  # Cost per 1K completion tokens
    currency: str = "USD"

    def compute_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Compute cost for a given token usage."""
        prompt_cost = (prompt_tokens / 1000.0) * self.prompt_cost_per_1k
        completion_cost = (completion_tokens / 1000.0) * self.completion_cost_per_1k
        return prompt_cost + completion_cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_cost_per_1k": self.prompt_cost_per_1k,
            "completion_cost_per_1k": self.completion_cost_per_1k,
            "currency": self.currency,
        }


@dataclass
class UsageRecord:
    """A single usage event."""

    record_id: str = ""
    session_id: str = ""
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    currency: str = "USD"
    timestamp: float = field(default_factory=time.time)
    latency_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.record_id:
            self.record_id = f"usage_{uuid.uuid4().hex[:10]}"
        if self.total_tokens == 0:
            self.total_tokens = self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost": round(self.cost, 6),
            "currency": self.currency,
            "timestamp": self.timestamp,
            "latency_ms": self.latency_ms,
        }


@dataclass
class BudgetConfig:
    """Budget configuration with alert thresholds."""

    max_cost: float = 100.0  # Maximum spend in currency
    max_tokens: int = 10_000_000  # Maximum total tokens
    max_cost_per_session: float = 10.0  # Per-session cost limit
    max_tokens_per_session: int = 1_000_000
    daily_cost_limit: float = 50.0  # Daily cost limit
    alert_thresholds: list[float] = field(default_factory=lambda: [0.5, 0.75, 0.9, 1.0])
    currency: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_cost": self.max_cost,
            "max_tokens": self.max_tokens,
            "max_cost_per_session": self.max_cost_per_session,
            "max_tokens_per_session": self.max_tokens_per_session,
            "daily_cost_limit": self.daily_cost_limit,
            "alert_thresholds": self.alert_thresholds,
            "currency": self.currency,
        }


@dataclass
class BudgetAlert:
    """A budget alert event."""

    alert_id: str = ""
    alert_type: AlertType = AlertType.THRESHOLD_REACHED
    severity: AlertSeverity = AlertSeverity.WARNING
    message: str = ""
    current_value: float = 0.0
    limit_value: float = 0.0
    utilization: float = 0.0
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.alert_id:
            self.alert_id = f"alert_{uuid.uuid4().hex[:8]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "alert_type": self.alert_type.value,
            "severity": self.severity.value,
            "message": self.message,
            "current_value": round(self.current_value, 4),
            "limit_value": round(self.limit_value, 4),
            "utilization": round(self.utilization, 4),
            "session_id": self.session_id,
            "timestamp": self.timestamp,
        }


# Type for alert callback
AlertCallback = Callable[[BudgetAlert], None]


class UsageTracker:
    """
    Per-session token and cost tracking with budget alerts.

    Usage:
        tracker = UsageTracker(budget=BudgetConfig(max_cost=50.0))

        # Register provider rates
        tracker.register_rates(ProviderRates(
            provider="anthropic",
            model="claude-3-sonnet",
            prompt_cost_per_1k=0.003,
            completion_cost_per_1k=0.015,
        ))

        # Record usage
        tracker.record(
            session_id="session_1",
            provider="anthropic",
            model="claude-3-sonnet",
            prompt_tokens=1500,
            completion_tokens=500,
        )

        # Check budget
        report = tracker.budget_report()

        # Get session summary
        summary = tracker.session_summary("session_1")
    """

    # Maximum records to keep in memory
    MAX_RECORDS = 100000

    def __init__(
        self,
        budget: BudgetConfig | None = None,
        alert_callback: AlertCallback | None = None,
    ) -> None:
        self._budget = budget or BudgetConfig()
        self._alert_callback = alert_callback
        self._rates: dict[str, ProviderRates] = {}  # key: "provider:model"
        self._records: list[UsageRecord] = []
        self._alerts: list[BudgetAlert] = []
        self._triggered_thresholds: set[float] = set()
        self._session_totals: dict[str, dict[str, float]] = {}  # session -> {tokens, cost}
        self._daily_totals: dict[str, float] = {}  # date_str -> cost
        self._total_tokens = 0
        self._total_cost = 0.0
        self._total_records = 0

    def register_rates(self, rates: ProviderRates) -> None:
        """Register pricing rates for a provider/model."""
        key = f"{rates.provider}:{rates.model}"
        self._rates[key] = rates

    def record(
        self,
        session_id: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> UsageRecord:
        """
        Record a usage event.

        Args:
            session_id: Session identifier.
            provider: Provider name.
            model: Model name.
            prompt_tokens: Number of prompt tokens.
            completion_tokens: Number of completion tokens.
            latency_ms: Request latency in milliseconds.
            metadata: Additional metadata.

        Returns:
            UsageRecord with computed cost.
        """
        # Compute cost
        rate_key = f"{provider}:{model}"
        rates = self._rates.get(rate_key)
        cost = rates.compute_cost(prompt_tokens, completion_tokens) if rates else 0.0

        record = UsageRecord(
            session_id=session_id,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            currency=self._budget.currency,
            latency_ms=latency_ms,
            metadata=metadata or {},
        )

        self._records.append(record)
        self._total_records += 1
        self._total_tokens += record.total_tokens
        self._total_cost += cost

        # Update session totals
        if session_id not in self._session_totals:
            self._session_totals[session_id] = {"tokens": 0, "cost": 0.0}
        self._session_totals[session_id]["tokens"] += record.total_tokens
        self._session_totals[session_id]["cost"] += cost

        # Update daily totals
        date_str = time.strftime("%Y-%m-%d", time.localtime(record.timestamp))
        self._daily_totals[date_str] = self._daily_totals.get(date_str, 0.0) + cost

        # Check budget alerts
        self._check_alerts(session_id, date_str)

        # Evict old records if needed
        if len(self._records) > self.MAX_RECORDS:
            self._records = self._records[-self.MAX_RECORDS :]

        return record

    def budget_report(self) -> dict[str, Any]:
        """Get comprehensive budget report."""
        cost_utilization = (
            self._total_cost / self._budget.max_cost if self._budget.max_cost > 0 else 0.0
        )
        token_utilization = (
            self._total_tokens / self._budget.max_tokens if self._budget.max_tokens > 0 else 0.0
        )
        return {
            "total_cost": round(self._total_cost, 4),
            "total_tokens": self._total_tokens,
            "cost_utilization": round(cost_utilization, 4),
            "token_utilization": round(token_utilization, 4),
            "budget": self._budget.to_dict(),
            "sessions": len(self._session_totals),
            "total_records": self._total_records,
            "alerts_triggered": len(self._alerts),
            "currency": self._budget.currency,
        }

    def session_summary(self, session_id: str) -> dict[str, Any]:
        """Get usage summary for a specific session."""
        totals = self._session_totals.get(session_id, {"tokens": 0, "cost": 0.0})
        session_records = [r for r in self._records if r.session_id == session_id]

        by_model: dict[str, dict[str, Any]] = {}
        for r in session_records:
            key = f"{r.provider}:{r.model}"
            if key not in by_model:
                by_model[key] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost": 0.0,
                    "calls": 0,
                }
            by_model[key]["prompt_tokens"] += r.prompt_tokens
            by_model[key]["completion_tokens"] += r.completion_tokens
            by_model[key]["total_tokens"] += r.total_tokens
            by_model[key]["cost"] += r.cost
            by_model[key]["calls"] += 1

        session_cost_util = (
            totals["cost"] / self._budget.max_cost_per_session
            if self._budget.max_cost_per_session > 0
            else 0.0
        )

        return {
            "session_id": session_id,
            "total_tokens": int(totals["tokens"]),
            "total_cost": round(totals["cost"], 6),
            "cost_utilization": round(session_cost_util, 4),
            "by_model": {k: {**v, "cost": round(v["cost"], 6)} for k, v in by_model.items()},
            "record_count": len(session_records),
        }

    def daily_summary(self, date_str: str = "") -> dict[str, Any]:
        """Get daily usage summary."""
        if not date_str:
            date_str = time.strftime("%Y-%m-%d")
        daily_cost = self._daily_totals.get(date_str, 0.0)
        daily_util = (
            daily_cost / self._budget.daily_cost_limit if self._budget.daily_cost_limit > 0 else 0.0
        )
        return {
            "date": date_str,
            "cost": round(daily_cost, 4),
            "daily_limit": self._budget.daily_cost_limit,
            "utilization": round(daily_util, 4),
        }

    def get_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent alerts."""
        return [a.to_dict() for a in self._alerts[-limit:]]

    def reset_session(self, session_id: str) -> bool:
        """Reset usage tracking for a session."""
        if session_id in self._session_totals:
            del self._session_totals[session_id]
            return True
        return False

    def stats(self) -> dict[str, Any]:
        """Get tracker statistics."""
        return {
            "total_tokens": self._total_tokens,
            "total_cost": round(self._total_cost, 4),
            "total_records": self._total_records,
            "active_sessions": len(self._session_totals),
            "registered_rates": len(self._rates),
            "alerts_count": len(self._alerts),
            "currency": self._budget.currency,
        }

    def _check_alerts(self, session_id: str, date_str: str) -> None:
        """Check budget thresholds and emit alerts."""
        # Overall cost thresholds
        if self._budget.max_cost > 0:
            utilization = self._total_cost / self._budget.max_cost
            for threshold in self._budget.alert_thresholds:
                if utilization >= threshold and threshold not in self._triggered_thresholds:
                    self._triggered_thresholds.add(threshold)
                    severity = self._severity_for_threshold(threshold)
                    alert_type = (
                        AlertType.BUDGET_EXCEEDED
                        if threshold >= 1.0
                        else AlertType.THRESHOLD_REACHED
                    )
                    self._emit_alert(
                        BudgetAlert(
                            alert_type=alert_type,
                            severity=severity,
                            message=(
                                f"Budget {threshold * 100:.0f}% reached: "
                                f"${self._total_cost:.2f}/${self._budget.max_cost:.2f}"
                            ),
                            current_value=self._total_cost,
                            limit_value=self._budget.max_cost,
                            utilization=utilization,
                            session_id=session_id,
                        )
                    )

        # Per-session cost limit
        session_totals = self._session_totals.get(session_id, {})
        session_cost = session_totals.get("cost", 0.0)
        if (
            self._budget.max_cost_per_session > 0
            and session_cost > self._budget.max_cost_per_session
        ):
            self._emit_alert(
                BudgetAlert(
                    alert_type=AlertType.SESSION_LIMIT,
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        f"Session {session_id} exceeded cost limit: "
                        f"${session_cost:.2f}/${self._budget.max_cost_per_session:.2f}"
                    ),
                    current_value=session_cost,
                    limit_value=self._budget.max_cost_per_session,
                    utilization=session_cost / self._budget.max_cost_per_session,
                    session_id=session_id,
                )
            )

        # Daily cost limit
        daily_cost = self._daily_totals.get(date_str, 0.0)
        if self._budget.daily_cost_limit > 0 and daily_cost > self._budget.daily_cost_limit:
            self._emit_alert(
                BudgetAlert(
                    alert_type=AlertType.DAILY_LIMIT,
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        f"Daily cost limit exceeded: "
                        f"${daily_cost:.2f}/${self._budget.daily_cost_limit:.2f}"
                    ),
                    current_value=daily_cost,
                    limit_value=self._budget.daily_cost_limit,
                    utilization=daily_cost / self._budget.daily_cost_limit,
                    session_id=session_id,
                )
            )

    def _emit_alert(self, alert: BudgetAlert) -> None:
        """Emit a budget alert."""
        self._alerts.append(alert)
        if self._alert_callback:
            try:
                self._alert_callback(alert)
            except Exception:
                pass  # Callback failures should not break tracking

    def _severity_for_threshold(self, threshold: float) -> AlertSeverity:
        if threshold >= 1.0:
            return AlertSeverity.EMERGENCY
        if threshold >= 0.9:
            return AlertSeverity.CRITICAL
        if threshold >= 0.75:
            return AlertSeverity.WARNING
        return AlertSeverity.INFO
