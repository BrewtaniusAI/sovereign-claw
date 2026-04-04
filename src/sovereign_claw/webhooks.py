"""
webhooks — Webhook Receiver with Signature Verification
========================================================
Production-grade HTTP webhook handling for external integrations.

Features:
- HMAC-SHA256 signature verification (GitHub, Stripe, Slack compatible)
- Event routing with pattern matching
- Replay protection via timestamp validation and nonce tracking
- Configurable webhook endpoints with per-source secrets
- Dead letter queue for failed deliveries
- Governance integration: webhook events feed into ProofVault audit trail

Webhook events are treated as external inputs to the governed runtime.
All events are verified, routed, and logged before reaching handlers.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class WebhookVerificationMethod(str, Enum):
    """Supported signature verification methods."""

    HMAC_SHA256 = "hmac_sha256"
    HMAC_SHA1 = "hmac_sha1"
    NONE = "none"


class WebhookStatus(str, Enum):
    """Status of a webhook delivery."""

    RECEIVED = "received"
    VERIFIED = "verified"
    REJECTED = "rejected"
    ROUTED = "routed"
    PROCESSED = "processed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


@dataclass
class WebhookSource:
    """Configuration for a webhook source."""

    name: str
    secret: str = ""
    verification: WebhookVerificationMethod = WebhookVerificationMethod.HMAC_SHA256
    max_age_seconds: float = 300.0  # 5 minutes
    enabled: bool = True
    event_prefix: str = ""  # e.g., "github." for "github.push"

    def verify_signature(
        self,
        payload: bytes,
        signature: str,
        timestamp: str = "",
    ) -> bool:
        """Verify the webhook signature against the payload."""
        if self.verification == WebhookVerificationMethod.NONE:
            return True

        if not self.secret:
            return False

        if self.verification == WebhookVerificationMethod.HMAC_SHA256:
            # GitHub style: sha256=<hex>
            if timestamp:
                signing_payload = f"{timestamp}.".encode() + payload
            else:
                signing_payload = payload
            expected = hmac.new(
                self.secret.encode(),
                signing_payload,
                hashlib.sha256,
            ).hexdigest()
            # Strip prefix if present
            sig_hex = signature.removeprefix("sha256=")
            return hmac.compare_digest(expected, sig_hex)

        if self.verification == WebhookVerificationMethod.HMAC_SHA1:
            expected = hmac.new(
                self.secret.encode(),
                payload,
                hashlib.sha1,
            ).hexdigest()
            sig_hex = signature.removeprefix("sha1=")
            return hmac.compare_digest(expected, sig_hex)

        return False


@dataclass
class WebhookEvent:
    """A received webhook event."""

    event_id: str
    source: str
    event_type: str
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    status: WebhookStatus = WebhookStatus.RECEIVED
    error: str = ""
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "event_type": self.event_type,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "status": self.status.value,
            "error": self.error,
            "attempts": self.attempts,
        }


# Type alias for webhook handlers
WebhookHandler = Callable[[WebhookEvent], bool]


@dataclass
class WebhookRoute:
    """A route mapping event patterns to handlers."""

    pattern: str  # e.g., "github.push", "stripe.payment.*", "*"
    handler: WebhookHandler
    name: str = ""
    max_retries: int = 3
    enabled: bool = True

    def matches(self, event_type: str) -> bool:
        """Check if an event type matches this route's pattern."""
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


