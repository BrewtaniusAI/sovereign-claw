"""Tests for sovereign_claw.usage_tracking."""

from __future__ import annotations

import pytest

from sovereign_claw.usage_tracking import (
    AlertSeverity,
    AlertType,
    BudgetAlert,
    BudgetConfig,
    ProviderRates,
    UsageRecord,
    UsageTracker,
)


# ── ProviderRates ────────────────────────────────────────────────────────────


class TestProviderRates:
    def test_creation(self) -> None:
        rates = ProviderRates(
            provider="anthropic",
            model="claude-3-sonnet",
            prompt_cost_per_1k=0.003,
            completion_cost_per_1k=0.015,
        )
        assert rates.provider == "anthropic"

    def test_compute_cost(self) -> None:
        rates = ProviderRates(
            provider="openai",
            model="gpt-4",
            prompt_cost_per_1k=0.01,
            completion_cost_per_1k=0.03,
        )
        cost = rates.compute_cost(1000, 500)
        assert cost == pytest.approx(0.01 + 0.015, abs=1e-6)

    def test_zero_cost(self) -> None:
        rates = ProviderRates(provider="free", model="test")
        assert rates.compute_cost(1000, 1000) == 0.0

    def test_to_dict(self) -> None:
        rates = ProviderRates(provider="test", model="m1")
        d = rates.to_dict()
        assert d["provider"] == "test"


# ── UsageRecord ──────────────────────────────────────────────────────────────


class TestUsageRecord:
    def test_creation(self) -> None:
        r = UsageRecord(
            session_id="s1",
            provider="openai",
            model="gpt-4",
            prompt_tokens=100,
            completion_tokens=50,
        )
        assert r.total_tokens == 150
        assert r.record_id.startswith("usage_")

    def test_auto_total(self) -> None:
        r = UsageRecord(prompt_tokens=200, completion_tokens=100)
        assert r.total_tokens == 300

    def test_to_dict(self) -> None:
        r = UsageRecord(session_id="s1", prompt_tokens=10, completion_tokens=5)
        d = r.to_dict()
        assert d["session_id"] == "s1"
        assert d["total_tokens"] == 15


# ── BudgetConfig ─────────────────────────────────────────────────────────────


class TestBudgetConfig:
    def test_defaults(self) -> None:
        config = BudgetConfig()
        assert config.max_cost == 100.0
        assert config.max_tokens == 10_000_000
        assert len(config.alert_thresholds) == 4

    def test_to_dict(self) -> None:
        config = BudgetConfig(max_cost=50.0)
        d = config.to_dict()
        assert d["max_cost"] == 50.0


# ── BudgetAlert ──────────────────────────────────────────────────────────────


class TestBudgetAlert:
    def test_creation(self) -> None:
        alert = BudgetAlert(
            alert_type=AlertType.THRESHOLD_REACHED,
            severity=AlertSeverity.WARNING,
            message="50% budget reached",
        )
        assert alert.alert_id.startswith("alert_")

    def test_to_dict(self) -> None:
        alert = BudgetAlert(
            alert_type=AlertType.BUDGET_EXCEEDED,
            severity=AlertSeverity.EMERGENCY,
            message="Budget exceeded",
            current_value=105.0,
            limit_value=100.0,
        )
        d = alert.to_dict()
        assert d["severity"] == "emergency"
        assert d["current_value"] == 105.0


# ── UsageTracker ─────────────────────────────────────────────────────────────


