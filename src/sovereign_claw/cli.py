"""
cli.py — Sovereign Claw command-line interface
=============================================

Minimal CLI for governed execution.
"""

from __future__ import annotations

import argparse
import json

from .orchestrator import Orchestrator
from .runtime import SovereignRuntime


class DemoBackend:
    """
    Minimal default backend for CLI smoke usage.
    Replace with real backend wiring later.
    """

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


def echo_text(text: str) -> str:
    return text


def build_runtime() -> SovereignRuntime:
    orchestrator = Orchestrator(
        llm_backend=DemoBackend(),
        tools={"echo_text": echo_text},
    )
    return SovereignRuntime(orchestrator=orchestrator)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sovereign",
        description="Sovereign Claw — deterministic governance layer for AI execution",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a governed objective")
    run_parser.add_argument("objective", help="Objective to execute")
    run_parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        help="Forbidden tool/action (can be repeated)",
    )
    run_parser.add_argument(
        "--t-max",
        type=int,
        default=8,
        dest="t_max_steps",
        help="Maximum execution steps",
    )
    run_parser.add_argument(
        "--risk-threshold",
        type=float,
        default=0.9,
        help="Soft halt threshold",
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        runtime = build_runtime()
        result = runtime.run(
            args.objective,
            forbidden_actions=args.forbid,
            t_max_steps=args.t_max_steps,
            risk_threshold=args.risk_threshold,
        )
        print(json.dumps(result, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())