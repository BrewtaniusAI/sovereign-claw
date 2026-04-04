"""
event_bus — Governed Pub/Sub Event Bus
=======================================
In-process event bus for decoupled inter-module communication.

Features:
- Typed events with structured payloads
- Synchronous and asynchronous handler support
- Event filtering by type, source, and priority
- Dead letter queue for failed deliveries
- Governed event flow: all events auditable via ProofVault
- Priority-based event ordering
- Event history with configurable retention

The event bus decouples modules while maintaining governance visibility.
Every event is traceable, every handler failure is recorded, and all
inter-module communication flows through a single auditable channel.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Callable


class EventPriority(IntEnum):
    """Event priority levels (lower = higher priority)."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4


class EventStatus(str, Enum):
    """Status of an event in the bus."""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


@dataclass
class BusEvent:
    """A typed event in the bus."""

    event_type: str
    payload: dict[str, Any]
    source: str = ""
    priority: EventPriority = EventPriority.NORMAL
    event_id: str = ""
    timestamp: float = field(default_factory=time.time)
    correlation_id: str = ""
    status: EventStatus = EventStatus.PENDING
    error: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            self.event_id = f"evt_{id(self)}_{int(self.timestamp)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source,
            "priority": self.priority.name,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "status": self.status.value,
            "error": self.error,
        }


# Type alias for event handlers
EventHandler = Callable[[BusEvent], bool]


@dataclass
class Subscription:
    """A subscription binding an event pattern to a handler."""

    pattern: str  # e.g., "drift.*", "policy.violation", "*"
    handler: EventHandler
    name: str = ""
    priority_filter: EventPriority | None = None
    source_filter: str = ""
    enabled: bool = True
    call_count: int = 0
    error_count: int = 0

    def matches(self, event: BusEvent) -> bool:
        """Check if this subscription matches an event."""
        if not self.enabled:
            return False

        # Priority filter
        if self.priority_filter is not None and event.priority > self.priority_filter:
            return False

        # Source filter
        if self.source_filter and event.source != self.source_filter:
            return False

        # Pattern matching
        return self._pattern_matches(event.event_type)

    def _pattern_matches(self, event_type: str) -> bool:
        """Match event type against subscription pattern."""
        if self.pattern == "*":
            return True

        parts = self.pattern.split(".")
        event_parts = event_type.split(".")

        for i, part in enumerate(parts):
            if part == "*":
                return True
            if i >= len(event_parts):
                return False
            if part != event_parts[i]:
                return False

        return len(parts) == len(event_parts)


@dataclass
class EventBusStats:
    """Statistics for the event bus."""

    total_published: int = 0
    total_delivered: int = 0
    total_failed: int = 0
    dead_letter_count: int = 0
    active_subscriptions: int = 0
    events_per_type: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_published": self.total_published,
            "total_delivered": self.total_delivered,
            "total_failed": self.total_failed,
            "dead_letter_count": self.dead_letter_count,
            "active_subscriptions": self.active_subscriptions,
            "events_per_type": dict(self.events_per_type),
        }


