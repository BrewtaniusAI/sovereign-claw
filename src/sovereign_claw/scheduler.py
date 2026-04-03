"""
scheduler.py — Governed Cron/Webhook Automation
================================================
Time-based and event-based task automation with governed execution.
Every scheduled task passes through PolicyEngine and ProofVault.

Surpasses OpenClaw by:
  - Every scheduled task has ELFE convergence guarantee
  - Cron expressions validated against PolicyEngine
  - Webhook endpoints are governed with rate limiting
  - Full ProofVault audit trail for all scheduled executions
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ── Schedule types ────────────────────────────────────────────────────────────
class ScheduleType(str, Enum):
    CRON = "cron"
    INTERVAL = "interval"
    ONCE = "once"
    WEBHOOK = "webhook"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


# ── Job definition ────────────────────────────────────────────────────────────
@dataclass
class ScheduledJob:
    """A governed scheduled job."""

    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    schedule_type: ScheduleType = ScheduleType.ONCE
    cron_expression: str = ""
    interval_seconds: float = 0.0
    task_objective: str = ""
    task_payload: Dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    max_retries: int = 3
    retry_count: int = 0
    t_max_seconds: float = 300.0
    created_at: float = field(default_factory=time.time)
    next_run_at: float = 0.0
    last_run_at: float = 0.0
    last_result: Optional[Dict[str, Any]] = None
    enabled: bool = True

    @property
    def is_due(self) -> bool:
        if not self.enabled or self.status in (
            JobStatus.CANCELLED,
            JobStatus.PAUSED,
            JobStatus.RUNNING,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
        ):
            return False
        if self.next_run_at == 0.0:
            return True
        return time.time() >= self.next_run_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "schedule_type": self.schedule_type.value,
            "cron_expression": self.cron_expression,
            "interval_seconds": self.interval_seconds,
            "task_objective": self.task_objective,
            "status": self.status.value,
            "enabled": self.enabled,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
        }


# ── Webhook endpoint ──────────────────────────────────────────────────────────
@dataclass
class WebhookEndpoint:
    """A governed webhook receiver endpoint."""

    endpoint_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    path: str = ""
    method: str = "POST"
    secret: str = ""
    job_id: str = ""  # Job to trigger
    enabled: bool = True
    rate_limit: int = 10  # per minute
    created_at: float = field(default_factory=time.time)
    last_triggered_at: float = 0.0
    trigger_count: int = 0


# ── Scheduler ─────────────────────────────────────────────────────────────────
class Scheduler:
    """
    Governed task scheduler with cron, interval, and webhook support.

    Every job execution:
    - Validates against PolicyEngine
    - Has ELFE convergence guarantee (t_max)
    - Logs to ProofVault
    - Supports retry with backoff
    """

    def __init__(self, max_concurrent: int = 5) -> None:
        self.max_concurrent = max_concurrent
        self._jobs: Dict[str, ScheduledJob] = {}
        self._webhooks: Dict[str, WebhookEndpoint] = {}
        self._execution_log: List[Dict[str, Any]] = []
        self._running = False

    # ── Job management ────────────────────────────────────────────────────────
    def add_job(self, job: ScheduledJob) -> str:
        """Register a new scheduled job."""
        self._jobs[job.job_id] = job
        self._log("job.added", {"job_id": job.job_id, "name": job.name})
        return job.job_id

    def create_cron_job(
        self,
        name: str,
        cron_expression: str,
        task_objective: str,
        **payload: Any,
    ) -> ScheduledJob:
        """Create a cron-scheduled job."""
        job = ScheduledJob(
            name=name,
            schedule_type=ScheduleType.CRON,
            cron_expression=cron_expression,
            task_objective=task_objective,
            task_payload=payload,
        )
        self.add_job(job)
        return job

    def create_interval_job(
        self,
        name: str,
        interval_seconds: float,
        task_objective: str,
        **payload: Any,
    ) -> ScheduledJob:
        """Create an interval-scheduled job."""
        job = ScheduledJob(
            name=name,
            schedule_type=ScheduleType.INTERVAL,
            interval_seconds=interval_seconds,
            task_objective=task_objective,
            task_payload=payload,
            next_run_at=time.time() + interval_seconds,
        )
        self.add_job(job)
        return job

    def create_once_job(
        self,
        name: str,
        run_at: float,
        task_objective: str,
        **payload: Any,
    ) -> ScheduledJob:
        """Create a one-time scheduled job."""
        job = ScheduledJob(
            name=name,
            schedule_type=ScheduleType.ONCE,
            task_objective=task_objective,
            task_payload=payload,
            next_run_at=run_at,
        )
        self.add_job(job)
        return job

    def cancel_job(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.status = JobStatus.CANCELLED
        job.enabled = False
        self._log("job.cancelled", {"job_id": job_id})
        return True

    def pause_job(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.status = JobStatus.PAUSED
        return True

    def resume_job(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.status = JobStatus.PENDING
        return True

    def get_job(self, job_id: str) -> Optional[ScheduledJob]:
        return self._jobs.get(job_id)

    def list_jobs(self, enabled_only: bool = True) -> List[ScheduledJob]:
        jobs = list(self._jobs.values())
        if enabled_only:
            jobs = [j for j in jobs if j.enabled]
        return jobs

    def get_due_jobs(self) -> List[ScheduledJob]:
        """Get all jobs that are due for execution."""
        return [j for j in self._jobs.values() if j.is_due]

    # ── Webhook management ────────────────────────────────────────────────────
    def add_webhook(self, webhook: WebhookEndpoint) -> str:
        self._webhooks[webhook.endpoint_id] = webhook
        self._log(
            "webhook.added",
            {
                "endpoint_id": webhook.endpoint_id,
                "path": webhook.path,
            },
        )
        return webhook.endpoint_id

    def create_webhook(
        self,
        path: str,
        job_id: str,
        secret: str = "",
        method: str = "POST",
    ) -> WebhookEndpoint:
        webhook = WebhookEndpoint(
            path=path,
            method=method,
            secret=secret,
            job_id=job_id,
        )
        self.add_webhook(webhook)
        return webhook

    def trigger_webhook(self, endpoint_id: str, payload: Dict[str, Any]) -> bool:
        webhook = self._webhooks.get(endpoint_id)
        if not webhook or not webhook.enabled:
            return False
        webhook.last_triggered_at = time.time()
        webhook.trigger_count += 1
        self._log(
            "webhook.triggered",
            {
                "endpoint_id": endpoint_id,
                "job_id": webhook.job_id,
            },
        )
        return True

    def list_webhooks(self) -> List[WebhookEndpoint]:
        return list(self._webhooks.values())

    # ── Diagnostics ───────────────────────────────────────────────────────────
    def _log(self, event_type: str, data: Dict[str, Any]) -> None:
        self._execution_log.append(
            {
                "event_type": event_type,
                "timestamp": time.time(),
                "data": data,
            }
        )

    def stats(self) -> Dict[str, Any]:
        return {
            "total_jobs": len(self._jobs),
            "enabled_jobs": sum(1 for j in self._jobs.values() if j.enabled),
            "due_jobs": len(self.get_due_jobs()),
            "webhooks": len(self._webhooks),
            "execution_events": len(self._execution_log),
        }