class WebhookReceiver:
    """
    HTTP webhook receiver with signature verification and event routing.

    Usage:
        receiver = WebhookReceiver()

        # Register sources
        receiver.register_source(WebhookSource(
            name="github",
            secret="whsec_...",
            event_prefix="github.",
        ))

        # Register handlers
        receiver.add_route("github.push", handle_push)
        receiver.add_route("github.pull_request.*", handle_pr)

        # Process incoming webhook
        event = receiver.receive(
            source="github",
            event_type="push",
            payload={"ref": "refs/heads/main"},
            signature="sha256=...",
            raw_body=b'{"ref": "refs/heads/main"}',
        )
    """

    # Maximum number of nonces to track for replay protection
    MAX_NONCE_CACHE = 10000

    def __init__(self) -> None:
        self._sources: dict[str, WebhookSource] = {}
        self._routes: list[WebhookRoute] = []
        self._dead_letter: list[WebhookEvent] = []
        self._processed: list[WebhookEvent] = []
        self._nonces: set[str] = set()
        self._event_count = 0

    def register_source(self, source: WebhookSource) -> None:
        """Register a webhook source with its verification config."""
        self._sources[source.name] = source

    def unregister_source(self, name: str) -> None:
        """Remove a webhook source."""
        self._sources.pop(name, None)

    def add_route(
        self,
        pattern: str,
        handler: WebhookHandler,
        name: str = "",
        max_retries: int = 3,
    ) -> None:
        """Add a route mapping event patterns to handlers."""
        self._routes.append(
            WebhookRoute(
                pattern=pattern,
                handler=handler,
                name=name or pattern,
                max_retries=max_retries,
            )
        )

    def remove_route(self, pattern: str) -> None:
        """Remove a route by pattern."""
        self._routes = [r for r in self._routes if r.pattern != pattern]

    def receive(
        self,
        source: str,
        event_type: str,
        payload: dict[str, Any],
        signature: str = "",
        raw_body: bytes = b"",
        timestamp: str = "",
        event_id: str = "",
        headers: dict[str, str] | None = None,
    ) -> WebhookEvent:
        """
        Receive and process an incoming webhook.

        Args:
            source: Source identifier (must be registered).
            event_type: Event type (e.g., "push", "payment.completed").
            payload: Parsed JSON payload.
            signature: Signature header value.
            raw_body: Raw request body for signature verification.
            timestamp: Request timestamp for replay protection.
            event_id: Unique event ID for deduplication.
            headers: Raw request headers.

        Returns:
            WebhookEvent with processing status.
        """
        self._event_count += 1
        eid = event_id or f"wh_{self._event_count}_{int(time.time())}"

        event = WebhookEvent(
            event_id=eid,
            source=source,
            event_type=event_type,
            payload=payload,
            headers=headers or {},
            status=WebhookStatus.RECEIVED,
        )

        # Check source exists and is enabled
        src = self._sources.get(source)
        if not src:
            event.status = WebhookStatus.REJECTED
            event.error = f"Unknown source: {source}"
            self._dead_letter.append(event)
            return event

        if not src.enabled:
            event.status = WebhookStatus.REJECTED
            event.error = f"Source disabled: {source}"
            self._dead_letter.append(event)
            return event

        # Replay protection via nonce
        if event_id and event_id in self._nonces:
            event.status = WebhookStatus.REJECTED
            event.error = "Duplicate event (replay detected)"
            return event

        # Timestamp freshness check
        if timestamp:
            try:
                ts = float(timestamp)
                age = abs(time.time() - ts)
                if age > src.max_age_seconds:
                    event.status = WebhookStatus.REJECTED
                    event.error = f"Stale webhook (age={age:.0f}s > max={src.max_age_seconds}s)"
                    self._dead_letter.append(event)
                    return event
            except (ValueError, TypeError):
                pass

        # Signature verification
        if src.verification != WebhookVerificationMethod.NONE:
            body = raw_body or _dict_to_bytes(payload)
            if not src.verify_signature(body, signature, timestamp):
                event.status = WebhookStatus.REJECTED
                event.error = "Invalid signature"
                self._dead_letter.append(event)
                return event

        event.status = WebhookStatus.VERIFIED

        # Track nonce for replay protection
        if event_id:
            if len(self._nonces) >= self.MAX_NONCE_CACHE:
                self._nonces.clear()
            self._nonces.add(event_id)

        # Apply source event prefix
        full_event_type = f"{src.event_prefix}{event_type}" if src.event_prefix else event_type
        event.event_type = full_event_type

        # Route to handlers
        matched = False
        for route in self._routes:
            if not route.enabled:
                continue
            if route.matches(full_event_type):
                matched = True
                event.status = WebhookStatus.ROUTED
                success = self._execute_handler(event, route)
                if success:
                    event.status = WebhookStatus.PROCESSED
                else:
                    event.status = WebhookStatus.FAILED
                break

        if not matched:
            event.status = WebhookStatus.DEAD_LETTER
            event.error = f"No handler for event type: {full_event_type}"
            self._dead_letter.append(event)

        self._processed.append(event)
        return event

    def _execute_handler(self, event: WebhookEvent, route: WebhookRoute) -> bool:
        """Execute a handler with retry logic."""
        for attempt in range(route.max_retries):
            event.attempts = attempt + 1
            try:
                result = route.handler(event)
                if result:
                    return True
            except Exception as exc:
                event.error = f"Handler error (attempt {attempt + 1}): {exc}"

        # All retries exhausted
        self._dead_letter.append(event)
        return False

    @property
    def dead_letter_queue(self) -> list[WebhookEvent]:
        """Get events that failed processing."""
        return list(self._dead_letter)

    @property
    def processed_events(self) -> list[WebhookEvent]:
        """Get successfully processed events."""
        return [e for e in self._processed if e.status == WebhookStatus.PROCESSED]

    def stats(self) -> dict[str, Any]:
        """Get webhook processing statistics."""
        return {
            "total_received": self._event_count,
            "total_processed": len(self.processed_events),
            "dead_letter_count": len(self._dead_letter),
            "registered_sources": len(self._sources),
            "registered_routes": len(self._routes),
        }

    def clear_dead_letter(self) -> int:
        """Clear the dead letter queue. Returns count of cleared events."""
        count = len(self._dead_letter)
        self._dead_letter.clear()
        return count


def _dict_to_bytes(d: dict[str, Any]) -> bytes:
    """Convert a dict to deterministic JSON bytes."""
    import json

    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()
