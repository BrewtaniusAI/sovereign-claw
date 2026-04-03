"""
cli.py — Sovereign Claw command-line interface
=============================================

Governed execution CLI with:
- human-readable output by default
- raw JSON output via --json
- provider selection with safe fallback
- side-effect-free preview mode via --preview
- onboard: bootstrap config + skills
- doctor: system health check
- gateway: show gateway status
- agent: run governed agent session
- message: send message via channel
- skills: list/install/evaluate skills
- config: view/edit configuration
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

from .orchestrator import Orchestrator
from .runtime import SovereignRuntime


class DemoBackend:
    """
    Minimal default backend for CLI smoke usage.
    Replace with real backend wiring later.
    """

    def decide_next_action(
        self,
        objective: str,
        history: Any,
        forbidden_actions: Any,
        drift: float,
    ) -> Dict[str, Any]:
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


def pretty_print_result(result: dict) -> None:  # type: ignore[type-arg]
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
        for i, drift_val in enumerate(result["drift_trajectory"]):
            try:
                print(f"  {i}: {float(drift_val):.4f}")
            except (TypeError, ValueError):
                print(f"  {i}: {drift_val}")

    print("\n===========================\n")


def build_runtime(provider: str = "demo") -> SovereignRuntime:
    backend: Any = None

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


# ── Subcommand handlers ──────────────────────────────────────────────────────
def _cmd_onboard(args: argparse.Namespace) -> int:
    """Bootstrap configuration directory and install bundled skills."""
    from .config import init_config_dir, load_config
    from .skills import SkillsManager

    config_dir = init_config_dir()
    print(f"Config directory: {config_dir}")

    cfg = load_config()
    mgr = SkillsManager(skills_dirs=[str(d) for d in cfg.skills_dirs])
    installed = mgr.install_bundled()
    for name in installed:
        mgr.evaluate(name)
        mgr.activate(name)
    print(f"Installed {len(installed)} bundled skills: {', '.join(installed)}")
    print("\nOnboarding complete. Run 'sovereign doctor' to verify.")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Run system health diagnostics."""
    from .config import DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_FILE

    checks = []

    # Config directory
    exists = DEFAULT_CONFIG_DIR.exists()
    checks.append(("Config directory", exists, str(DEFAULT_CONFIG_DIR)))

    # Config file
    exists = DEFAULT_CONFIG_FILE.exists()
    checks.append(("Config file", exists, str(DEFAULT_CONFIG_FILE)))

    # Python version
    py_ok = sys.version_info >= (3, 10)
    checks.append(("Python >= 3.10", py_ok, f"{sys.version_info.major}.{sys.version_info.minor}"))

    # httpx available
    try:
        import httpx  # noqa: F401

        checks.append(("httpx installed", True, ""))
    except ImportError:
        checks.append(("httpx installed", False, "pip install httpx"))

    # pydantic available
    try:
        import pydantic  # noqa: F401

        checks.append(("pydantic installed", True, ""))
    except ImportError:
        checks.append(("pydantic installed", False, "pip install pydantic"))

    print("\n=== Sovereign Doctor ===\n")
    all_ok = True
    for label, ok, note in checks:
        status = "OK" if ok else "FAIL"
        if not ok:
            all_ok = False
        line = f"  [{status}] {label}"
        if note:
            line += f"  ({note})"
        print(line)
    print()

    if all_ok:
        print("All checks passed.")
    else:
        print("Some checks failed. Fix the issues above and re-run.")
    return 0 if all_ok else 1


def _cmd_gateway(args: argparse.Namespace) -> int:
    """Show gateway status."""
    from .config import load_config

    cfg = load_config()
    print("\n=== Gateway Status ===\n")
    print(f"  Host:            {cfg.gateway.host}")
    print(f"  Port:            {cfg.gateway.port}")
    print(f"  Max connections: {cfg.gateway.max_connections}")
    print(f"  Heartbeat:       {cfg.gateway.heartbeat_interval}s")
    print(f"  Session timeout: {cfg.gateway.session_timeout}s")
    print()
    return 0


def _cmd_skills(args: argparse.Namespace) -> int:
    """List installed skills."""
    from .skills import SkillsManager

    mgr = SkillsManager()
    mgr.install_bundled()
    for name in list(mgr._bundled_skills.keys()):
        mgr.evaluate(name)
        mgr.activate(name)

    skills = mgr.list_skills()
    if args.json_output:
        print(json.dumps([s.spec.to_dict() for s in skills], indent=2))
    else:
        print(f"\n=== Skills ({len(skills)}) ===\n")
        for s in skills:
            status = s.status.value
            tools = ", ".join(s.spec.tools_provided) if s.spec.tools_provided else "none"
            print(f"  [{status:>9}] {s.spec.name} v{s.spec.version}")
            print(f"             {s.spec.description}")
            print(f"             tools: {tools}")
            print()
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    """View current configuration."""
    from .config import _dataclass_to_dict, load_config

    cfg = load_config()
    data = _dataclass_to_dict(cfg)
    if args.json_output:
        print(json.dumps(data, indent=2, default=str))
    else:
        print("\n=== Configuration ===\n")
        for key, val in data.items():
            if isinstance(val, dict):
                print(f"  {key}:")
                for k2, v2 in val.items():
                    print(f"    {k2}: {v2}")
            elif isinstance(val, list) and val and isinstance(val[0], dict):
                print(f"  {key}: [{len(val)} items]")
            else:
                print(f"  {key}: {val}")
        print()
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Run a governed objective."""
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


def _cmd_version(args: argparse.Namespace) -> int:
    from . import __version__

    print(f"sovereign-claw v{__version__}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sovereign",
        description="Sovereign Claw — deterministic governance layer for AI execution",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── run ────────────────────────────────────────────────────────────────
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

    # ── onboard ───────────────────────────────────────────────────────────
    subparsers.add_parser("onboard", help="Bootstrap config and install bundled skills")

    # ── doctor ────────────────────────────────────────────────────────────
    subparsers.add_parser("doctor", help="Run system health diagnostics")

    # ── gateway ───────────────────────────────────────────────────────────
    subparsers.add_parser("gateway", help="Show gateway configuration and status")

    # ── skills ────────────────────────────────────────────────────────────
    skills_parser = subparsers.add_parser("skills", help="List installed skills")
    skills_parser.add_argument("--json", dest="json_output", action="store_true")

    # ── config ────────────────────────────────────────────────────────────
    config_parser = subparsers.add_parser("config", help="View current configuration")
    config_parser.add_argument("--json", dest="json_output", action="store_true")

    # ── version ───────────────────────────────────────────────────────────
    subparsers.add_parser("version", help="Print version and exit")

    args = parser.parse_args(argv)

    dispatch = {
        "run": _cmd_run,
        "onboard": _cmd_onboard,
        "doctor": _cmd_doctor,
        "gateway": _cmd_gateway,
        "skills": _cmd_skills,
        "config": _cmd_config,
        "version": _cmd_version,
    }

    handler = dispatch.get(args.command)
    if handler:
        return handler(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
