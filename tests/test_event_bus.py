"""Tests for event_bus module."""

from __future__ import annotations

from collections.abc import Callable

from sovereign_claw.event_bus import (
    BusEvent,
    EventBus,
    EventBusStats,
    EventPriority,
    EventStatus,
    Subscription,
)


# ── BusEvent ─────────────────────────────────────────────────────────────────


class TestBusEvent:
    def test_auto_generates_event_id(self) -> None:
        event = BusEvent(event_type="test", payload={})
        assert event.event_id.startswith("evt_")

    def test_custom_event_id(self) -> None:
        event = BusEvent(event_type="test", payload={}, event_id="custom-id")
        assert event.event_id == "custom-id"

    def test_to_dict(self) -> None:
        event = BusEvent(
            event_type="drift.exceeded",
            payload={"drift": 0.85},
            source="orchestrator",
            priority=EventPriority.HIGH,
            correlation_id="cid-123",
        )
        d = event.to_dict()
        assert d["event_type"] == "drift.exceeded"
        assert d["source"] == "orchestrator"
        assert d["priority"] == "HIGH"
        assert d["payload"]["drift"] == 0.85
        assert d["correlation_id"] == "cid-123"

    def test_default_status_pending(self) -> None:
        event = BusEvent(event_type="test", payload={})
        assert event.status == EventStatus.PENDING


# ── Subscription ─────────────────────────────────────────────────────────────


class TestSubscription:
    def test_exact_match(self) -> None:
        sub = Subscription(pattern="drift.exceeded", handler=lambda e: True)
        event = BusEvent(event_type="drift.exceeded", payload={})
        assert sub.matches(event) is True

    def test_no_match(self) -> None:
        sub = Subscription(pattern="drift.exceeded", handler=lambda e: True)
        event = BusEvent(event_type="policy.violation", payload={})
        assert sub.matches(event) is False

    def test_wildcard_match(self) -> None:
        sub = Subscription(pattern="drift.*", handler=lambda e: True)
        event = BusEvent(event_type="drift.exceeded", payload={})
        assert sub.matches(event) is True

    def test_global_wildcard(self) -> None:
        sub = Subscription(pattern="*", handler=lambda e: True)
        event = BusEvent(event_type="anything.here", payload={})
        assert sub.matches(event) is True

    def test_disabled_no_match(self) -> None:
        sub = Subscription(pattern="*", handler=lambda e: True, enabled=False)
        event = BusEvent(event_type="test", payload={})
        assert sub.matches(event) is False

    def test_priority_filter(self) -> None:
        sub = Subscription(
            pattern="*",
            handler=lambda e: True,
            priority_filter=EventPriority.HIGH,
        )
        high = BusEvent(event_type="test", payload={}, priority=EventPriority.HIGH)
        low = BusEvent(event_type="test", payload={}, priority=EventPriority.LOW)
        critical = BusEvent(event_type="test", payload={}, priority=EventPriority.CRITICAL)
        assert sub.matches(high) is True
        assert sub.matches(critical) is True
        assert sub.matches(low) is False

    def test_source_filter(self) -> None:
        sub = Subscription(
            pattern="*",
            handler=lambda e: True,
            source_filter="orchestrator",
        )
        match = BusEvent(event_type="test", payload={}, source="orchestrator")
        no_match = BusEvent(event_type="test", payload={}, source="router")
        assert sub.matches(match) is True
        assert sub.matches(no_match) is False

    def test_shorter_pattern_no_match(self) -> None:
        sub = Subscription(pattern="a.b", handler=lambda e: True)
        event = BusEvent(event_type="a.b.c", payload={})
        assert sub.matches(event) is False

    def test_longer_pattern_no_match(self) -> None:
        sub = Subscription(pattern="a.b.c", handler=lambda e: True)
        event = BusEvent(event_type="a.b", payload={})
        assert sub.matches(event) is False


# ── EventBusStats ────────────────────────────────────────────────────────────


