"""Tests for rate_limiter module."""

from __future__ import annotations

import time

import pytest

from sovereign_claw.rate_limiter import (
    RateLimitCategory,
    RateLimitConfig,
    RateLimitResult,
    RateLimitStats,
    RateLimiter,
    _TokenBucket,
)


# ── RateLimitResult ──────────────────────────────────────────────────────────


class TestRateLimitResult:
    def test_to_dict(self) -> None:
        result = RateLimitResult(
            allowed=True,
            remaining_tokens=42.567,
            retry_after_seconds=0.0,
            bucket_key="channel:slack",
            category=RateLimitCategory.CHANNEL,
            reason="",
        )
        d = result.to_dict()
        assert d["allowed"] is True
        assert d["remaining_tokens"] == 42.57
        assert d["category"] == "channel"
        assert d["bucket_key"] == "channel:slack"


# ── RateLimitStats ───────────────────────────────────────────────────────────


class TestRateLimitStats:
    def test_denial_rate_zero_requests(self) -> None:
        stats = RateLimitStats()
        assert stats.denial_rate == 0.0

    def test_denial_rate_calculation(self) -> None:
        stats = RateLimitStats(total_requests=100, allowed_requests=80, denied_requests=20)
        assert stats.denial_rate == pytest.approx(0.2)

    def test_to_dict(self) -> None:
        stats = RateLimitStats(total_requests=10, allowed_requests=8, denied_requests=2)
        d = stats.to_dict()
        assert d["total_requests"] == 10
        assert d["denial_rate"] == 0.2


# ── _TokenBucket ─────────────────────────────────────────────────────────────


class TestTokenBucket:
    def test_consume_success(self) -> None:
        bucket = _TokenBucket(
            tokens=10.0,
            max_tokens=10,
            refill_rate=1.0,
        )
        result = bucket.try_consume(1)
        assert result.allowed is True
        assert result.remaining_tokens == pytest.approx(9.0, abs=0.5)

    def test_consume_exhausted(self) -> None:
        bucket = _TokenBucket(
            tokens=0.0,
            max_tokens=10,
            refill_rate=0.0,
        )
        result = bucket.try_consume(1)
        assert result.allowed is False
        assert result.reason == "token_bucket_exhausted"

    def test_consume_zero_refill_rate(self) -> None:
        bucket = _TokenBucket(
            tokens=0.0,
            max_tokens=10,
            refill_rate=0.0,
        )
        result = bucket.try_consume(1)
        assert result.allowed is False
        assert result.retry_after_seconds == float("inf")

    def test_sliding_window_exceeded(self) -> None:
        bucket = _TokenBucket(
            tokens=100.0,
            max_tokens=100,
            refill_rate=100.0,
            window_seconds=60.0,
            max_per_window=5,
        )
        # Consume 5 tokens to fill window
        for _ in range(5):
            result = bucket.try_consume(1)
            assert result.allowed is True

        # 6th should be rejected by sliding window
        result = bucket.try_consume(1)
        assert result.allowed is False
        assert result.reason == "sliding_window_exceeded"

    def test_refill_adds_tokens(self) -> None:
        bucket = _TokenBucket(
            tokens=5.0,
            max_tokens=10,
            refill_rate=100.0,  # 100 tokens/sec
        )
        time.sleep(0.05)  # 50ms → ~5 tokens refilled
        bucket.refill()
        assert bucket.tokens >= 5.0  # At least original amount

    def test_refill_capped_at_max(self) -> None:
        bucket = _TokenBucket(
            tokens=10.0,
            max_tokens=10,
            refill_rate=1000.0,
        )
        time.sleep(0.01)
        bucket.refill()
        assert bucket.tokens <= 10.0


# ── RateLimiter ──────────────────────────────────────────────────────────────


class TestRateLimiter:
    def test_default_check_allowed(self) -> None:
        limiter = RateLimiter()
        result = limiter.check("test-key", RateLimitCategory.GLOBAL)
        assert result.allowed is True
        assert result.category == RateLimitCategory.GLOBAL

    def test_custom_config(self) -> None:
        limiter = RateLimiter()
        limiter.configure(
            RateLimitCategory.USER,
            RateLimitConfig(tokens_per_second=1.0, burst_size=2),
        )
        # First two should pass (burst)
        assert limiter.check("user1", RateLimitCategory.USER).allowed is True
        assert limiter.check("user1", RateLimitCategory.USER).allowed is True
        # Third should fail (burst exhausted)
        result = limiter.check("user1", RateLimitCategory.USER)
        assert result.allowed is False

    def test_different_keys_independent(self) -> None:
        limiter = RateLimiter()
        limiter.configure(
            RateLimitCategory.USER,
            RateLimitConfig(tokens_per_second=0.0, burst_size=1),
        )
        assert limiter.check("user1", RateLimitCategory.USER).allowed is True
        assert limiter.check("user2", RateLimitCategory.USER).allowed is True
        # user1 should now be exhausted
        assert limiter.check("user1", RateLimitCategory.USER).allowed is False
        # user2 should also be exhausted
        assert limiter.check("user2", RateLimitCategory.USER).allowed is False

    def test_multi_token_consume(self) -> None:
        limiter = RateLimiter()
        limiter.configure(
            RateLimitCategory.TOOL,
            RateLimitConfig(tokens_per_second=0.0, burst_size=5),
        )
        result = limiter.check("tool1", RateLimitCategory.TOOL, tokens=3)
        assert result.allowed is True
        result = limiter.check("tool1", RateLimitCategory.TOOL, tokens=3)
        assert result.allowed is False

    def test_stats_tracking(self) -> None:
        limiter = RateLimiter()
        limiter.check("key1", RateLimitCategory.GLOBAL)
        limiter.check("key2", RateLimitCategory.CHANNEL)
        stats = limiter.stats()
        assert stats.total_requests == 2
        assert stats.allowed_requests == 2
        assert stats.denied_requests == 0
        assert stats.active_buckets == 2

    def test_reset_specific_bucket(self) -> None:
        limiter = RateLimiter()
        limiter.configure(
            RateLimitCategory.USER,
            RateLimitConfig(tokens_per_second=0.0, burst_size=1),
        )
        limiter.check("u1", RateLimitCategory.USER)
        assert limiter.check("u1", RateLimitCategory.USER).allowed is False

        limiter.reset("u1", RateLimitCategory.USER)
        assert limiter.check("u1", RateLimitCategory.USER).allowed is True

    def test_reset_all(self) -> None:
        limiter = RateLimiter()
        limiter.check("k1", RateLimitCategory.GLOBAL)
        limiter.check("k2", RateLimitCategory.CHANNEL)
        limiter.reset_all()
        stats = limiter.stats()
        assert stats.total_requests == 0
        assert stats.active_buckets == 0

    def test_cleanup_stale(self) -> None:
        limiter = RateLimiter()
        limiter.check("k1", RateLimitCategory.GLOBAL)
        # Should not clean up fresh buckets
        removed = limiter.cleanup_stale(max_age_seconds=3600.0)
        assert removed == 0

        # Force a very small age threshold
        removed = limiter.cleanup_stale(max_age_seconds=0.0)
        assert removed >= 1

    def test_all_categories(self) -> None:
        limiter = RateLimiter()
        for cat in RateLimitCategory:
            result = limiter.check(f"key-{cat.value}", cat)
            assert result.allowed is True
            assert result.category == cat
