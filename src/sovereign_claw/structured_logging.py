"""
structured_logging — JSON Structured Logging with Correlation IDs
=================================================================
Production-grade logging infrastructure for governed execution.

Features:
- JSON-formatted log output for machine parsing
- Correlation IDs for request tracing across modules
- Trace context propagation (trace_id, span_id, parent_span_id)
- Configurable formatters (JSON, human-readable, compact)
- Context-local storage for automatic field injection
- Log level filtering with governed defaults

Every log entry includes:
- timestamp (ISO-8601)
- level
- module / function / line
- correlation_id (auto-generated or inherited)
- trace context (if active)
- structured payload
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ── Context Variables ─────────────────────────────────────────────────────────

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("span_id", default=None)
_parent_span_id: ContextVar[str | None] = ContextVar("parent_span_id", default=None)
_extra_fields: ContextVar[dict[str, Any] | None] = ContextVar("extra_fields", default=None)


class LogFormat(str, Enum):
    """Supported log output formats."""

    JSON = "json"
    HUMAN = "human"
    COMPACT = "compact"


# ── Trace Context ─────────────────────────────────────────────────────────────


@dataclass
class TraceContext:
    """Distributed trace context for cross-module correlation."""

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    parent_span_id: str | None = None
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    extra: dict[str, Any] = field(default_factory=dict)

    def child_span(self) -> TraceContext:
        """Create a child span inheriting trace_id and correlation_id."""
        return TraceContext(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:8],
            parent_span_id=self.span_id,
            correlation_id=self.correlation_id,
            extra=dict(self.extra),
        )

    def activate(self) -> None:
        """Push this context into context-local storage."""
        _correlation_id.set(self.correlation_id)
        _trace_id.set(self.trace_id)
        _span_id.set(self.span_id)
        _parent_span_id.set(self.parent_span_id)

    @staticmethod
    def current() -> TraceContext:
        """Read the current active trace context."""
        return TraceContext(
            trace_id=_trace_id.get() or uuid.uuid4().hex[:16],
            span_id=_span_id.get() or uuid.uuid4().hex[:8],
            parent_span_id=_parent_span_id.get(),
            correlation_id=_correlation_id.get() or uuid.uuid4().hex[:12],
        )


# ── JSON Formatter ────────────────────────────────────────────────────────────


class JSONLogFormatter(logging.Formatter):
    """Formats log records as single-line JSON with trace context."""

    def __init__(self, include_trace: bool = True) -> None:
        super().__init__()
        self._include_trace = include_trace

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }

        # Inject correlation ID
        cid = _correlation_id.get()
        if cid:
            entry["correlation_id"] = cid

        # Inject trace context
        if self._include_trace:
            tid = _trace_id.get()
            sid = _span_id.get()
            psid = _parent_span_id.get()
            if tid:
                entry["trace_id"] = tid
            if sid:
                entry["span_id"] = sid
            if psid:
                entry["parent_span_id"] = psid

        # Inject extra fields from context
        ctx_extra = _extra_fields.get()
        if ctx_extra is not None and ctx_extra:
            entry["context"] = ctx_extra

        # Inject record extras (structured payload)
        if hasattr(record, "structured"):
            entry["data"] = record.structured  # type: ignore[attr-defined]

        # Exception info
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str, separators=(",", ":"))


class HumanLogFormatter(logging.Formatter):
    """Human-readable formatter with optional correlation ID prefix."""

    _LEVEL_COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.gmtime(record.created))
        color = self._LEVEL_COLORS.get(record.levelname, "")
        reset = self._RESET if color else ""
        cid = _correlation_id.get()
        cid_prefix = f"[{cid[:8]}] " if cid else ""
        return (
            f"{ts} {color}{record.levelname:<8}{reset} "
            f"{cid_prefix}{record.name}: {record.getMessage()}"
        )


class CompactLogFormatter(logging.Formatter):
    """Minimal single-line formatter for high-throughput logging."""

    def format(self, record: logging.LogRecord) -> str:
        cid = _correlation_id.get()
        cid_part = f" cid={cid[:8]}" if cid else ""
        return (
            f"{record.levelname[0]} {record.name}:{record.lineno}{cid_part} {record.getMessage()}"
        )


# ── Governed Logger ───────────────────────────────────────────────────────────


class GovernedLogger:
    """
    Wrapper providing structured logging with automatic trace context.

    Usage:
        log = GovernedLogger("sovereign_claw.orchestrator")
        log.info("Step executed", step=3, drift=0.12)
        log.warning("Drift threshold approaching", drift=0.45, threshold=0.5)
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def _log(self, level: int, msg: str, **kwargs: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        record = self._logger.makeRecord(
            self._name,
            level,
            "(structured)",
            0,
            msg,
            (),
            None,
        )
        if kwargs:
            record.structured = kwargs  # type: ignore[attr-defined]
        self._logger.handle(record)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, **kwargs)

    def critical(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, msg, **kwargs)


# ── Configuration ─────────────────────────────────────────────────────────────

# Track whether configure_logging has been called to prevent duplicate setup
_configured = False
_configure_lock = threading.Lock()


def configure_logging(
    level: int = logging.INFO,
    log_format: LogFormat = LogFormat.JSON,
    include_trace: bool = True,
    logger_name: str | None = None,
) -> None:
    """
    Configure the sovereign_claw logging infrastructure.

    Args:
        level: Minimum log level (default: INFO).
        log_format: Output format (JSON, HUMAN, COMPACT).
        include_trace: Include trace context in JSON output.
        logger_name: Specific logger to configure (default: root sovereign_claw).
    """
    global _configured

    with _configure_lock:
        target = logger_name or "sovereign_claw"
        logger = logging.getLogger(target)
        logger.setLevel(level)

        # Remove existing handlers to avoid duplicates
        logger.handlers.clear()

        handler = logging.StreamHandler()
        handler.setLevel(level)

        if log_format == LogFormat.JSON:
            handler.setFormatter(JSONLogFormatter(include_trace=include_trace))
        elif log_format == LogFormat.HUMAN:
            handler.setFormatter(HumanLogFormatter())
        else:
            handler.setFormatter(CompactLogFormatter())

        logger.addHandler(handler)
        logger.propagate = False
        _configured = True


def set_correlation_id(cid: str | None = None) -> str:
    """Set or generate a correlation ID for the current context."""
    cid = cid or uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str | None:
    """Get the current correlation ID."""
    return _correlation_id.get()


def set_extra_fields(**kwargs: Any) -> None:
    """Set extra fields that will be injected into all log entries."""
    current = dict(_extra_fields.get() or {})
    current.update(kwargs)
    _extra_fields.set(current)


def clear_extra_fields() -> None:
    """Clear all extra context fields."""
    _extra_fields.set({})


def get_logger(name: str) -> GovernedLogger:
    """Get a governed logger instance."""
    return GovernedLogger(name)
