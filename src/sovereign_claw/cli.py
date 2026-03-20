"""
cli.py — Sovereign Claw command-line interface
=============================================

Governed execution CLI with:
- human-readable output by default
- raw JSON output via --json
- provider selection with safe fallback
- side-effect-free preview mode via --preview
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


def pretty_print_result(result: dict) -> None:
    mode = "Preview" if result.get("preview") else "Execution"
    print(f"\n=== Sovereign {mode} ===\n")

    if "trace_id" in result and result["trace_id"]:
        print(f"Trace ID: {result['trace_id']}")

    print(f"Status: {result['status']}")

    if "reason" in result and result["reason"]:
        print(f"Reason: {result['reason']}")

    if "steps" in result and result["steps"] is not None:
        print(f"Steps: {result['steps']}")

    if "final_drift" in result and result["final_drift"] is not None:
        try:
            print(f"Final Drift: {float(result['final_drift']):.4f}")
        except (TypeError, ValueError):
            print(f"Final Drift: {result['final_drift']}")

    if "provider" in result and result["provider"]:
        print(f"Provider: {result['provider']}")

    if "policy_status" in result and result["policy_status"]:
        print(f"Policy: {result['policy_status']}")

    if "action" in result:
        print("\nProposed Action:")
        print(json.dumps(result["action"], indent=2))

    if "drift_trajectory" in result and result["drift_trajectory"]:
        print("\nDrift Trajectory:")
        for i, drift in enumerate(result["drift_trajectory"]):
            try:
                print(f"  {i}: {float(drift):.4f}")
            except (TypeError, ValueError):
                print(f"  {i}: {drift}")

    print("\n===========================\n")


def build_runtime(provider: str = "demo") -> SovereignRuntime:
    backend = None

    if provider == "ollama":
        try:
            from .backends_ollama import RabbitOllama

            backend = RabbitOllama()
        except Exception:
            backend = DemoBackend()

    elif provider == "giles":
        try:
            from .backends_giles import GilesTiered, GilesTieredConfig, ProviderConfig

            backend = GilesTiered(
                GilesTieredConfig(
                    primary=ProviderConfig(
                        name="openai",
                        api_key="DUMMY_KEY",
                        model="gpt-4o-mini",
                    )
                )
            )
        except Exception:
            backend = DemoBackend()

    else:
        backend = DemoBackend()

    orchestrator = Orchestrator(
        llm_backend=backend,
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
    run_parser.add_argument(
        "--provider",
        choices=["demo", "ollama", "giles"],
        default="demo",
        help="Backend provider",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted view",
    )
    run_parser.add_argument(
        "--preview",
        action="store_true",
        help="Compute governed outcome without execution side effects",
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        runtime = build_runtime(args.provider)

        if args.preview:
            result = runtime.preview(
                args.objective,
                forbidden_actions=args.forbid,
                t_max_steps=args.t_max_steps,
                risk_threshold=args.risk_threshold,
            )
        else:
            result = runtime.run(
                args.objective,
                forbidden_actions=args.forbid,
                t_max_steps=args.t_max_steps,
                risk_threshold=args.risk_threshold,
            )

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            pretty_print_result(result)

        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())