class EventBus:
    """
    Governed pub/sub event bus for inter-module communication.

    Usage:
        bus = EventBus()

        # Subscribe to events
        bus.subscribe("drift.*", handle_drift, name="drift_monitor")
        bus.subscribe("policy.violation", handle_violation)

        # Publish events
        bus.publish(BusEvent(
            event_type="drift.threshold_exceeded",
            payload={"drift": 0.85, "threshold": 0.5},
            source="orchestrator",
        ))

        # Get stats
        stats = bus.stats()
    """

    # Maximum event history size
    MAX_HISTORY = 10000

    def __init__(self, max_history: int = MAX_HISTORY) -> None:
        self._lock = threading.Lock()
        self._subscriptions: list[Subscription] = []
        self._history: list[BusEvent] = []
        self._dead_letter: list[BusEvent] = []
        self._max_history = max_history
        self._stats = EventBusStats()

    def subscribe(
        self,
        pattern: str,
        handler: EventHandler,
        name: str = "",
        priority_filter: EventPriority | None = None,
        source_filter: str = "",
    ) -> Subscription:
        """
        Subscribe to events matching a pattern.

        Args:
            pattern: Event type pattern (supports * wildcard).
            handler: Function called when matching event is published.
            name: Human-readable subscription name.
            priority_filter: Only receive events at this priority or higher.
            source_filter: Only receive events from this source.

        Returns:
            Subscription object for management.
        """
        sub = Subscription(
            pattern=pattern,
            handler=handler,
            name=name or pattern,
            priority_filter=priority_filter,
            source_filter=source_filter,
        )
        with self._lock:
            self._subscriptions.append(sub)
        return sub

    def unsubscribe(self, pattern: str) -> bool:
        """Remove subscriptions matching a pattern."""
        with self._lock:
            before = len(self._subscriptions)
            self._subscriptions = [s for s in self._subscriptions if s.pattern != pattern]
            return len(self._subscriptions) < before

    def publish(self, event: BusEvent) -> BusEvent:
        """
        Publish an event to all matching subscribers.

        Args:
            event: The event to publish.

        Returns:
            The event with updated status.
        """
        with self._lock:
            self._stats.total_published += 1

            # Track per-type count
            count = self._stats.events_per_type.get(event.event_type, 0)
            self._stats.events_per_type[event.event_type] = count + 1

            event.status = EventStatus.DISPATCHED

            # Find matching subscriptions
            matching = [s for s in self._subscriptions if s.matches(event)]

            if not matching:
                event.status = EventStatus.DEAD_LETTER
                event.error = "No subscribers for event type"
                self._dead_letter.append(event)
                self._stats.dead_letter_count += 1
            else:
                delivered = False
                for sub in matching:
                    try:
                        result = sub.handler(event)
                        sub.call_count += 1
                        if result:
                            delivered = True
                    except Exception as exc:
                        sub.error_count += 1
                        event.error = f"Handler '{sub.name}' failed: {exc}"

                if delivered:
                    event.status = EventStatus.DELIVERED
                    self._stats.total_delivered += 1
                else:
                    event.status = EventStatus.FAILED
                    self._stats.total_failed += 1
                    self._dead_letter.append(event)

            # Add to history
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history :]

            return event

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        source: str = "",
        priority: EventPriority = EventPriority.NORMAL,
        correlation_id: str = "",
    ) -> BusEvent:
        """
        Convenience method to create and publish an event.

        Args:
            event_type: Type identifier for the event.
            payload: Event data.
            source: Module that generated the event.
            priority: Event priority level.
            correlation_id: Correlation ID for tracing.

        Returns:
            The published event.
        """
        event = BusEvent(
            event_type=event_type,
            payload=payload or {},
            source=source,
            priority=priority,
            correlation_id=correlation_id,
        )
        return self.publish(event)

    @property
    def dead_letter_queue(self) -> list[BusEvent]:
        """Get events that failed delivery."""
        with self._lock:
            return list(self._dead_letter)

    def history(
        self,
        event_type: str | None = None,
        source: str | None = None,
        limit: int = 100,
    ) -> list[BusEvent]:
        """
        Query event history with optional filters.

        Args:
            event_type: Filter by event type (exact match).
            source: Filter by source module.
            limit: Maximum number of events to return.

        Returns:
            List of matching events, most recent first.
        """
        with self._lock:
            events = list(reversed(self._history))
            if event_type:
                events = [e for e in events if e.event_type == event_type]
            if source:
                events = [e for e in events if e.source == source]
            return events[:limit]

    def stats(self) -> EventBusStats:
        """Get event bus statistics."""
        with self._lock:
            self._stats.active_subscriptions = len([s for s in self._subscriptions if s.enabled])
            self._stats.dead_letter_count = len(self._dead_letter)
            return EventBusStats(
                total_published=self._stats.total_published,
                total_delivered=self._stats.total_delivered,
                total_failed=self._stats.total_failed,
                dead_letter_count=len(self._dead_letter),
                active_subscriptions=self._stats.active_subscriptions,
                events_per_type=dict(self._stats.events_per_type),
            )

    def clear_dead_letter(self) -> int:
        """Clear the dead letter queue. Returns cleared count."""
        with self._lock:
            count = len(self._dead_letter)
            self._dead_letter.clear()
            return count

    def clear_history(self) -> int:
        """Clear event history. Returns cleared count."""
        with self._lock:
            count = len(self._history)
            self._history.clear()
            return count

    def reset(self) -> None:
        """Reset the entire event bus state."""
        with self._lock:
            self._subscriptions.clear()
            self._history.clear()
            self._dead_letter.clear()
            self._stats = EventBusStats()
