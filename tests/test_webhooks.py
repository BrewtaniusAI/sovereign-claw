"""Tests for webhooks module."""

from __future__ import annotations

import hashlib
import hmac
import time

from sovereign_claw.webhooks import (
    WebhookEvent,
    WebhookReceiver,
    WebhookRoute,
    WebhookSource,
    WebhookStatus,
    WebhookVerificationMethod,
    _dict_to_bytes,
)


# ── WebhookSource ────────────────────────────────────────────────────────────


class TestWebhookSource:
    def test_verify_hmac_sha256(self) -> None:
        src = WebhookSource(name="test", secret="mysecret")
        payload = b'{"action": "push"}'
        sig = hmac.new(b"mysecret", payload, hashlib.sha256).hexdigest()
        assert src.verify_signature(payload, f"sha256={sig}") is True

    def test_verify_hmac_sha256_invalid(self) -> None:
        src = WebhookSource(name="test", secret="mysecret")
        payload = b'{"action": "push"}'
        assert src.verify_signature(payload, "sha256=invalid") is False

    def test_verify_hmac_sha256_with_timestamp(self) -> None:
        src = WebhookSource(name="test", secret="mysecret")
        payload = b'{"action": "push"}'
        ts = "1234567890"
        signing_payload = f"{ts}.".encode() + payload
        sig = hmac.new(b"mysecret", signing_payload, hashlib.sha256).hexdigest()
        assert src.verify_signature(payload, f"sha256={sig}", timestamp=ts) is True

    def test_verify_hmac_sha1(self) -> None:
        src = WebhookSource(
            name="test",
            secret="mysecret",
            verification=WebhookVerificationMethod.HMAC_SHA1,
        )
        payload = b"test body"
        sig = hmac.new(b"mysecret", payload, hashlib.sha1).hexdigest()
        assert src.verify_signature(payload, f"sha1={sig}") is True

    def test_verify_none(self) -> None:
        src = WebhookSource(name="test", verification=WebhookVerificationMethod.NONE)
        assert src.verify_signature(b"anything", "") is True

    def test_verify_no_secret(self) -> None:
        src = WebhookSource(name="test", secret="")
        assert src.verify_signature(b"payload", "sha256=abc") is False


# ── WebhookEvent ─────────────────────────────────────────────────────────────


class TestWebhookEvent:
    def test_to_dict(self) -> None:
        event = WebhookEvent(
            event_id="wh_1",
            source="github",
            event_type="push",
            payload={"ref": "main"},
            status=WebhookStatus.PROCESSED,
        )
        d = event.to_dict()
        assert d["event_id"] == "wh_1"
        assert d["source"] == "github"
        assert d["status"] == "processed"


# ── WebhookRoute ─────────────────────────────────────────────────────────────


class TestWebhookRoute:
    def test_exact_match(self) -> None:
        route = WebhookRoute(pattern="github.push", handler=lambda e: True)
        assert route.matches("github.push") is True
        assert route.matches("github.pull_request") is False

    def test_wildcard_match(self) -> None:
        route = WebhookRoute(pattern="github.*", handler=lambda e: True)
        assert route.matches("github.push") is True
        assert route.matches("github.pull_request") is True
        assert route.matches("stripe.payment") is False

    def test_global_wildcard(self) -> None:
        route = WebhookRoute(pattern="*", handler=lambda e: True)
        assert route.matches("anything") is True
        assert route.matches("a.b.c") is True

    def test_shorter_pattern(self) -> None:
        route = WebhookRoute(pattern="a.b", handler=lambda e: True)
        assert route.matches("a.b.c") is False

    def test_longer_pattern(self) -> None:
        route = WebhookRoute(pattern="a.b.c", handler=lambda e: True)
        assert route.matches("a.b") is False


# ── WebhookReceiver ──────────────────────────────────────────────────────────


