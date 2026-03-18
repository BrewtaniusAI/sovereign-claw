"""
examples/02_multi_agent_dag.py
Multi-agent routing demo with Byzantine drift penalties.
"""
from __future__ import annotations
from typing import Any, Dict, List
from sovereign_claw.orchestrator import Orchestrator
from sovereign_claw.thermodynamics import TaskManifold


class WorkerAgent:
    def __init__(self, name: str, reliability: float) -> None:
        self.name = name
        self.reliability = reliability

    def act(self, task: str) -> Dict[str, Any]:
        if self.reliability < 0.5:
            raise RuntimeError(f"{self.name} hallucinated on task: {task}")
        return {"worker": self.name, "result": f"completed:{task}"}


class SwarmLLM:
    def __init__(self) -> None:
        self.step = 0

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        if self.step == 0:
            self.step += 1
            return {"tool": "worker_a", "kwargs": {"task": "subtask-1"},
                    "comment": "Route to reliable worker A.", "agent_id": "worker_a"}
        if self.step == 1:
            self.step += 1
            return {"tool": "worker_b", "kwargs": {"task": "subtask-2"},
                    "comment": "Route to noisy worker B (drift penalty demo).", "agent_id": "worker_b"}
        return {"tool": "HALT", "kwargs": {}, "comment": "Swarm demo complete."}


def make_worker_tool(agent: WorkerAgent):
    def tool(task: str):
        return agent.act(task)
    return tool


def main() -> None:
    llm  = SwarmLLM()
    orch = Orchestrator(llm_backend=llm)

    worker_a = WorkerAgent("A", reliability=0.9)
    worker_b = WorkerAgent("B", reliability=0.2)
    orch.register_tool("worker_a", make_worker_tool(worker_a))
    orch.register_tool("worker_b", make_worker_tool(worker_b))

    manifold = TaskManifold(
        objective="Route work across two agents and demonstrate drift penalties.",
        forbidden_actions=[],
        t_max_steps=8,
        risk_threshold=0.95,
    )
    receipt = orch.execute(manifold)
    print("Status:      ", receipt.status)
    print("Steps:       ", receipt.steps)
    print("Final drift: ", receipt.final_drift)
    print("Trajectory:  ", receipt.drift_trajectory)
    print("Trace:       ", receipt.trace_id)

    # Show Byzantine reputation
    weights = orch.vault.list_agent_weights()
    print("\nAgent Reputation Weights:")
    for w in weights:
        print(f"  {w['agent_id']:20s}  w={w['weight']:.4f}  R={w['drift_integral']:.4f}")


if __name__ == "__main__":
    main()