class TestEventBusStats:
    def test_to_dict(self) -> None:
        stats = EventBusStats(
            total_published=10,
            total_delivered=8,
            total_failed=2,
            dead_letter_count=1,
            active_subscriptions=3,
            events_per_type={"drift.exceeded": 5},
        )
        d = stats.to_dict()
        assert d["total_published"] == 10
        assert d["events_per_type"]["drift.exceeded"] == 5


# ── EventBus ─────────────────────────────────────────────────────────────────


def _collector(store: list[BusEvent]) -> "Callable[[BusEvent], bool]":
    """Return a handler that appends events to *store* and returns True."""

    def _handler(e: BusEvent) -> bool:
        store.append(e)
        return True

    return _handler


def _type_collector(store: list[str]) -> "Callable[[BusEvent], bool]":
    """Return a handler that appends event_type strings to *store*."""

    def _handler(e: BusEvent) -> bool:
        store.append(e.event_type)
        return True

    return _handler


class TestEventBus:
    def test_subscribe_and_publish(self) -> None:
        bus = EventBus()
        received: list[BusEvent] = []
        bus.subscribe("test.event", _collector(received))

        event = bus.emit("test.event", payload={"key": "value"})
        assert event.status == EventStatus.DELIVERED
        assert len(received) == 1
        assert received[0].payload["key"] == "value"

    def test_publish_no_subscribers(self) -> None:
        bus = EventBus()
        event = bus.emit("orphan.event")
        assert event.status == EventStatus.DEAD_LETTER
        assert len(bus.dead_letter_queue) == 1

    def test_multiple_subscribers(self) -> None:
        bus = EventBus()
        counts = {"a": 0, "b": 0}

        def handler_a(e: BusEvent) -> bool:
            counts["a"] += 1
            return True

        def handler_b(e: BusEvent) -> bool:
            counts["b"] += 1
            return True

        bus.subscribe("test", handler_a, name="a")
        bus.subscribe("test", handler_b, name="b")
        bus.emit("test")
        assert counts["a"] == 1
        assert counts["b"] == 1

    def test_wildcard_subscription(self) -> None:
        bus = EventBus()
        received: list[str] = []
        bus.subscribe("drift.*", _type_collector(received))

        bus.emit("drift.exceeded")
        bus.emit("drift.reset")
        bus.emit("policy.violation")  # Should not match
        assert len(received) == 2
        assert "drift.exceeded" in received
        assert "drift.reset" in received

    def test_unsubscribe(self) -> None:
        bus = EventBus()
        bus.subscribe("test", lambda e: True)
        assert bus.unsubscribe("test") is True
        event = bus.emit("test")
        assert event.status == EventStatus.DEAD_LETTER

    def test_unsubscribe_nonexistent(self) -> None:
        bus = EventBus()
        assert bus.unsubscribe("nonexistent") is False

    def test_handler_exception_tracked(self) -> None:
        bus = EventBus()

        def bad_handler(e: BusEvent) -> bool:
            raise ValueError("Handler crash!")

        bus.subscribe("test", bad_handler, name="bad")
        event = bus.emit("test")
        assert event.status == EventStatus.FAILED
        assert "Handler crash!" in event.error

    def test_handler_returns_false(self) -> None:
        bus = EventBus()
        bus.subscribe("test", lambda e: False)
        event = bus.emit("test")
        assert event.status == EventStatus.FAILED

    def test_emit_with_priority(self) -> None:
        bus = EventBus()
        received: list[BusEvent] = []
        bus.subscribe("test", _collector(received))

        bus.emit("test", priority=EventPriority.CRITICAL)
        assert received[0].priority == EventPriority.CRITICAL

    def test_emit_with_source(self) -> None:
        bus = EventBus()
        received: list[BusEvent] = []
        bus.subscribe("test", _collector(received))

        bus.emit("test", source="orchestrator")
        assert received[0].source == "orchestrator"

    def test_emit_with_correlation_id(self) -> None:
        bus = EventBus()
        received: list[BusEvent] = []
        bus.subscribe("test", _collector(received))

        bus.emit("test", correlation_id="corr-123")
        assert received[0].correlation_id == "corr-123"

    def test_publish_direct(self) -> None:
        bus = EventBus()
        received: list[BusEvent] = []
        bus.subscribe("custom", _collector(received))

        event = BusEvent(
            event_type="custom",
            payload={"data": 42},
            event_id="my-event-id",
        )
        result = bus.publish(event)
        assert result.status == EventStatus.DELIVERED
        assert received[0].event_id == "my-event-id"

    def test_stats(self) -> None:
        bus = EventBus()
        bus.subscribe("test", lambda e: True)
        bus.emit("test")
        bus.emit("test")
        bus.emit("other")  # No handler → dead letter

        stats = bus.stats()
        assert stats.total_published == 3
        assert stats.total_delivered == 2
        assert stats.dead_letter_count == 1
        assert stats.active_subscriptions == 1
        assert stats.events_per_type["test"] == 2
        assert stats.events_per_type["other"] == 1

    def test_history(self) -> None:
        bus = EventBus()
        bus.subscribe("a", lambda e: True)
        bus.subscribe("b", lambda e: True)

        bus.emit("a", source="mod1")
        bus.emit("b", source="mod2")
        bus.emit("a", source="mod1")

        # All history
        all_events = bus.history()
        assert len(all_events) == 3

        # Filter by type
        a_events = bus.history(event_type="a")
        assert len(a_events) == 2

        # Filter by source
        mod2_events = bus.history(source="mod2")
        assert len(mod2_events) == 1

        # Limit
        limited = bus.history(limit=1)
        assert len(limited) == 1

    def test_history_most_recent_first(self) -> None:
        bus = EventBus()
        bus.subscribe("test", lambda e: True)
        bus.emit("test", payload={"seq": 1})
        bus.emit("test", payload={"seq": 2})
        events = bus.history()
        assert events[0].payload["seq"] == 2

    def test_history_capped_at_max(self) -> None:
        bus = EventBus(max_history=5)
        bus.subscribe("test", lambda e: True)
        for i in range(10):
            bus.emit("test", payload={"i": i})
        events = bus.history(limit=100)
        assert len(events) == 5

    def test_dead_letter_queue(self) -> None:
        bus = EventBus()
        bus.emit("no_handler")
        assert len(bus.dead_letter_queue) == 1
        assert bus.dead_letter_queue[0].event_type == "no_handler"

    def test_clear_dead_letter(self) -> None:
        bus = EventBus()
        bus.emit("no_handler")
        count = bus.clear_dead_letter()
        assert count == 1
        assert len(bus.dead_letter_queue) == 0

    def test_clear_history(self) -> None:
        bus = EventBus()
        bus.subscribe("test", lambda e: True)
        bus.emit("test")
        count = bus.clear_history()
        assert count == 1
        assert len(bus.history()) == 0

    def test_reset(self) -> None:
        bus = EventBus()
        bus.subscribe("test", lambda e: True)
        bus.emit("test")
        bus.reset()

        stats = bus.stats()
        assert stats.total_published == 0
        assert stats.active_subscriptions == 0
        assert len(bus.history()) == 0
        assert len(bus.dead_letter_queue) == 0

    def test_priority_ordering(self) -> None:
        assert EventPriority.CRITICAL < EventPriority.HIGH
        assert EventPriority.HIGH < EventPriority.NORMAL
        assert EventPriority.NORMAL < EventPriority.LOW
        assert EventPriority.LOW < EventPriority.BACKGROUND

    def test_subscription_call_count(self) -> None:
        bus = EventBus()
        sub = bus.subscribe("test", lambda e: True)
        bus.emit("test")
        bus.emit("test")
        assert sub.call_count == 2
        assert sub.error_count == 0

    def test_subscription_error_count(self) -> None:
        bus = EventBus()

        def bad(e: BusEvent) -> bool:
            raise RuntimeError("fail")

        sub = bus.subscribe("test", bad)
        bus.emit("test")
        assert sub.error_count == 1
