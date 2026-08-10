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

    def test_reset_session_subtracts_totals(self) -> None:
        """reset_session must subtract the session's known totals, not rebuild from
        bounded _records (which would drop evicted records from other sessions)."""
        rates = ProviderRates(
            provider="p",
            model="m",
            prompt_cost_per_1k=1.0,
            completion_cost_per_1k=1.0,
        )
        tracker = UsageTracker()
        tracker.register_rates(rates)
        tracker.record("s1", "p", "m", 1000, 1000)
        tracker.record("s2", "p", "m", 2000, 2000)

        tokens_before_s1 = tracker.stats()["total_tokens"]
        s1_tokens = tracker.session_summary("s1")["total_tokens"]

        tracker.reset_session("s1")

        stats = tracker.stats()
        assert stats["total_tokens"] == tokens_before_s1 - s1_tokens
        assert stats["active_sessions"] == 1
        # s2 should still be fully tracked
        s2_summary = tracker.session_summary("s2")
        assert s2_summary["total_tokens"] == 2000 + 2000

    def test_reset_session_reconciles_daily_dedup(self) -> None:
        """reset_session must clear triggered_daily_limits for dates whose cost
        has dropped back below the daily limit."""
        alerts: list[BudgetAlert] = []
        rates = ProviderRates(
            provider="p",
            model="m",
            prompt_cost_per_1k=1.0,
            completion_cost_per_1k=1.0,
        )
        tracker = UsageTracker(
            budget=BudgetConfig(daily_cost_limit=0.005),
            alert_callback=lambda a: alerts.append(a),
        )
        tracker.register_rates(rates)
        # First record crosses the daily limit
        tracker.record("s1", "p", "m", 5000, 5000)
        daily_alerts_before = [a for a in alerts if a.alert_type == AlertType.DAILY_LIMIT]
        assert len(daily_alerts_before) >= 1

        # After resetting s1, daily spend drops below the limit.
        # The dedup flag should be cleared so a future crossing re-alerts.
        tracker.reset_session("s1")
        alerts.clear()
        tracker.record("s2", "p", "m", 5000, 5000)
        daily_alerts_after = [a for a in alerts if a.alert_type == AlertType.DAILY_LIMIT]
        assert len(daily_alerts_after) >= 1

    def test_reset_session_reconciles_threshold_dedup(self) -> None:
        """reset_session must clear triggered_thresholds for thresholds above the
        new utilization so that future crossings re-alert."""
        alerts: list[BudgetAlert] = []
        rates = ProviderRates(
            provider="p",
            model="m",
            prompt_cost_per_1k=1.0,
            completion_cost_per_1k=1.0,
        )
        tracker = UsageTracker(
            budget=BudgetConfig(max_cost=0.005, alert_thresholds=[0.5, 1.0]),
            alert_callback=lambda a: alerts.append(a),
        )
        tracker.register_rates(rates)
        tracker.record("s1", "p", "m", 5000, 5000)
        assert len(alerts) >= 1

        tracker.reset_session("s1")
        alerts.clear()
        # Record enough to cross 50 % threshold again
        tracker.record("s2", "p", "m", 5000, 5000)
        threshold_alerts = [a for a in alerts if a.alert_type == AlertType.THRESHOLD_REACHED]
        assert len(threshold_alerts) >= 1


