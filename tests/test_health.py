"""Tests for health module."""

from __future__ import annotations

import time

import pytest

from sovereign_claw.health import (
    ComponentHealth,
    HealthChecker,
    HealthReport,
    HealthStatus,
    check_config_health,
    check_memory_health,
    check_proof_vault_health,
)


# ── ComponentHealth ──────────────────────────────────────────────────────────


class TestComponentHealth:
    def test_to_dict_minimal(self) -> None:
        ch = ComponentHealth(name="test", status=HealthStatus.HEALTHY)
        d = ch.to_dict()
        assert d["name"] == "test"
        assert d["status"] == "healthy"
        assert d["latency_ms"] == 0.0
        assert "message" not in d
        assert "details" not in d

    def test_to_dict_full(self) -> None:
        ch = ComponentHealth(
            name="db",
            status=HealthStatus.DEGRADED,
            message="Slow queries",
            latency_ms=150.123,
            details={"avg_query_ms": 120},
        )
        d = ch.to_dict()
        assert d["status"] == "degraded"
        assert d["message"] == "Slow queries"
        assert d["latency_ms"] == 150.12
        assert d["details"]["avg_query_ms"] == 120


# ── HealthReport ─────────────────────────────────────────────────────────────


class TestHealthReport:
    def test_is_healthy(self) -> None:
        report = HealthReport(
            status=HealthStatus.HEALTHY,
            version="3.2.0",
            uptime_seconds=100.0,
            components=[],
        )
        assert report.is_healthy is True
        assert report.is_ready is True

    def test_is_degraded(self) -> None:
        report = HealthReport(
            status=HealthStatus.DEGRADED,
            version="3.2.0",
            uptime_seconds=100.0,
            components=[],
        )
        assert report.is_healthy is False
        assert report.is_ready is True

    def test_is_unhealthy(self) -> None:
        report = HealthReport(
            status=HealthStatus.UNHEALTHY,
            version="3.2.0",
            uptime_seconds=100.0,
            components=[],
        )
        assert report.is_healthy is False
        assert report.is_ready is False

    def test_to_dict(self) -> None:
        comp = ComponentHealth(name="test", status=HealthStatus.HEALTHY)
        report = HealthReport(
            status=HealthStatus.HEALTHY,
            version="3.2.0",
            uptime_seconds=42.5,
            components=[comp],
            timestamp="2026-01-01T00:00:00Z",
        )
        d = report.to_dict()
        assert d["version"] == "3.2.0"
        assert d["uptime_seconds"] == 42.5
        assert len(d["components"]) == 1


# ── HealthChecker ────────────────────────────────────────────────────────────


