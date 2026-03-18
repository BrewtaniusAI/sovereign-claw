"""
examples/05_full_swarm_demo.py
Three-lane SwarmCoordinator: Rabbit → Cypher → Giles.
"""

from __future__ import annotations
import os
from typing import Any, Dict
from sovereign_claw.backends_ollama import RabbitOllama, CypherOllama
from sovereign_claw.backends_giles import GilesTiered, GilesTieredConfig, ProviderConfig
from sovereign_claw.orchestrator import Orchestrator, LLMBackend
from sovereign_claw.thermodynamics import TaskManifold
from sovereign_claw.tools_basic import echo_text


class SwarmCoordinator:
    def __init__(self, rabbit: LLMBackend, cypher: LLMBackend, giles: LLMBackend) -> None:
        self.rabbit = rabbit
        self.cypher = cypher
        self.giles = giles
        self._phase = "RABBIT"
        self._envelope: Dict[str, Any] = {}

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        if self._phase == "RABBIT":
            act = self.rabbit.decide_next_action(objective, history, forbidden_actions, drift)
            self._envelope = {"rabbit_action": act, "history": history}
            self._phase = "CYPHER"
            return act
        if self._phase == "CYPHER":
            act = self.cypher.decide_next_action(objective, history, forbidden_actions, drift)
            self._envelope["cypher_action"] = act
            self._phase = "GILES"
            return act
        act = self.giles.decide_next_action(
            objective=str({"objective": objective, "envelope": self._envelope}),
            history=history,
            forbidden_actions=forbidden_actions,
            drift=drift,
        )
        return act


def main() -> None:
    rabbit = RabbitOllama()
    cypher = CypherOllama()

    giles_cfg = GilesTieredConfig(
        primary=ProviderConfig(
            name="anthropic",
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            model="claude-opus-4-6",
        ),
    )
    giles = GilesTiered(giles_cfg)
    swarm = SwarmCoordinator(rabbit, cypher, giles)

    orch = Orchestrator(llm_backend=swarm)
    orch.register_tool("echo_text", echo_text)

    manifold = TaskManifold(
        objective="Plan a 3-step refactor strategy for a legacy Python codebase and echo it.",
        forbidden_actions=[],
        t_max_steps=10,
    )
    receipt = orch.execute(manifold)
    print("Status:", receipt.status)
    print("Steps: ", receipt.steps)
    print("Trace: ", receipt.trace_id)


if __name__ == "__main__":
    main()
