"""
examples/03_langgraph_elve_loop.py
LangGraph ELFE v∞.1 loop — requires: pip install langgraph>=0.2.0
"""
from __future__ import annotations
from sovereign_claw.graph_elve import build_elve_graph, ELFEState
from sovereign_claw.thermodynamics import TaskManifold


def main() -> None:
    manifold = TaskManifold(
        objective="Draft and audit a small refactor plan.",
        forbidden_actions=[],
        t_max_steps=6,
    )
    initial = ELFEState(objective=manifold.objective, manifold=manifold)

    try:
        app = build_elve_graph()
    except ImportError as e:
        print(f"LangGraph not installed: {e}")
        print("Install with: pip install langgraph>=0.2.0")
        return

    final = app.invoke(initial)
    print("Final state:")
    for k, v in final.items():
        if k != "_therm":
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
