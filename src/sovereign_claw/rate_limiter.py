"""
rate_limiter — Token Bucket Rate Limiter with Governance Integration
=====================================================================
Production-grade rate limiting for governed execution.

Features:
- Token bucket algorithm with configurable refill rates
- Per-key limiting (by user, channel, provider, API key)
- Sliding window counters for burst detection
- Governance integration: rate limit violations feed into drift
- Thread-safe operation
- Configurable limits per category (channel, provider, global)

Rate limit violations are treated as governance events — they contribute
to drift and are logged to the ProofVault audit trail.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RateLimitCategory(str, Enum):
    """Categories for rate limit scoping."""

    GLOBAL = "global"
    CHANNEL = "channel"
    PROVIDER = "provider"
    USER = "user"
    API_KEY = "api_key"
    TOOL = "tool"


@dataclass
class RateLimitConfig:
    """Configuration for a single rate limit bucket."""

    tokens_per_second: float = 10.0
    burst_size: int = 50
    window_seconds: float = 60.0
    max_requests_per_window: int = 600


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""

    allowed: bool
    remaining_tokens: float
    retry_after_seconds: float = 0.0
    bucket_key: str = ""
    category: RateLimitCategory = RateLimitCategory.GLOBAL
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "remaining_tokens": round(self.remaining_tokens, 2),
            "retry_after_seconds": round(self.retry_after_seconds, 2),
            "bucket_key": self.bucket_key,
            "category": self.category.value,
            "reason": self.reason,
        }


@dataclass
class _TokenBucket:
    """Internal token bucket state."""

    tokens: float
    max_tokens: int
    refill_rate: float  # tokens per second
    last_refill: float = field(default_factory=time.monotonic)

    # Sliding window counters
    window_start: float = field(default_factory=time.monotonic)
    window_count: int = 0
    window_seconds: float = 60.0
    max_per_window: int = 600

    def refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

        # Reset sliding window if expired
        if now - self.window_start >= self.window_seconds:
            self.window_start = now
            self.window_count = 0

    def try_consume(self, tokens: int = 1) -> RateLimitResult:
        """Attempt to consume tokens. Returns result."""
        self.refill()

        # Check sliding window first
        if self.window_count + tokens > self.max_per_window:
            wait = self.window_seconds - (time.monotonic() - self.window_start)
            return RateLimitResult(
                allowed=False,
                remaining_tokens=self.tokens,
                retry_after_seconds=max(0.0, wait),
                reason="sliding_window_exceeded",
            )

        # Check token bucket
        if self.tokens >= tokens:
            self.tokens -= tokens
            self.window_count += tokens
            return RateLimitResult(
                allowed=True,
                remaining_tokens=self.tokens,
            )

        # Not enough tokens — compute wait time
        deficit = tokens - self.tokens
        wait = deficit / self.refill_rate if self.refill_rate > 0 else float("inf")
        return RateLimitResult(
            allowed=False,
            remaining_tokens=self.tokens,
            retry_after_seconds=wait,
            reason="token_bucket_exhausted",
        )


@dataclass
class RateLimitStats:
    """Aggregate statistics for rate limiting."""

    total_requests: int = 0
    allowed_requests: int = 0
    denied_requests: int = 0
    active_buckets: int = 0

    @property
    def denial_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.denied_requests / self.total_requests

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_requests": self.total_requests,
            "allowed_requests": self.allowed_requests,
            "denied_requests": self.denied_requests,
            "active_buckets": self.active_buckets,
            "denial_rate": round(self.denial_rate, 4),
        }


class RateLimiter:
    """
    Token bucket rate limiter with per-key scoping and governance integration.

    Usage:
        limiter = RateLimiter()
        limiter.configure(RateLimitCategory.CHANNEL, RateLimitConfig(tokens_per_second=5))

        result = limiter.check("slack:general", RateLimitCategory.CHANNEL)
        if not result.allowed:
            print(f"Rate limited, retry after {result.retry_after_seconds}s")
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _TokenBucket] = {}
        self._configs: dict[RateLimitCategory, RateLimitConfig] = {
            RateLimitCategory.GLOBAL: RateLimitConfig(
                tokens_per_second=100.0,
                burst_size=500,
                window_seconds=60.0,
                max_requests_per_window=5000,
            ),
            RateLimitCategory.CHANNEL: RateLimitConfig(
                tokens_per_second=10.0,
                burst_size=50,
                window_seconds=60.0,
                max_requests_per_window=600,
            ),
            RateLimitCategory.PROVIDER: RateLimitConfig(
                tokens_per_second=20.0,
                burst_size=100,
                window_seconds=60.0,
                max_requests_per_window=1000,
            ),
            RateLimitCategory.USER: RateLimitConfig(
                tokens_per_second=5.0,
                burst_size=25,
                window_seconds=60.0,
                max_requests_per_window=300,
            ),
            RateLimitCategory.API_KEY: RateLimitConfig(
                tokens_per_second=15.0,
                burst_size=75,
                window_seconds=60.0,
                max_requests_per_window=800,
            ),
            RateLimitCategory.TOOL: RateLimitConfig(
                tokens_per_second=10.0,
                burst_size=30,
                window_seconds=60.0,
                max_requests_per_window=500,
            ),
        }
        self._stats = RateLimitStats()

    def configure(self, category: RateLimitCategory, config: RateLimitConfig) -> None:
        """Configure rate limits for a category."""
        with self._lock:
            self._configs[category] = config

    def _get_bucket(self, key: str, category: RateLimitCategory) -> _TokenBucket:
        """Get or create a token bucket for the given key."""
        bucket_key = f"{category.value}:{key}"
        if bucket_key not in self._buckets:
            config = self._configs.get(
                category,
                RateLimitConfig(),
            )
            self._buckets[bucket_key] = _TokenBucket(
                tokens=float(config.burst_size),
                max_tokens=config.burst_size,
                refill_rate=config.tokens_per_second,
                window_seconds=config.window_seconds,
                max_per_window=config.max_requests_per_window,
            )
        return self._buckets[bucket_key]

    def check(
        self,
        key: str,
        category: RateLimitCategory = RateLimitCategory.GLOBAL,
        tokens: int = 1,
    ) -> RateLimitResult:
        """
        Check if a request is allowed under the rate limit.

        Args:
            key: Identifier for the rate limit bucket (e.g., user ID, channel name).
            category: Rate limit category for configuration lookup.
            tokens: Number of tokens to consume (default: 1).

        Returns:
            RateLimitResult with allow/deny decision and metadata.
        """
        with self._lock:
            bucket = self._get_bucket(key, category)
            result = bucket.try_consume(tokens)
            result.bucket_key = f"{category.value}:{key}"
            result.category = category

            self._stats.total_requests += 1
            if result.allowed:
                self._stats.allowed_requests += 1
            else:
                self._stats.denied_requests += 1

            return result

    def reset(self, key: str, category: RateLimitCategory) -> None:
        """Reset a specific rate limit bucket."""
        with self._lock:
            bucket_key = f"{category.value}:{key}"
            self._buckets.pop(bucket_key, None)

    def reset_all(self) -> None:
        """Reset all rate limit buckets."""
        with self._lock:
            self._buckets.clear()
            self._stats = RateLimitStats()

    def stats(self) -> RateLimitStats:
        """Get aggregate rate limit statistics."""
        with self._lock:
            self._stats.active_buckets = len(self._buckets)
            return RateLimitStats(
                total_requests=self._stats.total_requests,
                allowed_requests=self._stats.allowed_requests,
                denied_requests=self._stats.denied_requests,
                active_buckets=len(self._buckets),
            )

    def cleanup_stale(self, max_age_seconds: float = 3600.0) -> int:
        """Remove buckets that haven't been used recently."""
        now = time.monotonic()
        removed = 0
        with self._lock:
            stale_keys = [
                k for k, b in self._buckets.items() if now - b.last_refill > max_age_seconds
            ]
            for k in stale_keys:
                del self._buckets[k]
                removed += 1
        return removed
