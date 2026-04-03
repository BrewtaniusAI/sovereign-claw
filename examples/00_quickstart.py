"""
00_quickstart.py — Minimal Sovereign Claw Demo
"""

from sovereign_claw import Orchestrator, SovereignRuntime


class DemoBackend:
    def decide_next_action(self, objective, history, forbidden_actions, drift):
        if drift <= 0.0:
            return {
                "tool": "HALT",
                "kwargs": {},
                "comment": "already closed",
                "agent_id": "demo_backend",
            }

        return {
            "tool": "echo_text",
            "kwargs": {"text": f"objective={objective}"},
            "comment": "safe demo action",
            "agent_id": "demo_backend",
        }


def echo_text(text: str):
    return text


if __name__ == "__main__":
    orchestrator = Orchestrator(
        llm_backend=DemoBackend(),
        tools={"echo_text": echo_text},
    )

    runtime = SovereignRuntime(orchestrator=orchestrator)

    result = runtime.run(
        "stabilize governed ai execution",
        forbidden_actions=["delete_everything"],
        t_max_steps=6,
        risk_threshold=0.95,
    )

    print(result)
