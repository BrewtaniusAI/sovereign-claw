"""
examples/01_safe_web_scrape.py
Simple scripted demo: fetch example.com title and halt in ≤5 steps.
"""
from __future__ import annotations

try:
    import httpx
    from bs4 import BeautifulSoup
    _DEPS = True
except ImportError:
    _DEPS = False

from sovereign_claw.orchestrator import Orchestrator
from sovereign_claw.thermodynamics import TaskManifold


class DummyLLM:
    def __init__(self) -> None:
        self._called = False

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        if not self._called:
            self._called = True
            return {"tool": "web_get", "kwargs": {"url": "https://example.com"},
                    "comment": "Fetch the page once."}
        return {"tool": "HALT", "kwargs": {}, "comment": "Demonstrate fixed-time closure."}


def web_get(url: str) -> str:
    if not _DEPS:
        return "TITLE:example (httpx/bs4 not installed)"
    resp = httpx.get(url, timeout=10.0)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.string if soup.title else "no-title"
    return f"TITLE:{title}"


def main() -> None:
    llm  = DummyLLM()
    orch = Orchestrator(llm_backend=llm)
    orch.register_tool("web_get", web_get)

    manifold = TaskManifold(
        objective="Fetch example.com title and stop.",
        forbidden_actions=[],
        t_max_steps=5,
    )
    receipt = orch.execute(manifold)
    print("Status:  ", receipt.status)
    print("Steps:   ", receipt.steps)
    print("Trace ID:", receipt.trace_id)


if __name__ == "__main__":
    main()
