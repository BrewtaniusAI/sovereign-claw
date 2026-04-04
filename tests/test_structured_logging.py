"""Tests for structured_logging module."""

from __future__ import annotations

import json
import logging
import uuid

from sovereign_claw.structured_logging import (
    CompactLogFormatter,
    GovernedLogger,
    HumanLogFormatter,
    JSONLogFormatter,
    LogFormat,
    TraceContext,
    clear_extra_fields,
    configure_logging,
    get_correlation_id,
    get_logger,
    set_correlation_id,
    set_extra_fields,
    _correlation_id,
    _trace_id,
    _span_id,
    _parent_span_id,
    _extra_fields,
)


# ── TraceContext ──────────────────────────────────────────────────────────────


class TestTraceContext:
    def test_default_fields(self) -> None:
        ctx = TraceContext()
        assert len(ctx.trace_id) == 16
        assert len(ctx.span_id) == 8
        assert ctx.parent_span_id is None
        assert len(ctx.correlation_id) == 12

    def test_child_span_inherits_trace_id(self) -> None:
        parent = TraceContext()
        child = parent.child_span()
        assert child.trace_id == parent.trace_id
        assert child.correlation_id == parent.correlation_id
        assert child.parent_span_id == parent.span_id
        assert child.span_id != parent.span_id

    def test_child_span_inherits_extra(self) -> None:
        parent = TraceContext(extra={"tenant": "acme"})
        child = parent.child_span()
        assert child.extra == {"tenant": "acme"}
        # Mutating child should not affect parent
        child.extra["foo"] = "bar"
        assert "foo" not in parent.extra

    def test_activate_sets_context_vars(self) -> None:
        ctx = TraceContext(
            trace_id="t123",
            span_id="s456",
            parent_span_id="p789",
            correlation_id="c012",
        )
        ctx.activate()
        assert _correlation_id.get() == "c012"
        assert _trace_id.get() == "t123"
        assert _span_id.get() == "s456"
        assert _parent_span_id.get() == "p789"

    def test_current_reads_active_context(self) -> None:
        ctx = TraceContext(trace_id="abc", span_id="def", correlation_id="ghi")
        ctx.activate()
        current = TraceContext.current()
        assert current.trace_id == "abc"
        assert current.span_id == "def"
        assert current.correlation_id == "ghi"

    def test_current_generates_defaults_if_none(self) -> None:
        _trace_id.set(None)
        _span_id.set(None)
        _correlation_id.set(None)
        current = TraceContext.current()
        assert len(current.trace_id) == 16
        assert len(current.span_id) == 8
        assert len(current.correlation_id) == 12


# ── Correlation ID management ────────────────────────────────────────────────


class TestCorrelationId:
    def test_set_generates_id(self) -> None:
        cid = set_correlation_id()
        assert len(cid) == 12
        assert get_correlation_id() == cid

    def test_set_uses_provided_id(self) -> None:
        cid = set_correlation_id("custom-id")
        assert cid == "custom-id"
        assert get_correlation_id() == "custom-id"

    def test_get_returns_none_when_unset(self) -> None:
        _correlation_id.set(None)
        assert get_correlation_id() is None


# ── Extra fields ─────────────────────────────────────────────────────────────


class TestExtraFields:
    def test_set_and_clear(self) -> None:
        clear_extra_fields()
        set_extra_fields(tenant="acme", region="us-east")
        ctx = _extra_fields.get()
        assert ctx["tenant"] == "acme"
        assert ctx["region"] == "us-east"

        clear_extra_fields()
        assert _extra_fields.get() == {}

    def test_set_merges_fields(self) -> None:
        clear_extra_fields()
        set_extra_fields(a=1)
        set_extra_fields(b=2)
        ctx = _extra_fields.get()
        assert ctx["a"] == 1
        assert ctx["b"] == 2


# ── JSONLogFormatter ─────────────────────────────────────────────────────────


