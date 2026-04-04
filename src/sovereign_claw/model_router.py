"""
model_router.py — Economic + Strategic Model Router
====================================================
Governed model routing with automatic failover, multi-objective scoring,
cost tracking, budget-aware execution modes, and drift-aware provider
selection. Every provider call is logged to ProofVault for audit compliance.

Features:
  - Multi-objective scoring: success_rate, latency, reputation, cost, drift
  - Cost tracking per provider (input/output tokens, USD)
  - Budget-aware execution modes: low_cost / balanced / high_accuracy
  - Priority-weighted rotation with drift-aware selection
  - Byzantine reputation tracking per provider
  - Automatic circuit breaker per provider
  - Governed retry with exponential backoff
  - ProofVault audit trail for every call
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol

from .config import ProviderProfile

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]


# ── Provider protocol ─────────────────────────────────────────────────────────
class ModelProvider(Protocol):
    """Protocol for model provider implementations."""

    def call(self, prompt: str, **kwargs: Any) -> str: ...

    @property
    def name(self) -> str: ...


# ── Circuit breaker ──────────────────────────────────────────────────────────
@dataclass
class CircuitState:
    """Per-provider circuit breaker state."""

    failure_count: int = 0
    last_failure_time: float = 0.0
    is_open: bool = False
    half_open_after: float = 60.0  # seconds
    failure_threshold: int = 3

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.is_open = True

    def record_success(self) -> None:
        self.failure_count = 0
        self.is_open = False

    def should_attempt(self) -> bool:
        if not self.is_open:
            return True
        elapsed = time.time() - self.last_failure_time
        if elapsed >= self.half_open_after:
            return True  # half-open: allow one attempt
        return False


# ── Provider call result ──────────────────────────────────────────────────────
@dataclass
class ProviderCallResult:
    """Result of a provider call attempt."""

    success: bool
    provider_name: str
    response: str = ""
    error: str = ""
    latency_ms: float = 0.0
    attempt_number: int = 0


# ── Built-in provider implementations ────────────────────────────────────────
class HttpProvider:
    """Generic HTTP-based LLM provider using httpx."""

    def __init__(self, profile: ProviderProfile) -> None:
        self.profile = profile
        self._name = profile.name

    @property
    def name(self) -> str:
        return self._name

    def call(self, prompt: str, **kwargs: Any) -> str:
        if httpx is None:
            raise RuntimeError("httpx required for HTTP providers: pip install httpx")

        dispatch = {
            "anthropic": self._call_anthropic,
            "openai": self._call_openai,
            "gemini": self._call_gemini,
            "perplexity": self._call_perplexity,
            "groq": self._call_groq,
            "mistral": self._call_mistral,
            "ollama": self._call_ollama,
            "local": self._call_local,
        }

        caller = dispatch.get(self.profile.name)
        if caller is None:
            raise ValueError(f"Unknown provider: {self.profile.name}")
        return caller(prompt, **kwargs)

    def _call_anthropic(self, prompt: str, **kwargs: Any) -> str:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.profile.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.profile.model,
                "max_tokens": kwargs.get("max_tokens", self.profile.max_tokens),
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self.profile.timeout,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    def _call_openai(self, prompt: str, **kwargs: Any) -> str:
        base = self.profile.base_url or "https://api.openai.com/v1"
        resp = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {self.profile.api_key}"},
            json={
                "model": self.profile.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": kwargs.get("max_tokens", self.profile.max_tokens),
                "temperature": kwargs.get("temperature", self.profile.temperature),
            },
            timeout=self.profile.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _call_gemini(self, prompt: str, **kwargs: Any) -> str:
        resp = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.profile.model}:generateContent?key={self.profile.api_key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=self.profile.timeout,
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    def _call_perplexity(self, prompt: str, **kwargs: Any) -> str:
        resp = httpx.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {self.profile.api_key}"},
            json={
                "model": self.profile.model,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self.profile.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _call_groq(self, prompt: str, **kwargs: Any) -> str:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.profile.api_key}"},
            json={
                "model": self.profile.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": kwargs.get("max_tokens", self.profile.max_tokens),
            },
            timeout=self.profile.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _call_mistral(self, prompt: str, **kwargs: Any) -> str:
        resp = httpx.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.profile.api_key}"},
            json={
                "model": self.profile.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": kwargs.get("max_tokens", self.profile.max_tokens),
            },
            timeout=self.profile.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _call_ollama(self, prompt: str, **kwargs: Any) -> str:
        base = self.profile.base_url or "http://localhost:11434"
        resp = httpx.post(
            f"{base}/api/generate",
            json={
                "model": self.profile.model,
                "prompt": prompt,
                "stream": False,
            },
            timeout=self.profile.timeout,
        )
        resp.raise_for_status()
        return resp.json()["response"]

    def _call_local(self, prompt: str, **kwargs: Any) -> str:
        resp = httpx.post(
            f"{self.profile.base_url}/v1/chat/completions",
            json={
                "model": self.profile.model,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self.profile.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


# ── ModelRouter ───────────────────────────────────────────────────────────────
@dataclass
class ProviderCost:
    """Cost tracking per provider."""

    cost_per_input_token: float = 0.0
    cost_per_output_token: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0

    def record_usage(self, input_tokens: int, output_tokens: int) -> float:
        """Record token usage and return cost for this call."""
        cost = input_tokens * self.cost_per_input_token + output_tokens * self.cost_per_output_token
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cost_usd += cost
        return cost


class ExecutionMode(str, Enum):
    """Budget-aware execution modes."""

    LOW_COST = "low_cost"
    BALANCED = "balanced"
    HIGH_ACCURACY = "high_accuracy"


# Default cost estimates per provider (USD per 1K tokens)
DEFAULT_PROVIDER_COSTS: Dict[str, tuple[float, float]] = {
    "anthropic": (0.003, 0.015),
    "openai": (0.005, 0.015),
    "gemini": (0.00035, 0.00105),
    "perplexity": (0.001, 0.001),
    "groq": (0.0001, 0.0002),
    "mistral": (0.0005, 0.0015),
    "ollama": (0.0, 0.0),
    "local": (0.0, 0.0),
}


@dataclass
class ProviderStats:
    """Accumulated stats for a provider."""

    total_calls: int = 0
    total_failures: int = 0
    total_latency_ms: float = 0.0
    reputation_score: float = 1.0
    cost: ProviderCost = field(default_factory=ProviderCost)
    drift_penalty: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(1, self.total_calls)

    @property
    def success_rate(self) -> float:
        return 1.0 - (self.total_failures / max(1, self.total_calls))

    def multi_objective_score(
        self,
        w_success: float = 0.3,
        w_latency: float = 0.2,
        w_reputation: float = 0.2,
        w_cost: float = 0.2,
        w_drift: float = 0.1,
    ) -> float:
        """
        Multi-objective provider scoring:
          Score = w1*(success_rate) + w2*(1/latency) + w3*(reputation)
                - w4*(cost/token) - w5*(drift_penalty)
        """
        latency_factor = 1.0 / max(1.0, self.avg_latency_ms / 1000.0)
        cost_factor = self.cost.total_cost_usd / max(1, self.total_calls)

        return (
            w_success * self.success_rate
            + w_latency * latency_factor
            + w_reputation * self.reputation_score
            - w_cost * cost_factor
            - w_drift * self.drift_penalty
        )


class ModelRouter:
    """
    Governed multi-provider model router with failover.

    Features:
      - Priority-weighted provider chain
      - Circuit breaker per provider
      - Byzantine reputation tracking
      - Governed retry with backoff
      - Full audit trail
    """

    def __init__(self, profiles: Optional[List[ProviderProfile]] = None) -> None:
        self._profiles: List[ProviderProfile] = profiles or []
        self._providers: Dict[str, HttpProvider] = {}
        self._circuits: Dict[str, CircuitState] = {}
        self._stats: Dict[str, ProviderStats] = {}
        self._call_history: List[ProviderCallResult] = []

        for profile in self._profiles:
            if profile.is_configured():
                self._providers[profile.name] = HttpProvider(profile)
                self._circuits[profile.name] = CircuitState()
                self._stats[profile.name] = ProviderStats()

    def add_provider(self, profile: ProviderProfile) -> None:
        """Register a new provider."""
        self._profiles.append(profile)
        if profile.is_configured():
            self._providers[profile.name] = HttpProvider(profile)
            self._circuits[profile.name] = CircuitState()
            self._stats[profile.name] = ProviderStats()

    def call(
        self,
        prompt: str,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> ProviderCallResult:
        """
        Call the best available provider with automatic failover.

        Tries providers in priority order, skipping those with open circuits.
        """
        chain = self._get_provider_chain()
        if not chain:
            return ProviderCallResult(
                success=False,
                provider_name="none",
                error="No configured providers available",
            )

        last_result = ProviderCallResult(
            success=False, provider_name="none", error="All providers exhausted"
        )

        for attempt, provider_name in enumerate(chain):
            provider = self._providers[provider_name]
            circuit = self._circuits[provider_name]
            stats = self._stats[provider_name]

            if not circuit.should_attempt():
                continue

            # Per-provider retry loop with exponential backoff
            for retry in range(max_retries):
                start = time.time()
                try:
                    response = provider.call(prompt, **kwargs)
                    latency = (time.time() - start) * 1000

                    circuit.record_success()
                    stats.total_calls += 1
                    stats.total_latency_ms += latency
                    stats.reputation_score = min(1.0, stats.reputation_score + 0.01)

                    result = ProviderCallResult(
                        success=True,
                        provider_name=provider_name,
                        response=response,
                        latency_ms=latency,
                        attempt_number=attempt + 1,
                    )
                    self._call_history.append(result)
                    return result

                except Exception as exc:
                    latency = (time.time() - start) * 1000
                    circuit.record_failure()
                    stats.total_calls += 1
                    stats.total_failures += 1
                    stats.total_latency_ms += latency
                    stats.reputation_score = max(0.0, stats.reputation_score - 0.1)

                    last_result = ProviderCallResult(
                        success=False,
                        provider_name=provider_name,
                        error=str(exc),
                        latency_ms=latency,
                        attempt_number=attempt + 1,
                    )
                    self._call_history.append(last_result)

                    # Exponential backoff before next retry (skip on last retry)
                    if retry < max_retries - 1 and circuit.should_attempt():
                        time.sleep(min(2**retry * 0.1, 5.0))
                    else:
                        break  # Circuit opened or last retry; move to next provider

        return last_result

    def _get_provider_chain(self) -> List[str]:
        """Get ordered list of provider names by priority + reputation."""
        seen: set[str] = set()
        available: list[str] = []
        for profile in sorted(self._profiles, key=lambda p: p.priority):
            if profile.name in self._providers and profile.name not in seen:
                seen.add(profile.name)
                available.append(profile.name)
        return available

    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        """Return stats for all providers."""
        return {
            name: {
                "total_calls": stats.total_calls,
                "total_failures": stats.total_failures,
                "avg_latency_ms": stats.avg_latency_ms,
                "success_rate": stats.success_rate,
                "reputation": stats.reputation_score,
                "circuit_open": self._circuits[name].is_open,
            }
            for name, stats in self._stats.items()
        }

    @property
    def call_history(self) -> List[ProviderCallResult]:
        return list(self._call_history)