class TestWebhookReceiver:
    def _make_receiver(self) -> WebhookReceiver:
        receiver = WebhookReceiver()
        receiver.register_source(
            WebhookSource(
                name="github",
                secret="gh_secret",
                event_prefix="github.",
            )
        )
        receiver.register_source(
            WebhookSource(
                name="stripe",
                secret="stripe_secret",
                event_prefix="stripe.",
            )
        )
        return receiver

    def _sign(self, payload: bytes, secret: str) -> str:
        return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    def test_receive_verified_and_routed(self) -> None:
        receiver = self._make_receiver()
        handled: list[WebhookEvent] = []

        def _track(e: WebhookEvent) -> bool:
            handled.append(e)
            return True

        receiver.add_route("github.push", _track)

        payload = {"ref": "refs/heads/main"}
        raw = _dict_to_bytes(payload)
        sig = self._sign(raw, "gh_secret")

        event = receiver.receive(
            source="github",
            event_type="push",
            payload=payload,
            signature=sig,
            raw_body=raw,
        )
        assert event.status == WebhookStatus.PROCESSED
        assert len(handled) == 1
        assert event.event_type == "github.push"

    def test_receive_unknown_source(self) -> None:
        receiver = self._make_receiver()
        event = receiver.receive(
            source="unknown",
            event_type="test",
            payload={},
        )
        assert event.status == WebhookStatus.REJECTED
        assert "Unknown source" in event.error

    def test_receive_disabled_source(self) -> None:
        receiver = WebhookReceiver()
        receiver.register_source(WebhookSource(name="disabled", enabled=False))
        event = receiver.receive(source="disabled", event_type="test", payload={})
        assert event.status == WebhookStatus.REJECTED
        assert "disabled" in event.error

    def test_receive_invalid_signature(self) -> None:
        receiver = self._make_receiver()
        event = receiver.receive(
            source="github",
            event_type="push",
            payload={"ref": "main"},
            signature="sha256=bad",
            raw_body=b'{"ref":"main"}',
        )
        assert event.status == WebhookStatus.REJECTED
        assert "Invalid signature" in event.error

    def test_replay_protection_nonce(self) -> None:
        receiver = self._make_receiver()
        # Use NONE verification for simplicity
        receiver.register_source(
            WebhookSource(
                name="simple",
                verification=WebhookVerificationMethod.NONE,
            )
        )
        receiver.add_route("test", lambda e: True)

        # First delivery
        event1 = receiver.receive(
            source="simple",
            event_type="test",
            payload={},
            event_id="nonce-123",
        )
        assert event1.status == WebhookStatus.PROCESSED

        # Replay
        event2 = receiver.receive(
            source="simple",
            event_type="test",
            payload={},
            event_id="nonce-123",
        )
        assert event2.status == WebhookStatus.REJECTED
        assert "replay" in event2.error.lower()

    def test_stale_timestamp_rejected(self) -> None:
        receiver = WebhookReceiver()
        receiver.register_source(
            WebhookSource(
                name="ts",
                verification=WebhookVerificationMethod.NONE,
                max_age_seconds=10.0,
            )
        )
        stale_ts = str(time.time() - 3600)  # 1 hour ago
        event = receiver.receive(
            source="ts",
            event_type="test",
            payload={},
            timestamp=stale_ts,
        )
        assert event.status == WebhookStatus.REJECTED
        assert "Stale" in event.error

    def test_no_handler_dead_letter(self) -> None:
        receiver = WebhookReceiver()
        receiver.register_source(
            WebhookSource(
                name="nohandler",
                verification=WebhookVerificationMethod.NONE,
            )
        )
        event = receiver.receive(
            source="nohandler",
            event_type="unrouted",
            payload={},
        )
        assert event.status == WebhookStatus.DEAD_LETTER
        assert len(receiver.dead_letter_queue) >= 1

    def test_handler_retry_on_failure(self) -> None:
        receiver = WebhookReceiver()
        receiver.register_source(
            WebhookSource(
                name="retry",
                verification=WebhookVerificationMethod.NONE,
            )
        )
        call_count = [0]

        def failing_handler(e: WebhookEvent) -> bool:
            call_count[0] += 1
            return False

        receiver.add_route("test", failing_handler, max_retries=3)
        event = receiver.receive(source="retry", event_type="test", payload={})
        assert event.status == WebhookStatus.FAILED
        assert call_count[0] == 3
        assert event.attempts == 3

    def test_handler_exception_retried(self) -> None:
        receiver = WebhookReceiver()
        receiver.register_source(
            WebhookSource(
                name="exc",
                verification=WebhookVerificationMethod.NONE,
            )
        )

        def exploding_handler(e: WebhookEvent) -> bool:
            raise ValueError("Boom!")

        receiver.add_route("test", exploding_handler, max_retries=2)
        event = receiver.receive(source="exc", event_type="test", payload={})
        assert event.status == WebhookStatus.FAILED
        assert "Boom!" in event.error

    def test_unregister_source(self) -> None:
        receiver = self._make_receiver()
        receiver.unregister_source("github")
        event = receiver.receive(source="github", event_type="push", payload={})
        assert event.status == WebhookStatus.REJECTED

    def test_remove_route(self) -> None:
        receiver = WebhookReceiver()
        receiver.register_source(
            WebhookSource(name="src", verification=WebhookVerificationMethod.NONE)
        )
        receiver.add_route("test", lambda e: True)
        receiver.remove_route("test")
        event = receiver.receive(source="src", event_type="test", payload={})
        assert event.status == WebhookStatus.DEAD_LETTER

    def test_stats(self) -> None:
        receiver = WebhookReceiver()
        receiver.register_source(
            WebhookSource(name="s", verification=WebhookVerificationMethod.NONE)
        )
        receiver.add_route("test", lambda e: True)
        receiver.receive(source="s", event_type="test", payload={})
        stats = receiver.stats()
        assert stats["total_received"] == 1
        assert stats["total_processed"] == 1
        assert stats["registered_sources"] == 1

    def test_clear_dead_letter(self) -> None:
        receiver = WebhookReceiver()
        receiver.register_source(
            WebhookSource(name="s", verification=WebhookVerificationMethod.NONE)
        )
        receiver.receive(source="s", event_type="unrouted", payload={})
        assert len(receiver.dead_letter_queue) >= 1
        count = receiver.clear_dead_letter()
        assert count >= 1
        assert len(receiver.dead_letter_queue) == 0

    def test_processed_events(self) -> None:
        receiver = WebhookReceiver()
        receiver.register_source(
            WebhookSource(name="s", verification=WebhookVerificationMethod.NONE)
        )
        receiver.add_route("test", lambda e: True)
        receiver.receive(source="s", event_type="test", payload={})
        assert len(receiver.processed_events) == 1


# ── _dict_to_bytes ───────────────────────────────────────────────────────────


class TestDictToBytes:
    def test_deterministic_output(self) -> None:
        d = {"b": 2, "a": 1}
        result = _dict_to_bytes(d)
        assert result == b'{"a":1,"b":2}'

    def test_empty_dict(self) -> None:
        assert _dict_to_bytes({}) == b"{}"