class TestJSONLogFormatter:
    def setup_method(self) -> None:
        _correlation_id.set(None)
        _trace_id.set(None)
        _span_id.set(None)
        _parent_span_id.set(None)
        clear_extra_fields()

    def test_basic_json_output(self) -> None:
        formatter = JSONLogFormatter(include_trace=False)
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Hello %s",
            args=("world",),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"
        assert data["message"] == "Hello world"
        assert "timestamp" in data

    def test_includes_correlation_id(self) -> None:
        set_correlation_id("test-cid")
        formatter = JSONLogFormatter()
        record = logging.LogRecord("test", logging.INFO, "test.py", 1, "msg", (), None)
        data = json.loads(formatter.format(record))
        assert data["correlation_id"] == "test-cid"

    def test_includes_trace_context(self) -> None:
        ctx = TraceContext(trace_id="t1", span_id="s1", parent_span_id="p1")
        ctx.activate()
        formatter = JSONLogFormatter(include_trace=True)
        record = logging.LogRecord("test", logging.INFO, "test.py", 1, "msg", (), None)
        data = json.loads(formatter.format(record))
        assert data["trace_id"] == "t1"
        assert data["span_id"] == "s1"
        assert data["parent_span_id"] == "p1"

    def test_excludes_trace_when_disabled(self) -> None:
        ctx = TraceContext(trace_id="t1")
        ctx.activate()
        formatter = JSONLogFormatter(include_trace=False)
        record = logging.LogRecord("test", logging.INFO, "test.py", 1, "msg", (), None)
        data = json.loads(formatter.format(record))
        assert "trace_id" not in data

    def test_includes_extra_fields(self) -> None:
        set_extra_fields(tenant="acme")
        formatter = JSONLogFormatter(include_trace=False)
        record = logging.LogRecord("test", logging.INFO, "test.py", 1, "msg", (), None)
        data = json.loads(formatter.format(record))
        assert data["context"]["tenant"] == "acme"

    def test_includes_structured_data(self) -> None:
        formatter = JSONLogFormatter(include_trace=False)
        record = logging.LogRecord("test", logging.INFO, "test.py", 1, "msg", (), None)
        record.structured = {"drift": 0.42}  # type: ignore[attr-defined]
        data = json.loads(formatter.format(record))
        assert data["data"]["drift"] == 0.42

    def test_includes_exception_info(self) -> None:
        formatter = JSONLogFormatter(include_trace=False)
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            record = logging.LogRecord(
                "test", logging.ERROR, "test.py", 1, "error", (), sys.exc_info()
            )
        data = json.loads(formatter.format(record))
        assert "ValueError: test error" in data["exception"]


# ── HumanLogFormatter ────────────────────────────────────────────────────────


class TestHumanLogFormatter:
    def test_basic_format(self) -> None:
        _correlation_id.set(None)
        formatter = HumanLogFormatter()
        record = logging.LogRecord("test.logger", logging.INFO, "test.py", 1, "hello", (), None)
        output = formatter.format(record)
        assert "INFO" in output
        assert "test.logger" in output
        assert "hello" in output

    def test_includes_correlation_prefix(self) -> None:
        set_correlation_id("abcdef123456")
        formatter = HumanLogFormatter()
        record = logging.LogRecord("test", logging.WARNING, "test.py", 1, "warning msg", (), None)
        output = formatter.format(record)
        assert "[abcdef12]" in output


# ── CompactLogFormatter ──────────────────────────────────────────────────────


class TestCompactLogFormatter:
    def test_basic_format(self) -> None:
        _correlation_id.set(None)
        formatter = CompactLogFormatter()
        record = logging.LogRecord("test.mod", logging.DEBUG, "test.py", 42, "debug msg", (), None)
        output = formatter.format(record)
        assert output.startswith("D ")
        assert "test.mod:42" in output
        assert "debug msg" in output

    def test_includes_cid(self) -> None:
        set_correlation_id("abcdef123456")
        formatter = CompactLogFormatter()
        record = logging.LogRecord("test", logging.ERROR, "test.py", 1, "err", (), None)
        output = formatter.format(record)
        assert "cid=abcdef12" in output


# ── GovernedLogger ───────────────────────────────────────────────────────────


class TestGovernedLogger:
    def test_name_property(self) -> None:
        log = GovernedLogger("test.module")
        assert log.name == "test.module"

    def test_all_log_levels(self) -> None:
        log = GovernedLogger("test.levels")
        logger = logging.getLogger("test.levels")
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)

        # These should not raise
        log.debug("debug msg", key="value")
        log.info("info msg")
        log.warning("warn msg")
        log.error("error msg")
        log.critical("critical msg")

        logger.handlers.clear()


# ── configure_logging ────────────────────────────────────────────────────────


class TestConfigureLogging:
    def test_configure_json(self) -> None:
        configure_logging(level=logging.DEBUG, log_format=LogFormat.JSON)
        logger = logging.getLogger("sovereign_claw")
        assert logger.level == logging.DEBUG
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, JSONLogFormatter)

    def test_configure_human(self) -> None:
        configure_logging(log_format=LogFormat.HUMAN)
        logger = logging.getLogger("sovereign_claw")
        assert isinstance(logger.handlers[0].formatter, HumanLogFormatter)

    def test_configure_compact(self) -> None:
        configure_logging(log_format=LogFormat.COMPACT)
        logger = logging.getLogger("sovereign_claw")
        assert isinstance(logger.handlers[0].formatter, CompactLogFormatter)

    def test_configure_specific_logger(self) -> None:
        name = f"test_specific_{uuid.uuid4().hex[:6]}"
        configure_logging(log_format=LogFormat.JSON, logger_name=name)
        logger = logging.getLogger(name)
        assert len(logger.handlers) == 1
        assert not logger.propagate


# ── get_logger ───────────────────────────────────────────────────────────────


class TestGetLogger:
    def test_returns_governed_logger(self) -> None:
        log = get_logger("test.get")
        assert isinstance(log, GovernedLogger)
        assert log.name == "test.get"
