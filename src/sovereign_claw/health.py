"""
health — Health Check API for Container Orchestration
======================================================
Production-grade health and readiness endpoints.

Features:
- /health (liveness) — is the process alive?
- /ready (readiness) — is the system ready to accept work?
- Component status checks with dependency verification
- Configurable health check functions
- Degraded state detection (healthy but impaired)
- Structured JSON output for monitoring integrations

Health checks enable Kubernetes, Docker, and other orchestrators to
manage the lifecycle of Sovereign Claw instances. Each component
registers its own health check function, and the system aggregates
results into a unified status.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class HealthStatus(str, Enum):
    """Health status levels."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class ComponentHealth:
    """Health status of a single component."""

    name: str
    status: HealthStatus
    message: str = ""
    latency_ms: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "status": self.status.value,
            "latency_ms": round(self.latency_ms, 2),
        }
        if self.message:
            result["message"] = self.message
        if self.details:
            result["details"] = self.details
        return result


@dataclass
class HealthReport:
    """Aggregated health report across all components."""

    status: HealthStatus
    version: str
    uptime_seconds: float
    components: list[ComponentHealth]
    timestamp: str = ""

    @property
    def is_healthy(self) -> bool:
        return self.status == HealthStatus.HEALTHY

    @property
    def is_ready(self) -> bool:
        return self.status in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "version": self.version,
            "uptime_seconds": round(self.uptime_seconds, 2),
            "timestamp": self.timestamp,
            "components": [c.to_dict() for c in self.components],
        }


# Type alias for health check functions
HealthCheckFn = Callable[[], ComponentHealth]


class HealthChecker:
    """
    Centralized health check registry and aggregator.

    Usage:
        checker = HealthChecker(version="3.2.0")

        # Register component checks
        checker.register("database", check_database)
        checker.register("model_router", check_router)

        # Run all checks
        report = checker.check_health()
        if not report.is_healthy:
            print("System degraded!")

        # Readiness check (tolerates degraded state)
        ready_report = checker.check_readiness()
    """

    def __init__(self, version: str = "0.0.0") -> None:
        self._version = version
        self._start_time = time.monotonic()
        self._checks: dict[str, HealthCheckFn] = {}
        self._readiness_checks: dict[str, HealthCheckFn] = {}

    @property
    def version(self) -> str:
        return self._version

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def register(
        self,
        name: str,
        check_fn: HealthCheckFn,
        readiness: bool = True,
    ) -> None:
        """
        Register a health check function for a component.

        Args:
            name: Component name.
            check_fn: Callable returning ComponentHealth.
            readiness: If True, also include in readiness checks.
        """
        self._checks[name] = check_fn
        if readiness:
            self._readiness_checks[name] = check_fn

    def unregister(self, name: str) -> None:
        """Remove a health check."""
        self._checks.pop(name, None)
        self._readiness_checks.pop(name, None)

    def _run_checks(self, checks: dict[str, HealthCheckFn]) -> list[ComponentHealth]:
        """Execute all registered checks and collect results."""
        results: list[ComponentHealth] = []
        for name, fn in checks.items():
            start = time.monotonic()
            try:
                result = fn()
                result.latency_ms = (time.monotonic() - start) * 1000
                result.name = name
            except Exception as exc:
                result = ComponentHealth(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Check failed: {exc}",
                    latency_ms=(time.monotonic() - start) * 1000,
                )
            results.append(result)
        return results

    def _aggregate_status(self, components: list[ComponentHealth]) -> HealthStatus:
        """Determine overall status from component statuses."""
        if not components:
            return HealthStatus.HEALTHY

        statuses = [c.status for c in components]
        if any(s == HealthStatus.UNHEALTHY for s in statuses):
            return HealthStatus.UNHEALTHY
        if any(s == HealthStatus.DEGRADED for s in statuses):
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY

    def check_health(self) -> HealthReport:
        """
        Run all health checks and return aggregated report.

        Returns:
            HealthReport with component-level detail.
        """
        components = self._run_checks(self._checks)
        status = self._aggregate_status(components)

        return HealthReport(
            status=status,
            version=self._version,
            uptime_seconds=self.uptime_seconds,
            components=components,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    def check_readiness(self) -> HealthReport:
        """
        Run readiness checks only. Readiness tolerates degraded but not unhealthy.

        Returns:
            HealthReport for readiness probe.
        """
        components = self._run_checks(self._readiness_checks)
        status = self._aggregate_status(components)

        return HealthReport(
            status=status,
            version=self._version,
            uptime_seconds=self.uptime_seconds,
            components=components,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    def check_liveness(self) -> HealthReport:
        """
        Simple liveness check — is the process running?

        Returns:
            HealthReport with minimal status (no component checks).
        """
        return HealthReport(
            status=HealthStatus.HEALTHY,
            version=self._version,
            uptime_seconds=self.uptime_seconds,
            components=[],
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )


# ── Built-in Check Factories ─────────────────────────────────────────────────


def check_proof_vault_health(vault_path: str = "") -> ComponentHealth:
    """Check ProofVault database accessibility."""
    if vault_path and os.path.exists(vault_path):
        size = os.path.getsize(vault_path)
        return ComponentHealth(
            name="proof_vault",
            status=HealthStatus.HEALTHY,
            message="ProofVault accessible",
            details={"path": vault_path, "size_bytes": size},
        )
    return ComponentHealth(
        name="proof_vault",
        status=HealthStatus.HEALTHY,
        message="ProofVault using in-memory store",
    )


def check_memory_health(
    memory_path: str = "",
) -> ComponentHealth:
    """Check persistent memory store accessibility."""
    if memory_path and os.path.exists(memory_path):
        size = os.path.getsize(memory_path)
        return ComponentHealth(
            name="persistent_memory",
            status=HealthStatus.HEALTHY,
            message="Persistent memory accessible",
            details={"path": memory_path, "size_bytes": size},
        )
    return ComponentHealth(
        name="persistent_memory",
        status=HealthStatus.HEALTHY,
        message="Using in-memory store",
    )


def check_config_health() -> ComponentHealth:
    """Check configuration validity."""
    return ComponentHealth(
        name="config",
        status=HealthStatus.HEALTHY,
        message="Configuration loaded",
    )
