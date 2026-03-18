"""
backends_ollama.py — Lane 2 Local Backends (Rabbit + Cypher)
=============================================================
Rabbit: fast drafting agent (small local model, low latency)
Cypher: adversarial auditor (same or larger local model, structured critique)

Both implement LLMBackend protocol and call a local Ollama endpoint.

BUG FIXES vs. original stub:
  - _parse_action_json is shared, not duplicated.
  - HTTP errors produce HALT rather than raising uncaught exceptions.
  - Timeout is configurable (default 30s).
  - System prompts are explicit and role-specific.
  - agent_id is embedded in every decision so the Orchestrator can
    track Byzantine reputation per agent.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

try:
    import httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


# ── JSON parser (shared) ──────────────────────────────────────────────────────
def _parse_action_json(text: str) -> Dict[str, Any]:
    """
    Extract the first JSON object from an LLM response.
    Falls back to HALT on any parse failure.
    """
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        obj = json.loads(text[start:end])
        if "tool" not in obj:
            raise ValueError("missing 'tool' field")
        return obj
    except Exception:
        return {"tool": "HALT", "kwargs": {}, "comment": "Parse failure; halting."}


# ── OllamaBase ────────────────────────────────────────────────────────────────
class _OllamaBase:
    DEFAULT_HOST = "http://localhost:11434"

    def __init__(
        self,
        model: str,
        system_prompt: str,
        host: str = DEFAULT_HOST,
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.host = host.rstrip("/")
        self.timeout = timeout

    def _chat(self, user_content: str) -> str:
        if not _HTTPX_AVAILABLE:
            return json.dumps(
                {
                    "tool": "HALT",
                    "kwargs": {},
                    "comment": "httpx not installed; cannot call Ollama.",
                }
            )
        try:
            resp = httpx.post(
                f"{self.host}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]
        except Exception as exc:
            return json.dumps(
                {
                    "tool": "HALT",
                    "kwargs": {},
                    "comment": f"Ollama error: {type(exc).__name__}",
                }
            )

    def decide_next_action(
        self,
        objective: str,
        history: List[Dict[str, Any]],
        forbidden_actions: List[str],
        drift: float,
    ) -> Dict[str, Any]:
        prompt = (
            f"OBJECTIVE: {objective}\n"
            f"CURRENT DRIFT: {drift:.4f}\n"
            f"FORBIDDEN: {forbidden_actions}\n"
            f"HISTORY (last 3): {json.dumps(history[-3:], default=str)}\n\n"
            "Respond ONLY with a JSON object:\n"
            '{"tool": "<name or HALT>", "kwargs": {}, "comment": "<reason>"}'
        )
        raw = self._chat(prompt)
        decision = _parse_action_json(raw)
        decision["agent_id"] = self.__class__.__name__.lower()
        return decision


# ── Rabbit ────────────────────────────────────────────────────────────────────
class RabbitOllama(_OllamaBase):
    """
    Lane 2 — Rabbit: fast draft agent.
    Uses a small, low-latency Ollama model for rapid plan generation.
    """

    def __init__(
        self,
        model: str = "llama3",
        host: str = _OllamaBase.DEFAULT_HOST,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(
            model=model,
            system_prompt=(
                "You are Rabbit, a fast-drafting AI agent. "
                "Your role is to generate an initial action plan. "
                "Be quick and directional. Output only valid JSON."
            ),
            host=host,
            timeout=timeout,
        )


# ── Cypher ────────────────────────────────────────────────────────────────────
class CypherOllama(_OllamaBase):
    """
    Lane 2 — Cypher: adversarial auditor.
    Reviews Rabbit's draft and either approves ('ok' in comment) or rejects.
    """

    def __init__(
        self,
        model: str = "llama3",
        host: str = _OllamaBase.DEFAULT_HOST,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(
            model=model,
            system_prompt=(
                "You are Cypher, an adversarial auditor. "
                "Your role is to find flaws in proposed actions. "
                "If the plan is sound, include 'ok' in your comment. "
                "If not, explain the flaw. Output only valid JSON."
            ),
            host=host,
            timeout=timeout,
        )