class TestUsageTracker:
    def _make_tracker(self, max_cost: float = 100.0) -> UsageTracker:
        tracker = UsageTracker(budget=BudgetConfig(max_cost=max_cost))
        tracker.register_rates(
            ProviderRates(
                provider="openai",
                model="gpt-4",
                prompt_cost_per_1k=0.01,
                completion_cost_per_1k=0.03,
            )
        )
        tracker.register_rates(
            ProviderRates(
                provider="anthropic",
                model="claude-3",
                prompt_cost_per_1k=0.003,
                completion_cost_per_1k=0.015,
            )
        )
        return tracker

    def test_record_usage(self) -> None:
        tracker = self._make_tracker()
        record = tracker.record(
            session_id="s1",
            provider="openai",
            model="gpt-4",
            prompt_tokens=1000,
            completion_tokens=500,
        )
        assert record.total_tokens == 1500
        assert record.cost > 0

    def test_record_without_rates(self) -> None:
        tracker = UsageTracker()
        record = tracker.record(
            session_id="s1",
            provider="unknown",
            model="unknown",
            prompt_tokens=100,
            completion_tokens=50,
        )
        assert record.cost == 0.0

    def test_session_summary(self) -> None:
        tracker = self._make_tracker()
        tracker.record("s1", "openai", "gpt-4", 1000, 500)
        tracker.record("s1", "openai", "gpt-4", 2000, 1000)
        summary = tracker.session_summary("s1")
        assert summary["total_tokens"] == 4500
        assert summary["record_count"] == 2
        assert "openai:gpt-4" in summary["by_model"]

    def test_session_summary_empty(self) -> None:
        tracker = self._make_tracker()
        summary = tracker.session_summary("nonexistent")
        assert summary["total_tokens"] == 0

    def test_budget_report(self) -> None:
        tracker = self._make_tracker(max_cost=10.0)
        tracker.record("s1", "openai", "gpt-4", 5000, 2000)
        report = tracker.budget_report()
        assert report["total_cost"] > 0
        assert report["cost_utilization"] > 0
        assert report["sessions"] == 1

    def test_budget_alerts_triggered(self) -> None:
        alerts_received: list[BudgetAlert] = []
        tracker = UsageTracker(
            budget=BudgetConfig(
                max_cost=1.0,
                alert_thresholds=[0.5, 1.0],
            ),
            alert_callback=lambda a: alerts_received.append(a),
        )
        tracker.register_rates(
            ProviderRates(
                provider="expensive",
                model="big",
                prompt_cost_per_1k=10.0,
                completion_cost_per_1k=30.0,
            )
        )
        # This will blow through the budget
        tracker.record("s1", "expensive", "big", 1000, 1000)
        assert len(alerts_received) >= 1

    def test_session_cost_limit_alert(self) -> None:
        alerts: list[BudgetAlert] = []
        tracker = UsageTracker(
            budget=BudgetConfig(
                max_cost=1000.0,
                max_cost_per_session=0.01,
            ),
            alert_callback=lambda a: alerts.append(a),
        )
        tracker.register_rates(
            ProviderRates(
                provider="p",
                model="m",
                prompt_cost_per_1k=1.0,
                completion_cost_per_1k=1.0,
            )
        )
        tracker.record("s1", "p", "m", 1000, 1000)
        session_alerts = [a for a in alerts if a.alert_type == AlertType.SESSION_LIMIT]
        assert len(session_alerts) >= 1

    def test_daily_summary(self) -> None:
        tracker = self._make_tracker()
        tracker.record("s1", "openai", "gpt-4", 1000, 500)
        daily = tracker.daily_summary()
        assert daily["cost"] >= 0

    def test_get_alerts(self) -> None:
        tracker = UsageTracker(
            budget=BudgetConfig(max_cost=0.001, alert_thresholds=[0.5, 1.0]),
        )
        tracker.register_rates(
            ProviderRates(
                provider="p",
                model="m",
                prompt_cost_per_1k=10.0,
                completion_cost_per_1k=10.0,
            )
        )
        tracker.record("s1", "p", "m", 1000, 1000)
        alerts = tracker.get_alerts()
        assert len(alerts) >= 1

    def test_reset_session(self) -> None:
        tracker = self._make_tracker()
        tracker.record("s1", "openai", "gpt-4", 100, 50)
        assert tracker.reset_session("s1")
        assert not tracker.reset_session("nonexistent")

    def test_stats(self) -> None:
        tracker = self._make_tracker()
        tracker.record("s1", "openai", "gpt-4", 100, 50)
        stats = tracker.stats()
        assert stats["total_records"] == 1
        assert stats["registered_rates"] == 2

    def test_multiple_providers(self) -> None:
        tracker = self._make_tracker()
        tracker.record("s1", "openai", "gpt-4", 1000, 500)
        tracker.record("s1", "anthropic", "claude-3", 1000, 500)
        summary = tracker.session_summary("s1")
        assert len(summary["by_model"]) == 2

    def test_severity_levels(self) -> None:
        tracker = self._make_tracker()
        # Test internal method
        assert tracker._severity_for_threshold(0.4) == AlertSeverity.INFO
        assert tracker._severity_for_threshold(0.75) == AlertSeverity.WARNING
        assert tracker._severity_for_threshold(0.9) == AlertSeverity.CRITICAL
        assert tracker._severity_for_threshold(1.0) == AlertSeverity.EMERGENCY