class TestHealthChecker:
    def test_version_and_uptime(self) -> None:
        checker = HealthChecker(version="3.2.0")
        assert checker.version == "3.2.0"
        time.sleep(0.01)
        assert checker.uptime_seconds > 0

    def test_check_liveness(self) -> None:
        checker = HealthChecker(version="3.2.0")
        report = checker.check_liveness()
        assert report.status == HealthStatus.HEALTHY
        assert report.components == []

    def test_check_health_no_components(self) -> None:
        checker = HealthChecker(version="3.2.0")
        report = checker.check_health()
        assert report.is_healthy is True

    def test_check_health_all_healthy(self) -> None:
        checker = HealthChecker(version="3.2.0")

        def check_ok() -> ComponentHealth:
            return ComponentHealth(name="ok", status=HealthStatus.HEALTHY)

        checker.register("comp1", check_ok)
        checker.register("comp2", check_ok)
        report = checker.check_health()
        assert report.is_healthy is True
        assert len(report.components) == 2

    def test_check_health_one_degraded(self) -> None:
        checker = HealthChecker(version="3.2.0")

        def check_ok() -> ComponentHealth:
            return ComponentHealth(name="ok", status=HealthStatus.HEALTHY)

        def check_slow() -> ComponentHealth:
            return ComponentHealth(
                name="slow", status=HealthStatus.DEGRADED, message="High latency"
            )

        checker.register("comp1", check_ok)
        checker.register("comp2", check_slow)
        report = checker.check_health()
        assert report.status == HealthStatus.DEGRADED
        assert report.is_ready is True

    def test_check_health_one_unhealthy(self) -> None:
        checker = HealthChecker(version="3.2.0")

        def check_ok() -> ComponentHealth:
            return ComponentHealth(name="ok", status=HealthStatus.HEALTHY)

        def check_down() -> ComponentHealth:
            return ComponentHealth(
                name="down", status=HealthStatus.UNHEALTHY, message="Connection refused"
            )

        checker.register("comp1", check_ok)
        checker.register("comp2", check_down)
        report = checker.check_health()
        assert report.status == HealthStatus.UNHEALTHY
        assert report.is_ready is False

    def test_check_handles_exception(self) -> None:
        checker = HealthChecker(version="3.2.0")

        def check_crash() -> ComponentHealth:
            raise RuntimeError("Boom!")

        checker.register("crasher", check_crash)
        report = checker.check_health()
        assert report.status == HealthStatus.UNHEALTHY
        assert "Boom!" in report.components[0].message

    def test_readiness_separate_from_health(self) -> None:
        checker = HealthChecker(version="3.2.0")

        def check_ok() -> ComponentHealth:
            return ComponentHealth(name="ok", status=HealthStatus.HEALTHY)

        def check_optional() -> ComponentHealth:
            return ComponentHealth(
                name="optional", status=HealthStatus.UNHEALTHY, message="Not ready"
            )

        checker.register("core", check_ok, readiness=True)
        checker.register("optional", check_optional, readiness=False)

        # Health includes both
        health_report = checker.check_health()
        assert health_report.status == HealthStatus.UNHEALTHY
        assert len(health_report.components) == 2

        # Readiness only includes core
        ready_report = checker.check_readiness()
        assert ready_report.status == HealthStatus.HEALTHY
        assert len(ready_report.components) == 1

    def test_unregister(self) -> None:
        checker = HealthChecker(version="3.2.0")

        def check_ok() -> ComponentHealth:
            return ComponentHealth(name="ok", status=HealthStatus.HEALTHY)

        checker.register("comp1", check_ok)
        assert len(checker.check_health().components) == 1

        checker.unregister("comp1")
        assert len(checker.check_health().components) == 0

    def test_latency_measured(self) -> None:
        checker = HealthChecker(version="3.2.0")

        def slow_check() -> ComponentHealth:
            time.sleep(0.01)
            return ComponentHealth(name="slow", status=HealthStatus.HEALTHY)

        checker.register("slow", slow_check)
        report = checker.check_health()
        assert report.components[0].latency_ms >= 5  # At least 5ms

    def test_timestamp_in_report(self) -> None:
        checker = HealthChecker(version="3.2.0")
        report = checker.check_health()
        assert report.timestamp != ""
        assert "T" in report.timestamp


# ── Built-in check factories ────────────────────────────────────────────────


class TestBuiltinChecks:
    def test_proof_vault_in_memory(self) -> None:
        result = check_proof_vault_health("")
        assert result.status == HealthStatus.HEALTHY
        assert "in-memory" in result.message

    def test_proof_vault_with_file(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = str(tmp_path / "vault.db")  # type: ignore[operator]
        with open(db_path, "w") as f:
            f.write("test")
        result = check_proof_vault_health(db_path)
        assert result.status == HealthStatus.HEALTHY
        assert result.details["size_bytes"] > 0

    def test_memory_health_in_memory(self) -> None:
        result = check_memory_health("")
        assert result.status == HealthStatus.HEALTHY

    def test_config_health(self) -> None:
        result = check_config_health()
        assert result.status == HealthStatus.HEALTHY