class TestUsageTrackerEvictionAndResetAccuracy:
    """Regression tests for record-eviction safety and accurate accounting."""

    def _make_tracker_with_rates(self, max_records: int = 5) -> "UsageTracker":
        from sovereign_claw.usage_tracking import BudgetConfig, ProviderRates, UsageTracker

        tracker = UsageTracker(
            budget=BudgetConfig(max_cost=1000.0, daily_cost_limit=1000.0),
        )
        tracker.MAX_RECORDS = max_records
        tracker.register_rates(
            ProviderRates(
                provider="p",
                model="m",
                prompt_cost_per_1k=1.0,
                completion_cost_per_1k=1.0,
            )
        )
        return tracker

    def test_total_records_after_reset_excludes_only_reset_session(self) -> None:
        """reset_session must subtract the reset session's record count, not
        drop contributions from other sessions."""
        from sovereign_claw.usage_tracking import BudgetConfig, ProviderRates, UsageTracker

        tracker = UsageTracker(budget=BudgetConfig())
        tracker.register_rates(ProviderRates(provider="p", model="m"))

        tracker.record("s1", "p", "m", 100, 100)
        tracker.record("s1", "p", "m", 100, 100)
        tracker.record("s2", "p", "m", 100, 100)

        assert tracker._total_records == 3

        tracker.reset_session("s1")

        # s2's record must still be counted.
        assert tracker._total_records == 1

    def test_total_records_correct_after_eviction_and_reset(self) -> None:
        """When records are evicted from _records due to MAX_RECORDS cap,
        _total_records must still account for the evicted records from other sessions."""
        from sovereign_claw.usage_tracking import BudgetConfig, ProviderRates, UsageTracker

        tracker = UsageTracker(budget=BudgetConfig())
        tracker.MAX_RECORDS = 3  # tiny cap to force eviction
        tracker.register_rates(ProviderRates(provider="p", model="m"))

        # s2 contributes 4 records — 1 will be evicted from _records.
        tracker.record("s2", "p", "m", 10, 10)
        tracker.record("s2", "p", "m", 10, 10)
        tracker.record("s2", "p", "m", 10, 10)
        tracker.record("s2", "p", "m", 10, 10)

        # s1 adds 2 records (these are in _records since MAX_RECORDS=3 keeps last 3 overall).
        tracker.record("s1", "p", "m", 10, 10)
        tracker.record("s1", "p", "m", 10, 10)

        # Lifetime total is 6 records.
        assert tracker._total_records == 6

        # Resetting s1 should subtract exactly 2.
        tracker.reset_session("s1")
        assert tracker._total_records == 4

    def test_daily_totals_correct_after_eviction_and_reset(self) -> None:
        """Daily totals must remain exact after record eviction when a session is reset.

        The per-session/per-day cost breakdown must be used (not reconstructed
        from bounded _records) so that evicted records do not create gaps.
        """
        from sovereign_claw.usage_tracking import BudgetConfig, ProviderRates, UsageTracker

        tracker = UsageTracker(budget=BudgetConfig(daily_cost_limit=1000.0))
        tracker.MAX_RECORDS = 2  # force early eviction
        tracker.register_rates(
            ProviderRates(
                provider="p", model="m", prompt_cost_per_1k=1.0, completion_cost_per_1k=1.0
            )
        )

        # s2 contributes 3 records worth $6 total today (will be partially evicted).
        tracker.record("s2", "p", "m", 1000, 1000)
        tracker.record("s2", "p", "m", 1000, 1000)
        tracker.record("s2", "p", "m", 1000, 1000)
        s2_cost = tracker._session_totals["s2"]["cost"]

        # s1 contributes 1 record (in _records since MAX_RECORDS=2 keeps last 2).
        tracker.record("s1", "p", "m", 1000, 1000)
        s1_cost = tracker._session_totals["s1"]["cost"]

        # Reset s1 — daily totals must decrease only by s1's contribution.
        today = list(tracker._daily_totals.keys())[0]
        daily_before = tracker._daily_totals[today]
        tracker.reset_session("s1")
        daily_after = tracker._daily_totals[today]

        assert abs(daily_before - daily_after - s1_cost) < 1e-9
        # s2's totals are untouched.
        assert abs(tracker._total_cost - s2_cost) < 1e-9

    def test_alerts_bounded_by_max_alerts(self) -> None:
        """_alerts list must not grow beyond MAX_ALERTS."""
        from sovereign_claw.usage_tracking import (
            BudgetAlert,
            BudgetConfig,
            UsageTracker,
        )

        tracker = UsageTracker(budget=BudgetConfig())
        tracker.MAX_ALERTS = 5

        for i in range(20):
            tracker._emit_alert(BudgetAlert(message=f"alert {i}"))

        assert len(tracker._alerts) <= tracker.MAX_ALERTS

    def test_reset_session_total_cost_not_affected_by_other_sessions(self) -> None:
        """reset_session must not alter other sessions' contribution to _total_cost."""
        from sovereign_claw.usage_tracking import BudgetConfig, ProviderRates, UsageTracker

        tracker = UsageTracker(budget=BudgetConfig())
        tracker.register_rates(
            ProviderRates(
                provider="p", model="m", prompt_cost_per_1k=2.0, completion_cost_per_1k=2.0
            )
        )
        tracker.record("s1", "p", "m", 500, 500)
        tracker.record("s2", "p", "m", 500, 500)
        tracker.record("s3", "p", "m", 500, 500)

        s2_cost = tracker._session_totals["s2"]["cost"]
        s3_cost = tracker._session_totals["s3"]["cost"]

        tracker.reset_session("s1")

        assert abs(tracker._total_cost - (s2_cost + s3_cost)) < 1e-9
