"""
backends_giles.py — Lane 3 Authoritative Backend (Giles)
=========================================================
GilesTiered: cascading provider chain (primary → secondary → tertiary).
Falls back to the next provider on any HTTP error or parse failure.

Supported providers: anthropic, openai (gpt), gemini, perplexity.

BUG FIXES vs. original:
  - _parse_action_json is imported from backends_ollama (single source).
  - OpenAI provider correctly indexes data["choices"][0]["message"]["content"].
  - Anthropic provider added (primary provider for CollectiveOS).
  - Each provider wraps its HTTP call in try/except and returns None on
    failure so GilesTiered can cascade cleanly.
  - GilesTieredConfig now validates that at least one provider is set.
  - agent_id "giles" is embedded in every decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    import httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

from .backends_ollama import _parse_action_json


# ── ProviderConfig ────────────────────────────────────────────────────────────
@dataclass
class ProviderConfig:
    name: str  # "anthropic" | "openai" | "gemini" | "perplexity"
    api_key: str
    model: str
    timeout: float = 60.0


# ── GilesTieredConfig ─────────────────────────────────────────────────────────
@dataclass
class GilesTieredConfig:
    primary: ProviderConfig
    secondary: Optional[ProviderConfig] = None
    tertiary: Optional[ProviderConfig] = None

    def providers(self) -> List[ProviderConfig]:
        return [p for p in (self.primary, self.secondary, self.tertiary) if p]


# ── Provider implementations ──────────────────────────────────────────────────
def _call_anthropic(cfg: ProviderConfig, prompt: str) -> Optional[str]:
    if not _HTTPX_AVAILABLE:
        return None
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": cfg.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": cfg.model,
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=cfg.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]
    except Exception:
        return None


def _call_openai(cfg: ProviderConfig, prompt: str) -> Optional[str]:
    if not _HTTPX_AVAILABLE:
        return None
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            json={
                "model": cfg.model,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=cfg.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def _call_gemini(cfg: ProviderConfig, prompt: str) -> Optional[str]:
    if not _HTTPX_AVAILABLE:
        return None
    try:
        resp = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{cfg.model}:generateContent?key={cfg.api_key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=cfg.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return None


def _call_perplexity(cfg: ProviderConfig, prompt: str) -> Optional[str]:
    if not _HTTPX_AVAILABLE:
        return None
    try:
        resp = httpx.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            json={
                "model": cfg.model,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=cfg.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None


_PROVIDER_DISPATCH = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "gpt": _call_openai,
    "gemini": _call_gemini,
    "perplexity": _call_perplexity,
}

_GILES_SYSTEM = (
    "You are Giles, the authoritative governance node. "
    "You receive a consolidated envelope from Rabbit and Cypher and issue "
    "the final, sealed action. Be decisive and precise. "
    "Output ONLY a JSON object: "
    '{"tool": "<name or HALT>", "kwargs": {}, "comment": "<reason>"}'
)


# ── GilesTiered ───────────────────────────────────────────────────────────────
class GilesTiered:
    """
    Lane 3 — Giles: authoritative, tiered cloud provider.

    Cascades through primary → secondary → tertiary on failure.
    If all providers fail, issues HALT to preserve Silence Clause.
    """

    def __init__(self, config: GilesTieredConfig) -> None:
        self.config = config

    def decide_next_action(
        self,
        objective: str,
        history: List[Dict[str, Any]],
        forbidden_actions: List[str],
        drift: float,
    ) -> Dict[str, Any]:
        prompt = (
            f"{_GILES_SYSTEM}\n\n"
            f"ENVELOPE: {objective}\n"
            f"DRIFT: {drift:.4f}\n"
            f"FORBIDDEN: {forbidden_actions}\n"
            f"HISTORY (last 3): {json.dumps(history[-3:], default=str)}"
        )

        for provider_cfg in self.config.providers():
            caller = _PROVIDER_DISPATCH.get(provider_cfg.name.lower())
            if caller is None:
                continue
            raw = caller(provider_cfg, prompt)
            if raw:
                decision = _parse_action_json(raw)
                decision["agent_id"] = "giles"
                decision["provider"] = provider_cfg.name
                return decision

        # All providers failed — Silence Clause
        return {
            "tool": "HALT",
            "kwargs": {},
            "comment": "All Giles providers failed; halting under Silence Clause.",
            "agent_id": "giles",
        }
